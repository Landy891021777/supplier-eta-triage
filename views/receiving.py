# -*- coding: utf-8 -*-
"""
🛠 收貨處理天數 —— 料到廠後要幾天才能投產，依實際情況逐料號調整。

這是工具內的設定，不會寫回 ERP 料號主檔；調整會立刻反映在行動清單。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

import ui_state  # noqa: E402
import planner_settings  # noqa: E402
from domain import CATEGORY_LABEL_ZH  # noqa: E402

ctx = ui_state.context()
overrides = planner_settings.load_overrides()

st.subheader("🛠 收貨處理天數")
st.caption(
    "料到廠後要幾天才能投產（進料檢驗、入庫、光阻回溫等）。預設值依料別，"
    "你可以依實際情況逐料號調整；調整會立刻反映在行動清單。"
    "**這是工具內的設定，不會寫回 ERP 料號主檔**；雲端展示環境重新啟動後會回到預設。")

mats_df = ui_state.materials_df()
table_rows = []
for m in mats_df.itertuples(index=False):
    days, source = planner_settings.effective_gr_days(
        m.material_id, m.gr_processing_days, m.category, overrides)
    default_days = m.gr_processing_days
    table_rows.append({
        "料號": m.material_id,
        "料別": CATEGORY_LABEL_ZH.get(m.category, m.category),
        "主檔預設天數": int(default_days) if default_days == default_days else None,
        "目前使用天數": days,
        "來源": source,
    })
st.dataframe(pd.DataFrame(table_rows), width="stretch", hide_index=True, height=320)

st.divider()
st.markdown("#### 調整料號的收貨處理天數")
material_ids = mats_df["material_id"].tolist()
with st.form("gr_override_form", clear_on_submit=True):
    fc1, fc2 = st.columns([2, 1])
    sel_material = fc1.selectbox("料號", material_ids)
    sel_days = fc2.number_input("天數", min_value=0,
                                max_value=planner_settings.MAX_DAYS, step=1, value=0)
    sel_reason = st.text_input("調整原因（必填）")
    sel_user = st.text_input("姓名（必填）")
    if st.form_submit_button("送出調整"):
        default_days = mats_df.loc[
            mats_df["material_id"] == sel_material, "gr_processing_days"].iloc[0]
        try:
            planner_settings.set_override(
                None, sel_material, sel_days, sel_reason, sel_user,
                default_days=int(default_days) if default_days == default_days else None)
        except ValueError as e:
            st.error(str(e))
        else:
            st.success(f"已更新 {sel_material} 的收貨處理天數為 {int(sel_days)} 天。")
            st.rerun()

if overrides:
    st.markdown("#### 恢復預設")
    with st.form("gr_clear_form", clear_on_submit=True):
        cc1, cc2 = st.columns([2, 1])
        clr_material = cc1.selectbox("料號（已調整）", sorted(overrides))
        clr_reason = cc2.text_input("恢復原因（必填）")
        clr_user = st.text_input("姓名（必填）", key="clear_user")
        if st.form_submit_button("恢復預設值"):
            default_days = mats_df.loc[
                mats_df["material_id"] == clr_material, "gr_processing_days"].iloc[0]
            try:
                planner_settings.clear_override(
                    None, clr_material, clr_reason, clr_user,
                    default_days=int(default_days) if default_days == default_days else 0)
            except ValueError as e:
                st.error(str(e))
            else:
                st.success(f"{clr_material} 已恢復為料別預設天數。")
                st.rerun()

st.markdown("#### 調整紀錄")
log = planner_settings.change_log()
if not log:
    st.info("尚無調整紀錄。")
else:
    log_df = pd.DataFrame(log).sort_values("log_id", ascending=False)
    st.dataframe(
        log_df[["changed_at", "material_id", "old_days", "new_days",
                "reason", "changed_by"]]
        .rename(columns={"changed_at": "時間", "material_id": "料號",
                         "old_days": "原天數", "new_days": "新天數",
                         "reason": "原因", "changed_by": "調整人"}),
        width="stretch", hide_index=True, height=240)
