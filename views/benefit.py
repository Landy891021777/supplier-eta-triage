# -*- coding: utf-8 -*-
"""
📊 效益估算 —— 效益公式與可調參數。

不宣稱「節省 30% 人力」這種數字：壓縮率是程式直接算出來的事實，
工時節省是假設，K 參數（每日可仔細追的案件數）只影響本頁。
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
import benefit  # noqa: E402

ctx = ui_state.context()
actions, stats, cfg = ctx["actions"], ctx["stats"], ctx["cfg"]

st.subheader("效益估算")

# K 參數原本在側邊欄（跨頁共用的位置容易讓人以為它影響全部頁面），
# 移到本頁頂端：它只影響下面的 Recall@K，不影響行動清單排序。
k = st.number_input("物料企劃每日可仔細追的案件數 (K)", 5, 100,
                    int(cfg["benefit"]["daily_review_capacity"]))
st.caption("僅影響本頁的 Recall@K，不影響行動清單排序。")

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
    f"若物料企劃一天只能仔細追 {k} 件，各種排序方式各能抓到多少比例的"
    "「真的會來不及」案件？（會不會來不及＝新交期是否晚於下游需求日，"
    "由日期相減得到，不是模型輸出）")
st.dataframe(rep["strategies"], width="stretch", hide_index=True)
st.info(
    f"本批共 {rep['n_actions']} 件，其中 {rep['n_will_be_short']} 件實際會來不及。\n\n"
    "**必須說明的限制**：排序鍵「預估缺料天數」與 outcome 用了同樣的緩衝天數"
    "訊號，因此這個比較對本工具有利。它證明的是排序邏輯有效地把"
    "緩衝訊號傳遞到清單前段，**不能**證明工具可以預測未知結果。"
    "真正的驗證見「方法驗證」分頁與 `output/回測結果.md`。")

st.markdown("#### 三、工時敏感度分析")
st.dataframe(rep["sensitivity"], width="stretch", hide_index=True)
