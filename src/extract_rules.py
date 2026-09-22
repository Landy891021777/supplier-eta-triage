# -*- coding: utf-8 -*-
"""
第 1 層：規則式解析器（免費、毫秒級、完全可預測）。

設計立場（重要）：
    這一層是「認真寫」的，不是為了襯托 LLM 而故意寫爛的稻草人。
    它涵蓋了實務上最常見的欄位標籤與四種日期格式，
    在格式化通知信上的表現應該要相當好。

    正因為它是認真寫的，當它在自然語言敘述上失敗時，
    那個失敗才構成「這裡確實需要 LLM」的證據 ——
    而不是我覺得 AI 很潮所以硬要用。

它必然做不到的事（也不該勉強用 regex 硬幹）：
    1. 相對日期推算：「往後抓個兩週」「大概月底」「next quarter」
    2. 語意判斷承諾強度：「confirmed」vs「I cannot commit a firm date」
    3. 轉寄串裡分辨新舊資訊
    4. 一封信多張 PO 時，把日期正確歸屬到各自的 PO
    5. 反向語意：「no change」「照原計畫」不是延遲

    這五項就是升級到 LLM 的判準，也是 config.yaml 裡
    confidence_threshold 存在的理由。
"""
from __future__ import annotations

import re
from datetime import date, datetime

from domain import ChangeType, CommitmentStrength, ExtractedRecord

# --------------------------------------------------------------------------
# Pattern 定義
# --------------------------------------------------------------------------
PO_PATTERN = re.compile(r"\bPO[-\s]?\d{4}[-\s]?\d{4,6}\b", re.IGNORECASE)

_MONTHS = "Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec"
DATE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b(20\d{2})[-/](\d{1,2})[-/](\d{1,2})\b"), "iso"),
    (re.compile(rf"\b(\d{{1,2}})[-\s]({_MONTHS})[a-z]*[-\s](20\d{{2}})\b", re.IGNORECASE), "dmy"),
    (re.compile(rf"\b({_MONTHS})[a-z]*\.?\s+(\d{{1,2}})(?:,?\s*(20\d{{2}}))?\b", re.IGNORECASE), "mdy"),
    (re.compile(r"(\d{1,2})\s*月\s*(\d{1,2})\s*日"), "zh"),
    (re.compile(r"\b(\d{1,2})/(\d{1,2})\b(?!/)"), "slash"),
]

# 「這是新的交期」的欄位標籤 —— 命中代表這個日期的角色明確，信心可以拉高
NEW_ETA_LABELS = [
    r"revised\s*eta", r"new\s*eta", r"updated\s*eta", r"revised\s*(delivery|date)",
    r"new\s*(target|date)", r"reschedul\w*\s*to", r"push\w*\s*(out\s*)?to",
    r"move\s*(the\s*)?(delivery\s*)?(from\s*[^\s]+\s*)?to",
    r"順延至", r"延至", r"改為", r"調整至", r"排在", r"改到",
]
NEW_ETA_LABEL_RE = re.compile("|".join(NEW_ETA_LABELS), re.IGNORECASE)

# 「這是舊的交期」的標籤 —— 命中代表這個日期不該被當成新承諾
ORIGINAL_LABELS = re.compile(
    r"original\s*eta|originally\s*scheduled|original\s*date|原訂|原本|原定|由原",
    re.IGNORECASE,
)

# 明確表示「沒有變更」—— 這組是為了避免把確認信誤判成延遲
NO_CHANGE_RE = re.compile(
    r"no\s*change|on\s*track|unchanged|remains?\s*on\s*schedule|as\s*originally\s*scheduled"
    r"|照原計畫|沒有問題|維持原|不變",
    re.IGNORECASE,
)

# 承諾強度的語意線索
CONFIRMED_RE = re.compile(
    r"\bconfirm(ed|ing)?\b|\block(ed)?\b|已確認|確認過|確定的|this is confirmed",
    re.IGNORECASE,
)
HEDGE_RE = re.compile(
    r"\bmay\b|\bmight\b|\blikely\b|\baround\b|\babout\b|\broughly\b|\bapprox|\bestimat"
    r"|not\s*yet\s*locked|cannot\s*commit|can'?t\s*commit|trying\s*(our\s*)?best"
    r"|will\s*update|pending|tbc|to\s*be\s*confirmed"
    r"|大概|可能|預計|盡量|左右|前後|再跟你確認|再確認|還要再",
    re.IGNORECASE,
)

# 相對／模糊日期 —— regex 無法可靠推算，命中即代表「該升級到 LLM」
RELATIVE_DATE_RE = re.compile(
    r"\b(next|this)\s+(week|month|quarter|year)\b|\b\d+\s*(more\s*)?week"
    r"|\bmiddle\s+of\b|\bend\s+of\b|\bearly\s+\w+\b"
    r"|下個?月|這個?月|下週|下季|next\s*quarter|月底|月中|月初|週左右|兩週|三週",
    re.IGNORECASE,
)

