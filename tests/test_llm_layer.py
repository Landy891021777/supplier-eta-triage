# -*- coding: utf-8 -*-
"""
LLM 層的測試。

這兩組測試守住的是本專案開發過程中**實際踩過的兩個坑**。
它們不是為了覆蓋率而寫，是為了不再犯同一個錯。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import extract_llm  # noqa: E402
from llm.provider import NullProvider, _is_retryable, _retry_delay_from  # noqa: E402


# ---------------------------------------------------------------------------
# 坑一：把配額限制誤讀成模型能力
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("error", [
    "HTTP 429: quota exceeded",
    "HTTP 503: service unavailable",
    "Timeout: read timed out",
    "ConnectionError: connection reset",
])
def test_transient_errors_are_retryable(error):
    """429/5xx/連線問題是暫時性的，必須重試而不是當成解析失敗。"""
    assert _is_retryable(error) is True


@pytest.mark.parametrize("error", [
    "HTTP 400: invalid request",
    "HTTP 404: model no longer available",
    "HTTP 401: unauthorized",
])
def test_permanent_errors_are_not_retryable(error):
    """
    prompt 有問題、模型名稱錯、金鑰無效 —— 重試一百次也不會過，
    只會白白消耗配額與時間。必須快速失敗。
    """
    assert _is_retryable(error) is False


def test_retry_delay_respects_server_hint():
    """
    服務商在錯誤內容裡指定了 retryDelay 就要照做，不要自己亂猜。
    等太短會繼續打 429，等太長會讓批次處理慢到沒人願意用。
    """
    body = '{"error": {"details": [{"retryDelay": "21s"}]}}'
    assert _retry_delay_from(body, attempt=0) == pytest.approx(22.0)


def test_retry_delay_falls_back_to_exponential_backoff():
    assert _retry_delay_from("no hint here", attempt=3) == pytest.approx(8.0)
    assert _retry_delay_from("no hint here", attempt=10) == pytest.approx(30.0)  # 有上限


# ---------------------------------------------------------------------------
# 坑二：沒把系統已知的事實給模型，卻怪模型算不出來
# ---------------------------------------------------------------------------
def test_prompt_includes_committed_date_context():
    """
    供應商寫「往後抓個兩週」時，信裡沒有原承諾日 ——
    那個資訊在我方系統裡。不把它放進 prompt，就是要求模型猜一個
    它不可能知道的數字。這是設計者的錯，不是模型的錯。
    """
    email = {"email_id": "T-1", "supplier_id": "SUP-F01",
             "received_at": "2026-09-08 09:00", "subject": "RE: PO-2026-04417",
             "body": "we may need roughly 2 more week(s) beyond the original date"}
    known = [{"po_no": "PO-2026-04417", "material_id": "WF-N6-XR3390",
              "committed_date": "2026-10-15", "need_date": "2026-10-22"}]
    prompt = extract_llm.build_prompt(email, "2026-09-08", known)

    assert "PO-2026-04417" in prompt
    assert "2026-10-15" in prompt, "原承諾日必須出現在 prompt 裡"
    assert "原承諾日" in prompt, "必須明確標示哪一欄是推算基準"


def test_prompt_still_works_with_plain_po_list():
    """向後相容：只給 PO 號清單時不應該壞掉。"""
    email = {"supplier_id": "SUP-F01", "subject": "x", "body": "y"}
    prompt = extract_llm.build_prompt(email, "2026-09-08", ["PO-2026-04417"])
    assert "PO-2026-04417" in prompt


# ---------------------------------------------------------------------------
# 降級行為
# ---------------------------------------------------------------------------
def test_null_provider_fails_explicitly_rather_than_faking():
    """
    無金鑰時必須明確失敗，不能偷偷回傳看起來像真的結果 ——
    使用者無法分辨哪些欄位可信，比直接失敗更危險。
    """
    p = NullProvider()
    assert p.available is False
    r = p.complete("anything")
    assert r.ok is False
    assert r.text == ""
    assert r.error, "降級時必須留下可讀的原因，供 UI 顯示"


def test_extract_returns_empty_and_flags_when_no_provider():
    email = {"supplier_id": "SUP-F01", "subject": "x", "body": "y"}
    recs, meta = extract_llm.extract(email, "2026-09-08", [], NullProvider())
    assert recs == []
    assert meta["ok"] is False
    assert "降級" in meta["error"]


# ---------------------------------------------------------------------------
# 防禦性驗證：不無條件相信模型輸出
# ---------------------------------------------------------------------------
def test_invalid_commitment_strength_falls_back_to_conservative():
    """
    模型吐出不在允許集合內的值時，一律退回保守值。
    保守的定義是「假設它不是承諾」—— 誤判為承諾的代價遠高於多追一通電話。
    """
    rec = extract_llm._coerce({"po_no": "PO-2026-04417",
                               "commitment_strength": "definitely_yes"})
    assert rec.commitment_strength == "estimated"


def test_malformed_date_is_dropped_not_guessed():
    rec = extract_llm._coerce({"po_no": "PO-2026-04417", "new_eta": "月底"})
    assert rec.new_eta is None, "無法解析的日期必須留空，不可猜一個"
