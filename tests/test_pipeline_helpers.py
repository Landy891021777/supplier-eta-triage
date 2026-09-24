# -*- coding: utf-8 -*-
"""
pipeline.py 裡不依賴資料庫／信件的小函式測試，跟 test_pipeline_triage.py
分開放，因為那個檔案在沒有 data/ 時整個模組會被 skip，但這裡測的是
純函式，不該連帶被跳過。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pipeline  # noqa: E402


def test_no_new_date_uses_most_conservative_percentile_basis():
    assert pipeline._strength_for_percentile({"new_eta": None}) == "none"


def test_unparseable_new_date_uses_most_conservative_percentile_basis():
    """
    回歸測試：解析失敗的髒日期（例如 LLM 抽到不存在的 "2026-13-45"）
    曾經因為 `if rec.get("new_eta")` 只檢查欄位有沒有值而被誤判成
    「有承諾強度」，實際上根本沒有可用的新日期，百分位因此選得不夠保守。
    """
    assert pipeline._strength_for_percentile(
        {"new_eta": "2026-13-45", "commitment_strength": "confirmed"}) == "none"


def test_valid_new_date_uses_its_own_commitment_strength():
    assert pipeline._strength_for_percentile(
        {"new_eta": "2026-10-10", "commitment_strength": "confirmed"}) == "confirmed"