REASON_HINTS = [
    ("yield", r"yield|excursion|良率|製程異常"),
    ("capacity", r"capacity|loading|tool\s*down|產能|吃緊|滿載"),
    ("upstream_shortage", r"upstream|raw\s*material|shortage|上游|材料短缺|缺料"),
    ("logistics", r"customs|logistic|shipment\s*delay|運輸|通關"),
    ("customer_priority", r"allocation|other\s*(accounts|customers)|插單|排配"),
    ("internal_reschedule", r"internal\s*reschedul|重排|內部"),
]


def _norm_po(text: str) -> str:
    """把 PO 號正規化成 PO-YYYY-NNNNN，吸收空白與大小寫差異。"""
    digits = re.sub(r"[^0-9]", "", text)
    return f"PO-{digits[:4]}-{digits[4:]}" if len(digits) >= 8 else text.upper()


def _parse_date(match: re.Match, kind: str, ref_year: int) -> date | None:
    try:
        if kind == "iso":
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        if kind == "dmy":
            return datetime.strptime(
                f"{match.group(1)} {match.group(2)[:3].title()} {match.group(3)}",
                "%d %b %Y").date()
        if kind == "mdy":
            year = int(match.group(3)) if match.group(3) else ref_year
            return datetime.strptime(
                f"{match.group(1)[:3].title()} {match.group(2)} {year}", "%b %d %Y").date()
        if kind in ("zh", "slash"):
            return date(ref_year, int(match.group(1)), int(match.group(2)))
    except (ValueError, TypeError):
        return None
    return None


def _find_dates(text: str, ref_year: int) -> list[tuple[date, int, int, str]]:
    """回傳 (日期, 起始位置, 結束位置, 原文字樣)，依出現順序。"""
    found: list[tuple[date, int, int, str]] = []
    taken: list[tuple[int, int]] = []
    for pattern, kind in DATE_PATTERNS:
        for m in pattern.finditer(text):
            if any(m.start() < e and s < m.end() for s, e in taken):
                continue  # 已被更精確的 pattern 吃掉，避免 2026-10-09 又被 slash 抓一次
            d = _parse_date(m, kind, ref_year)
            if d:
                found.append((d, m.start(), m.end(), m.group(0)))
                taken.append((m.start(), m.end()))
    return sorted(found, key=lambda x: x[1])


def _nearest_label(text: str, pos: int, window: int = 60) -> str | None:
    """
    看日期前方 window 個字元內有沒有欄位標籤，用來判斷這個日期的角色。

    必須取「最靠近日期的那個」標籤，不能只看有沒有出現。
    典型的表格式通知長這樣：

        Original ETA : 2026-09-25
        Revised ETA  : 2026-10-09

    若只判斷「左側視窗有沒有 Original」，第二個日期會被上一行的
    Original 標籤污染，整封信因此被誤判、白白升級去呼叫 LLM。
    """
    left = text[max(0, pos - window):pos]
    orig = max((m.start() for m in ORIGINAL_LABELS.finditer(left)), default=-1)
    new = max((m.start() for m in NEW_ETA_LABEL_RE.finditer(left)), default=-1)
    if orig < 0 and new < 0:
        return None
    return "original" if orig > new else "new"


def _line_scoped(text: str, po_at: int, ref_year: int = 2026) -> tuple[date, str] | None:
    """
    同列解析：若 PO 號所在的那一列自己就帶了日期，直接在該列內判定新交期。

    適用於表格式通知，例如：
        PO No.         Original ETA   Revised ETA
        PO-2026-04390  2026-09-25     2026-10-09

    列內判定順序：
      1. 該列有被 new-ETA 標籤修飾的日期 -> 取它
      2. 該列有兩個以上日期且無標籤 -> 取最後一個
         （表格慣例是「原交期在前、新交期在後」；這是啟發式，
           因此仍會被後續的模糊措辭檢查降級）
      3. 該列只有一個日期 -> 取它

    回傳 None 代表該列沒有日期，需退回全文範圍解析。
    """
    start = text.rfind("\n", 0, po_at) + 1
    end = text.find("\n", po_at)
    line = text[start:end if end != -1 else len(text)]
    if len(line.strip()) < 5:
        return None

    dates = _find_dates(line, ref_year)
    if not dates:
        return None

    labelled = [(d, raw) for (d, s, _e, raw) in dates
                if _nearest_label(line, s, window=40) == "new"]
    if labelled:
        return labelled[-1]

    not_original = [(d, raw) for (d, s, _e, raw) in dates
                    if _nearest_label(line, s, window=40) != "original"]
    if not not_original:
        return None
    return not_original[-1]


def _detect_reason(text: str) -> str:
    for code, pat in REASON_HINTS:
        if re.search(pat, text, re.IGNORECASE):
            return code
    return "not_stated"


