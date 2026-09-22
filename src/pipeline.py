# -*- coding: utf-8 -*-
"""
主流程編排：信件 → 分層解析 → 對位 PO → 分級 → 行動清單。

流程設計的三個關鍵決定：

1. **分層解析**：先跑免費的規則層，只有信心不足的才升級呼叫 LLM。
   理由是成本與延遲 —— 一天幾百封信全丟 API，同仁會嫌慢也會被 IT 關切。

2. **對位之後才判定 delay / pull_in**。
   單看信件無法知道新日期是提前還是延後，必須跟 PO 主檔的原承諾日比對。
   這是「解析」與「判斷」分工的具體體現：LLM 負責讀懂信，系統負責比對事實。

3. **人工確認閘門**：非 confirmed 的解析結果，一律不覆寫系統承諾日。
   工具只提出建議，寫回 ERP 必須由人按下確認。
   交期資料錯了，下游整條排程都會跟著錯 —— 這個代價不能由工具自動承擔。
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pandas as pd
import yaml

import extract_llm
import extract_rules
import planner_settings
import triage
from adapters import get_source
from domain import ChangeType, CommitmentStrength
from llm.provider import get_provider
from supplier_stats import estimate_delay, load_outcomes

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
INBOX = DATA / "inbox"


def load_config() -> dict:
    with open(ROOT / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def get_data_source(cfg: dict | None = None):
    """
    依設定建立資料來源。

    刻意不在這裡做「找不到就退回 CSV」的容錯 ——
    使用者以為在讀 ERP、實際卻在讀 CSV，是最糟的失敗方式。
    寧可大聲失敗。
    """
    cfg = cfg or load_config()
    source = get_source(cfg.get("data_source", "csv"))
    problems = source.validate()
    if problems:
        raise ValueError(
            f"資料來源 '{source.name}' 不符合資料合約：" + "；".join(problems))
    return source


def load_reference_data(cfg: dict | None = None):
    """回傳 (採購單, 料號主檔, 供應商主檔)。來源由 config.yaml 決定。"""
    src = get_data_source(cfg)
    return src.purchase_orders(), src.materials(), src.suppliers()


def load_emails() -> list[dict]:
    idx = INBOX / "_index.json"
    if not idx.exists():
        raise FileNotFoundError(
            "找不到 data/inbox/_index.json，請先執行： py src/generate_data.py")
    return json.loads(idx.read_text(encoding="utf-8"))


def load_ground_truth() -> list[dict]:
    p = INBOX / "_ground_truth.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []


_OUTCOME_COLUMNS = ["supplier_id", "delay_days", "reschedule_count"]


def _load_outcomes() -> pd.DataFrame:
    """
    歷史收貨紀錄。讀不到時回傳空表 —— 之後每一筆都會明確標示
    「歷史樣本不足」，而不是悄悄假裝有估計。
    """
    try:
        return load_outcomes()
    except (FileNotFoundError, RuntimeError):
        return pd.DataFrame(columns=_OUTCOME_COLUMNS)


def _sort_by_urgency(df: pd.DataFrame) -> pd.DataFrame:
    """先依分級，同級內依預估缺料天數由大到小，最後依收信時間。"""
    rank = df["priority"].map(triage.PRIORITY_RANK).fillna(9)
    return (df.assign(_rank=rank)
              .sort_values(["_rank", "gap_days", "received_at"],
                           ascending=[True, False, True], na_position="last")
              .drop(columns="_rank").reset_index(drop=True))


def _strength_for_percentile(rec: dict) -> str | None:
    """
    決定要用哪個百分位的承諾強度依據。

    只看「new_eta 是否解析得出有效日期」，不是只看欄位有沒有值 ——
    解析失敗的髒日期（例如 LLM 抽到 "2026-13-45" 這種不存在的日期）
    跟完全沒給日期一樣不可靠，都該走 triage 裡最保守的 "none" 百分位，
    不能因為欄位有字串內容就誤判成真的有一個可用的承諾日期。
    用 triage._d 而不是自己重寫一次日期解析，是因為 triage.evaluate
    最終也是用它判斷有沒有新日期，兩處標準不一致才是真正的風險。
    """
    has_date = triage._d(rec.get("new_eta")) is not None
    return rec.get("commitment_strength") if has_date else "none"


# ---------------------------------------------------------------------------
def extract_one(email: dict, cfg: dict, known_pos: list[str],
                provider=None, use_llm: bool = True) -> tuple[list[dict], dict]:
    """
    對單封信執行分層解析。

    回傳 (records, trace)。trace 記錄這封信走過哪幾層、為什麼升級、
    LLM 呼叫的耗時與成敗 —— 沒有 trace 的 AI 工具無法被稽核，也不會被信任。
    """
    threshold = float(cfg["extraction"]["confidence_threshold"])
    rule_records = [r.to_dict() for r in extract_rules.extract(email)]
    rule_conf = max([r["confidence"] for r in rule_records], default=0.0)

    trace = {
        "email_id": email["email_id"],
        "rule_confidence": round(rule_conf, 2),
        "escalated": False,
        "llm_ok": False,
        "llm_latency_ms": 0,
        "llm_error": "",
        "final_layer": "rule",
    }

    if not use_llm or rule_conf >= threshold:
        trace["reason"] = ("未啟用 LLM" if not use_llm
                           else f"規則層信心 {rule_conf:.2f} ≥ 門檻 {threshold}，未升級")
        return rule_records, trace

    trace["escalated"] = True
    trace["reason"] = f"規則層信心 {rule_conf:.2f} < 門檻 {threshold}，升級至 LLM"
    llm_records, meta = extract_llm.extract(
        email, cfg["data_generation"]["as_of_date"], known_pos, provider)
    trace.update(llm_ok=meta["ok"], llm_latency_ms=meta["latency_ms"],
                 llm_error=meta["error"])

    if meta["ok"] and llm_records:
        trace["final_layer"] = "llm"
        return [r.to_dict() for r in llm_records], trace

    # LLM 不可用或失敗 —— 退回規則層結果，並在 UI 標示可信度低。
    trace["final_layer"] = "rule(fallback)"
    return rule_records, trace


def retriage(all_df: pd.DataFrame, gr_days_by_material: dict, tcfg: dict) -> pd.DataFrame:
    """
    只重算分級，不重跑讀信。

    企劃在「收貨處理天數」分頁調完某個料號的天數後，只需要重新跑這支函式
    就能立刻看到新的優先序——不必、也不該為了一個天數調整再去呼叫 LLM
    重新解析一次信件（成本與延遲都划不來，何況信件內容根本沒變）。這也是
    run() 唯一的分級路徑：run() 把抽取與對位的原始欄位寫進 all_df，
    分級一律交給這支函式，避免兩套邏輯各自演化到對不上。

    gr_days_by_material：{material_id: (天數, 來源說明)}，只放企劃覆寫過的
    料號。沒被覆寫的料號，用列上原本的 gr_processing_days／category 呼叫
    planner_settings.effective_gr_days 取得料別預設與說明文字。
    """
    if all_df.empty:
        return all_df

    rows: list[dict] = []
    for _, series in all_df.iterrows():
        row = series.to_dict()
        if not row.get("matched"):
            # 對不到 PO 主檔的列本來就沒有可以重算的東西，原樣保留。
            rows.append(row)
            continue

        material_id = row.get("material_id")
        override = gr_days_by_material.get(material_id)
        if override is not None:
            gr_days, gr_source = override
        else:
            gr_days, gr_source = planner_settings.effective_gr_days(
                material_id, row.get("gr_processing_days"), row.get("category"), {})

        record = {"new_eta": row.get("new_eta"), "change_type": row.get("change_type"),
                  "commitment_strength": row.get("commitment_strength")}
        po = {"committed_date": row.get("committed_date"), "need_date": row.get("need_date"),
              "downstream_scheduled": row.get("downstream_scheduled")}
        material = {"has_qualified_second_source": row.get("has_second_source"),
                    "is_bottleneck": row.get("is_bottleneck"),
                    "alt_material_id": row.get("alt_material_id"),
                    "gr_processing_days": gr_days, "gr_source": gr_source}

        if "estimate_available" not in row:
            # 相容舊格式的 all_df（例如 Task 7 之前產生、沒有這幾個原始欄位
            # 的資料）：用 delay_n > 0 反推有沒有估計——有估計一定有算過
            # 至少一筆歷史樣本，delay_n 才會是正的。百分位優先讀
            # delay_percentile，沒有（或是 NaN）就照承諾強度現算，
            # 跟 run() 選百分位的邏輯一致。
            n = row.get("delay_n", 0)
            try:
                n = int(n) if not triage._missing(n) else 0
            except (TypeError, ValueError):
                n = 0
            if n > 0:
                pct = row.get("delay_percentile")
                if triage._missing(pct):
                    pct = triage.percentile_for(row.get("commitment_strength"), tcfg)
                estimate = {"available": True, "delay_days": row.get("delay_days_est", 0),
                            "percentile": pct, "basis": row.get("delay_basis", ""), "n": n}
            else:
                estimate = {"available": False, "n": n,
                            "reason": row.get("estimate_reason") or "沒有歷史收貨紀錄"}
        elif row.get("estimate_available"):
            estimate = {"available": True, "delay_days": row.get("delay_days_est", 0),
                        "percentile": row.get("delay_percentile"),
                        "basis": row.get("delay_basis", ""), "n": row.get("delay_n", 0)}
        elif triage._clean_str(row.get("estimate_reason")):
            estimate = {"available": False, "n": row.get("delay_n", 0),
                        "reason": row.get("estimate_reason")}
        else:
            estimate = None

        result = triage.evaluate(record, po, material, tcfg, estimate)
        row.update(gap_days=result["gap_days"], conservative_eta=result["conservative_eta"],
                   available_date=result["available_date"], priority=result["priority"],
                   reasons=result["reasons"], actions=result["actions"], note=result["note"],
                   gr_processing_days=gr_days, gr_source=gr_source)
        rows.append(row)

    out = pd.DataFrame(rows)
    out = out[out["priority"] != "—"].reset_index(drop=True)
    return _sort_by_urgency(out)


def run(cfg: dict | None = None, use_llm: bool = True) -> dict:
    """執行完整流程，回傳可直接餵給 UI 的結果集。"""
    cfg = cfg or load_config()
    tcfg = cfg["triage"]
    as_of = date.fromisoformat(cfg["data_generation"]["as_of_date"])

    source = get_data_source(cfg)
    pos_df, mats_df, sups_df = (source.purchase_orders(), source.materials(),
                                source.suppliers())
    emails = load_emails()
    po_index = pos_df.set_index("po_no").to_dict("index")
    mat_index = mats_df.set_index("material_id").to_dict("index")
    sup_index = sups_df.set_index("supplier_id").to_dict("index")
    outcomes = _load_outcomes()

    provider = get_provider() if use_llm else None
    llm_available = bool(provider and provider.available)

    rows: list[dict] = []
    traces: list[dict] = []

    for email in emails:
        # 只把該供應商名下的 PO 帶進 prompt，縮短 prompt 並降低錯配機會。
        # 帶的是完整情境（含原承諾日）而非只有單號 —— 模型要能推算相對日期。
        known = pos_df.loc[
            pos_df["supplier_id"] == email["supplier_id"],
            ["po_no", "material_id", "committed_date", "need_date"]
        ].to_dict("records")
        records, trace = extract_one(email, cfg, known, provider, use_llm)
        traces.append(trace)

        for rec in records:
            po_no = rec.get("po_no")
            if not po_no:
                continue
            rec["email_id"] = email["email_id"]
            rec["received_at"] = email["received_at"]
            rec["supplier_id"] = email["supplier_id"]
            rec["subject"] = email["subject"]
            rec["email_tags"] = ",".join(email.get("tags", []))

            po = po_index.get(po_no)
            if po is None:
                # 對不到 PO 主檔 —— 不能靜默丟掉，這通常代表 PO 號打錯或
                # 是別的單位的單，必須讓人看到。
                rows.append({**rec, "po_no": po_no, "matched": False,
                             "gap_days": None, "priority": "待查",
                             "reasons": ["信中的 PO 號對不到主檔，需人工確認"],
                             "actions": [],
                             "needs_human_review": True, "note": ""})
                continue

            material = mat_index.get(po["material_id"], {})
            supplier = sup_index.get(po["supplier_id"], {})

            # ---- 對位之後才判定變更類型（單看信件做不到）----
            if rec.get("change_type") != ChangeType.NO_CHANGE.value and rec.get("new_eta"):
                try:
                    new_eta = date.fromisoformat(rec["new_eta"])
                    committed = date.fromisoformat(str(po["committed_date"])[:10])
                    if new_eta < committed:
                        rec["change_type"] = ChangeType.PULL_IN.value
                    elif new_eta > committed:
                        rec["change_type"] = ChangeType.DELAY.value
                    else:
                        rec["change_type"] = ChangeType.NO_CHANGE.value
                except ValueError:
                    pass

            # 信裡沒給新日期（或給的日期解析不出來）時，不論模型把承諾強度
            # 判成什麼，都取最保守的百分位。
            strength = _strength_for_percentile(rec)
            pct = triage.percentile_for(strength, tcfg)
            estimate = estimate_delay(outcomes, po["supplier_id"], percentile=pct,
                                      min_samples=int(tcfg["min_samples"]))

            # 收貨處理天數：run() 這裡還沒有企劃的覆寫（覆寫只在使用者按
            # 「收貨處理天數」分頁的表單時才會有），所以永遠傳空 overrides，
            # 取到的就是料號主檔＋料別預設。之後 retriage() 用同一支函式，
            # 分級只有這一套邏輯，不會兩邊各自算一次而對不上。
            gr_days, gr_source = planner_settings.effective_gr_days(
                po["material_id"], material.get("gr_processing_days"),
                material.get("category"), {})
            material_for_eval = {**material, "gr_processing_days": gr_days,
                                 "gr_source": gr_source}
            result = triage.evaluate(rec, po, material_for_eval, tcfg, estimate)

            # ---- 人工確認閘門 ----
            gate = set(cfg["extraction"]["require_human_review_when"])
            needs_review = (
                rec.get("commitment_strength") in gate
                or rec.get("confidence", 0) < 0.6
                or (rec.get("change_type") == ChangeType.DELAY.value
                    and not rec.get("new_eta"))
            )

            rows.append({
                **rec,
                "matched": True,
                "material_id": po["material_id"],
                "category": material.get("category", ""),
                "committed_date": po["committed_date"],
                "need_date": po["need_date"],
                "qty": po["qty"],
                "downstream_scheduled": po["downstream_scheduled"],
                "reschedule_count": po["reschedule_count"],
                "has_second_source": material.get("has_qualified_second_source", False),
                "is_bottleneck": material.get("is_bottleneck", False),
                "share_of_period_demand": po.get("share_of_period_demand"),
                "criticality": material.get("criticality", ""),
                "std_lead_time_days": material.get("std_lead_time_days", 0),
                "alt_material_id": material.get("alt_material_id", ""),
                "supplier_name": supplier.get("supplier_name", ""),
                "supplier_otd": supplier.get("historical_otd_rate", None),
                "gr_processing_days": gr_days,
                "gr_source": gr_source,
                "estimate_available": bool(estimate and estimate.get("available")),
                "estimate_reason": "" if (estimate and estimate.get("available")) \
                    else (estimate or {}).get("reason", ""),
                "delay_percentile": (float(estimate["percentile"])
                                     if estimate and estimate.get("available") else pct),
                "gap_days": result["gap_days"],
                "conservative_eta": result["conservative_eta"],
                "available_date": result["available_date"],
                "delay_days_est": result["delay_days_est"],
                "delay_basis": result["delay_basis"],
                "delay_n": result["delay_n"],
                "priority": result["priority"],
                "reasons": result["reasons"],
                "actions": result["actions"],
                "note": result["note"],
                "needs_human_review": needs_review,
            })

    df = pd.DataFrame(rows)
    if df.empty:
        return {"actions": df, "traces": pd.DataFrame(traces),
                "llm_available": llm_available, "stats": {}, "as_of": as_of,
                "data_source": source.describe()}

    # ---- 去重：同一張 PO 被多封信提到時，以最新一封為準 ----
    # （對應手寫案例 HC-001 / HC-009：同一天內供應商又補了一封確認信）
    df["received_at"] = pd.to_datetime(df["received_at"], errors="coerce")
    df = (df.sort_values("received_at")
            .drop_duplicates(subset=["po_no"], keep="last")
            .reset_index(drop=True))

    # 分級交給 retriage()，跟企劃事後調整天數走同一套邏輯（不重覆一份
    # 過濾＋排序），這裡沒有任何覆寫，結果等同於直接濾掉「—」再排序。
    actionable = retriage(df, {}, tcfg)

    stats = {
        "emails_processed": len(emails),
        "records_extracted": len(rows),
        "unique_pos": int(df["po_no"].nunique()),
        "no_change_filtered": int((df["priority"] == "—").sum()),
        "actionable": int(len(actionable)),
        "p1": int((actionable["priority"] == "P1").sum()),
        "p2": int((actionable["priority"] == "P2").sum()),
        "p3": int((actionable["priority"] == "P3").sum()),
        "unmatched": int((~df["matched"]).sum()),
        "needs_review": int(actionable["needs_human_review"].sum()),
        "escalated": int(sum(t["escalated"] for t in traces)),
        "llm_ok": int(sum(t["llm_ok"] for t in traces)),
    }

    return {"actions": actionable, "all": df, "traces": pd.DataFrame(traces),
            "llm_available": llm_available, "stats": stats, "as_of": as_of,
            "data_source": source.describe()}


if __name__ == "__main__":
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    out = run(use_llm=True)
    print(json.dumps(out["stats"], ensure_ascii=False, indent=2))
    cols = ["priority", "gap_days", "po_no", "material_id", "supplier_name",
            "new_eta", "commitment_strength", "needs_human_review"]
    print(out["actions"][cols].head(15).to_string(index=False))
