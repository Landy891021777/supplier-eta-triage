# -*- coding: utf-8 -*-
"""
第 2 層：LLM 解析。

只在規則層信心不足時才被呼叫（門檻寫在 config.yaml）。

為什麼不全部丟 LLM？
    成本與延遲。一天幾百封回覆信，如果每封都呼叫一次 API，
    成本與等待時間都會讓同仁不想用。能用規則解決的就不要花 token ——
    這是內部工具能不能長期存活的現實問題，不是技術潔癖。

    分層的另一個好處：規則層的結果是完全可重現的。
    對於格式化通知信，每次跑都得到一樣的答案，稽核起來容易得多。
"""
from __future__ import annotations

from pathlib import Path

from domain import ChangeType, CommitmentStrength, ExtractedRecord
from llm.provider import BaseProvider, extract_json, get_provider

PROMPT_PATH = Path(__file__).resolve().parent / "llm" / "prompts" / "extract_eta.md"

_VALID_STRENGTH = {c.value for c in CommitmentStrength}
_VALID_CHANGE = {c.value for c in ChangeType}


def build_prompt(email: dict, reference_date: str, known_pos: list[str]) -> str:
    """組出送給模型的完整 prompt。"""
    template = PROMPT_PATH.read_text(encoding="utf-8")
    # 只帶入該供應商相關的 PO，避免 prompt 過長且降低模型亂配的機會
    listing = "\n".join(f"- {p}" for p in known_pos[:60]) or "（無）"
    email_text = (
        f"Date: {email.get('received_at', '')}\n"
        f"Subject: {email.get('subject', '')}\n\n"
        f"{email.get('body', '')}"
    )
    return (template
            .replace("{{REFERENCE_DATE}}", reference_date)
            .replace("{{SUPPLIER_ID}}", str(email.get("supplier_id", "")))
            .replace("{{KNOWN_POS}}", listing)
            .replace("{{EMAIL_TEXT}}", email_text))


def _coerce(rec: dict) -> ExtractedRecord:
    """
    把模型輸出轉成內部資料結構，並做防禦性驗證。

    絕不無條件相信模型的輸出：欄位值不在允許集合內時一律退回保守值。
    在這個工具裡，「保守」的定義是「假設它不是承諾」——
    因為誤判為承諾的代價（下游照著排、然後爆掉）遠高於多追一通電話。
    """
    strength = str(rec.get("commitment_strength", "")).lower()
    if strength not in _VALID_STRENGTH:
        strength = CommitmentStrength.ESTIMATED.value

    change = str(rec.get("change_type", "")).lower()
    if change not in _VALID_CHANGE:
        change = ChangeType.UNKNOWN.value

    eta = rec.get("new_eta")
    if isinstance(eta, str):
        eta = eta.strip() or None
        if eta and len(eta) != 10:  # 不是 YYYY-MM-DD 就不採信
            eta = None
    else:
        eta = None

    try:
        conf = float(rec.get("confidence", 0.5))
    except (TypeError, ValueError):
        conf = 0.5

    return ExtractedRecord(
        po_no=(str(rec["po_no"]).strip().upper() if rec.get("po_no") else None),
        material_id=None,
        new_eta=eta,
        raw_date_text=(rec.get("raw_date_text") or None),
        commitment_strength=strength,
        change_type=change,
        reason_code=str(rec.get("reason_code", "not_stated")),
        confidence=max(0.0, min(1.0, conf)),
        extracted_by="llm",
        notes=str(rec.get("notes", ""))[:400],
    )


def extract(email: dict, reference_date: str, known_pos: list[str],
            provider: BaseProvider | None = None) -> tuple[list[ExtractedRecord], dict]:
    """
    回傳 (解析結果, 呼叫中繼資料)。

    中繼資料含 provider、模型、耗時、成敗與錯誤訊息，
    供 UI 顯示與稽核使用 —— 使用者有權知道這筆結果是怎麼來的。
    """
    provider = provider or get_provider()
    meta = {"provider": provider.name, "model": provider.model,
            "ok": False, "latency_ms": 0, "error": ""}

    if not provider.available:
        meta["error"] = "無可用 LLM provider，已降級為僅規則層"
        return [], meta

    prompt = build_prompt(email, reference_date, known_pos)
    resp = provider.complete(prompt)
    meta.update(ok=resp.ok, latency_ms=resp.latency_ms, error=resp.error)
    if not resp.ok:
        return [], meta

    data = extract_json(resp.text)
    if isinstance(data, dict):
        rows = data.get("records", [])
    elif isinstance(data, list):
        rows = data
    else:
        meta["ok"] = False
        meta["error"] = "模型回應無法解析為 JSON"
        return [], meta

    out = [_coerce(r) for r in rows if isinstance(r, dict) and r.get("po_no")]
    return out, meta
