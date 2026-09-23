# -*- coding: utf-8 -*-
"""
📋 今日行動清單 —— 物料企劃打開工具的第一眼。

先給答案、再給過程：首頁五個指標＋依預估缺料天數排序的清單，
每一筆都能展開看到為什麼、建議動作、回信草稿，以及（需人工確認的單）
登錄向供應商確認到的交期。
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

import ui_state  # noqa: E402
import draft as draft_mod  # noqa: E402
import exports  # noqa: E402
import planner_settings  # noqa: E402
import triage  # noqa: E402
from domain import COMMITMENT_LABEL_ZH  # noqa: E402
from llm.provider import NullProvider, get_provider  # noqa: E402

CONFIRM_LOG_RENAME = {"confirmed_at": "確認時間", "confirmed_date": "確認後交期",
                      "note": "備註", "confirmed_by": "確認人"}

ctx = ui_state.context()
result, actions, stats = ctx["result"], ctx["actions"], ctx["stats"]

# actions 本身沒有 base_uom（那是料號主檔的欄位，不是分級要用的欄位，
# run()／retriage() 沒理由帶著它）；CSV 與 Excel 兩個匯出都要顯示
# 「數量＋單位」，在這裡併入一次，兩邊共用，不動 actions 本身。
# 用 .map() 不用 .merge()：merge 會把索引重設成 0..n-1，
# 底下 CSV 匯出要用 view.index 對回 actions_for_export，索引對不上
# 會整批拿錯列；.map() 是逐元素查表，索引原封不動留著。
_base_uom_by_material = ui_state.materials_df().set_index("material_id")["base_uom"]
actions_for_export = actions.assign(
    base_uom=actions["material_id"].map(_base_uom_by_material))

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
st.caption("💡 勾選下方「只看需人工確認」可逐張登錄確認交期。")

# ==================== 今日已確認 ====================
# 企劃登錄過的確認，不管那張單現在還在不在清單裡（例如確認日期剛好等於
# 原承諾日，會被判為 no_change 而從清單消失——見 pipeline.retriage 的
# 說明，這是設計上的預期行為，不是 bug），都要有地方查得到「我今天
# 確認過哪些單」，不能登錄完就石沉大海。
confirmed_map = planner_settings.load_confirmations()
with st.expander(f"📌 今日已確認（{len(confirmed_map)}）"):
    if not confirmed_map:
        st.caption("目前沒有登錄過的確認交期。")
    else:
        current_email = ui_state.current_email_by_po(result)
        in_list = set(actions["po_no"])
        conf_rows = [{
            "採購單號": po,
            "確認後交期": c.get("confirmed_date", ""),
            "確認人": c.get("confirmed_by", ""),
            "確認時間": c.get("confirmed_at", ""),
            "備註": c.get("note") or "",
            # 生效中／已被新信取代：跟這張單目前（去重後最新一封）的
            # email_id 比對，對不上代表供應商後來又寄過新信，這筆確認
            # 已經作廢（見 pipeline.retriage 的說明）。
            "狀態": ("生效中" if current_email.get(po) == c.get("email_id")
                    else "已被新信取代"),
            "仍在行動清單中": "是" if po in in_list else "否",
        } for po, c in confirmed_map.items()]
        st.dataframe(
            pd.DataFrame(conf_rows).sort_values("確認時間", ascending=False),
            width="stretch", hide_index=True)

st.subheader("今日行動清單")
st.caption("依預估缺料天數排序（缺越多天越前面）。展開任一筆可看到為什麼、建議動作，以及回信草稿。")

fcol1, fcol2, fcol3 = st.columns([1, 1, 2])
pick = fcol1.multiselect("優先級", ["P1", "P2", "P3", "待查"],
                         default=["P1", "P2"], key="action_priority_pick")
only_review = fcol2.checkbox("只看需人工確認", value=False, key="action_only_review")
sup_pick = fcol3.multiselect(
    "供應商", sorted(actions["supplier_name"].dropna().unique().tolist()),
    key="action_supplier_pick")

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
    display_df["needs_human_review"] = display_df["needs_human_review"].map({True: "是", False: "否"})
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
    # 只看需人工確認時，這份清單就是企劃今天真正要逐張處理的工作清單，
    # 不該被硬性的 12 筆上限擋住看不到、也登錄不到後面幾張的確認表單
    # （I4）；平常瀏覽全部案件才限制 12 筆，避免一次展開太多拖慢畫面。
    rows_to_show = view if only_review else view.head(12)
    # 一次撈全部確認紀錄，展開時用 po_no 篩：迴圈裡各自查一次資料庫
    # 沒必要，資料量也不大，不差這一次查詢。
    all_confirm_log = planner_settings.confirmation_log()
    for _, row in rows_to_show.iterrows():
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
                    if not row.get("matched", True):
                        # 對不到 PO 主檔的列沒有原承諾日可比對，登錄確認
                        # 也不會被 retriage 套用（見 pipeline.retriage 的
                        # 說明）——與其讓企劃填一個永遠不會生效的表單，
                        # 不如直接說清楚該先做什麼。
                        st.warning("信中的採購單號對不到系統，請先確認單號。")
                    else:
                        st.warning(
                            "**此筆不會覆寫系統承諾日。**\n\n"
                            "原因：供應商未明確承諾、或解析信心不足。"
                            "交期資料錯誤會連動整條下游排程，"
                            "因此寫回 ERP 必須由人確認後執行。")

                        po_no, email_id = row["po_no"], row["email_id"]
                        # 預設值：信中新交期能解析就用它，不能就退回原承諾日，
                        # 兩個都解析不出來（理論上不會發生，防禦用）才用今天。
                        default_date = (triage._d(row.get("new_eta"))
                                        or triage._d(row.get("committed_date"))
                                        or date.today())
                        with st.form(key=f"confirm-{po_no}-{email_id}"):
                            st.caption(
                                "向供應商要到確切日期後在這裡登錄，清單會改用這個日期"
                                "重算。**這裡只記錄在工具內，不會寫回 ERP**；"
                                "請依公司流程更新交貨排程行。")
                            c_date = st.date_input("確認後的交期", value=default_date,
                                                   key=f"confirm_date_{po_no}_{email_id}")
                            c_note = st.text_input("備註（選填）",
                                                   key=f"confirm_note_{po_no}_{email_id}")
                            c_user = st.text_input("姓名（必填）",
                                                   key=f"confirm_user_{po_no}_{email_id}")
                            if st.form_submit_button(
                                    "登錄確認", key=f"confirm_submit_{po_no}_{email_id}"):
                                ok, msg = ui_state.submit_confirmation(
                                    po_no, email_id, c_date.isoformat(), c_note, c_user)
                                if ok:
                                    # st.success 接著 st.rerun() 會來不及顯示就被
                                    # 蓋掉；st.toast 設計上會跨這一次 rerun 留著，
                                    # 企劃才看得到「有登錄成功」。
                                    st.toast("已登錄確認，清單將改用這個日期重算。",
                                            icon="✅")
                                    st.rerun()
                                else:
                                    st.error(msg)

                # 已確認的單，理由第一條已經寫明誰、哪天確認（見 pipeline.retriage）；
                # 這裡另外列出完整確認歷史（新到舊），即使這張單現在已經不再
                # 需人工確認，只要曾經確認過就要看得到「改口過幾次」。
                po_log = [c for c in all_confirm_log if c["po_no"] == row["po_no"]]
                if po_log:
                    st.markdown("**這張單的確認紀錄**")
                    log_df = pd.DataFrame(
                        sorted(po_log, key=lambda c: c["confirm_id"], reverse=True))
                    # 狀態：這筆確認的 email_id 是不是這張單目前最新一封信——
                    # 不是的話代表供應商後來又寄過新信，這筆確認已經作廢
                    # （跟「今日已確認」那個 expander 用同一套判斷）。
                    log_df["狀態"] = log_df["email_id"].map(
                        lambda e: "生效中" if e == row["email_id"] else "已被新信取代")
                    st.dataframe(
                        log_df[["confirmed_at", "confirmed_date", "note",
                               "confirmed_by", "狀態"]]
                        .rename(columns=CONFIRM_LOG_RENAME),
                        width="stretch", hide_index=True)

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

    st.divider()
    dl1, dl2 = st.columns(2)
    with dl1:
        st.download_button(
            "⬇️ 匯出行動清單 CSV",
            exports.to_planner_rows(
                actions_for_export.loc[view.index]
            ).to_csv(index=False).encode("utf-8-sig"),
            file_name="行動清單.csv", mime="text/csv")
    with dl2:
        # 內容取自篩選前的完整清單（P1、P2、待查）——給明天早上追料、或帶去
        # 缺料檢討會用，不該因為使用者當下的篩選條件漏掉某些單。
        as_of = str(result["as_of"])
        st.download_button(
            "⬇️ 匯出明日追料清單（Excel）",
            exports.followup_workbook(actions_for_export, as_of),
            file_name=f"追料清單_{as_of}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        st.caption("給明天早上追料、或帶去缺料檢討會用。")
