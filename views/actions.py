# -*- coding: utf-8 -*-
"""
📋 今日行動清單 —— 物料企劃打開工具的第一眼。

先給答案、再給過程：首頁五個指標＋依預估缺料天數排序的清單，
每一筆都能展開看到為什麼、建議動作，以及回信草稿。
"""
from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

import ui_state  # noqa: E402
import draft as draft_mod  # noqa: E402
from domain import COMMITMENT_LABEL_ZH  # noqa: E402
from llm.provider import NullProvider, get_provider  # noqa: E402

ctx = ui_state.context()
result, actions, stats = ctx["result"], ctx["actions"], ctx["stats"]

# ---------------- 標題與摘要 ----------------
st.title("📦 供應鏈 AI 工具：交期風險優先排序")
st.caption(
    f"模擬基準日 **{result['as_of']}** ｜ 本次處理 **{stats['emails_processed']}** 封供應商回覆信 "
    "｜ 資料為合成資料，僅供展示（見 README 的誠實聲明）"
)

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("收到信件", stats["emails_processed"])
c2.metric("自動濾除（確認無變更）", stats["no_change_filtered"])
c3.metric("進入行動清單", stats["actionable"])
c4.metric("🔴 今天要處理 P1", stats["p1"])
c5.metric("⚠️ 需人工確認", int(actions["needs_human_review"].sum()))

st.subheader("今日行動清單")
st.caption("依預估缺料天數排序（缺越多天越前面）。展開任一筆可看到為什麼、建議動作，以及回信草稿。")

fcol1, fcol2, fcol3 = st.columns([1, 1, 2])
pick = fcol1.multiselect("優先級", ["P1", "P2", "P3", "待查"],
                         default=["P1", "P2"])
only_review = fcol2.checkbox("只看需人工確認", value=False)
sup_pick = fcol3.multiselect(
    "供應商", sorted(actions["supplier_name"].dropna().unique().tolist()))

view = actions[actions["priority"].isin(pick)] if pick else actions
if only_review:
    view = view[view["needs_human_review"]]
if sup_pick:
    view = view[view["supplier_name"].isin(sup_pick)]

if view.empty:
    st.info("目前條件下沒有待處理案件。")
else:
    # 顯示用的表格才轉中文標籤；view／actions 本身的 commitment_strength
    # 仍是原始值（confirmed/estimated/...），下游的匯出與判斷邏輯要用的是它。
    display_df = view[["priority", "gap_days", "conservative_eta", "available_date", "po_no",
                       "material_id", "supplier_name", "committed_date", "new_eta",
                       "commitment_strength", "needs_human_review"]].copy()
    display_df["commitment_strength"] = display_df["commitment_strength"].map(
        lambda v: COMMITMENT_LABEL_ZH.get(v, v))
    st.dataframe(
        display_df.rename(columns={
            "priority": "優先級", "gap_days": "預估缺料天數", "conservative_eta": "保守到料日",
            "available_date": "可投產日", "po_no": "採購單號",
            "material_id": "料號", "supplier_name": "供應商",
            "committed_date": "原承諾日", "new_eta": "新交期",
            "commitment_strength": "承諾強度", "needs_human_review": "需人工確認"}),
        width="stretch", hide_index=True, height=280)

    st.divider()
    st.markdown("#### 逐案展開")
    for _, row in view.head(12).iterrows():
        icon = ui_state.PRIORITY_COLOR.get(row["priority"], "⚪")
        flag = " ⚠️ 需人工確認" if row["needs_human_review"] else ""
        header = (f"{icon} **{row['priority']}**　{row['po_no']}　"
                  f"{row['material_id']}　{row['supplier_name']}　"
                  f"（{ui_state.fmt_gap(row['gap_days'])}）{flag}")
        with st.expander(header):
            a, b = st.columns([3, 2])
            with a:
                st.markdown("**為什麼是這個優先級**")
                for r in (row["reasons"] or []):
                    st.markdown(f"- {r}")
                if row.get("note"):
                    st.info(row["note"])
                if row["needs_human_review"]:
                    st.warning(
                        "**此筆不會覆寫系統承諾日。**\n\n"
                        "原因：供應商未明確承諾、或解析信心不足。"
                        "交期資料錯誤會連動整條下游排程，"
                        "因此寫回 ERP 必須由人確認後執行。")
                if row["actions"]:
                    st.markdown("**建議動作**")
                    for act in row["actions"]:
                        st.markdown(f"- {act}")
            with b:
                st.markdown("**原始信件**")
                st.caption(f"{row['email_id']}｜{row['subject']}")
                st.text(str(row.get("notes", ""))[:400])
                if st.button("產生回信草稿", key=f"draft-{row['po_no']}"):
                    # 額度用完時改用規則式模板：草稿照樣產出，只是不再消耗 API 配額
                    draft_provider = get_provider() if ui_state.llm_budget_left() > 0 else None
                    text, src = draft_mod.generate(
                        row.to_dict(),
                        provider=draft_provider if draft_provider else NullProvider())
                    if "ms）" in src and "（0 ms）" not in src:
                        st.session_state["llm_live_calls"] = (
                            st.session_state.get("llm_live_calls", 0) + 1)
                    st.caption(f"來源：{src}")
                    st.text_area("草稿（寄出前請自行確認語氣）", text,
                                 height=280, key=f"ta-{row['po_no']}")

    st.download_button(
        "⬇️ 匯出行動清單 CSV",
        view.assign(
            reasons=view["reasons"].map(
                lambda v: "；".join(v) if isinstance(v, list) else ""),
            actions=view["actions"].map(
                lambda v: "；".join(v) if isinstance(v, list) else ""))
            .to_csv(index=False).encode("utf-8-sig"),
        file_name="行動清單.csv", mime="text/csv")
