# -*- coding: utf-8 -*-
"""
物料智能檢索（RAG）的測試。全部離線執行，不呼叫任何 API。

守住的重點：
    1. 切塊不能把一個實體切成兩半
    2. 精確識別碼必須被找到，不能交給模糊比對碰運氣
    3. 模型引用了沒給它的資料，必須被抓出來
    4. 沒有金鑰時工具照樣可用，而且不假裝有 AI 回答
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from rag.answer import answer, check_citations, choose_mode  # noqa: E402
from rag.knowledge import (Card, doc_cards, material_cards, po_cards,  # noqa: E402
                           summary_cards, supplier_cards)
from rag.retriever import Hit, HybridRetriever, extract_ids  # noqa: E402


@pytest.fixture(scope="module")
def new_world_db(tmp_path_factory):
    """
    在 tmp_path 重建一份新世界（晶圓廠）的模擬 ERP 資料庫，含歷史收貨紀錄。

    不能直接用 build_cards() 讀真實 data/erp_sim.db：那份資料庫要到 Task 10
    才會用新版 generate_data.py 重建，在那之前仍是舊世界（Fabless：
    WF-／SUB-FCCSP-／ASM- 這些料號、SUP-F03 這些供應商代號），
    本檔要測的新世界識別碼（PR-ArF-1088、SW-300-E-3390、SUP-W03…）
    在那份資料庫裡根本不存在，卡片會建不出來、精確 ID 釘選測試會全滅。

    作法跟 test_generate_history.py 的 rebuilt fixture 相同：monkeypatch
    產生器的模組級路徑常數，讓 generate_data／build_erp_db 寫進 tmp_path
    而不是真實 data/。
    """
    import build_erp_db
    import generate_data
    import generate_history

    tmp = tmp_path_factory.mktemp("rag_erp")
    saved = (generate_data.DATA, generate_data.INBOX,
              build_erp_db.DATA, build_erp_db.DB_PATH)
    try:
        generate_data.DATA = tmp
        generate_data.INBOX = tmp / "inbox"
        build_erp_db.DATA = tmp
        build_erp_db.DB_PATH = tmp / "erp_sim.db"
        generate_data.main()
        build_erp_db.build(verbose=False)
    finally:
        (generate_data.DATA, generate_data.INBOX,
         build_erp_db.DATA, build_erp_db.DB_PATH) = saved

    db_path = tmp / "erp_sim.db"
    generate_history.build_history(verbose=False, db_path=db_path)
    return db_path


@pytest.fixture(scope="module")
def cards(new_world_db):
    """比照 rag.knowledge.build_cards()，只是資料來源換成 tmp_path 的新世界資料庫。"""
    from datetime import date

    import pipeline
    import supplier_stats
    from adapters.sqlite_source import SqliteSource

    src = SqliteSource(db_path=new_world_db)
    pos, mats, sups = src.purchase_orders(), src.materials(), src.suppliers()
    as_of = date.fromisoformat(pipeline.load_config()["data_generation"]["as_of_date"])
    perf = supplier_stats.supplier_performance(
        supplier_stats.load_outcomes(db_path=new_world_db))

    return (summary_cards(sups, perf, mats, pos)
            + supplier_cards(sups, perf, pos)
            + material_cards(mats, pos)
            + po_cards(pos, mats, sups, as_of)
            + doc_cards())


@pytest.fixture(scope="module")
def retriever(cards):
    return HybridRetriever(cards, provider=None)   # provider=None：只用詞彙檢索


# ---------------------------------------------------------------------------
# 切塊
# ---------------------------------------------------------------------------
def test_one_card_per_entity(cards):
    """一個實體一張卡：同一張採購單不可以被切成兩張。"""
    ids = [c.card_id for c in cards]
    assert len(ids) == len(set(ids)), "卡片編號重複"
    kinds = {c.kind for c in cards}
    assert {"po", "material", "supplier", "summary", "doc"} <= kinds


def test_po_card_is_self_contained(cards):
    """採購單卡必須同時帶有單號、承諾日與需求日，單獨被撈出來也能回答問題。"""
    card = next(c for c in cards if c.card_id == "PO:PO-2026-04205")
    for token in ("PO-2026-04205", "承諾交期", "下游需求日", "緩衝天數"):
        assert token in card.text


def test_supplier_ranking_card_is_sorted_worst_first(cards):
    """
    回歸測試：pandas 的 itertuples 會把含括號的中文欄名改成 _3、_5 位置名，
    早期版本因此在排名卡上悄悄取錯欄。排名第一的必須是準交率最低的。
    """
    import re
    card = next(c for c in cards if c.card_id == "SUM:supplier_otd_ranking")
    rates = [int(x) for x in re.findall(r"準交率 (\d+)%", card.text)]
    assert rates == sorted(rates), f"排名卡未依準交率由低到高排序：{rates}"


def test_doc_chunks_carry_source_path():
    """文件切塊必須帶上「文件 > 章節」出處，被檢索出來時才知道在回答什麼。"""
    for c in doc_cards():
        assert c.text.startswith("【出處："), c.card_id
        assert " > " in c.title


def test_content_hash_changes_with_content():
    a = Card("X:1", "doc", "t", "內容一")
    b = Card("X:1", "doc", "t", "內容二")
    assert a.content_hash != b.content_hash


# ---------------------------------------------------------------------------
# 精確識別碼
# ---------------------------------------------------------------------------
def test_extract_ids_follow_question_order_across_types():
    """不同類型的識別碼混在一起時，順序依問題中出現的位置，而非依識別碼種類。"""
    q = "PO-2026-04205 跟 PR-ArF-1088、SW-300-E-3390、SUP-W03 的狀況"
    assert extract_ids(q) == ["PO-2026-04205", "PR-ARF-1088", "SW-300-E-3390", "SUP-W03"]


def test_extract_ids_is_case_insensitive_for_mixed_case_material_ids():
    """
    光阻料號的段別本來就混合大小寫（PR-ArF-1088），但 extract_ids() 為了讓不同
    大小寫寫法都能命中而統一轉大寫。若卡片釘選比對沒有跟著做大小寫不敏感，
    問句小寫或全大寫都會釘選不到卡片，等同精確 ID 釘選整組失效。
    """
    q = "PR-ARF-1088 這顆料有沒有第二家可以買"
    assert extract_ids(q) == ["PR-ARF-1088"]


def test_pinning_matches_mixed_case_material_card_id():
    """卡片 ID 保留料號原本大小寫（MAT:PR-ArF-1088），問句大小寫不論怎麼寫都要釘選到同一張卡。"""
    card = Card("MAT:PR-ArF-1088", "material", "料號 PR-ArF-1088", "料號：PR-ArF-1088")
    retriever = HybridRetriever([card], provider=None)
    for q in ("PR-ARF-1088 有沒有第二家可以買", "pr-arf-1088 有沒有第二家可以買"):
        hits = retriever.search(q, k=3)
        assert hits and hits[0].card.card_id == "MAT:PR-ArF-1088", q


def test_pinning_brings_related_supplier_card(retriever):
    """
    問採購單的供應商時，答案不在採購單卡上。
    純語意檢索在評估中就是這題答錯；ID 釘選必須沿關聯把供應商卡帶出來。
    """
    ids = [h.card.card_id for h in retriever.search("PO-2026-04278 的供應商靠得住嗎", k=5)]
    assert ids[0] == "PO:PO-2026-04278"
    assert "SUP:SUP-T01" in ids[:3]


def test_pinning_preserves_question_order(retriever):
    ids = [h.card.card_id for h in retriever.search("比較 SUP-R02 和 SUP-W02", k=4)]
    assert ids[:2] == ["SUP:SUP-R02", "SUP:SUP-W02"]


def test_pinning_can_be_disabled_for_evaluation(retriever):
    pinned = retriever.search("PO-2026-04278 的供應商", k=5, pin=True)
    unpinned = retriever.search("PO-2026-04278 的供應商", k=5, pin=False)
    assert pinned[0].source == "pinned"
    assert all(h.source != "pinned" for h in unpinned)


# ---------------------------------------------------------------------------
# 排序合併
# ---------------------------------------------------------------------------
def test_rrf_rewards_agreement():
    """兩路都排在前面的卡，應該勝過只有一路排第一的卡。"""
    a, b, c = (Card(f"X:{i}", "doc", i, i) for i in "abc")
    lex = [Hit(a, 9, "lexical"), Hit(b, 5, "lexical")]
    sem = [Hit(c, 0.9, "semantic"), Hit(b, 0.8, "semantic")]
    fused = [h.card.card_id for h in HybridRetriever.rrf(lex, sem)]
    assert fused[0] == "X:b"


def test_choose_mode_falls_back_without_semantic_index():
    class Stub:
        semantic_ready = False
    assert choose_mode(Stub()) == "lexical"
    assert choose_mode(Stub(), "hybrid") == "lexical"
    Stub.semantic_ready = True
    assert choose_mode(Stub()) == "semantic"


# ---------------------------------------------------------------------------
# 回答與引用
# ---------------------------------------------------------------------------
def test_citation_check_flags_fabricated_source():
    """
    模型引用了沒給它的卡片 —— 開發時實際發生過：
    模型引用了 [SUP:SUP-S01]，但那次檢索根本沒撈到那張卡。
    """
    given = [Hit(Card("SUM:supplier_otd_ranking", "summary", "t", "x"), 1, "semantic")]
    text = "最差的是 SUP-S01 [SUM:supplier_otd_ranking]，它的料號風險很高 [SUP:SUP-S01]。"
    result = check_citations(text, given)
    assert result["invalid"] == ["SUP:SUP-S01"]
    assert not result["uncited"]


def test_citation_check_detects_missing_citations():
    given = [Hit(Card("PO:X", "po", "t", "x"), 1, "pinned")]
    assert check_citations("這張單很緊急。", given)["uncited"] is True


def test_no_provider_returns_raw_sources_not_fake_answer(retriever):
    """沒有金鑰時不可以假裝有 AI 回答，要直接列出原始資料並說明原因。"""
    res = answer("PO-2026-04205 現在狀況如何", retriever, provider=None)
    assert res["llm"] is False
    assert res["answer"] == ""
    assert res["hits"] and res["hits"][0].card.card_id == "PO:PO-2026-04205"
    assert "未設定 LLM 金鑰" in res["note"]


def test_empty_question_does_nothing():
    class Stub:
        semantic_ready = False
    assert answer("   ", Stub())["hits"] == []


# ---------------------------------------------------------------------------
# 向量化切批
# ---------------------------------------------------------------------------
def test_embedding_batches_split_by_characters(monkeypatch):
    """
    回歸測試：原本固定每批 100 筆，一批約 2.4 萬字，直接超過每分鐘 token 上限。
    改為依字數切批後，長卡片不會讓單一批次過肥。
    """
    import llm.provider as prov

    monkeypatch.setattr(prov, "_throttle", lambda: None)
    batches: list[int] = []

    class Fake(prov.BaseProvider):
        name = "fake"
        embed_model = "fake-embed"

        @property
        def available(self):
            return True

        def _raw_embed(self, texts, task_type):
            batches.append(sum(len(t) for t in texts))
            return [[0.0] for _ in texts]

    texts = ["字" * 1000] * 20            # 20 張各一千字的卡
    out = Fake("m").embed(texts, max_batch_chars=6000)
    assert len(out) == 20
    assert max(batches) <= 6000
    assert len(batches) >= 4


def test_provider_without_embedding_reports_capability():
    """Anthropic 沒有 embedding API，抽象層必須允許這項能力缺席。"""
    import llm.provider as prov
    p = prov.AnthropicProvider()
    p.api_key = "dummy"
    assert p.available is True
    assert p.can_embed is False
    assert p.embed(["x"]) is None
