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

import os  # noqa: E402

import benefit  # noqa: E402
import draft as draft_mod  # noqa: E402
import pipeline  # noqa: E402
from llm.provider import get_provider  # noqa: E402

st.set_page_config(page_title="Supply Chain AI Tool: Delivery Risk Prioritization",
                   page_icon="📦", layout="wide")

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

    實際邏輯在 src/bootstrap.py。踩坑紀錄：原本用「資料庫檔案存在」當作
    「準備好了」，雲端首次載入時兩個工作階段重疊，第二個會撞上建到一半的
    資料庫而整個打不開；也曾因為只建了資料庫沒補歷史，讓三個分頁整片空白。
    """
    import bootstrap
    bootstrap.ensure_data(ROOT, step=st.spinner)


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

    # 分頁順序依使用情境排列：生管每天用的在前，驗證與稽核用的在後。
    # 以具名變數取代 tabs[0]～tabs[6] 索引：插入或調整分頁時不會整批錯位。
    (t_actions, t_search, t_erp, t_calib,
     t_experiment, t_benefit, t_trace, t_emails) = st.tabs([
        "📋 今日行動清單", "🔎 物料智能檢索", "📄 ERP 單據", "⚖️ 供應商歷史與權重",
        "🧪 評估實驗", "📊 效益量化", "🔍 解析軌跡", "📨 原始信件"])

    # ==================== 分頁 1：行動清單 ====================
    with t_actions:
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
                width="stretch", hide_index=True, height=280)

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
                            width="stretch", hide_index=True)
                    with b:
                        # 供應商歷史表現：把 ERP 收貨紀錄變成當下用得到的判斷依據。
                        # 生管看到「他說 10/29」時，真正想知道的是
                        # 「這家供應商說的話能信幾分、實際大概哪天到」。
                        hist = _supplier_history(row.get("supplier_id"),
                                                 row.get("new_eta") or row.get("committed_date"))
                        if hist:
                            st.markdown("**供應商歷史表現（近 12 個月）**")
                            if hist.get("available"):
                                st.metric("保守到料日（歷史 P80）", hist["conservative"],
                                          delta=f"較承諾日 +{hist['p80_delay']} 天",
                                          delta_color="inverse")
                                st.caption(hist["note"] + "。此為歷史統計，非預測模型。")
                            else:
                                st.caption(hist.get("reason", ""))
                            st.divider()
                        st.markdown("**原始信件**")
                        st.caption(f"{row['email_id']}｜{row['subject']}")
                        st.text(str(row.get("notes", ""))[:400])
                        if st.button("產生回信草稿", key=f"draft-{row['po_no']}"):
                            # 額度用完時改用規則式模板：草稿照樣產出，只是不再消耗 API 配額
                            draft_provider = get_provider() if _llm_budget_left() > 0 else None
                            text, src = draft_mod.generate(
                                row.to_dict(),
                                provider=draft_provider if draft_provider else _NoLLM())
                            if "ms）" in src and "（0 ms）" not in src:
                                st.session_state["llm_live_calls"] = (
                                    st.session_state.get("llm_live_calls", 0) + 1)
                            st.caption(f"來源：{src}")
                            st.text_area("草稿（寄出前請自行確認語氣）", text,
                                         height=280, key=f"ta-{row['po_no']}")

            st.download_button(
                "⬇️ 匯出行動清單 CSV",
                view.drop(columns=["rule_details", "top_reasons"], errors="ignore")
                    .to_csv(index=False).encode("utf-8-sig"),
                file_name="行動清單.csv", mime="text/csv")

    # ==================== 分頁 2：效益量化 ====================
    with t_benefit:
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
        st.dataframe(rep["strategies"], width="stretch", hide_index=True)
        st.info(
            f"本批共 {rep['n_actions']} 件，其中 {rep['n_will_be_short']} 件實際會來不及。\n\n"
            "**必須說明的限制**：影響分數的規則 1（緩衝天數）使用了與 outcome "
            "相同的訊號，因此這個比較對本工具有利。它證明的是排序邏輯有效地把"
            "緩衝訊號傳遞到清單前段，**不能**證明工具可以預測未知結果。")

        st.markdown("#### 三、工時敏感度分析")
        st.dataframe(rep["sensitivity"], width="stretch", hide_index=True)

    # ==================== 分頁 3：解析軌跡 ====================
    with t_trace:
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
                    width="stretch", hide_index=True)
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
            width="stretch", hide_index=True, height=420)

    # ==================== 評估實驗 ====================
    with t_experiment:
        st.subheader("評估實驗")
        st.caption("每一個設計決定都要有證據，而且評估必須容許推翻原本的假設。")
        exp_parse, exp_rag = st.tabs(["解析：規則層 vs LLM 層", "檢索：六種配置比較"])
        for container, fname, cmd in (
            (exp_parse, "實驗結果.md", "py src/evaluate.py"),
            (exp_rag, "RAG檢索評估.md", "py src/rag/evaluate_rag.py"),
        ):
            with container:
                path = ROOT / "output" / fname
                if path.exists():
                    st.markdown(path.read_text(encoding="utf-8"))
                else:
                    st.info(f"尚未執行評估。請於終端機執行： `{cmd}`")

    # ==================== 分頁 5：原始信件 ====================
    with t_emails:
        st.subheader("原始信件")
        st.caption("含 10 封手寫的刁鑽案例（轉寄串、一信多單、模糊措辭、分批交貨…）。")
        emails = pipeline.load_emails()
        opts = {f"{e['email_id']}｜{e['subject'][:50]}": e for e in emails}
        chosen = st.selectbox("選擇信件", list(opts.keys()))
        e = opts[chosen]
        st.text(f"From: {e['supplier_id']}\nDate: {e['received_at']}\n"
                f"Subject: {e['subject']}\nTags: {', '.join(e.get('tags', []))}\n")
        st.code(e["body"], language=None)


    # ==================== 分頁 6：ERP 單據 ====================
    with t_erp:
        st.subheader("ERP 單據軌跡")
        st.caption(
            "一張採購單在 ERP 裡不是一列資料，是散落在六張表裡的一串單據。"
            "這一頁把它們串起來 — 也是最能說明「為什麼整合 ERP 不是撈一張表就好」的一頁。")
        src6 = pipeline.get_data_source(cfg)
        if not hasattr(src6, "document_trail"):
            st.info("目前的資料來源是 CSV，沒有單據結構。"
                    "請將 config.yaml 的 data_source 改為 sqlite。")
        else:
            c1, c2 = st.columns([1, 2])
            kind = c1.radio("單據狀態", ["在途（未收貨）", "已結案（有收貨）"],
                            help="已結案的單有實際到料日，是權重校準的資料來源。")
            pool = (src6.open_po_numbers() if kind.startswith("在途")
                    else src6.closed_po_numbers())
            if not pool:
                st.info("這個狀態下沒有單據。")
            else:
                default = ("PO-2026-04205" if "PO-2026-04205" in pool else pool[0])
                po_sel = c2.selectbox("選擇採購單", pool,
                                      index=pool.index(default))
                trail = src6.document_trail(po_sel)
                for label, tdf in trail.items():
                    st.markdown(f"**{label}**")
                    if tdf.empty:
                        st.caption("（無資料）")
                    else:
                        st.dataframe(tdf, width="stretch", hide_index=True)
                st.caption(
                    "注意第 ④ 與第 ⑤ 張表：**承諾日不在採購單頭，改期次數也沒有現成欄位**。"
                    "工具必須自己 JOIN 與 COUNT — 這就是接 ERP 真正的工作量所在。")

    # ==================== 分頁 7：權重校準 ====================
    with t_calib:
        st.subheader("權重校準：資料同不同意我訂的權重")
        st.caption(
            "評分卡的權重是依實務直覺訂的假設。一旦累積了歷史結果"
            "（實際到料日 vs 下游需求日），就可以反過來檢驗它。")
        st.warning(
            "⚠️ **本頁使用模擬歷史資料，係數僅供展示流程，不可用於決策。**\n\n"
            "歷史結果由 `src/generate_history.py` 的因果模型產生，非真實資料。"
            "在真實環境，這裡的輸入應該是 ERP 的收貨紀錄。")
        try:
            st.markdown("#### 一、歷史單據直接給生管的判斷依據")
            st.caption(
                "在讓資料去校準權重之前，歷史單據更直接的用途是回答生管當下的問題："
                "這家供應商說的話能信幾分？以下兩張表就是行動清單上「保守到料日」的來源。")
            pcol, rcol = st.columns([3, 2])
            with pcol:
                st.markdown("**各供應商歷史表現**")
                st.dataframe(_supplier_performance(), width="stretch",
                             hide_index=True, height=260)
            with rcol:
                st.markdown("**改期次數 vs 最終是否延遲**")
                st.dataframe(_reschedule_reliability(), width="stretch",
                             hide_index=True)
                st.caption(
                    "這張表在**驗證規則 7（累犯）**：改期越多次的單，最終仍延遲的"
                    "比例越高。若資料顯示無關，這條規則就該被拿掉 —— "
                    "工具必須容許自己的規則被自己的資料推翻。")
            st.divider()
            st.markdown("#### 二、用歷史結果回頭校準十條權重")

            res = _run_calibration()
            m1, m2, m3 = st.columns(3)
            m1.metric("已結案樣本", res["n_total"])
            m2.metric("實際造成缺料", f"{res['n_shortage']}（{res['shortage_rate']:.1%}）")
            m3.metric("人訂 vs 學習 AUC",
                      f"{res['auc_hand']:.3f} → {res['auc_model']:.3f}")
            t = res["table"].copy()
            t.index.name = "規則"
            t = t.reset_index()
            t["規則"] = t["規則"].map(labels).fillna(t["規則"])
            st.dataframe(t, width="stretch", hide_index=True)
            st.info(
                "**「方向相反」不等於規則錯 — 這是本次校準最重要的發現。**\n\n"
                "校準的 outcome 是「會不會缺料」，衡量的是**發生機率**。"
                "但「下游已排定」「延遲量佔比」「可替代性」預測的根本不是機率，"
                "而是**缺了之後有多痛**（代價）。\n\n"
                "下游有沒有排定產能，不會改變供應商延不延；"
                "但它決定延了之後要動幾條排程、要不要跟客戶道歉。\n\n"
                "**評分卡本來就在混合兩件事：發生機率 × 影響代價。**"
                "歷史資料只能校準機率那一半 — 除非公司有在記錄每次缺料的實際損失。")
            st.markdown(
                "**沒有被校準的規則：`commitment_strength`（承諾強度）**\n\n"
                "訊號來自信件語氣，而 ERP 只存結果、不存語氣。"
                "歷史資料裡根本沒有這個欄位，必須等工具上線後自行累積。"
                "這一條校準不了，恰好說明了這個工具存在的理由："
                "**它產生的是 ERP 結構上不會有的資料。**")
        except (FileNotFoundError, RuntimeError) as e:
            st.info(f"{e}\n\n請先執行： `py src/generate_history.py`")

    # ==================== 物料智能檢索（RAG） ====================
    with t_search:
        _render_search_tab(str(result["as_of"]))


# ---------------------------------------------------------------------------
# 物料智能檢索
# ---------------------------------------------------------------------------
from rag.answer import EXAMPLE_QUESTIONS as RAG_EXAMPLES  # noqa: E402
from llm.provider import NullProvider as _NoLLM  # noqa: E402
RETRIEVAL_LABEL = {"pinned": "識別碼精確比對", "semantic": "語意相近",
                   "lexical": "字詞相符", "hybrid": "綜合排序"}
# 公開展示時的保護：每位訪客在一次工作階段內最多觸發的「未快取」LLM 呼叫次數。
# 超過後改為直接列出檢索到的原始資料，工具照樣可用，但不再消耗 API 配額。
LLM_CALLS_PER_SESSION = int(os.getenv("LLM_CALLS_PER_SESSION", "20"))


@st.cache_resource(show_spinner="建立物料檢索索引…")
def _load_retriever():
    from rag.knowledge import build_cards
    from rag.retriever import HybridRetriever
    retriever = HybridRetriever(build_cards(), provider=get_provider())
    retriever.build_semantic_index()
    return retriever


def _llm_budget_left() -> int:
    return LLM_CALLS_PER_SESSION - st.session_state.get("llm_live_calls", 0)


def _render_search_tab(reference_date: str) -> None:
    from rag.answer import answer as rag_answer

    st.subheader("物料智能檢索")
    st.caption(
        "用平常講話的方式查詢採購單、料號、供應商表現與工具使用說明。"
        "回答只根據檢索到的資料，每一個事實都標註出處；資料裡沒有的，會直接說沒有。")

    try:
        retriever = _load_retriever()
    except (FileNotFoundError, RuntimeError, ValueError) as e:
        st.error(f"無法建立檢索索引：{e}")
        return

    mode_label = ("語意檢索 ＋ 識別碼精確比對" if retriever.semantic_ready
                  else "詞彙檢索 ＋ 識別碼精確比對（未設定 embedding 金鑰時的備援模式）")
    st.caption(f"檢索模式：**{mode_label}**｜知識庫 **{len(retriever.cards)}** 張卡片"
               "（採購單、料號、供應商、摘要與工具文件）")

    cols = st.columns(3)
    for i, example in enumerate(RAG_EXAMPLES):
        if cols[i % 3].button(example, key=f"rag-ex-{i}", width="stretch"):
            st.session_state["rag_question"] = example

    question = st.text_input("輸入問題", key="rag_question",
                             placeholder="例：SUB-FCCSP-1088 有沒有第二家可以買？")
    if not question:
        return

    budget = _llm_budget_left()
    provider = get_provider() if budget > 0 else None
    with st.spinner("檢索並整理回答中…"):
        res = rag_answer(question, retriever, provider, reference_date=reference_date)

    if res.get("llm"):
        # 只有真的打到 API 才扣額度；命中快取的回答不算
        if res.get("latency_ms", 0) > 0:
            st.session_state["llm_live_calls"] = st.session_state.get("llm_live_calls", 0) + 1
        st.markdown(res["answer"])
        cites = res.get("citations") or {}
        if cites.get("invalid"):
            st.warning(
                f"模型引用了未提供給它的資料 {cites['invalid']}，相關敘述可能是推測，"
                "請以下方原始資料為準。")
        elif cites.get("uncited") and "沒有這項資訊" not in res["answer"]:
            st.warning("回答未標註任何出處，請以下方原始資料為準。")
    else:
        if budget <= 0:
            st.info("本次工作階段的 AI 回答額度已用完，以下直接列出檢索到的原始資料。")
        elif res.get("note"):
            st.info(res["note"])

    if res.get("hits"):
        st.markdown("**參考資料**")
        for hit in res["hits"]:
            label = RETRIEVAL_LABEL.get(hit.source, hit.source)
            with st.expander(f"[{hit.card.card_id}] {hit.card.title}　·　{label}",
                             expanded=not res.get("llm")):
                st.text(hit.card.text)


@st.cache_data(show_spinner="校準中…")
def _run_calibration():
    import calibrate as calib
    return calib.calibrate()


@st.cache_data(show_spinner=False)
def _supplier_performance():
    import supplier_stats
    return supplier_stats.supplier_performance()


@st.cache_data(show_spinner=False)
def _reschedule_reliability():
    import supplier_stats
    return supplier_stats.reschedule_reliability()


def _supplier_history(supplier_id, promised):
    """查供應商歷史表現；沒有歷史資料時安靜略過，不讓畫面壞掉。"""
    if not supplier_id or not promised:
        return None
    try:
        import supplier_stats
        return supplier_stats.conservative_eta(
            supplier_id, promised, _supplier_performance())
    except (FileNotFoundError, RuntimeError):
        return None


if __name__ == "__main__":
    main()
