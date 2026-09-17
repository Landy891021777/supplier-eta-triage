# -*- coding: utf-8 -*-
"""
回答生成：只根據檢索到的卡片回答，並驗證引用出處。

===========================  兩道防線  ===========================
**防線一：prompt 限制模型只能用參考資料回答，且每個事實要標出處。**

**防線二：程式檢查模型引用的出處，是不是真的在給它的資料裡。**
prompt 叫模型不要亂引用，不代表它就不會。模型偶爾會「引用」一張
根本沒給它的卡片 —— 那代表那句話很可能是它自己編的。
這種情況要在畫面上明確標示，而不是讓一個看起來有出處的假答案過關。

無 API 金鑰時不生成文字，直接把檢索到的卡片原文列給使用者看。
原文就是事實本身，沒有被模型改寫過 —— 某些情境下這反而更可靠。
================================================================
"""
from __future__ import annotations

import re
from pathlib import Path

from rag.retriever import Hit, HybridRetriever

PROMPT_PATH = Path(__file__).resolve().parent.parent / "llm" / "prompts" / "rag_answer.md"
CITATION_RE = re.compile(r"\[((?:PO|MAT|SUP|SUM|DOC):[^\]\s]+)\]")


def build_prompt(question: str, hits: list[Hit], reference_date: str) -> str:
    context = "\n\n".join(
        f"### [{h.card.card_id}] {h.card.title}\n{h.card.text}" for h in hits)
    return (PROMPT_PATH.read_text(encoding="utf-8")
            .replace("{{REFERENCE_DATE}}", reference_date)
            .replace("{{CONTEXT}}", context)
            .replace("{{QUESTION}}", question.strip()))


def check_citations(text: str, hits: list[Hit]) -> dict:
    """
    驗證引用：模型引用的卡片編號，必須是真的有給它的那些。

    回傳：
        cited     模型引用了哪些編號
        invalid   引用了、但根本不在參考資料裡的編號（疑似編造）
        uncited   回答裡完全沒有引用任何出處
    """
    given = {h.card.card_id for h in hits}
    cited = list(dict.fromkeys(CITATION_RE.findall(text)))
    return {
        "cited": cited,
        "invalid": [c for c in cited if c not in given],
        "uncited": not cited,
    }


def choose_mode(retriever: HybridRetriever, mode: str = "auto") -> str:
    """
    檢索模式的預設值由評估結果決定（見 src/rag/evaluate_rag.py）：

        有語意索引  → 語意檢索 ＋ ID 釘選   （24 題 Hit@3 0.958，六種配置中最佳）
        沒有        → 詞彙檢索 ＋ ID 釘選   （0.583，但識別碼題全對，仍堪用）

    原本預設是混合檢索，評估後發現它反而比純語意檢索差，因此改掉。
    """
    if mode != "auto":
        return mode if (mode == "lexical" or retriever.semantic_ready) else "lexical"
    return "semantic" if retriever.semantic_ready else "lexical"


def answer(question: str, retriever: HybridRetriever, provider=None,
           reference_date: str = "", k: int = 6, mode: str = "auto") -> dict:
    question = (question or "").strip()
    if not question:
        return {"answer": "", "hits": [], "mode": "none", "citations": None, "llm": False}

    effective_mode = choose_mode(retriever, mode)
    hits = retriever.search(question, k=k, mode=effective_mode, pin=True)

    if not hits:
        return {"answer": "提供的資料中找不到與這個問題相關的內容。",
                "hits": [], "mode": effective_mode, "citations": None, "llm": False}

    if provider is None or not provider.available:
        return {"answer": "", "hits": hits, "mode": effective_mode,
                "citations": None, "llm": False,
                "note": "未設定 LLM 金鑰，以下直接列出檢索到的原始資料（未經模型改寫）。"}

    resp = provider.complete(build_prompt(question, hits, reference_date),
                             temperature=0.0, json_mode=False)
    if not resp.ok or not resp.text.strip():
        return {"answer": "", "hits": hits, "mode": effective_mode,
                "citations": None, "llm": False,
                "note": f"LLM 呼叫失敗，以下直接列出檢索到的原始資料。（{resp.error[:120]}）"}

    return {"answer": resp.text.strip(), "hits": hits, "mode": effective_mode,
            "citations": check_citations(resp.text, hits), "llm": True,
            "latency_ms": resp.latency_ms}
