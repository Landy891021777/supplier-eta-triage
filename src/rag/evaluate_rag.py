# -*- coding: utf-8 -*-
"""
RAG 檢索評估：每一層設計到底有沒有用？

比較六種配置，逐層加上或拿掉設計：

    ① 純語意檢索              多數 RAG 教學的預設做法（只用 embedding）
    ② 純詞彙檢索              TF-IDF 字元 n-gram，不需任何 API
    ③ 混合檢索                ① ＋ ② 以 RRF 合併
    ④ 混合 ＋ ID 釘選          原本的設計假設
    ⑤ 語意 ＋ ID 釘選          評估後改採的配置
    ⑥ 詞彙 ＋ ID 釘選          沒有 API 金鑰時的備援

===========================  這個評估推翻了原本的設計  ===========================
原本的假設是「混合檢索最好」——業界文章多半這樣建議。
第一次跑出來，純語意檢索（Hit@3 0.917）反而贏過混合檢索（0.833）。

原因：字元 n-gram 的詞彙檢索在中文換句話說的題目上全數失敗，
而 RRF 讓兩路等權合併，弱的一路把強的一路的正確答案往後推
（「哪家供應商最常延遲交貨」語意檢索排第 1，混合後掉到第 8）。

ID 釘選則確實有用：它解決了純語意檢索唯一答錯的識別碼題
（問採購單的供應商，答案在另一張卡上）。

因此改採「語意 ＋ ID 釘選」，詞彙檢索退居無金鑰時的備援。
這個調整有過度擬合 24 題的風險，但理由是結構性的（中文詞彙比對不懂換句話說、
精確識別碼不該交給向量比對），而不是為了讓分數好看去調權重。
==================================================================================

指標：
    Hit@1   第一名就是對的卡片
    Hit@3   前三名內有對的卡片（回答生成時實際會餵給模型的量級）
    MRR     第一張對的卡片排第幾名的倒數平均，綜合衡量排序品質

**只評檢索，不評生成。** 檢索沒撈到對的卡，後面模型寫得再好也是錯的；
而生成品質需要人工判讀，24 題的規模做不出有意義的自動評分。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from llm.provider import get_provider  # noqa: E402
from rag.eval_questions import QUESTIONS, is_relevant  # noqa: E402
from rag.knowledge import build_cards  # noqa: E402
from rag.retriever import HybridRetriever  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent.parent
OUT = ROOT / "output"

CONFIGS = [
    ("① 純語意檢索", "semantic", False),
    ("② 純詞彙檢索", "lexical", False),
    ("③ 混合檢索", "hybrid", False),
    ("④ 混合＋ID 釘選", "hybrid", True),
    ("⑤ 語意＋ID 釘選（本工具）", "semantic", True),
    ("⑥ 詞彙＋ID 釘選（無金鑰備援）", "lexical", True),
]


def first_relevant_rank(hits, accepted) -> int | None:
    for rank, h in enumerate(hits, start=1):
        if is_relevant(h.card, accepted):
            return rank
    return None


def run(k: int = 10) -> dict:
    cards = build_cards()
    provider = get_provider()
    retriever = HybridRetriever(cards, provider=provider)
    semantic_on = retriever.build_semantic_index()

    rows = []
    for label, mode, pin in CONFIGS:
        if mode in ("semantic", "hybrid") and not semantic_on:
            continue
        for qtype, question, accepted in QUESTIONS:
            rank = first_relevant_rank(
                retriever.search(question, k=k, mode=mode, pin=pin), accepted)
            rows.append({
                "配置": label, "題型": qtype, "問題": question,
                "名次": rank,
                "Hit@1": int(rank == 1), "Hit@3": int(rank is not None and rank <= 3),
                "RR": (1.0 / rank) if rank else 0.0,
            })

    df = pd.DataFrame(rows)
    order = [c[0] for c in CONFIGS if c[0] in set(df["配置"])]
    overall = (df.groupby("配置")[["Hit@1", "Hit@3", "RR"]].mean()
               .rename(columns={"RR": "MRR"}).reindex(order).round(3))
    by_type = (df.pivot_table(index="題型", columns="配置", values="Hit@3", aggfunc="mean")
               .reindex(columns=order).round(2))
    return {"detail": df, "overall": overall, "by_type": by_type,
            "semantic_on": semantic_on, "n_cards": len(cards),
            "n_questions": len(QUESTIONS), "error": retriever.semantic_error}


def to_markdown(res: dict) -> str:
    lines = [
        "# 物料智能檢索（RAG）評估", "",
        f"知識庫：**{res['n_cards']}** 張卡片｜評估題數：**{res['n_questions']}** 題", "",
        "> ⚠️ 題目由作者自行撰寫、題數僅 24 題，知識庫內容為合成資料。",
        "> 以下結果**只適合比較不同檢索設計的相對強弱**，不能當作真實環境的準確率引用。", "",
    ]
    if not res["semantic_on"]:
        lines += [f"> 本次未啟用語意檢索（{res['error']}），僅列出詞彙檢索結果。", ""]

    lines += ["## 整體表現", "", res["overall"].reset_index().to_markdown(index=False), "",
              "## 各題型 Hit@3", "", res["by_type"].reset_index().to_markdown(index=False), "",
              "## 各配置沒有答對的題目（前三名內未撈到正確卡片）", ""]
    miss = res["detail"][res["detail"]["Hit@3"] == 0]
    for label in res["overall"].index:
        sub = miss[miss["配置"] == label]
        lines.append(f"**{label}**：" + ("全部答對" if sub.empty else ""))
        for r in sub.itertuples(index=False):
            lines.append(f"- [{r.題型}] {r.問題}（{'未進前十' if pd.isna(r.名次) else f'排第 {int(r.名次)}'}）")
        lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    result = run()
    OUT.mkdir(exist_ok=True)
    md = to_markdown(result)
    (OUT / "RAG檢索評估.md").write_text(md, encoding="utf-8")
    print(md)
