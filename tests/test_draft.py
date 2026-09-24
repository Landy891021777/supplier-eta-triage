# -*- coding: utf-8 -*-
"""
草稿生成的測試：不可讓 pandas 的 NaN／浮點字面上出現在給供應商看的草稿或 prompt 裡。

回歸測試：pandas 3 會把 `df.iloc[i].to_dict()` 裡原本是 `None` 的欄位
靜默轉成 float NaN（只要同一欄還有其他列是字串或數字，就會被統一成
object/float dtype）。`NaN` 是 truthy，`row.get(...) or 預設值` 完全擋
不住，草稿因此對供應商寫出「交期為 nan」「預估缺料天數 nan」這種字面
上的 bug；`gap_days` 欄位只要整欄混進 NaN，還會被 pandas 升成
float64，整數 39 就變成 39.0，一樣不能直接印給供應商看。

因此下面刻意用 `pd.DataFrame([...]).iloc[i].to_dict()` 建 row，
不能用手寫的乾淨 dict —— 那樣測不出這個 bug，app.py 傳進來的
本來就是 `row.to_dict()`。
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import draft  # noqa: E402


def _rows() -> pd.DataFrame:
    return pd.DataFrame([
        # 待查：信中沒給新日期，缺料天數估不出來，也沒有原承諾日／需求日
        # （對應「PO 對不到主檔」那條路徑）。
        {"po_no": "PO-2026-00001", "material_id": "WF-1", "supplier_name": "Foundry-A",
         "committed_date": None, "need_date": None,
         "new_eta": None, "gap_days": None, "commitment_strength": "none",
         "priority": "待查",
         "reasons": ["信中對到採購單，但讀不出新日期或變更內容，需人工看信"]},
        # 正常案件：gap_days 是整數 39，與上一列的 None 混在同一欄，
        # pandas 會把整欄升成 float64。
        {"po_no": "PO-2026-00002", "material_id": "WF-2", "supplier_name": "Foundry-B",
         "committed_date": "2026-10-01", "need_date": "2026-09-01",
         "new_eta": "2026-10-10", "gap_days": 39, "commitment_strength": "confirmed",
         "priority": "P1", "reasons": ["比下游需求日晚 39 天，預估缺料"]},
    ])


@dataclass
class _FakeResponse:
    ok: bool
    text: str
    provider: str
    model: str
    latency_ms: int = 0
    error: str = ""


class _FakeProvider:
    """假 LLM provider：記錄收到的 prompt，回傳固定的成功回應，不呼叫任何 API。"""
    available = True

    def __init__(self):
        self.captured_prompt: str | None = None

    def complete(self, prompt, *, temperature: float = 0.0, json_mode: bool = False):
        self.captured_prompt = prompt
        return _FakeResponse(ok=True, text="x", provider="fake", model="m", latency_ms=0)


# ---------------------------------------------------------------------------
# 模板模式（無 LLM）
# ---------------------------------------------------------------------------
def test_template_draft_has_no_nan_when_fields_are_missing():
    row = _rows().iloc[0].to_dict()
    text = draft._template_draft(row)
    assert "nan" not in text.lower()
    assert "（未提供）" in text


def test_template_draft_has_no_float_suffix_for_integer_gap_row():
    row = _rows().iloc[1].to_dict()
    text = draft._template_draft(row)
    assert "nan" not in text.lower()
    assert "39.0" not in text


# ---------------------------------------------------------------------------
# LLM 模式：prompt 本身也不可以出現 nan
# ---------------------------------------------------------------------------
def test_generate_prompt_has_no_nan_and_says_cannot_estimate():
    row = _rows().iloc[0].to_dict()
    provider = _FakeProvider()
    draft.generate(row, provider=provider)
    prompt = provider.captured_prompt
    assert prompt is not None
    assert "nan" not in prompt.lower()
    assert "無法估計" in prompt
    assert "（信中未提供明確日期）" in prompt


def test_generate_prompt_shows_integer_gap_not_float():
    row = _rows().iloc[1].to_dict()
    provider = _FakeProvider()
    draft.generate(row, provider=provider)
    prompt = provider.captured_prompt
    assert prompt is not None
    assert "nan" not in prompt.lower()
    assert "39.0" not in prompt
    assert "39" in prompt
