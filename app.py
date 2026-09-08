# -*- coding: utf-8 -*-
"""
供應商交期回覆解析與影響評估工具 — Streamlit 介面。

介面設計的三個原則（給生管用，不是給工程師用）：

1. **先給答案，再給過程。**
   打開就是「今天要處理的前幾件事」，不是一堆圖表。
   同仁早上只有十分鐘，工具必須在十秒內講完重點。

2. **每個判斷都能追問「為什麼」。**
   每一筆都可以展開看到十條規則各拿幾分、扣分理由是什麼。
   說不出理由的排序，同仁用兩週就會棄用。

3. **不確定的地方要顯眼，不要藏。**
   承諾強度不是「已確認」的案件會被明確標示，且不覆寫系統承諾日。
   工具寧可讓人多看一眼，也不能讓人誤以為事情定了。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

import benefit  # noqa: E402
import draft as draft_mod  # noqa: E402
import pipeline  # noqa: E402
from llm.provider import get_provider  # noqa: E402

st.set_page_config(page_title="供應商交期回覆解析工具", page_icon="📦", layout="wide")

PRIORITY_COLOR = {"P1": "🔴", "P2": "🟠", "P3": "🟡", "待查": "⚪", "—": "⚫"}


@st.cache_data(show_spinner="解析信件中…")
def load_pipeline(use_llm: bool):
    """解析結果快取。權重調整不會觸發重跑，避免重複呼叫 LLM。"""
    return pipeline.run(use_llm=use_llm)


def _ensure_data() -> None:
    """
    首次啟動時自動產生合成資料。

    `data/` 被 .gitignore 排除（它是可重新生成的產物，不該進版控），
    因此雲端部署後第一次啟動會找不到資料。與其要求使用者先跑一次
    指令，不如讓工具自己補上 —— 導入的第一步不該卡在環境設定。
    """
    if (ROOT / "data" / "po_master.csv").exists():
        return
    import generate_data
    with st.spinner("首次啟動：正在產生合成資料…"):
        generate_data.main()


def main() -> None:
    _ensure_data()
    cfg = pipeline.load_config()

    # ---------------- 側邊欄 ----------------
    st.sidebar.title("⚙️ 設定")

    provider = get_provider()
    if provider.available:
        st.sidebar.success(f"LLM 已連線：{provider.name} / {provider.model}")
    else:
        st.sidebar.warning(
            "**未偵測到 LLM 金鑰 — 目前為僅規則層模式**\n\n"
            "工具仍可完整執行，但自然語言敘述型的信件（相對日期、"
            "模糊措辭）解析會失敗，這是預期行為。\n\n"
            "接上金鑰：複製 `.env.example` 為 `.env` 並填入金鑰。"
        )
    use_llm = st.sidebar.toggle("啟用 LLM 升級解析", value=provider.available,
                                disabled=not provider.available)

    # 使用者有權知道畫面上的數字是從哪裡來的。
    st.sidebar.caption(f"**資料來源**：{pipeline.get_data_source(cfg).describe()}")

    st.sidebar.divider()
    st.sidebar.subheader("① 影響評估權重")
    st.sidebar.caption(
        "**這是「相對份量」，不是分數。**\n\n"
        "十條規則各自算出 0~1 的得分，再依這裡的份量加權平均，"
        "換算成 0~100 的影響分數。所以你只要在意**相對大小** — "
        "把某條調成兩倍，代表它的話語權變兩倍。\n\n"
        "這十條規則來自供應商端與物料企劃的實務判斷。"
        "不同意哪一條的份量，直接調，清單會立刻重排 — "
        "工具要能被質疑，才會被使用。"
    )
    labels = {
        "buffer_days": "1. 緩衝天數（距下游需求日）",
        "delay_magnitude": "2. 延遲幅度（延幾天）",
        "single_source": "3. 單一來源（有無二源）",
        "downstream_scheduled": "4. 下游已排定",
        "material_criticality": "5. 料的關鍵性（瓶頸／長LT）",
        "commitment_strength": "6. 承諾強度（是否真的承諾）",
        "reschedule_count": "7. 累犯（已改期幾次）",
        "delay_share": "8. 延遲量佔需求比例",
        "notice_lead_time": "9. 通知時機（多晚才講）",
        "substitutability": "10. 可替代性（有無替代料）",
    }
    weights = {}
    for key, label in labels.items():
        weights[key] = st.sidebar.slider(label, 0, 40, int(cfg["impact_weights"][key]), 1)

    # 把「相對份量」換算成「實際佔比」顯示出來。
    # 沒有這個對照，使用者看到滑桿上的 25 會誤以為那是分數。
    total_w = sum(weights.values())
    if total_w > 0:
        share = " ｜ ".join(
            f"{labels[key].split('.')[0]}:{weights[key] / total_w * 100:.0f}%"
            for key in labels if weights[key] > 0)
        st.sidebar.caption(f"目前份量總和 **{total_w}**，換算後各條佔比：\n\n{share}")
    else:
        st.sidebar.error("所有權重都是 0，無法評分。請至少給一條規則份量。")

    st.sidebar.divider()
    st.sidebar.subheader("② 優先級門檻")
    st.sidebar.caption(
        "**這裡才是分數，範圍 0~100。**\n\n"
        "上面的權重決定每張單得幾分，這裡決定幾分以上要今天處理。"
        "門檻調低 → 清單變長、不會漏但會累；調高 → 清單變短、省力但可能漏。"
        "這個取捨由使用單位自己決定。"
    )
    p1 = st.sidebar.slider("P1 門檻：幾分以上今天要處理", 40, 95,
                           int(cfg["priority_thresholds"]["P1"]))
    p2 = st.sidebar.slider("P2 門檻：幾分以上本週要追", 10, 90,
                           int(cfg["priority_thresholds"]["P2"]))
    if p2 >= p1:
        st.sidebar.warning(
            f"P2 門檻（{p2}）不低於 P1 門檻（{p1}），這樣不會有任何 P2 案件。"
            "通常 P2 應該設得比 P1 低。")

    st.sidebar.divider()
    st.sidebar.subheader("③ 效益試算參數")
    k = st.sidebar.number_input("生管每日可仔細追的案件數 (K)", 5, 100,
                                int(cfg["benefit"]["daily_review_capacity"]))
    st.sidebar.caption("僅影響「效益量化」分頁的 Recall@K，不影響行動清單排序。")

    thresholds = {"P1": p1, "P2": max(p2, 0)}

    # ---------------- 資料 ----------------
    try:
        result = load_pipeline(use_llm)
    except FileNotFoundError as e:
        st.error(f"{e}")
        st.stop()

    actions = pipeline.rescore(result["all"], weights, thresholds)
    actions = actions[actions["priority"] != "—"].reset_index(drop=True)

    stats = dict(result["stats"])
    stats.update(
        actionable=len(actions),
        p1=int((actions["priority"] == "P1").sum()),
        p2=int((actions["priority"] == "P2").sum()),
        p3=int((actions["priority"] == "P3").sum()),
    )

    # ---------------- 標題與摘要 ----------------
    st.title("📦 供應商交期回覆解析與影響評估")
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

    tabs = st.tabs(["📋 今日行動清單", "📊 效益量化", "🔍 解析軌跡",
                    "🧪 對照實驗", "📨 原始信件"])

    # ==================== 分頁 1：行動清單 ====================
    with tabs[0]:
        st.subheader("今日行動清單")
        st.caption("依影響分數排序。展開任一筆可看到十條規則各拿幾分，以及回信草稿。")

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
            st.dataframe(
                view[["priority", "impact_score", "po_no", "material_id",
                      "supplier_name", "committed_date", "new_eta",
                      "commitment_strength", "needs_human_review"]]
                .rename(columns={
                    "priority": "優先級", "impact_score": "影響分數", "po_no": "採購單號",
                    "material_id": "料號", "supplier_name": "供應商",
                    "committed_date": "原承諾日", "new_eta": "新交期",
                    "commitment_strength": "承諾強度", "needs_human_review": "需人工確認"}),
                use_container_width=True, hide_index=True, height=280)

            st.divider()
            st.markdown("#### 逐案展開")
            for _, row in view.head(12).iterrows():
                icon = PRIORITY_COLOR.get(row["priority"], "⚪")
                flag = " ⚠️ 需人工確認" if row["needs_human_review"] else ""
                header = (f"{icon} **{row['priority']}**　{row['po_no']}　"
                          f"{row['material_id']}　{row['supplier_name']}　"
                          f"（影響分數 {row['impact_score']}）{flag}")
                with st.expander(header):
                    a, b = st.columns([3, 2])
                    with a:
                        st.markdown("**為什麼是這個優先級**")
                        for r in (row["top_reasons"] or []):
                            st.markdown(f"- {r}")
                        if row.get("note"):
                            st.info(row["note"])
                        if row["needs_human_review"]:
                            st.warning(
                                "**此筆不會覆寫系統承諾日。**\n\n"
                                "原因：供應商未明確承諾、或解析信心不足。"
                                "交期資料錯誤會連動整條下游排程，"
                                "因此寫回 ERP 必須由人確認後執行。")
                        st.markdown("**十條規則明細**")
                        detail = pd.DataFrame(row["rule_details"])
                        detail["rule"] = detail["rule"].map(labels).fillna(detail["rule"])
                        st.dataframe(
                            detail.rename(columns={
                                "rule": "規則", "score": "得分(0~1)", "weight": "權重",
                                "contribution": "貢獻", "explain": "理由"}),
                            use_container_width=True, hide_index=True)
                    with b:
                        st.markdown("**原始信件**")
                        st.caption(f"{row['email_id']}｜{row['subject']}")
                        st.text(str(row.get("notes", ""))[:400])
                        if st.button("產生回信草稿", key=f"draft-{row['po_no']}"):
                            text, src = draft_mod.generate(row.to_dict())
                            st.caption(f"來源：{src}")
                            st.text_area("草稿（寄出前請自行確認語氣）", text,
                                         height=280, key=f"ta-{row['po_no']}")

            st.download_button(
                "⬇️ 匯出行動清單 CSV",
                view.drop(columns=["rule_details", "top_reasons"], errors="ignore")
                    .to_csv(index=False).encode("utf-8-sig"),
                file_name="行動清單.csv", mime="text/csv")

    # ==================== 分頁 2：效益量化 ====================
    with tabs[1]:
        st.subheader("效益量化")
        st.warning(
            "**這裡不會出現「節省 30% 人力」這種數字。**\n\n"
            "以下提供的是效益公式與可調參數。壓縮率是程式直接算出來的事實；"
            "工時節省是**假設**，請挑你認同的那一列來看。"
            "引用任何數字時請連同假設一起引用。")

        rep = benefit.full_report(actions, stats, {**cfg, "benefit": {**cfg["benefit"],
                                                                     "daily_review_capacity": k}})
        st.markdown("#### 一、訊息壓縮（無假設，程式直接計算）")
        comp = rep["compression"]
        cc = st.columns(len(comp))
        for col, (kk, vv) in zip(cc, comp.items()):
            col.metric(kk, vv if vv is not None else "—")

        st.markdown(f"#### 二、排序品質：Recall@{k}")
        st.caption(
            f"若生管一天只能仔細追 {k} 件，各種排序方式各能抓到多少比例的"
            "「真的會來不及」案件？（會不會來不及＝新交期是否晚於下游需求日，"
            "由日期相減得到，不是模型輸出）")
        st.dataframe(rep["strategies"], use_container_width=True, hide_index=True)
        st.info(
            f"本批共 {rep['n_actions']} 件，其中 {rep['n_will_be_short']} 件實際會來不及。\n\n"
            "**必須說明的限制**：影響分數的規則 1（緩衝天數）使用了與 outcome "
            "相同的訊號，因此這個比較對本工具有利。它證明的是排序邏輯有效地把"
            "緩衝訊號傳遞到清單前段，**不能**證明工具可以預測未知結果。")

        st.markdown("#### 三、工時敏感度分析")
        st.dataframe(rep["sensitivity"], use_container_width=True, hide_index=True)

    # ==================== 分頁 3：解析軌跡 ====================
    with tabs[2]:
        st.subheader("解析軌跡（可稽核性）")
        st.caption(
            "每封信走過哪幾層、為什麼升級、LLM 是否成功、花了多久。"
            "沒有軌跡的 AI 工具無法被稽核，出錯時也無從追查 — 同仁不會信任它。")
        tr = result["traces"]
        m1, m2, m3 = st.columns(3)
        m1.metric("規則層即可處理", int((~tr["escalated"]).sum()))
        m2.metric("升級至 LLM", int(tr["escalated"].sum()))
        m3.metric("LLM 呼叫成功", int(tr["llm_ok"].sum()))
        if not provider.available:
            st.warning(
                "目前無 LLM 金鑰，因此所有被升級的信件都退回規則層結果，"
                "並在行動清單中標示為需人工確認。這是刻意設計的降級行為："
                "工具寧可承認自己看不懂，也不能猜一個日期給生管。")
        src = pipeline.get_data_source(cfg)
        if hasattr(src, "table_counts"):
            with st.expander("📚 資料來源：模擬 ERP 資料表結構（點開看）"):
                st.markdown(
                    "工具讀的不是一張扁平表，而是一組結構貼近 SAP MM 的關聯式資料表。"
                    "**承諾日、改期次數、有無二源這些欄位在 ERP 裡都不是現成的一欄**，"
                    "必須用 SQL JOIN 推導出來 —— 這才是接 ERP 時真正要面對的工作。")
                counts = src.table_counts()
                sap = {
                    "vendor_master": "供應商主檔 ≈ LFA1",
                    "material_master": "物料主檔 ≈ MARA/MARC",
                    "material_alternate": "替代料關係",
                    "source_list": "來源清單 ≈ EORD（有無二源的來源）",
                    "purchase_req": "請購單 ≈ EBAN（需求日的來源）",
                    "po_header": "採購單頭 ≈ EKKO",
                    "po_item": "採購單項次 ≈ EKPO",
                    "po_schedule": "交貨排程行 ≈ EKET（承諾日的來源）",
                    "po_change_log": "變更文件 ≈ CDHDR/CDPOS（改期次數的來源）",
                    "goods_receipt": "收貨紀錄 ≈ MKPF/MSEG（未來校準權重的 outcome）",
                }
                st.dataframe(
                    pd.DataFrame([{"資料表": t, "筆數": c, "對應（SAP 為例）": sap.get(t, "")}
                                  for t, c in counts.items()]),
                    use_container_width=True, hide_index=True)
                st.caption(
                    "`goods_receipt` 刻意為空：它代表工具上線後才會累積的真實結果，"
                    "也是未來用資料校準規則權重的唯一來源。"
                    "表名與結構是參考公開資料整理的近似版本，非任何公司的真實 schema。")

        st.dataframe(
            tr.rename(columns={
                "email_id": "信件", "rule_confidence": "規則層信心",
                "escalated": "是否升級", "llm_ok": "LLM成功",
                "llm_latency_ms": "LLM耗時(ms)", "llm_error": "錯誤",
                "final_layer": "最終採用", "reason": "升級判斷"}),
            use_container_width=True, hide_index=True, height=420)

    # ==================== 分頁 4：對照實驗 ====================
    with tabs[3]:
        st.subheader("對照實驗：規則層 vs LLM 層")
        st.caption("回答一個必須被回答的問題：這裡到底需不需要 LLM？")
        exp = ROOT / "output" / "實驗結果.md"
        if exp.exists():
            st.markdown(exp.read_text(encoding="utf-8"))
        else:
            st.info("尚未執行實驗。請於終端機執行： `py src/evaluate.py`")

    # ==================== 分頁 5：原始信件 ====================
    with tabs[4]:
        st.subheader("原始信件")
        st.caption("含 10 封手寫的刁鑽案例（轉寄串、一信多單、模糊措辭、分批交貨…）。")
        emails = pipeline.load_emails()
        opts = {f"{e['email_id']}｜{e['subject'][:50]}": e for e in emails}
        chosen = st.selectbox("選擇信件", list(opts.keys()))
        e = opts[chosen]
        st.text(f"From: {e['supplier_id']}\nDate: {e['received_at']}\n"
                f"Subject: {e['subject']}\nTags: {', '.join(e.get('tags', []))}\n")
        st.code(e["body"], language=None)


if __name__ == "__main__":
    main()
