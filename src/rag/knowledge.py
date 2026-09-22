# -*- coding: utf-8 -*-
"""
知識庫建置：把 ERP 資料與工具文件切成可檢索的「卡片」。

===========================  切塊策略（chunking）  ===========================
RAG 的品質，一半取決於怎麼切。這裡刻意不用「每 500 字切一段」的通用做法，
因為資料有兩種完全不同的形狀：

1. **結構化資料（採購單、料號、供應商）→ 一個實體一張卡**
   一張採購單如果被切成兩段，前半段有單號、後半段有交期，兩段單獨都沒用。
   所以按實體切，每張卡自成一個完整的事實單位，而且帶上單號當 ID。

2. **文件（操作指引、設計決策、ERP 對應）→ 依 Markdown 標題切**
   每段前面補上「文件名 > 章節名」路徑。被檢索出來時，
   模型才知道這段話出自哪裡、在回答什麼問題。

3. **摘要卡 → 為「排名／比較」類問題預先算好**
   「哪家供應商準交率最差？」這種問題，檢索本身回答不了 ——
   檢索只會撈出幾張相似的卡，不會把 12 家供應商全部拿來比。
   **檢索做不了聚合**，所以這類答案必須事先算好放進一張摘要卡。
   這是 RAG 最常被忽略的限制之一。
===========================================================================
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

import pandas as pd

from domain import CATEGORY_LABEL_ZH, SUPPLIER_TYPE_LABEL_ZH

ROOT = Path(__file__).resolve().parent.parent.parent
DOCS = ROOT / "docs"

# 文件卡的來源。刻意不收 README：它是給面試官看的，不是給生管用的操作知識。
DOC_FILES = ["操作指引.md", "設計決策.md", "ERP欄位對應.md"]


@dataclass
class Card:
    card_id: str          # 例：PO:PO-2026-04205、SUP:SUP-F03、DOC:操作指引#3
    kind: str             # po / material / supplier / summary / doc
    title: str
    text: str
    meta: dict = field(default_factory=dict)

    @property
    def content_hash(self) -> str:
        """內容雜湊。內容沒變就不重算 embedding，省配額也省時間。"""
        return hashlib.sha256(f"{self.title}\n{self.text}".encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict:
        return asdict(self)


def _yn(v) -> str:
    return "是" if bool(v) else "否"


def _clean(v) -> str:
    if v is None or v != v:
        return ""
    s = str(v).strip()
    return "" if s.lower() in {"nan", "none"} else s


# ---------------------------------------------------------------------------
# 結構化資料卡
# ---------------------------------------------------------------------------
def supplier_cards(suppliers: pd.DataFrame, perf: pd.DataFrame | None,
                   pos: pd.DataFrame) -> list[Card]:
    perf_idx = perf.set_index("supplier_id").to_dict("index") if perf is not None else {}
    open_count = pos.groupby("supplier_id").size().to_dict()
    cards = []
    for s in suppliers.itertuples(index=False):
        p = perf_idx.get(s.supplier_id, {})
        lines = [
            f"供應商代號：{s.supplier_id}",
            f"供應商名稱：{s.supplier_name}",
            f"供應商類型：{SUPPLIER_TYPE_LABEL_ZH.get(s.supplier_type, s.supplier_type)}"
            f"（{s.supplier_type}）",
            f"目前未結採購單數：{open_count.get(s.supplier_id, 0)} 張",
        ]
        if p:
            lines += [
                f"歷史已結案單：{int(p['樣本數'])} 筆",
                f"歷史準交率：{p['準交率']:.0%}",
                f"延遲時的中位延遲天數：{_clean(p['延遲時中位數(天)']) or '無'} 天",
                f"P80 延遲天數（八成的單在承諾日後幾天內到料）：{int(p['P80 延遲(天)'])} 天",
                f"最長延遲：{int(p['最長延遲(天)'])} 天",
            ]
        cards.append(Card(
            card_id=f"SUP:{s.supplier_id}", kind="supplier",
            title=f"供應商 {s.supplier_id} {s.supplier_name}",
            text="\n".join(lines),
            meta={"supplier_id": s.supplier_id}))
    return cards


def material_cards(materials: pd.DataFrame, pos: pd.DataFrame) -> list[Card]:
    by_mat = pos.groupby("material_id")
    cards = []
    for m in materials.itertuples(index=False):
        mine = by_mat.get_group(m.material_id) if m.material_id in by_mat.groups else pos.iloc[0:0]
        alt = _clean(m.alt_material_id)
        lines = [
            f"料號：{m.material_id}",
            f"料件類別：{CATEGORY_LABEL_ZH.get(m.category, m.category)}（{m.category}）",
            f"標準前置期：{int(m.std_lead_time_days)} 天",
            f"是否為瓶頸料：{_yn(m.is_bottleneck)}",
            f"是否有已認證二源：{_yn(m.has_qualified_second_source)}"
            + ("" if m.has_qualified_second_source else "（單一來源，延遲時無法轉單）"),
            f"替代料：{alt or '無登錄替代料'}",
            f"關鍵性等級：{m.criticality}",
        ]
        # 舊世界資料（本 Task 尚未重建）沒有這兩欄，或欄位值是 NaN——
        # 缺值不寫進卡片，不然模型會把「未維護」誤讀成「就是這個值」。
        uom = _clean(getattr(m, "base_uom", None))
        if uom:
            lines.append(f"計量單位：{uom}")
        gr_days = getattr(m, "gr_processing_days", None)
        if gr_days is not None and gr_days == gr_days:  # 排除 NaN（NaN 不等於自己）
            lines.append(f"收貨處理天數（料號主檔預設）：{int(gr_days)} 天")
        lines.append(
            f"目前未結採購單：{len(mine)} 張"
            + (f"（{', '.join(mine['po_no'].head(6))}）" if len(mine) else ""))
        cards.append(Card(
            card_id=f"MAT:{m.material_id}", kind="material",
            title=f"料號 {m.material_id}", text="\n".join(lines),
            meta={"material_id": m.material_id, "category": m.category}))
    return cards


def po_cards(pos: pd.DataFrame, materials: pd.DataFrame,
             suppliers: pd.DataFrame, as_of: date) -> list[Card]:
    mat_idx = materials.set_index("material_id").to_dict("index")
    sup_idx = suppliers.set_index("supplier_id").to_dict("index")
    cards = []
    for p in pos.itertuples(index=False):
        m = mat_idx.get(p.material_id, {})
        s = sup_idx.get(p.supplier_id, {})
        committed = date.fromisoformat(str(p.committed_date)[:10])
        need = date.fromisoformat(str(p.need_date)[:10])
        buffer_days = (need - committed).days
        lines = [
            f"採購單號：{p.po_no}",
            f"料號：{p.material_id}（{m.get('category', '')}）",
            f"供應商：{p.supplier_id} {s.get('supplier_name', '')}",
            f"採購數量：{int(p.qty)}",
            f"供應商承諾交期：{committed.isoformat()}",
            f"下游需求日：{need.isoformat()}",
            f"緩衝天數（需求日減承諾日）：{buffer_days} 天",
            f"距今天（{as_of.isoformat()}）還有：{(committed - as_of).days} 天到承諾日",
            f"已改期次數：{int(p.reschedule_count)} 次",
            f"下游是否已排定產能：{_yn(p.downstream_scheduled)}",
            f"是否有已認證二源：{_yn(m.get('has_qualified_second_source'))}",
            f"是否為瓶頸料：{_yn(m.get('is_bottleneck'))}",
        ]
        cards.append(Card(
            card_id=f"PO:{p.po_no}", kind="po",
            title=f"採購單 {p.po_no}", text="\n".join(lines),
            meta={"po_no": p.po_no, "material_id": p.material_id,
                  "supplier_id": p.supplier_id}))
    return cards


def summary_cards(suppliers: pd.DataFrame, perf: pd.DataFrame | None,
                  materials: pd.DataFrame, pos: pd.DataFrame) -> list[Card]:
    """
    為排名、比較、篩選類問題預先算好的摘要卡。

    檢索只會撈出「最相似的幾張卡」，不會把全部實體拿來比較。
    問「準交率最差的是哪家」時，若沒有這張卡，模型只能從撈到的三、四家
    裡挑一個最差的 —— 答案看起來很有自信，但可能是錯的。
    """
    cards = []
    if perf is not None and not perf.empty:
        # 先換成英文欄名：itertuples 會把含括號、空白的中文欄名改成 _3、_5 這種
        # 位置名，直接用會悄悄取錯欄。
        ranked = (perf.rename(columns={"樣本數": "n", "準交率": "otd",
                                       "P80 延遲(天)": "p80"})
                  .merge(suppliers[["supplier_id", "supplier_name", "supplier_type"]],
                         on="supplier_id")
                  .sort_values("otd"))
        rows = [f"{i}. {r.supplier_id} {r.supplier_name}（{r.supplier_type}）："
                f"準交率 {r.otd:.0%}，P80 延遲 {int(r.p80)} 天，樣本 {int(r.n)} 筆"
                for i, r in enumerate(ranked.itertuples(index=False), 1)]
        cards.append(Card(
            card_id="SUM:supplier_otd_ranking", kind="summary",
            title="供應商歷史準交率排名（由差到好）",
            text="依歷史收貨紀錄計算，準交率由低到高排序：\n" + "\n".join(rows)))

        weighted = ranked.assign(hits=ranked["otd"] * ranked["n"])
        agg = weighted.groupby("supplier_type")[["hits", "n"]].sum()
        by_type = (agg["hits"] / agg["n"]).sort_values()
        cards.append(Card(
            card_id="SUM:otd_by_supplier_type", kind="summary",
            title="各類供應商的整體準交率",
            text="依供應商類型加權計算之歷史準交率（由差到好）：\n" + "\n".join(
                f"- {SUPPLIER_TYPE_LABEL_ZH.get(t, t)}（{t}）：{v:.0%}"
                for t, v in by_type.items())))

    single = materials[~materials["has_qualified_second_source"].astype(bool)]
    bottleneck_single = single[single["is_bottleneck"].astype(bool)]
    cards.append(Card(
        card_id="SUM:single_source_materials", kind="summary",
        title="單一來源料號清單（無已認證二源）",
        text=(f"共 {len(single)} 顆料號沒有已認證二源，延遲時無法轉單。\n"
              f"其中同時是瓶頸料的有 {len(bottleneck_single)} 顆："
              f"{', '.join(bottleneck_single['material_id'])}\n"
              f"其餘單一來源料號：{', '.join(single[~single['is_bottleneck'].astype(bool)]['material_id'])}")))

    tight = pos.assign(
        buffer=(pd.to_datetime(pos["need_date"]) - pd.to_datetime(pos["committed_date"])).dt.days
    ).sort_values("buffer").head(15)
    cards.append(Card(
        card_id="SUM:tightest_buffer_pos", kind="summary",
        title="緩衝天數最少的未結採購單（前 15 張）",
        text="承諾交期距下游需求日最近的單，一旦延遲最容易直接造成缺料：\n" + "\n".join(
            f"- {r.po_no}（{r.material_id}，{r.supplier_id}）：緩衝 {int(r.buffer)} 天，"
            f"已改期 {int(r.reschedule_count)} 次"
            for r in tight.itertuples(index=False))))

    repeat = pos[pos["reschedule_count"].astype(int) >= 3].sort_values(
        "reschedule_count", ascending=False)
    cards.append(Card(
        card_id="SUM:repeat_reschedule_pos", kind="summary",
        title="已改期三次以上的未結採購單",
        text=(f"共 {len(repeat)} 張未結採購單已改期三次以上"
              f"（歷史上這類單最終仍延遲的比例明顯較高）：\n" + "\n".join(
                  f"- {r.po_no}（{r.supplier_id}）：已改期 {int(r.reschedule_count)} 次"
                  for r in repeat.itertuples(index=False)))))
    return cards


# ---------------------------------------------------------------------------
# 文件卡
# ---------------------------------------------------------------------------
def doc_cards(doc_dir: Path = DOCS, files: list[str] | None = None,
              max_chars: int = 1200) -> list[Card]:
    """
    依 Markdown 標題切文件，並在每段前面補上來源路徑。

    段落過長時再依空行細切，避免單張卡塞太多不相關內容，
    稀釋掉檢索的精準度。
    """
    cards = []
    for fname in (files or DOC_FILES):
        path = doc_dir / fname
        if not path.exists():
            continue
        doc_title = path.stem
        text = path.read_text(encoding="utf-8")
        sections = re.split(r"(?m)^(?=##\s)", text)
        n = 0
        for sec in sections:
            sec = sec.strip()
            if not sec:
                continue
            first = sec.splitlines()[0]
            heading = first.lstrip("#").strip() if first.startswith("#") else "前言"
            pieces, buf = [], ""
            for para in re.split(r"\n\s*\n", sec):
                if buf and len(buf) + len(para) > max_chars:
                    pieces.append(buf)
                    buf = ""
                buf = f"{buf}\n\n{para}" if buf else para
            if buf:
                pieces.append(buf)
            for piece in pieces:
                n += 1
                cards.append(Card(
                    card_id=f"DOC:{doc_title}#{n}", kind="doc",
                    title=f"{doc_title} > {heading}",
                    text=f"【出處：{doc_title} > {heading}】\n{piece}",
                    meta={"doc": doc_title, "heading": heading}))
    return cards


# ---------------------------------------------------------------------------
def build_cards(cfg: dict | None = None) -> list[Card]:
    """建立完整知識庫。資料來源與行動清單相同（config.yaml 的 data_source）。"""
    import pipeline

    cfg = cfg or pipeline.load_config()
    src = pipeline.get_data_source(cfg)
    pos, mats, sups = src.purchase_orders(), src.materials(), src.suppliers()
    as_of = date.fromisoformat(cfg["data_generation"]["as_of_date"])

    try:
        import supplier_stats
        perf = supplier_stats.supplier_performance()
    except (FileNotFoundError, RuntimeError):
        perf = None  # 沒有歷史資料時照樣能建庫，只是少了準交率相關內容

    return (summary_cards(sups, perf, mats, pos)
            + supplier_cards(sups, perf, pos)
            + material_cards(mats, pos)
            + po_cards(pos, mats, sups, as_of)
            + doc_cards())
