# -*- coding: utf-8 -*-
"""
主流程編排：信件 → 分層解析 → 對位 PO → 影響評估 → 行動清單。

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
from domain import ChangeType, CommitmentStrength
from impact import evaluate
from llm.provider import get_provider

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
INBOX = DATA / "inbox"


def load_config() -> dict:
    with open(ROOT / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_reference_data() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    pos = pd.read_csv(DATA / "po_master.csv", encoding="utf-8-sig")
    mats = pd.read_csv(DATA / "materials.csv", encoding="utf-8-sig")
    sups = pd.read_csv(DATA / "suppliers.csv", encoding="utf-8-sig")
    return pos, mats, sups


def load_emails() -> list[dict]:
    idx = INBOX / "_index.json"
    if not idx.exists():
        raise FileNotFoundError(
            "找不到 data/inbox/_index.json，請先執行： py src/generate_data.py")
    return json.loads(idx.read_text(encoding="utf-8"))


def load_ground_truth() -> list[dict]:
    p = INBOX / "_ground_truth.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []


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


def run(cfg: dict | None = None, use_llm: bool = True,
        weights: dict | None = None) -> dict:
    """執行完整流程，回傳可直接餵給 UI 的結果集。"""
    cfg = cfg or load_config()
    weights = weights or cfg["impact_weights"]
    thresholds = cfg["priority_thresholds"]
    as_of = date.fromisoformat(cfg["data_generation"]["as_of_date"])

    pos_df, mats_df, sups_df = load_reference_data()
    emails = load_emails()
    po_index = pos_df.set_index("po_no").to_dict("index")
    mat_index = mats_df.set_index("material_id").to_dict("index")
    sup_index = sups_df.set_index("supplier_id").to_dict("index")

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
                             "impact_score": 0.0, "priority": "待查",
                             "top_reasons": ["信中的 PO 號對不到主檔，需人工確認"],
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

            result = evaluate(rec, po, material, supplier, weights, thresholds)

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
                # 以下欄位是為了讓 UI 能在使用者調整權重時「就地重算」影響分數，
                # 而不必重跑一次解析（重跑會再次呼叫 LLM，既慢又花錢）。
                "share_of_period_demand": po.get("share_of_period_demand"),
                "criticality": material.get("criticality", ""),
                "std_lead_time_days": material.get("std_lead_time_days", 0),
                "alt_material_id": material.get("alt_material_id", ""),
                "supplier_name": supplier.get("supplier_name", ""),
                "supplier_otd": supplier.get("historical_otd_rate", None),
                "impact_score": result["impact_score"],
                "priority": result["priority"],
                "top_reasons": result["top_reasons"],
                "rule_details": result["rule_details"],
                "note": result["note"],
                "needs_human_review": needs_review,
            })

    df = pd.DataFrame(rows)
    if df.empty:
        return {"actions": df, "traces": pd.DataFrame(traces),
                "llm_available": llm_available, "stats": {}, "as_of": as_of}

    # ---- 去重：同一張 PO 被多封信提到時，以最新一封為準 ----
    # （對應手寫案例 HC-001 / HC-009：同一天內供應商又補了一封確認信）
    df["received_at"] = pd.to_datetime(df["received_at"], errors="coerce")
    df = (df.sort_values("received_at")
            .drop_duplicates(subset=["po_no"], keep="last")
            .reset_index(drop=True))

    actionable = df[df["priority"].isin(["P1", "P2", "P3", "待查"])].copy()
    actionable = actionable.sort_values(
        ["impact_score", "received_at"], ascending=[False, True]).reset_index(drop=True)

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
            "llm_available": llm_available, "stats": stats, "as_of": as_of}


def rescore(df: pd.DataFrame, weights: dict, thresholds: dict) -> pd.DataFrame:
    """
    以新的權重就地重算影響分數，不重跑解析。

    這個函式存在的理由很實際：使用者在 UI 上拉權重滑桿時，
    每動一次就重跑一次解析會再次呼叫 LLM API —— 又慢又花錢，
    而且同一批信重複送出去也是不必要的資料外流。
    解析結果與評分邏輯必須分離，這是工具能被同仁反覆試玩的前提。
    """
    if df.empty:
        return df
    out = []
    for _, r in df.iterrows():
        row = r.to_dict()
        if not row.get("matched", False):
            out.append(row)
            continue
        po = {"need_date": row.get("need_date"), "committed_date": row.get("committed_date"),
              "downstream_scheduled": row.get("downstream_scheduled"),
              "reschedule_count": row.get("reschedule_count"),
              "share_of_period_demand": row.get("share_of_period_demand")}
        material = {"has_qualified_second_source": row.get("has_second_source"),
                    "criticality": row.get("criticality"),
                    "std_lead_time_days": row.get("std_lead_time_days"),
                    "is_bottleneck": row.get("is_bottleneck"),
                    "alt_material_id": row.get("alt_material_id")}
        res = evaluate(row, po, material, {}, weights, thresholds)
        row.update(impact_score=res["impact_score"], priority=res["priority"],
                   top_reasons=res["top_reasons"], rule_details=res["rule_details"],
                   note=res["note"])
        out.append(row)
    return (pd.DataFrame(out)
            .sort_values(["impact_score", "received_at"], ascending=[False, True])
            .reset_index(drop=True))


if __name__ == "__main__":
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    out = run(use_llm=True)
    print(json.dumps(out["stats"], ensure_ascii=False, indent=2))
    cols = ["priority", "impact_score", "po_no", "material_id", "supplier_name",
            "new_eta", "commitment_strength", "needs_human_review"]
    print(out["actions"][cols].head(15).to_string(index=False))
