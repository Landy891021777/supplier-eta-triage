# -*- coding: utf-8 -*-
"""
對照實驗：規則層 vs LLM 層。

===========================  這支程式在證明什麼  ===========================
它在回答一個必須被回答的問題：**這裡到底需不需要 LLM？**

很多 AI 專案跳過這一步，直接假設「用了 AI 就比較好」。
本專案認為那是不負責任的：LLM 有成本、有延遲、有不可重現性，
導入它必須先證明規則做不到。

作法：對同一批信件、同一份人工標註答案，分別用兩層解析，分群比較。
    如果規則層在某一群上表現就夠好，那一群就不該送 LLM（省錢又穩定）。
    如果規則層在某一群上崩掉，那就是 LLM 的存在理由。

分群依據是信件的書寫風格，因為那才是造成難度差異的真正原因：
    formal      格式化通知，有欄位標籤
    semi        半格式化，日期格式雜
    narrative   純敘述、相對日期、模糊措辭
    no_change   確認不變（陷阱組，最容易被誤判成延遲）
    handcrafted 手寫刁鑽案例（轉寄串、一信多單、分批交貨…）
===========================================================================
"""
from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

import extract_llm  # noqa: E402
import extract_rules  # noqa: E402
from llm.provider import get_provider  # noqa: E402
from pipeline import (load_config, load_emails, load_ground_truth,  # noqa: E402
                      load_reference_data)

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "output"


def _group_of(email: dict) -> str:
    style = email.get("style", "")
    return "handcrafted" if style == "handcrafted" else (style or "other")


def _truth_by_email(truth: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = defaultdict(list)
    for t in truth:
        out[t["email_id"]].append(t)
    return out


def score_email(pred: list[dict], truth: list[dict]) -> dict:
    """
    對單封信計分。

    四個指標刻意分開看，因為它們的失敗代價完全不同：
      po_hit       PO 抓錯 -> 整筆資訊掛到錯的單上，最嚴重
      eta_exact    日期抓錯 -> 排程算錯
      strength_ok  承諾強度判錯 -> 把託辭當承諾，這是本工具最在意的錯誤型態
      change_ok    變更類型判錯 -> 把「確認不變」當成延遲，清單被雜訊灌爆
    """
    pt = {t["po_no"] for t in truth}
    pp = {p["po_no"] for p in pred if p.get("po_no")}
    po_hit = len(pt & pp) / len(pt) if pt else float("nan")

    tmap = {t["po_no"]: t for t in truth}
    eta_n = eta_ok = str_n = str_ok = chg_n = chg_ok = 0
    for p in pred:
        t = tmap.get(p.get("po_no"))
        if not t:
            continue
        str_n += 1
        if p.get("commitment_strength") == t.get("commitment_strength"):
            str_ok += 1
        chg_n += 1
        if p.get("change_type") == t.get("change_type"):
            chg_ok += 1
        if t.get("new_eta"):
            eta_n += 1
            if p.get("new_eta") == t.get("new_eta"):
                eta_ok += 1

    return {
        "po_hit": po_hit,
        "eta_exact": (eta_ok / eta_n) if eta_n else float("nan"),
        "strength_ok": (str_ok / str_n) if str_n else float("nan"),
        "change_ok": (chg_ok / chg_n) if chg_n else float("nan"),
    }


def run_experiment() -> dict:
    cfg = load_config()
    emails = load_emails()
    truth_map = _truth_by_email(load_ground_truth())
    pos_df, _, _ = load_reference_data()
    ref_date = cfg["data_generation"]["as_of_date"]

    provider = get_provider()
    llm_on = provider.available

    rows = []
    for e in emails:
        truth = truth_map.get(e["email_id"], [])
        if not truth:
            continue
        grp = _group_of(e)

        rule_pred = [r.to_dict() for r in extract_rules.extract(e)]
        rule_conf = max([r["confidence"] for r in rule_pred], default=0.0)
        rows.append({"group": grp, "layer": "規則層", "email_id": e["email_id"],
                     "confidence": rule_conf, **score_email(rule_pred, truth)})

        if llm_on:
            known = pos_df.loc[
                pos_df["supplier_id"] == e["supplier_id"],
                ["po_no", "material_id", "committed_date", "need_date"]
            ].to_dict("records")
            llm_pred, meta = extract_llm.extract(e, ref_date, known, provider)
            rows.append({"group": grp, "layer": "LLM 層", "email_id": e["email_id"],
                         "confidence": (max([p.confidence for p in llm_pred], default=0.0)),
                         **score_email([p.to_dict() for p in llm_pred], truth)})

    df = pd.DataFrame(rows)
    summary = (df.groupby(["group", "layer"])[
        ["po_hit", "eta_exact", "strength_ok", "change_ok"]]
        .mean().round(3).reset_index())
    counts = df[df["layer"] == "規則層"].groupby("group").size().rename("信件數")
    summary = summary.merge(counts, left_on="group", right_index=True, how="left")
    return {"detail": df, "summary": summary, "llm_on": llm_on,
            "provider": provider.name, "model": provider.model}


def to_markdown(res: dict) -> str:
    lines = ["# 對照實驗：規則層 vs LLM 層", ""]
    if res["llm_on"]:
        lines += [f"LLM provider：`{res['provider']}` / 模型：`{res['model']}`", ""]
    else:
        lines += [
            "> **本次執行未接上 LLM**（環境中無可用 API 金鑰），",
            "> 因此下表僅有規則層結果。設定 `.env` 後重新執行本程式，",
            "> 即可產生完整的兩層對照。", ""]
    lines += [
        "指標說明：",
        "",
        "| 指標 | 意義 | 判錯的代價 |",
        "|---|---|---|",
        "| `po_hit` | 是否抓到信中提及的所有 PO | 資訊掛到錯的單上，最嚴重 |",
        "| `eta_exact` | 新交期日期是否完全正確 | 下游排程算錯 |",
        "| `strength_ok` | 承諾強度是否判對 | **把託辭當成承諾**，本工具最在意的錯誤 |",
        "| `change_ok` | 變更類型是否判對 | 把「確認不變」當延遲，清單被雜訊灌爆 |",
        "",
        "## 分群結果", "",
        res["summary"].to_markdown(index=False),
        "",
        "## 怎麼讀這張表", "",
        "- `formal` / `semi` 群若規則層表現已足夠，這些信就**不該**送 LLM——",
        "  省成本、省延遲，而且結果完全可重現。",
        "- `narrative` / `handcrafted` 群是規則層的失效區：相對日期、模糊措辭、",
        "  轉寄串、一信多單。這幾群的差距就是導入 LLM 的實質理由。",
        "- 若兩層在所有群組都差不多，那結論應該是**不要用 LLM** ——",
        "  這個實驗必須容許得出否定 AI 的結論，否則它不是實驗，是背書。",
        "",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    res = run_experiment()
    OUT.mkdir(exist_ok=True)
    md = to_markdown(res)
    (OUT / "實驗結果.md").write_text(md, encoding="utf-8")
    print(md)
