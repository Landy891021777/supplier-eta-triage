# -*- coding: utf-8 -*-
"""
企劃確認交期的測試。

守住：確認要有日期與姓名；確認後分級立即改用確認日期；
供應商之後又來新信時，舊的確認不能蓋掉新資訊；永不寫回 ERP。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pipeline  # noqa: E402
import planner_settings as ps  # noqa: E402

TCFG = {"tight_buffer_days": 3, "percentile_confirmed": .8,
        "percentile_estimated": .9, "percentile_intent_only": .95, "min_samples": 20}


@pytest.fixture
def db(tmp_path):
    return tmp_path / "planner_settings.db"


def _row(**kw):
    base = dict(po_no="A", email_id="E1", matched=True, change_type="delay",
                commitment_strength="intent_only", new_eta="2026-10-10",
                committed_date="2026-10-01", need_date="2026-10-20",
                downstream_scheduled=False, has_second_source=True, is_bottleneck=False,
                alt_material_id="", material_id="PR-ArF-1088", category="PHOTORESIST",
                gr_processing_days=0, delay_days_est=0, delay_basis="改期過的單",
                delay_n=40, delay_percentile=.95, estimate_available=True,
                estimate_reason="", needs_human_review=True, supplier_id="SUP-R01",
                received_at=pd.Timestamp("2026-09-08"))
    base.update(kw)
    return base


def test_confirmation_requires_date_and_name(db):
    with pytest.raises(ValueError, match="日期"):
        ps.confirm_eta(db, "A", "E1", "", "電話確認", "王小明")
    with pytest.raises(ValueError, match="姓名"):
        ps.confirm_eta(db, "A", "E1", "2026-10-25", "電話確認", " ")


def test_confirmed_date_is_used_and_review_flag_cleared(db):
    ps.confirm_eta(db, "A", "E1", "2026-10-25", "電話確認", "王小明", now="2026-09-22 10:00")
    out = pipeline.retriage(pd.DataFrame([_row()]), {}, TCFG,
                            confirmations=ps.load_confirmations(db))
    r = out.set_index("po_no").loc["A"]
    assert r["new_eta"] == "2026-10-25" and r["commitment_strength"] == "confirmed"
    assert not r["needs_human_review"]
    assert "王小明" in r["reasons"][0] and "尚未寫回 ERP" in r["reasons"][0]
    assert r["gap_days"] == 5          # 10/25 + 延遲估計 0 − 需求日 10/20


def test_newer_supplier_email_supersedes_old_confirmation(db):
    """
    企劃 9/22 依 E1 確認了 10/25；隔天供應商又寄 E2 說要延到 11/10。
    若舊確認蓋掉新信，工具會把一個已經作廢的日期當真。
    """
    ps.confirm_eta(db, "A", "E1", "2026-10-25", "電話確認", "王小明")
    out = pipeline.retriage(pd.DataFrame([_row(email_id="E2", new_eta="2026-11-10")]), {},
                            TCFG, confirmations=ps.load_confirmations(db))
    r = out.set_index("po_no").loc["A"]
    assert r["new_eta"] == "2026-11-10" and r["needs_human_review"]
    assert not any("王小明" in s for s in r["reasons"])


def test_confirmation_log_keeps_every_entry(db):
    ps.confirm_eta(db, "A", "E1", "2026-10-25", "電話", "王小明", now="2026-09-22 10:00")
    ps.confirm_eta(db, "A", "E1", "2026-10-28", "改口", "王小明", now="2026-09-22 15:00")
    assert [c["confirmed_date"] for c in ps.confirmation_log(db)] == ["2026-10-25", "2026-10-28"]
    assert ps.load_confirmations(db)["A"]["confirmed_date"] == "2026-10-28"


def test_no_confirmations_changes_nothing():
    df = pd.DataFrame([_row()])
    a = pipeline.retriage(df, {}, TCFG)
    b = pipeline.retriage(df, {}, TCFG, confirmations={})
    assert a[["po_no", "priority", "gap_days"]].equals(b[["po_no", "priority", "gap_days"]])