def _detect_strength(text: str) -> str:
    """
    承諾強度的規則式判斷。

    注意順序：先看有沒有 hedge（模糊措辭），再看有沒有 confirm。
    因為「I cannot commit a firm date ... will confirm later」同時含兩者，
    而實務上只要出現退路措辭，就不該當成承諾。寧可多追一次，不可少追一次。
    """
    if HEDGE_RE.search(text):
        return CommitmentStrength.ESTIMATED.value
    if CONFIRMED_RE.search(text):
        return CommitmentStrength.CONFIRMED.value
    return CommitmentStrength.ESTIMATED.value


def extract(email: dict, ref_year: int = 2026) -> list[ExtractedRecord]:
    """對單封信做規則式解析，回傳 0..N 筆結果（每張 PO 一筆）。"""
    text = f"{email.get('subject', '')}\n{email.get('body', '')}"
    pos_found = [(_norm_po(m.group(0)), m.start()) for m in PO_PATTERN.finditer(text)]
    # 去重但保留首次出現位置
    seen: dict[str, int] = {}
    for po, at in pos_found:
        seen.setdefault(po, at)

    if not seen:
        return [ExtractedRecord(
            po_no=None, material_id=None, new_eta=None, raw_date_text=None,
            confidence=0.0, extracted_by="rule",
            notes="規則層未在信中找到 PO 號",
        )]

    dates = _find_dates(text, ref_year)
    has_relative = bool(RELATIVE_DATE_RE.search(text))
    multi_po = len(seen) > 1
    reason = _detect_reason(text)
    strength = _detect_strength(text)

    records: list[ExtractedRecord] = []
    for po, po_at in seen.items():
        no_change = bool(NO_CHANGE_RE.search(text))

        # ---- 先試「同列解析」----
        # 供應商的排程變更通知常以表格呈現，每一列自成一筆：
        #     PO-2026-04390  SW-300-P-2210  2026-09-25  2026-10-09  Yield excursion
        # 當 PO 號與日期出現在同一列時，歸屬是明確的，不存在歧義，
        # 因此可以放心給高信心，不必為了「這封信有多張 PO」就整封升級到 LLM。
        # 這個改進讓表格式通知留在免費的規則層處理 —— 省錢，且結果可重現。
        line_hit = _line_scoped(text, po_at)
        multi_po_ambiguous = multi_po and line_hit is None

        best: tuple[date, str] | None = None
        if line_hit is not None:
            best, conf = line_hit, 0.85
        else:
            # 退回全文範圍：優先取被 new-ETA 標籤修飾的日期，
            # 其次取離該 PO 號最近、且未被標成 original 的日期。
            labelled = [(d, raw) for (d, s, _e, raw) in dates
                        if _nearest_label(text, s) == "new"]
            if labelled:
                best = labelled[-1]
                conf = 0.90
            else:
                cands = [(d, s, raw) for (d, s, _e, raw) in dates
                         if _nearest_label(text, s) != "original"]
                if cands:
                    d, _s, raw = min(cands, key=lambda x: abs(x[1] - po_at))
                    best = (d, raw)
                    conf = 0.60
                else:
                    conf = 0.25

        notes: list[str] = []
        if no_change:
            change_type = ChangeType.NO_CHANGE.value
            new_eta, raw = None, (best[1] if best else None)
            conf = 0.70
            notes.append("偵測到『無變更』措辭")
        elif best is None:
            change_type = ChangeType.UNKNOWN.value
            new_eta, raw = None, None
            notes.append("找到 PO 但找不到任何可解析日期")
        else:
            new_eta, raw = best[0].isoformat(), best[1]
            change_type = ChangeType.DELAY.value  # 規則層無法區分 delay / pull_in，
            notes.append("規則層預設為 delay；提前交貨需比對 PO 主檔才能判定")

        # 以下情況主動調降信心，把案子讓給 LLM ——
        # 這是本層最重要的設計：知道自己什麼時候不該逞強。
        if has_relative:
            conf = min(conf, 0.30)
            notes.append("偵測到相對／模糊日期，regex 無法可靠推算")
        if multi_po_ambiguous:
            conf = min(conf, 0.45)
            notes.append("一封信含多張 PO 且非表格式，日期歸屬有歧義")
        elif multi_po:
            notes.append("一封信含多張 PO，但採同列解析，歸屬明確")
        if re.search(r"forward|fwd|-------", text, re.IGNORECASE):
            conf = min(conf, 0.40)
            notes.append("疑似轉寄串，無法分辨新舊資訊")
        if HEDGE_RE.search(text):
            conf = min(conf, 0.50)
            notes.append("含退路措辭，承諾強度需語意判斷")

        records.append(ExtractedRecord(
            po_no=po, material_id=None, new_eta=new_eta, raw_date_text=raw,
            commitment_strength=(CommitmentStrength.CONFIRMED.value
                                 if change_type == ChangeType.NO_CHANGE.value else strength),
            change_type=change_type, reason_code=reason,
            confidence=round(conf, 2), extracted_by="rule",
            notes="；".join(notes),
        ))
    return records
