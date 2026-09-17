# -*- coding: utf-8 -*-
"""
混合檢索：精確 ID 釘選 ＋ 詞彙檢索 ＋ 語意檢索。

===========================  為什麼不只用向量檢索  ===========================
「PO-2026-04205 現在狀況如何？」—— 這種問題向量檢索反而容易出錯。
embedding 擅長捕捉「意思相近」，但單號、料號這種**精確識別碼**，
在向量空間裡 PO-2026-04205 和 PO-2026-04250 幾乎是同一個點。

所以跟解析層同一個哲學：**能用確定性方法解決的，就不交給模型。**

  第 1 路  精確 ID 釘選   問題裡出現單號、料號、供應商代號 → 直接取出，
                          並順著關聯多取一層（採購單 → 它的料號與供應商）
  第 2 路  詞彙檢索       TF-IDF 字元 n-gram，不需斷詞就能處理中英混合，
                          也能抓到「載板」「瓶頸料」這種領域詞
  第 3 路  語意檢索       embedding，處理「哪家比較不可靠」這種換句話說的問題

第 2、3 路以 Reciprocal Rank Fusion（RRF）合併。RRF 只看名次不看分數，
兩種分數尺度完全不同也能公平合併，不需要調權重。

沒有 API 金鑰時第 3 路自動關閉，工具照樣可用 —— 跟整個專案的降級原則一致。
==========================================================================
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer

from rag.knowledge import Card

ROOT = Path(__file__).resolve().parent.parent.parent
INDEX_DIR = ROOT / ".cache" / "rag"
# 唯讀種子：部署時附上預先算好的向量，雲端冷啟動不必重建索引（本機首次建立約需數分鐘）
SEED_DIR = ROOT / "demo_cache" / "rag"

ID_PATTERNS = [
    re.compile(r"\bPO-\d{4}-\d{4,6}\b", re.IGNORECASE),
    re.compile(r"\bSUP-[A-Z]\d{2}\b", re.IGNORECASE),
    re.compile(r"\b(?:WF|MSK)-N\d{1,2}-[A-Z]{2}\d{4}\b", re.IGNORECASE),
    re.compile(r"\bSUB-FCCSP-\d{4}\b", re.IGNORECASE),
    re.compile(r"\bASM-[A-Z]{2}\d{4}-\d{2}\b", re.IGNORECASE),
]
RRF_K = 60


@dataclass
class Hit:
    card: Card
    score: float
    source: str      # pinned / lexical / semantic / hybrid


def extract_ids(query: str) -> list[str]:
    """依識別碼在問題中出現的位置排序：使用者先問的，就排在前面。"""
    hits: list[tuple[int, str]] = []
    for pat in ID_PATTERNS:
        for m in pat.finditer(query):
            hits.append((m.start(), m.group(0).upper()))
    found: list[str] = []
    for _, v in sorted(hits):
        if v not in found:
            found.append(v)
    return found


class HybridRetriever:
    def __init__(self, cards: list[Card], provider=None) -> None:
        self.cards = cards
        self.provider = provider
        self._by_id = {c.card_id: c for c in cards}

        # 字元 n-gram：中文不需要斷詞器，單號這種混合字串也能部分比對
        self._tfidf = TfidfVectorizer(analyzer="char", ngram_range=(2, 3),
                                      sublinear_tf=True, min_df=1)
        self._tfidf_matrix = self._tfidf.fit_transform(
            [f"{c.title}\n{c.text}" for c in cards])

        self._vectors: np.ndarray | None = None
        self._query_vectors: dict[str, np.ndarray] = {}
        self._query_disk: dict[str, list[float]] = {}
        self._doc_cache: dict[str, list[float]] = {}
        self._query_cache_path = INDEX_DIR / "queries.json"
        self.semantic_ready = False
        self.semantic_error = ""

    # -----------------------------------------------------------------
    # 語意索引：以內容雜湊快取，內容沒變就不重新呼叫 API
    # -----------------------------------------------------------------
    def build_semantic_index(self) -> bool:
        if self.provider is None or not getattr(self.provider, "can_embed", False):
            self.semantic_error = "未設定可用的 embedding provider，僅使用詞彙檢索"
            return False

        INDEX_DIR.mkdir(parents=True, exist_ok=True)
        fname = f"embeddings_{self.provider.name}_{self.provider.embed_model}.json"
        cache_path = INDEX_DIR / fname
        cache = self._load_json(SEED_DIR / fname)
        cache.update(self._load_json(cache_path))   # 本機快取優先於種子

        missing = [c for c in self.cards if c.content_hash not in cache]
        if missing:
            vectors = self.provider.embed(
                [f"{c.title}\n{c.text}" for c in missing], task_type="RETRIEVAL_DOCUMENT")
            if vectors is None or len(vectors) != len(missing):
                self.semantic_error = "embedding 呼叫失敗，已降級為詞彙檢索"
                return False
            for c, v in zip(missing, vectors):
                cache[c.content_hash] = v
            try:
                cache_path.write_text(json.dumps(cache), encoding="utf-8")
            except OSError:
                pass

        mat = np.array([cache[c.content_hash] for c in self.cards], dtype=np.float32)
        mat /= np.linalg.norm(mat, axis=1, keepdims=True) + 1e-12
        self._vectors = mat
        self._doc_cache = {c.content_hash: cache[c.content_hash] for c in self.cards}
        self._query_cache_path = INDEX_DIR / f"queries_{self.provider.name}_{self.provider.embed_model}.json"
        self._query_disk = self._load_json(SEED_DIR / self._query_cache_path.name)
        self._query_disk.update(self._load_json(self._query_cache_path))
        self.semantic_ready = True
        return True

    @staticmethod
    def _load_json(path: Path) -> dict:
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    # -----------------------------------------------------------------
    def pinned(self, query: str) -> list[Card]:
        """
        精確 ID 釘選，並沿關聯多取一層。

        問「PO-2026-04205 的供應商可不可靠」時，答案不在採購單卡上，
        在它的供應商卡上。向量檢索不會知道這兩張卡有關，但資料結構知道。
        """
        ids = extract_ids(query)
        direct: list[str] = []
        related: list[str] = []
        for ident in ids:
            for prefix in ("PO", "MAT", "SUP"):
                cid = f"{prefix}:{ident}"
                if cid in self._by_id and cid not in direct:
                    direct.append(cid)
            # 關聯一層：採購單 → 它的料號卡與供應商卡
            po_card = self._by_id.get(f"PO:{ident}")
            if po_card:
                for rel in (f"MAT:{po_card.meta.get('material_id')}",
                            f"SUP:{po_card.meta.get('supplier_id')}"):
                    if rel in self._by_id and rel not in related:
                        related.append(rel)
        # 直接點名的實體排前面，關聯帶出來的排後面
        ordered = direct + [c for c in related if c not in direct]
        return [self._by_id[c] for c in ordered]

    def lexical(self, query: str, k: int = 10) -> list[Hit]:
        q = self._tfidf.transform([query])
        scores = (self._tfidf_matrix @ q.T).toarray().ravel()
        order = np.argsort(-scores)[:k]
        return [Hit(self.cards[i], float(scores[i]), "lexical")
                for i in order if scores[i] > 0]

    def semantic(self, query: str, k: int = 10) -> list[Hit]:
        if not self.semantic_ready or self._vectors is None:
            return []
        q = self._query_vectors.get(query)
        if q is None:
            # 同一個問題只向量化一次，並落地保存：重複查詢與範例問題不會重複呼叫 API
            raw = self._query_disk.get(query)
            if raw is None:
                qv = self.provider.embed([query], task_type="RETRIEVAL_QUERY")
                if not qv:
                    return []
                raw = qv[0]
                self._query_disk[query] = raw
                try:
                    self._query_cache_path.write_text(
                        json.dumps(self._query_disk, ensure_ascii=False), encoding="utf-8")
                except OSError:
                    pass
            q = np.array(raw, dtype=np.float32)
            q /= np.linalg.norm(q) + 1e-12
            self._query_vectors[query] = q
        scores = self._vectors @ q
        order = np.argsort(-scores)[:k]
        return [Hit(self.cards[i], float(scores[i]), "semantic") for i in order]

    @staticmethod
    def rrf(*rankings: list[Hit], k: int = RRF_K) -> list[Hit]:
        """Reciprocal Rank Fusion：只看名次，不受各路分數尺度影響。"""
        fused: dict[str, float] = {}
        cards: dict[str, Card] = {}
        for ranking in rankings:
            for rank, hit in enumerate(ranking, start=1):
                cid = hit.card.card_id
                fused[cid] = fused.get(cid, 0.0) + 1.0 / (k + rank)
                cards[cid] = hit.card
        order = sorted(fused, key=lambda c: -fused[c])
        return [Hit(cards[c], fused[c], "hybrid") for c in order]

    def search(self, query: str, k: int = 6, mode: str = "hybrid",
               pin: bool = True) -> list[Hit]:
        """
        mode：
            lexical   只用詞彙檢索
            semantic  只用語意檢索
            hybrid    詞彙 ＋ 語意（RRF）
        pin：是否先套用精確 ID 釘選。工具內一律開啟 —— 那是確定性規則，
             不該被模糊比對蓋掉。關閉選項僅供評估時量化它的貢獻。
        """
        pinned = [Hit(c, 1.0, "pinned") for c in self.pinned(query)] if pin else []
        if mode == "lexical":
            ranked = self.lexical(query, k=k * 2)
        elif mode == "semantic":
            ranked = self.semantic(query, k=k * 2)
        else:
            lex = self.lexical(query, k=k * 2)
            sem = self.semantic(query, k=k * 2)
            ranked = self.rrf(lex, sem) if sem else lex

        seen = {h.card.card_id for h in pinned}
        merged = pinned + [h for h in ranked if h.card.card_id not in seen]
        return merged[:k]
