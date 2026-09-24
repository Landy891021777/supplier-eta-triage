# 晶圓廠世界設定＋收貨處理天數（Plan 1.5）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把合成資料的世界從 Fabless（晶圓代工、光罩、載板、封測委外）改成**晶圓廠前段製造**（矽晶圓、光阻、特殊氣體、濕式化學品、靶材、光罩、研磨液），並加入「收貨處理天數」：料到廠後要幾天才能投產，依料別給預設值，物料企劃可逐料號調整並留下紀錄。

**Architecture:** 領域常數（料別、供應商類型、兩者對應、中文名稱、各料別收貨處理天數預設）集中在 `src/domain.py` 與 `config.yaml`。收貨處理天數仿 SAP 料號主檔 MARC-WEBAZ，寫在模擬 ERP 的 `material_master`；企劃的調整存在工具自己的 `data/planner_settings.db`，**永不寫回 ERP**。分級改成 `預估缺料天數 = 保守到料日 + 收貨處理天數 − 需求日`；主流程拆出 `retriage()`，企劃調整天數後只重算分級，不重跑讀信。

**Tech Stack:** Python 3.13、pandas、SQLite、Streamlit、pytest。Task 1–10 全部離線；Task 11 會呼叫 Gemini，**執行前必須先取得使用者同意**。

---

## 背景與已確認的決定（2026-09-22，與使用者討論定案）

- 使用者是晶圓廠物料企劃出身，對 Fabless 不熟 → 世界設定改為晶圓廠。
- 收貨處理天數預設（查證過程見對話；沒有半導體業公開的依料別天數，以下是依到廠作業步驟推得的**領域假設**）：

| 料別 | 天數 | 依據 |
|---|---:|---|
| 矽晶圓原片 SILICON_WAFER | 1 | 目視、厚度與平坦度量測、雷射微粒掃描，機台作業 |
| 光阻 PHOTORESIST | 2 | 一般檢驗 1 天＋冷藏 5–10°C、開瓶前需回溫一晚（MicroChemicals 文件） |
| 特殊氣體 SPECIALTY_GAS | 1 | 每支鋼瓶附供應商 CoA，廠內審報告、上機前測漏 |
| 濕式化學品 WET_CHEMICAL | 1 | 廠內 ICP-MS 微量金屬分析（外送實驗室需 1–2 天） |
| 靶材 TARGET | 1 | 進料檢驗；上機後的預濺鍍屬機台時間，不計入 |
| 光罩 MASK | 3 | 規格核對、光罩檢驗、上線跑晶圓驗證曝光；**把握最低** |
| 研磨液 CMP_SLURRY | 1 | 大顆粒數量與粒徑分布檢驗 |

- 企劃可在工具內逐料號調整天數，**原因必填**，記錄誰、何時、原值、新值；工具內設定、不同步 ERP；雲端展示環境重啟後會回到預設。
- 備品（MRO）不納入：屬設備工程的補料邏輯，和生產用料的追料不同。
- **不在本計畫**：分批交貨（Plan 3）、介面兩區與全站「生管→物料企劃」用語（Plan 2）、README／CLAUDE.md 全面改寫（Plan 3）。
- 手寫 10 封刁鑽案例：**採購單號、日期、標準答案結構全部保留**，只換料號、供應商與領域用語，讓既有抽取測試繼續有效。

## 新世界的資料設計（所有 Task 共用，實作時以此為準）

**供應商（14 家）**

| 代號 | 名稱 | 類型 | 主檔準交率 | 備註 |
|---|---|---|---:|---|
| SUP-W01 | Wafer-Alpha | WAFER_MAKER | 0.90 | |
| SUP-W02 | Wafer-Bravo | WAFER_MAKER | 0.86 | |
| SUP-W03 | Wafer-Charlie | WAFER_MAKER | 0.78 | 2026-03-01 起表現變差（drift） |
| SUP-R01 | Resist-Delta | RESIST_MAKER | 0.84 | |
| SUP-R02 | Resist-Echo | RESIST_MAKER | 0.72 | 體質最差 |
| SUP-G01 | Gas-Foxtrot | GAS_SUPPLIER | 0.75 | 2026-04-01 起改善（drift） |
| SUP-G02 | Gas-Golf | GAS_SUPPLIER | 0.88 | |
| SUP-C01 | Chem-Hotel | CHEMICAL_SUPPLIER | 0.92 | |
| SUP-C02 | Chem-India | CHEMICAL_SUPPLIER | 0.85 | |
| SUP-T01 | Target-Juliet | TARGET_MAKER | 0.80 | |
| SUP-T02 | Target-Kilo | TARGET_MAKER | 0.83 | |
| SUP-M01 | Mask-Lima | MASK_SHOP | 0.91 | |
| SUP-M02 | Mask-Mike | MASK_SHOP | 0.85 | |
| SUP-L01 | Slurry-November | SLURRY_MAKER | 0.83 | 唯一一家 → 研磨液一律單一來源 |

**料別 → 供應商類型、料號格式、前置期、單位、數量選項**（前置期與數量皆為領域假設）

| 料別 | 供應商類型 | 料號格式（例） | 標準前置期（天） | 單位 | 數量選項 |
|---|---|---|---|---|---|
| SILICON_WAFER | WAFER_MAKER | `SW-300-P-2210`（P=拋光片、E=磊晶片） | 60–120 | PCS | 500, 1000, 1500, 2000, 3000, 5000 |
| PHOTORESIST | RESIST_MAKER | `PR-ArF-1088`（ArF／KrF／EUV／iLine） | 30–90 | GAL | 20, 40, 80, 120, 200 |
| SPECIALTY_GAS | GAS_SUPPLIER | `GS-NF3-01`（NF3／WF6／SiH4／NH3／C4F6） | 20–60 | CYL | 10, 20, 40, 60, 100 |
| WET_CHEMICAL | CHEMICAL_SUPPLIER | `CH-HF-05`（H2SO4／HF／H2O2／NH4OH／IPA） | 10–30 | DRM | 20, 40, 80, 160 |
| TARGET | TARGET_MAKER | `TG-Cu-90`（Ti／Ta／Cu／Co／W） | 45–100 | PCS | 2, 4, 8, 12, 20, 30 |
| MASK | MASK_SHOP | `MSK-N7-XR3390`（節點 N7／N12／N16／N22／N28） | 14–35 | PCS | 1 |
| CMP_SLURRY | SLURRY_MAKER | `SL-Cu-12`（Cu／Ox／W／STI） | 20–50 | GAL | 50, 100, 200, 400 |

隨機料號的料別權重：SILICON_WAFER .25、PHOTORESIST .15、SPECIALTY_GAS .15、WET_CHEMICAL .15、TARGET .10、MASK .10、CMP_SLURRY .10。

**固定料號（手寫案例會用到）**

| 料號 | 料別 | 前置期 | 瓶頸 | 已認證二源 | 替代料 |
|---|---|---:|---|---|---|
| SW-300-E-3390 | SILICON_WAFER | 110 | 是 | 否 | — |
| SW-300-P-2210 | SILICON_WAFER | 90 | 否 | 是 | SW-300-P-2211 |
| SW-300-P-2211 | SILICON_WAFER | 90 | 否 | 是 | SW-300-P-2210 |
| MSK-N7-XR3390 | MASK | 21 | 是 | 否 | — |
| PR-ArF-1088 | PHOTORESIST | 75 | 是 | 否 | — |
| GS-NF3-01 | SPECIALTY_GAS | 45 | 否 | 是 | — |
| CH-HF-05 | WET_CHEMICAL | 20 | 否 | 是 | — |
| TG-Ti-77 | TARGET | 80 | 否 | 是 | — |
| TG-Ta-79 | TARGET | 85 | 是 | 否 | — |
| TG-Cu-90 | TARGET | 80 | 否 | 是 | TG-Cu-91 |
| TG-Cu-91 | TARGET | 80 | 否 | 是 | TG-Cu-90 |

**固定採購單**（單號、日期、旗標與舊版相同；只換料號、供應商、數量）

| 單號 | 料號 | 供應商 | 數量 | 承諾日 | 需求日 | 下游已排定 | 改期次數 | 佔當期需求 |
|---|---|---|---:|---|---|---|---:|---:|
| PO-2026-04417 | SW-300-E-3390 | SUP-W01 | 3000 | 2026-10-15 | 2026-10-22 | 是 | 2 | 0.80 |
| PO-2026-04452 | SW-300-P-2210 | SUP-W01 | 2000 | 2026-10-20 | 2026-11-10 | 否 | 0 | 0.35 |
| PO-2026-04390 | SW-300-P-2210 | SUP-W02 | 2500 | 2026-09-25 | 2026-10-05 | 是 | 1 | 0.55 |
| PO-2026-04391 | SW-300-P-2210 | SUP-W02 | 2500 | 2026-10-02 | 2026-10-30 | 否 | 0 | 0.45 |
| PO-2026-04205 | PR-ArF-1088 | SUP-R02 | 120 | 2026-09-10 | 2026-09-18 | 是 | 3 | 0.95 |
| PO-2026-04501 | GS-NF3-01 | SUP-G02 | 60 | 2026-09-30 | 2026-10-25 | 否 | 0 | 0.30 |
| PO-2026-04333 | CH-HF-05 | SUP-C02 | 80 | 2026-09-20 | 2026-09-28 | 是 | 1 | 0.60 |
| PO-2026-04466 | SW-300-P-2211 | SUP-W02 | 1800 | 2026-09-30 | 2026-10-20 | 否 | 0 | 0.40 |
| PO-2026-04120 | MSK-N7-XR3390 | SUP-M01 | 1 | 2026-09-12 | 2026-09-16 | 是 | 0 | 1.00 |
| PO-2026-04277 | TG-Ti-77 | SUP-T01 | 20 | 2026-10-05 | 2026-10-28 | 否 | 0 | 0.50 |
| PO-2026-04278 | TG-Ti-77 | SUP-T01 | 20 | 2026-10-10 | 2026-10-18 | 是 | 2 | 0.70 |
| PO-2026-04279 | TG-Ta-79 | SUP-T01 | 30 | 2026-10-12 | 2026-11-05 | 否 | 3 | 0.85 |
| PO-2026-04188 | TG-Cu-90 | SUP-T02 | 20 | 2026-09-30 | 2026-10-08 | 是 | 1 | 0.75 |

## 檔案結構

| 檔案 | 動作 | 責任 |
|---|---|---|
| `src/domain.py` | 修改 | 新料別與供應商類型 enum、`CATEGORY_SUPPLIER_TYPE`、中文名稱對照 |
| `config.yaml` | 修改 | `receiving.gr_processing_days`（各料別預設）、`n_suppliers: 14` |
| `src/generate_data.py` | 修改 | 新供應商、料號、數量、單位、收貨處理天數欄位、原因用語 |
| `src/handcrafted_emails.py` | 修改 | 10 封改寫（單號、日期、標準答案結構不變） |
| `src/build_erp_db.py`、`src/adapters/sqlite_source.py`、`src/adapters/base.py`、`src/adapters/csv_source.py` | 修改 | 料號主檔加 `gr_processing_days`、`base_uom`；料別→供應商類型改用 domain 對照 |
| `src/generate_history.py` | 修改 | `CATEGORY_RISK` 新料別、`SUPPLIER_DRIFT` 換成 SUP-W03／SUP-G01 |
| `src/planner_settings.py` | 新增 | 企劃調整收貨處理天數的存取與紀錄（工具自己的 SQLite） |
| `src/triage.py` | 修改 | 缺料天數加入收貨處理天數；新增「請 IQC 優先檢驗」動作 |
| `src/pipeline.py` | 修改 | 拆出 `retriage()`；料號帶入收貨處理天數 |
| `src/backtest.py` | 修改 | 實際缺料與排序都納入收貨處理天數 |
| `src/rag/retriever.py`、`src/rag/knowledge.py`、`src/rag/eval_questions.py`、`src/rag/answer.py` | 修改 | 新料號識別碼、中文料別名、收貨處理天數、評估題目 |
| `src/llm/prompts/extract_eta.md`、`src/llm/prompts/rag_answer.md` | 修改 | 「生管」→「物料企劃」、「半導體公司」→「晶圓廠」 |
| `app.py` | 修改 | 新分頁「收貨處理天數」；行動清單加「可投產日」；檢索範例用新料號 |
| `docs/ERP欄位對應.md`、`docs/設計決策.md` | 修改 | 新欄位對應、決策 17 |
| 測試 | 新增／修改 | `test_world.py`、`test_planner_settings.py`、`test_retriage.py` 等 |

## 通用規則（每個 Task 都適用）

- 一律 `py -X utf8`；檔案讀寫 `encoding="utf-8"`；**所有測試與執行一律加 `LLM_PROVIDER=none`**（Task 11 除外）。輸出若出現 gemini、embedding、429，立刻停下回報。
- 註解寫「為什麼」，繁體中文；領域假設標 `# 領域假設：`。
- 測試 docstring 寫出「錯了會怎樣」；回歸測試寫出原本的 bug。
- Commit：中文 conventional commits，結尾 `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`；只 add 本 Task 動到的檔案。
- 不可弱化既有測試斷言；計畫與程式對不上時回報 NEEDS_CONTEXT，不要猜。
- 本機 port 8512 可能有使用者正在看的 Streamlit 預覽，不要關掉它。
- 真實資料庫 `data/erp_sim.db` 與 `data/*.csv` 只在 Task 10 重新產生；Task 1–9 的測試若需要新世界的資料，一律在 `tmp_path` 產生副本。

---

### Task 1: 領域常數與設定

**Files:** Modify `src/domain.py`、`config.yaml`；Test `tests/test_world.py`（新增，本 Task 只放常數測試）

- [ ] **Step 1: 寫失敗的測試** `tests/test_world.py`：

```python
# -*- coding: utf-8 -*-
"""
晶圓廠世界設定的一致性測試。

守住的是「世界不自相矛盾」：光阻不會去跟氣體廠買、每個料別都有收貨處理天數、
手寫信件裡的單號真的存在而且供應商對得上。這些錯了不會報錯，只會讓整個展示
講出不合理的故事。
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from domain import (CATEGORY_LABEL_ZH, CATEGORY_SUPPLIER_TYPE,  # noqa: E402
                    SUPPLIER_TYPE_LABEL_ZH, MaterialCategory, SupplierType)

CFG = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))


def test_every_category_maps_to_a_supplier_type():
    assert set(CATEGORY_SUPPLIER_TYPE) == {c.value for c in MaterialCategory}
    assert set(CATEGORY_SUPPLIER_TYPE.values()) <= {t.value for t in SupplierType}


def test_every_category_and_type_has_a_chinese_label():
    """卡片與畫面顯示中文料別；少一個就會在畫面上露出英文代碼。"""
    assert set(CATEGORY_LABEL_ZH) == {c.value for c in MaterialCategory}
    assert set(SUPPLIER_TYPE_LABEL_ZH) == {t.value for t in SupplierType}


def test_every_category_has_a_gr_processing_default():
    days = CFG["receiving"]["gr_processing_days"]
    assert set(days) == {c.value for c in MaterialCategory}
    assert all(isinstance(v, int) and 0 <= v <= 30 for v in days.values())


def test_fabless_categories_are_gone():
    """回歸：情境已改為晶圓廠，載板與封測不該再出現。"""
    values = {c.value for c in MaterialCategory} | {t.value for t in SupplierType}
    assert not values & {"SUBSTRATE", "ASSEMBLY", "FOUNDRY", "OSAT", "WAFER"}
```

- [ ] **Step 2: 確認失敗**：`LLM_PROVIDER=none py -X utf8 -m pytest tests/test_world.py -v` → ImportError。

- [ ] **Step 3: 改 `src/domain.py`**：`MaterialCategory` 與 `SupplierType` 整段換成：

```python
class MaterialCategory(str, Enum):
    """
    晶圓廠（前段製造）的生產用料類別。

    範圍是物料企劃追的「生產用料」，不含備品（MRO）：備品屬設備工程的補料邏輯，
    追料方式和生產用料不同。
    """
    SILICON_WAFER = "SILICON_WAFER"    # 矽晶圓原片（拋光片、磊晶片）
    PHOTORESIST = "PHOTORESIST"        # 光阻
    SPECIALTY_GAS = "SPECIALTY_GAS"    # 特殊氣體
    WET_CHEMICAL = "WET_CHEMICAL"      # 濕式化學品
    TARGET = "TARGET"                  # 濺鍍靶材
    MASK = "MASK"                      # 光罩
    CMP_SLURRY = "CMP_SLURRY"          # 研磨液


class SupplierType(str, Enum):
    WAFER_MAKER = "WAFER_MAKER"
    RESIST_MAKER = "RESIST_MAKER"
    GAS_SUPPLIER = "GAS_SUPPLIER"
    CHEMICAL_SUPPLIER = "CHEMICAL_SUPPLIER"
    TARGET_MAKER = "TARGET_MAKER"
    MASK_SHOP = "MASK_SHOP"
    SLURRY_MAKER = "SLURRY_MAKER"


# 領域假設：一個料別只向一種供應商類型採購 —— 光阻不會去跟氣體廠買。
# 產生資料、建模擬 ERP 的來源清單都用這張表，避免兩邊各寫一份而對不上。
CATEGORY_SUPPLIER_TYPE = {
    MaterialCategory.SILICON_WAFER.value: SupplierType.WAFER_MAKER.value,
    MaterialCategory.PHOTORESIST.value: SupplierType.RESIST_MAKER.value,
    MaterialCategory.SPECIALTY_GAS.value: SupplierType.GAS_SUPPLIER.value,
    MaterialCategory.WET_CHEMICAL.value: SupplierType.CHEMICAL_SUPPLIER.value,
    MaterialCategory.TARGET.value: SupplierType.TARGET_MAKER.value,
    MaterialCategory.MASK.value: SupplierType.MASK_SHOP.value,
    MaterialCategory.CMP_SLURRY.value: SupplierType.SLURRY_MAKER.value,
}

# 畫面與知識卡用中文顯示；企劃問「光阻廠準不準」時，檢索才對得到字。
CATEGORY_LABEL_ZH = {
    "SILICON_WAFER": "矽晶圓", "PHOTORESIST": "光阻", "SPECIALTY_GAS": "特殊氣體",
    "WET_CHEMICAL": "濕式化學品", "TARGET": "靶材", "MASK": "光罩",
    "CMP_SLURRY": "研磨液",
}
SUPPLIER_TYPE_LABEL_ZH = {
    "WAFER_MAKER": "矽晶圓廠", "RESIST_MAKER": "光阻廠", "GAS_SUPPLIER": "特殊氣體廠",
    "CHEMICAL_SUPPLIER": "化學品廠", "TARGET_MAKER": "靶材廠", "MASK_SHOP": "光罩廠",
    "SLURRY_MAKER": "研磨液廠",
}
```

檔頭 docstring 的「採購/生管同仁」改為「採購與物料企劃同仁」。`REASON_CODES["yield"]` 改為 `"製程或品質異常"`。

- [ ] **Step 4: 改 `config.yaml`**：`n_suppliers: 12` → `14`；在 `triage:` 區塊之後加入：

```yaml
# ---------------------------------------------------------------------
# 收貨處理天數（仿 SAP 料號主檔 MARC-WEBAZ）：料到廠後，要幾天才能投產
#   預估缺料天數 = 保守到料日 + 收貨處理天數 − 需求日
#   這裡是各料別的預設值，寫進模擬 ERP 的料號主檔；物料企劃可在工具內
#   逐料號調整（存在 data/planner_settings.db，不寫回 ERP）。
# 領域假設：查不到半導體業依料別的公開天數，以下依到廠作業步驟推得（見決策 17）。
# ---------------------------------------------------------------------
receiving:
  gr_processing_days:
    SILICON_WAFER: 1   # 目視、厚度平坦度量測、微粒掃描
    PHOTORESIST: 2     # 檢驗 1 天＋冷藏品開瓶前需回溫一晚
    SPECIALTY_GAS: 1   # 審鋼瓶 CoA、上機前測漏
    WET_CHEMICAL: 1    # 廠內 ICP-MS 微量金屬分析
    TARGET: 1          # 進料檢驗（上機後預濺鍍屬機台時間，不計）
    MASK: 3            # 光罩檢驗＋上線驗證曝光；把握最低
    CMP_SLURRY: 1      # 大顆粒與粒徑分布檢驗
```

- [ ] **Step 5: 確認新測試通過**。此時其他測試會因 enum 改名而失敗（`generate_data`、`build_erp_db` 等還在用舊值）——**預期**，由 Task 2–4 修復。只跑 `tests/test_world.py`。

- [ ] **Step 6: Commit**：`feat(domain): 世界設定改為晶圓廠：料別、供應商類型與收貨處理天數預設`（body 註明其他模組於後續 Task 跟進，中間狀態部分測試會失敗）。

---

### Task 2: 合成資料產生器（供應商、料號、採購單、信件用語）

**Files:** Modify `src/generate_data.py`；Test `tests/test_world.py`（追加）

- [ ] **Step 1: 追加失敗的測試**（在 `tests/test_world.py` 末尾）：

```python
import random  # noqa: E402

import generate_data  # noqa: E402


def _world():
    random.seed(CFG["data_generation"]["seed"])
    from datetime import date
    as_of = date.fromisoformat(CFG["data_generation"]["as_of_date"])
    sups = generate_data.build_suppliers()
    mats = generate_data.build_materials(CFG["data_generation"]["n_materials"])
    pos = generate_data.build_pos(mats, sups, CFG["data_generation"]["n_purchase_orders"], as_of)
    return sups, mats, pos


def test_po_supplier_type_matches_material_category():
    """光阻單不能掛在氣體廠名下 —— 那是整個故事最容易被一眼看穿的破綻。"""
    sups, mats, pos = _world()
    stype = {s["supplier_id"]: s["supplier_type"] for s in sups}
    cat = {m["material_id"]: m["category"] for m in mats}
    for p in pos:
        assert stype[p["supplier_id"]] == CATEGORY_SUPPLIER_TYPE[cat[p["material_id"]]], p["po_no"]


def test_materials_carry_uom_and_gr_days_from_config():
    _, mats, _ = _world()
    days = CFG["receiving"]["gr_processing_days"]
    for m in mats:
        assert m["base_uom"] in {"PCS", "GAL", "CYL", "DRM"}
        assert m["gr_processing_days"] == days[m["category"]]


def test_all_categories_present():
    _, mats, _ = _world()
    assert {m["category"] for m in mats} == {c.value for c in MaterialCategory}


def test_fourteen_suppliers_with_unique_ids():
    sups, _, _ = _world()
    assert len(sups) == 14 == len({s["supplier_id"] for s in sups})


def test_handcrafted_pos_exist_with_the_email_sender_as_supplier():
    """
    手寫信件的寄件供應商，必須就是那張單在主檔上的供應商。
    對不上時，工具會把信對到別家的單，展示時一眼就穿幫。
    """
    from handcrafted_emails import HANDCRAFTED
    fixed = {p[0]: p for p in generate_data.FIXED_POS}
    for hc in HANDCRAFTED:
        for g in hc["ground_truth"]:
            assert g["po_no"] in fixed, (hc["email_id"], g["po_no"])
            assert fixed[g["po_no"]][2] == hc["supplier_id"], (hc["email_id"], g["po_no"])
```

（最後一個測試要到 Task 3 改完信件才會通過；本 Task 結束時它仍失敗，屬預期。）

- [ ] **Step 2: 確認失敗**。

- [ ] **Step 3: 改 `src/generate_data.py`**
  - import 改為 `from domain import CATEGORY_SUPPLIER_TYPE, MaterialCategory, SupplierType`（兩處 try/except 都改）。
  - `SUPPLIER_SEED` 換成「新世界的資料設計」的 14 家供應商。上方領域假設註解改寫為晶圓廠語境：光阻與特殊氣體的供應集中、交期波動較大；化學品供應商多、替代性高。
  - 料號段落的領域假設註解保留「二源 ≠ 替代料」那段，其餘改為晶圓廠語境；刪除 `WAFER_NODES`、`PRODUCT_CODES` 以外不再使用的常數。新增：

```python
NODES = ["N7", "N12", "N16", "N22", "N28"]
PRODUCT_CODES = ["XR3390", "KL2210", "MT8195", "AB7710", "CD4420",
                 "EF9930", "GH1180", "IJ6650", "KL7720", "MN3310"]

# 領域假設：各料別的標準前置期、計量單位與常見下單量。
#   前置期是「下單到到廠」的合約天數；12 吋矽晶圓與靶材最長，化學品最短。
CATEGORY_SPEC = {
    "SILICON_WAFER": {"lt": (60, 120), "uom": "PCS", "qty": [500, 1000, 1500, 2000, 3000, 5000]},
    "PHOTORESIST":   {"lt": (30, 90),  "uom": "GAL", "qty": [20, 40, 80, 120, 200]},
    "SPECIALTY_GAS": {"lt": (20, 60),  "uom": "CYL", "qty": [10, 20, 40, 60, 100]},
    "WET_CHEMICAL":  {"lt": (10, 30),  "uom": "DRM", "qty": [20, 40, 80, 160]},
    "TARGET":        {"lt": (45, 100), "uom": "PCS", "qty": [2, 4, 8, 12, 20, 30]},
    "MASK":          {"lt": (14, 35),  "uom": "PCS", "qty": [1]},
    "CMP_SLURRY":    {"lt": (20, 50),  "uom": "GAL", "qty": [50, 100, 200, 400]},
}
CATEGORY_WEIGHTS = {"SILICON_WAFER": .25, "PHOTORESIST": .15, "SPECIALTY_GAS": .15,
                    "WET_CHEMICAL": .15, "TARGET": .10, "MASK": .10, "CMP_SLURRY": .10}


def _new_material_id(cat: str) -> str:
    if cat == "SILICON_WAFER":
        return f"SW-300-{random.choice('PE')}-{random.randint(1000, 9999)}"
    if cat == "PHOTORESIST":
        return f"PR-{random.choice(['ArF', 'KrF', 'EUV', 'iLine'])}-{random.randint(1000, 9999)}"
    if cat == "SPECIALTY_GAS":
        return f"GS-{random.choice(['NF3', 'WF6', 'SiH4', 'NH3', 'C4F6'])}-{random.randint(10, 99)}"
    if cat == "WET_CHEMICAL":
        return f"CH-{random.choice(['H2SO4', 'HF', 'H2O2', 'NH4OH', 'IPA'])}-{random.randint(10, 99)}"
    if cat == "TARGET":
        return f"TG-{random.choice(['Ti', 'Ta', 'Cu', 'Co', 'W'])}-{random.randint(10, 99)}"
    if cat == "MASK":
        return f"MSK-{random.choice(NODES)}-{random.choice(PRODUCT_CODES)}"
    return f"SL-{random.choice(['Cu', 'Ox', 'W', 'STI'])}-{random.randint(10, 99)}"
```

  - `build_materials(n)` 改寫：`fixed` 換成「固定料號」表（`(料號, 料別字串, 前置期, 瓶頸, 二源, 替代料)`）；每列加 `base_uom`（取 `CATEGORY_SPEC`）與 `gr_processing_days`（取 `config.yaml` 的 `receiving.gr_processing_days`，由 `_load_config()` 讀一次傳入或在函式內讀）；隨機段用 `CATEGORY_WEIGHTS` 抽料別、`_new_material_id` 產料號、`CATEGORY_SPEC[cat]["lt"]` 抽前置期；**研磨液（只有一家供應商）強制 `has_qualified_second_source=False`**，並加註解說明原因。瓶頸與二源的抽法（約 1/4 瓶頸、非瓶頸者 55% 有二源）保留。`criticality` 的判斷改為 `"high" if bottleneck else ("medium" if lt >= 60 else "low")`。
  - `FIXED_POS` 換成「固定採購單」表。`build_pos` 中補料號的 fallback 改用 `SILICON_WAFER`、`base_uom="PCS"`、`gr_processing_days` 取設定；隨機採購單的供應商類型改為 `CATEGORY_SUPPLIER_TYPE[m["category"]]`，數量改為 `random.choice(CATEGORY_SPEC[m["category"]]["qty"])`；「下游已排定」的領域假設註解改為「已排入投片或機台排程」。
  - `REASON_TEXT_EN`／`REASON_TEXT_ZH`：`capacity` → `"capacity constraint at our plant"`／`"產線產能吃緊"`；`yield` → `"a quality excursion (lot out of spec)"`／`"品質異常（批號規格不符）"`；`upstream_shortage` → `"upstream raw material shortage"`／`"上游原料短缺"`；其餘保留。**`_email_*` 的寫信函式結構與承諾強度邏輯不變。**
  - 檔頭「在晶圓廠做物料企劃時的實際觀察」保留。

- [ ] **Step 4: 確認 `tests/test_world.py` 除最後一個測試外都通過**。
- [ ] **Step 5: Commit**：`feat(data): 合成資料改為晶圓廠：14 家供應商、7 類料、單位與收貨處理天數`。

---

### Task 3: 手寫刁鑽案例改寫

**Files:** Modify `src/handcrafted_emails.py`、`tests/test_llm_layer.py`（只換 ID 字串）

**原則：** 每封的 `email_id`、`received_at`、單號、日期、`ground_truth` 的 `new_eta`／`commitment_strength`／`change_type` 完全不變；只換 `supplier_id`、料號與領域用語。`reason_code` 只在下表註明處調整。

- [ ] **Step 1: 逐封改寫**（`tags` 與註解保留，註解中的「生管」改「物料企劃」，Fabless 用語改掉）：

| 信件 | supplier_id | 主旨 | 內文重點（其餘文字照舊） |
|---|---|---|---|
| HC-001 | SUP-W01 | `RE: PO Confirmation - PO-2026-04417` | `PO-2026-04417 (SW-300-E-3390) 這批磊晶片我們 epi 爐 loading 有點滿，` / `原本 10/15 可能要往後抓個兩週，大概月底前後，我再跟你確認。` / `另外 PO-2026-04452 那批拋光片先照原計畫走沒問題。` |
| HC-002 | SUP-W02 | 不變 | 表格兩列料號改 `SW-300-P-2210`；Remark 改 `Yield excursion at final polish`、`Same ingot lot` |
| HC-003 | SUP-R02 | `Fwd: RE: RE: 光阻交期 PR-ArF-1088` | 轉寄段 `Subject: RE: 光阻交期`；最新段 `因為上游光酸（PAG）原料供應仍然吃緊，` |
| HC-004 | SUP-G02 | 不變 | `PO-2026-04501 這批 NF3 鋼瓶我們提前充填完成，可以在 9/18 出，比原本的 9/30 早。` / `你們氣瓶區放得下嗎？如果可以我們就安排。` / 署名 `Kevin` |
| HC-005 | SUP-C02 | 不變 | `Currently the batch is still queued on our filling line, which is fully loaded.`（取代 `the lot is still at photo stage`）；reason 仍為 capacity |
| HC-006 | SUP-W02 | 不變 | 不變 |
| HC-007 | SUP-M01 | `光罩交期通知 MSK-N7-XR3390` | 不變 |
| HC-008 | SUP-T01 | 不變 | `slipping by about 10 days due to a bonding furnace down event.` |
| HC-009 | SUP-W01 | 不變 | 不變 |
| HC-010 | SUP-T02 | 不變 | `PO-2026-04188 quantity 20 pcs will be` / `split: 8 pcs on the original date 2026-09-30, remaining 12 pcs` |

- [ ] **Step 2**：`tests/test_llm_layer.py` 中 `SUP-F01` → `SUP-W01`、`WF-N6-XR3390` → `SW-300-E-3390`（只換字串，不改斷言）。
- [ ] **Step 3: 跑** `tests/test_world.py tests/test_extraction.py tests/test_llm_layer.py -v`：全部通過。`test_extraction.py` 若有測試失敗，**先確認是否只因用語改變**（例如原因碼的 regex 沒抓到新字），回報 NEEDS_CONTEXT，不要改測試。
- [ ] **Step 4: Commit**：`feat(data): 手寫刁鑽案例改為晶圓廠情境（單號、日期與標準答案不變）`。

---

### Task 4: 模擬 ERP 與資料來源

**Files:** Modify `src/build_erp_db.py`、`src/adapters/base.py`、`src/adapters/sqlite_source.py`、`src/adapters/csv_source.py`（若需要）、`docs/ERP欄位對應.md`；Test `tests/test_adapters.py`（追加）

- [ ] **Step 1: 追加失敗的測試**到 `tests/test_adapters.py`：在 `tmp_path` 內用新產生器產生 CSV 與資料庫（參考既有測試的 fixture 寫法；若既有 fixture 讀的是真實 `data/`，新增一個在 `tmp_path` 重建的 fixture：`monkeypatch` `generate_data.DATA`/`INBOX` 與 `build_erp_db.DATA`/`DB_PATH` 指到 `tmp_path`），斷言：
  1. `SqliteSource(db).materials()` 有 `gr_processing_days`、`base_uom` 欄，且值與 `config.yaml` 的料別預設一致；
  2. 研磨液的 `has_qualified_second_source` 全為 False；
  3. 每個料號的來源清單供應商類型都等於 `CATEGORY_SUPPLIER_TYPE[category]`。

- [ ] **Step 2: 實作**
  - `build_erp_db.py` SCHEMA 的 `material_master` 加兩欄並加註：

```sql
    base_uom           TEXT,     -- ≈ MARA-MEINS 基本計量單位
    gr_processing_days INTEGER,  -- ≈ MARC-WEBAZ 收貨處理時間：到廠後幾天才能投產
```
    `INSERT INTO material_master` 改為 7 欄並寫入這兩個值；`type_of_cat` 刪除，改用 `from domain import CATEGORY_SUPPLIER_TYPE`（依檔案既有的 import 慣例處理路徑）。
  - `sqlite_source.py` 的 `MATERIAL_SQL` 加 `m.base_uom, m.gr_processing_days`；`materials()` 把 `gr_processing_days` 轉 int（缺值補 0 並註明：缺值代表主檔沒維護，視為當天可用）。
  - `adapters/base.py` 的 `MATERIAL_CONTRACT` 加 `gr_processing_days`、`base_uom`；`csv_source.py` 若有欄位轉型一併處理。
  - `docs/ERP欄位對應.md` 的料號主檔對應表加兩列：`base_uom ↔ MARA-MEINS`、`gr_processing_days ↔ MARC-WEBAZ（收貨處理時間，MRP 算可用日時加上）`。
- [ ] **Step 3: 跑** `tests/test_adapters.py tests/test_world.py -v` 通過。
- [ ] **Step 4: Commit**：`feat(erp): 料號主檔加入收貨處理時間（≈MARC-WEBAZ）與計量單位`。

---

### Task 5: 歷史產生器改為新料別

**Files:** Modify `src/generate_history.py`、`tests/test_generate_history.py`

- [ ] **Step 1**：`CATEGORY_RISK` 換成下列並附領域假設註解（光阻與特殊氣體供應集中、化學品替代性高、光罩多為客製短前置期）：

```python
CATEGORY_RISK = {
    "PHOTORESIST": 0.10, "SPECIALTY_GAS": 0.08, "TARGET": 0.06,
    "SILICON_WAFER": 0.05, "CMP_SLURRY": 0.04, "WET_CHEMICAL": 0.03, "MASK": 0.02,
}
```
  `_simulate_outcome` 中 `if category == "SUBSTRATE": base *= 1.4` 改為 `if category == "PHOTORESIST": base *= 1.3`（註解：光阻供應商少、批次長，延遲時拖得更久）。
- [ ] **Step 2**：`SUPPLIER_DRIFT` 換成 `SUP-W03`（2026-03-01 起，+0.25、×1.4，變差）與 `SUP-G01`（2026-04-01 起，−0.22、×0.8，改善）。
- [ ] **Step 3**：`tests/test_generate_history.py` 把 `SUP-S02`→`SUP-W03`、`SUP-F03`→`SUP-G01`、`_simulate_outcome` 的 `category="WAFER"`→`"SILICON_WAFER"`，其餘不變。資料庫層級測試要在 `tmp_path` 用**新世界**重建的資料庫上跑（該檔 fixture 目前複製真實 DB；改為先在 tmp 產生 CSV、建庫、再跑歷史，理由寫進 docstring：真實 DB 要到 Task 10 才重建）。
- [ ] **Step 4: 跑** `tests/test_generate_history.py -v`。**若資料庫層級的方向性測試因樣本不足（每邊 < 15）失敗**：印出兩家供應商各期樣本數，回報 NEEDS_CONTEXT；不可降低門檻。
- [ ] **Step 5: Commit**：`feat(history): 歷史產生器改為晶圓廠料別，漂移供應商改為 SUP-W03 與 SUP-G01`。

---

### Task 6: 企劃設定的收貨處理天數（`planner_settings.py`）

**Files:** Create `src/planner_settings.py`；Test `tests/test_planner_settings.py`

- [ ] **Step 1: 寫失敗的測試**：

```python
# -*- coding: utf-8 -*-
"""
企劃調整收貨處理天數的測試。

守住三件事：調整要有原因與紀錄；工具永不寫回 ERP；沒調整的料號回到料別預設。
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import planner_settings as ps  # noqa: E402


@pytest.fixture
def db(tmp_path):
    return tmp_path / "planner_settings.db"


def test_default_comes_from_material_master_when_not_overridden(db):
    days, source = ps.effective_gr_days("PR-ArF-1088", 2, "PHOTORESIST", ps.load_overrides(db))
    assert days == 2 and "光阻料別預設" in source


def test_override_wins_and_says_who_and_why(db):
    ps.set_override(db, "PR-ArF-1088", 4, "本批需全檢", "王小明", now="2026-09-22 10:00")
    days, source = ps.effective_gr_days("PR-ArF-1088", 2, "PHOTORESIST", ps.load_overrides(db))
    assert days == 4
    assert "王小明" in source and "本批需全檢" in source and "09-22" in source


def test_reason_and_name_are_required(db):
    with pytest.raises(ValueError, match="原因"):
        ps.set_override(db, "PR-ArF-1088", 4, "  ", "王小明")
    with pytest.raises(ValueError, match="姓名"):
        ps.set_override(db, "PR-ArF-1088", 4, "全檢", "")


def test_days_must_be_within_0_to_30(db):
    with pytest.raises(ValueError):
        ps.set_override(db, "PR-ArF-1088", 31, "x", "王小明")
    with pytest.raises(ValueError):
        ps.set_override(db, "PR-ArF-1088", -1, "x", "王小明")


def test_every_change_is_logged_with_old_and_new_value(db):
    ps.set_override(db, "PR-ArF-1088", 4, "全檢", "王小明", default_days=2, now="2026-09-22 10:00")
    ps.set_override(db, "PR-ArF-1088", 3, "改抽檢", "陳大華", default_days=2, now="2026-09-23 09:00")
    ps.clear_override(db, "PR-ArF-1088", "恢復預設", "陳大華", default_days=2, now="2026-09-24 09:00")
    log = ps.change_log(db)
    assert [(r["old_days"], r["new_days"]) for r in log] == [(2, 4), (4, 3), (3, 2)]
    assert ps.load_overrides(db) == {}


def test_never_writes_to_the_erp_database(tmp_path, db):
    """原則 5：工具永不寫回 ERP。設定只能落在工具自己的資料庫。"""
    erp = tmp_path / "erp_sim.db"
    sqlite3.connect(erp).close()
    before = erp.stat().st_mtime_ns
    ps.set_override(db, "PR-ArF-1088", 4, "全檢", "王小明")
    assert erp.stat().st_mtime_ns == before
    assert db.exists() and db != erp
```

- [ ] **Step 2: 確認失敗**。
- [ ] **Step 3: 實作 `src/planner_settings.py`**：

```python
# -*- coding: utf-8 -*-
"""
物料企劃在工具內調整「收貨處理天數」的存取層。

===========================  為什麼不寫回 ERP  ===========================
收貨處理天數在 SAP 是料號主檔的欄位（MARC-WEBAZ），實務上改主檔要走核准流程。
本工具的原則是永不寫回 ERP（決策 11），所以企劃的調整存在工具自己的資料庫
data/planner_settings.db，畫面上標明「工具內設定，尚未同步 ERP 主檔」。

每次調整都留下紀錄（誰、何時、原值、新值、原因），原因必填：
同仁半年後才看得懂「為什麼這顆料特別慢」。

限制：雲端展示環境的檔案不持久，App 休眠或重新部署後會回到預設；
真正上線時應改存公司資料庫。
=========================================================================
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

from domain import CATEGORY_LABEL_ZH

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "planner_settings.db"
MAX_DAYS = 30

_SCHEMA = """
CREATE TABLE IF NOT EXISTS gr_override (
    material_id TEXT PRIMARY KEY, days INTEGER NOT NULL, reason TEXT NOT NULL,
    updated_by TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS gr_override_log (
    log_id INTEGER PRIMARY KEY AUTOINCREMENT, material_id TEXT NOT NULL,
    old_days INTEGER, new_days INTEGER NOT NULL, reason TEXT NOT NULL,
    changed_by TEXT NOT NULL, changed_at TEXT NOT NULL);
"""


def _connect(db: Path | str | None) -> sqlite3.Connection:
    path = Path(db or DEFAULT_DB)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(_SCHEMA)
    return con


def load_overrides(db: Path | str | None = None) -> dict[str, dict]:
    with closing(_connect(db)) as con:
        return {r["material_id"]: dict(r) for r in con.execute("SELECT * FROM gr_override")}


def _validate(days: int, reason: str, user: str) -> tuple[int, str, str]:
    if not str(reason or "").strip():
        raise ValueError("請填寫調整原因")
    if not str(user or "").strip():
        raise ValueError("請填寫姓名")
    d = int(days)
    if not 0 <= d <= MAX_DAYS:
        raise ValueError(f"收貨處理天數須介於 0 到 {MAX_DAYS} 天")
    return d, reason.strip(), user.strip()


def _now(now: str | None) -> str:
    return now or datetime.now().strftime("%Y-%m-%d %H:%M")


def set_override(db, material_id: str, days: int, reason: str, user: str, *,
                 default_days: int | None = None, now: str | None = None) -> None:
    d, reason, user = _validate(days, reason, user)
    ts = _now(now)
    with closing(_connect(db)) as con:
        row = con.execute("SELECT days FROM gr_override WHERE material_id=?",
                          (material_id,)).fetchone()
        old = row["days"] if row else default_days
        con.execute("INSERT OR REPLACE INTO gr_override VALUES (?,?,?,?,?)",
                    (material_id, d, reason, user, ts))
        con.execute("INSERT INTO gr_override_log (material_id, old_days, new_days, reason,"
                    " changed_by, changed_at) VALUES (?,?,?,?,?,?)",
                    (material_id, old, d, reason, user, ts))
        con.commit()


def clear_override(db, material_id: str, reason: str, user: str, *,
                   default_days: int, now: str | None = None) -> None:
    """恢復料別預設。也要留紀錄：拿掉一個調整本身就是一次決定。"""
    d, reason, user = _validate(default_days, reason, user)
    ts = _now(now)
    with closing(_connect(db)) as con:
        row = con.execute("SELECT days FROM gr_override WHERE material_id=?",
                          (material_id,)).fetchone()
        if row is None:
            return
        con.execute("DELETE FROM gr_override WHERE material_id=?", (material_id,))
        con.execute("INSERT INTO gr_override_log (material_id, old_days, new_days, reason,"
                    " changed_by, changed_at) VALUES (?,?,?,?,?,?)",
                    (material_id, row["days"], d, reason, user, ts))
        con.commit()


def change_log(db: Path | str | None = None) -> list[dict]:
    with closing(_connect(db)) as con:
        return [dict(r) for r in con.execute(
            "SELECT * FROM gr_override_log ORDER BY log_id")]


def effective_gr_days(material_id: str, default_days, category: str,
                      overrides: dict[str, dict]) -> tuple[int, str]:
    """回傳 (天數, 來源說明)。來源一定要講出來，企劃才知道這個數字能不能信。"""
    o = overrides.get(material_id)
    if o:
        return int(o["days"]), f"企劃 {o['updated_by']} {o['updated_at'][5:10]} 調整：{o['reason']}"
    try:
        d = int(default_days)
    except (TypeError, ValueError):
        d = 0
    return d, f"{CATEGORY_LABEL_ZH.get(category, category)}料別預設"
```

- [ ] **Step 4: 確認通過**；**Step 5: Commit**：`feat(settings): 企劃可逐料號調整收貨處理天數，原因必填並留紀錄，不寫回 ERP`。

---

### Task 7: 分級加入收貨處理天數，主流程拆出 `retriage()`

**Files:** Modify `src/triage.py`、`src/pipeline.py`；Test `tests/test_triage.py`（追加）、`tests/test_retriage.py`（新增）

- [ ] **Step 1: 追加失敗的測試**到 `tests/test_triage.py`（沿用檔內 `_run`、`_est`、`PO`、`MAT`；`MAT` 預設不含收貨處理天數＝0，既有測試不受影響）：

```python
def test_gr_processing_days_push_arrival_to_usable_date():
    """
    料 10/18 到、需求日 10/20，看似還有 2 天；但光阻到廠要 2 天檢驗＋回溫，
    10/20 才能投產 → 沒有緩衝。不算收貨處理時間會把這張單看得太樂觀。
    """
    r = _run(new_eta="2026-10-18", est=_est(0),
             mat={"gr_processing_days": 2, "gr_source": "光阻料別預設",
                  "has_qualified_second_source": False})
    assert r["gap_days"] == 0 and r["available_date"] == "2026-10-20"
    assert r["priority"] == "P2"   # 單一來源且沒有緩衝
    assert any("收貨處理 2 天" in s and "光阻料別預設" in s for s in r["reasons"])


def test_zero_gr_days_adds_no_reason_line():
    r = _run(est=_est(0), mat={"gr_processing_days": 0, "gr_source": "x"})
    assert not any("收貨處理" in s for s in r["reasons"])


def test_shortage_within_gr_days_suggests_expedited_inspection():
    """缺的天數在收貨處理天數以內 → 請 IQC 優先檢驗就能趕上，這是企劃救得回來的動作。"""
    r = _run(new_eta="2026-10-19", est=_est(0),
             mat={"gr_processing_days": 3, "gr_source": "光罩料別預設"})
    assert r["gap_days"] == 2
    assert any("IQC" in a and "優先" in a for a in r["actions"])


def test_shortage_beyond_gr_days_does_not_suggest_iqc():
    r = _run(new_eta="2026-10-30", est=_est(0),
             mat={"gr_processing_days": 1, "gr_source": "x"})
    assert not any("IQC" in a for a in r["actions"])
```

- [ ] **Step 2: 改 `src/triage.py`**
  - 模組 docstring 公式改為 `預估缺料天數 = 保守到料日 + 收貨處理天數 − 下游需求日`，並說明收貨處理天數仿 SAP MARC-WEBAZ。
  - `evaluate()`：讀 `gr = int(material.get("gr_processing_days") or 0)`（用 `_missing` 防 NaN），`available = conservative + timedelta(days=gr)`，`gap = (available - need).days`；輸出加 `"available_date": available.isoformat()`（`out` 預設 `None`）；第一條理由之後，若 `gr > 0` 加一條 `f"加上收貨處理 {gr} 天（{material.get('gr_source') or '料號主檔'}），可投產日 {available.isoformat()}"`。缺料／緩衝理由裡的日期比較改用可投產日。
  - `suggest_actions()` 加參數 `gr_days`：在「通知生管」之後、「可評估的手段」之前，若 `0 < gap <= gr_days` 加 `f"請品保（IQC）優先安排進料檢驗：收貨處理 {gr_days} 天若能縮短 {gap} 天就趕得上"`。
- [ ] **Step 3: 寫失敗的 `tests/test_retriage.py`**：

```python
# -*- coding: utf-8 -*-
"""
retriage：企劃調整收貨處理天數後，只重算分級，不重跑讀信。

守住的是「調了就生效、而且沒有偷偷呼叫 LLM」。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pipeline  # noqa: E402

TCFG = {"tight_buffer_days": 3, "percentile_confirmed": .8,
        "percentile_estimated": .9, "percentile_intent_only": .95, "min_samples": 20}


def _all():
    base = dict(matched=True, change_type="delay", commitment_strength="confirmed",
                committed_date="2026-10-01", need_date="2026-10-20",
                downstream_scheduled=False, has_second_source=True, is_bottleneck=False,
                alt_material_id="", category="PHOTORESIST", gr_processing_days=0,
                delay_days_est=0, delay_basis="改期過的單", delay_n=40, delay_percentile=.8,
                received_at=pd.Timestamp("2026-09-08"), needs_human_review=False,
                supplier_id="SUP-R01")
    return pd.DataFrame([
        {**base, "po_no": "A", "material_id": "PR-ArF-1088", "new_eta": "2026-10-18"},
        {**base, "po_no": "B", "material_id": "PR-ArF-2000", "new_eta": "2026-10-05"},
    ])


def test_override_changes_priority_without_rerunning_extraction(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("retriage 不可重跑讀信")
    monkeypatch.setattr(pipeline, "extract_one", boom)
    base = pipeline.retriage(_all(), {}, TCFG)
    assert base.set_index("po_no").loc["A", "priority"] == "P3"
    changed = pipeline.retriage(_all(), {"PR-ArF-1088": (5, "企劃 王小明 09-22 調整：全檢")}, TCFG)
    row = changed.set_index("po_no").loc["A"]
    assert row["gap_days"] == 3 and row["priority"] == "P2"
    assert any("王小明" in s for s in row["reasons"])


def test_unmatched_rows_pass_through_untouched():
    df = pd.concat([_all(), pd.DataFrame([{"po_no": "X", "matched": False, "priority": "待查",
                                           "reasons": ["對不到"], "actions": [],
                                           "received_at": pd.Timestamp("2026-09-08")}])])
    out = pipeline.retriage(df, {}, TCFG)
    assert out.set_index("po_no").loc["X", "priority"] == "待查"
```

  （`gr_days_by_material` 的值是 `(天數, 來源說明)`；沒有覆寫的料號，`retriage` 以列上的 `gr_processing_days` 與 `category` 呼叫 `planner_settings.effective_gr_days` 取得「料別預設」說明。`PR-ArF-2000` 的單 gap = 10/05 − 10/20 = −15 → P3，用來確認排序照常。）

- [ ] **Step 4: 改 `src/pipeline.py`**
  - `run()` 在逐筆評估時，除原有欄位外，行資料再寫入 `category`、`gr_processing_days`（來自料號主檔）、`delay_percentile`（`estimate["percentile"]`，無估計時用 `pct`）、`available_date`；並額外保留原始 `estimate` 所需欄位（`delay_days_est`、`delay_basis`、`delay_n` 已有）與 `estimate_available`（布林）、`estimate_reason`（樣本不足時的說明）。
  - 新增 `retriage(all_df, gr_days_by_material, tcfg) -> pd.DataFrame`：對 `matched` 的列重建 `record`／`po`／`material`／`estimate` 四個 dict（`estimate` 由 `estimate_available`、`delay_days_est`、`delay_percentile`、`delay_basis`、`delay_n`、`estimate_reason` 組回；缺這些欄位時視為無估計），`material` 帶入 `gr_processing_days`／`gr_source`（覆寫優先，否則 `planner_settings.effective_gr_days(..., overrides={})` 的料別預設說明），呼叫 `triage.evaluate`，更新 `gap_days`、`conservative_eta`、`available_date`、`priority`、`reasons`、`actions`、`note`，濾掉 `priority == "—"`，最後 `_sort_by_urgency`。未對位列原樣保留。docstring 寫：這支函式存在是為了讓企劃調整天數後立即生效，而不再次呼叫 LLM。
  - `run()` 回傳的 `actions` 改由 `retriage(df, {}, tcfg)` 產生（單一路徑，避免兩套分級邏輯），`stats` 依此計算。**`run()` 呼叫 `triage.evaluate` 的那段改為只負責抽取與對位欄位**，分級一律交給 `retriage`——若這樣改動過大，保留 `run()` 內的評估、但最後再以 `retriage` 覆寫一次，並在 docstring 註明原因；兩者結果必須一致（測試：`run(use_llm=False)["actions"]` 與 `retriage(run(...)["all"], {}, tcfg)` 的 `po_no, priority, gap_days` 相同）。
- [ ] **Step 5: 跑** `tests/test_triage.py tests/test_retriage.py tests/test_pipeline_helpers.py -v`，以及全套（真實資料尚為舊世界，`test_pipeline_triage.py` 等資料相關測試可能因料號改名而 skip 或失敗——若失敗，確認原因僅為「真實資料尚未重建」，於報告中列出，Task 10 會重建）。
- [ ] **Step 6: Commit**：`feat(triage): 缺料天數納入收貨處理天數；拆出 retriage 讓企劃調整立即生效`。

---

### Task 8: 回測納入收貨處理天數

**Files:** Modify `src/backtest.py`、`src/supplier_stats.py`（`load_outcomes` 帶料別）；Test `tests/test_backtest.py`（追加）

- [ ] **Step 1: 追加失敗的測試**：toy 資料加 `category` 欄；`rolling_backtest(..., gr_days_by_category={"PHOTORESIST": 2})` 時：
  - 一張 `receipt = need − 1` 的光阻單：不加收貨處理時「沒缺料」，加了之後 `actual_short` 為 True；
  - `buffer_days` 欄＝`need − committed − gr`；
  - 未提供 `gr_days_by_category` 時行為與現在完全相同（既有測試不變）。
- [ ] **Step 2: 實作**：`supplier_stats.HISTORY_SQL` 已有 `category`（確認）；`rolling_backtest` 加 keyword 參數 `gr_days_by_category: dict | None = None`，逐列取 `gr = gr_days_by_category.get(category, 0)`，`actual_short = receipt + gr > need`，`buffer_days = (need − committed).days − gr`；`run()` 從 `config.yaml` 讀 `receiving.gr_processing_days` 傳入；報告「實際缺料的定義」改為「實際收貨日＋收貨處理天數 > 下游需求日」，並加一行「收貨處理天數用料別預設；歷史沒有企劃的逐料號調整」。
- [ ] **Step 3: 跑** `tests/test_backtest.py -v` 通過。**Step 4: Commit**：`feat(backtest): 實際缺料與排序皆納入收貨處理天數`。

---

### Task 9: 物料檢索、提示詞與介面

**Files:** Modify `src/rag/retriever.py`、`src/rag/knowledge.py`、`src/rag/eval_questions.py`、`src/rag/answer.py`（若需）、`src/llm/prompts/extract_eta.md`、`src/llm/prompts/rag_answer.md`、`app.py`、`tests/test_rag.py`

- [ ] **Step 1: 識別碼**：`retriever.ID_PATTERNS` 的三條料號樣式（WF／MSK、SUB-FCCSP、ASM）換成：

```python
    re.compile(r"\bSW-300-[PE]-\d{4}\b", re.IGNORECASE),
    re.compile(r"\bPR-(?:ArF|KrF|EUV|iLine)-\d{4}\b", re.IGNORECASE),
    re.compile(r"\b(?:GS|CH|TG|SL)-[A-Za-z0-9]{1,6}-\d{2}\b", re.IGNORECASE),
    re.compile(r"\bMSK-N\d{1,2}-[A-Z]{2}\d{4}\b", re.IGNORECASE),
```
  確認 `extract_ids` 與卡片 ID 的大小寫比對方式（例如 `PR-ArF-1088` 混合大小寫）；若 `extract_ids` 會轉大寫而卡片 ID 保留原樣，改為大小寫不敏感比對並加測試。
- [ ] **Step 2: 知識卡**：供應商卡「供應商類型：光阻廠（RESIST_MAKER）」；料號卡「料件類別：光阻（PHOTORESIST）」、加「計量單位：GAL」與「收貨處理天數（料號主檔預設）：2 天」；`otd_by_supplier_type` 摘要卡的類型名稱改用中文加代碼。中文名稱一律取 `domain.CATEGORY_LABEL_ZH`／`SUPPLIER_TYPE_LABEL_ZH`。
- [ ] **Step 3: 評估題目**（`eval_questions.py`；docstring「用生管平常講話的方式寫」改「用物料企劃平常講話的方式寫」）：
  - A 類：`PO-2026-04205 目前承諾哪天交？`（不變）；`PO-2026-04417 投片排程排好了沒` → `PO:PO-2026-04417`；`PR-ArF-1088 這顆料有沒有第二家可以買` → `MAT:PR-ArF-1088`；`SW-300-E-3390 算不算瓶頸料` → `MAT:SW-300-E-3390`；`SUP-W03 過去交貨準不準` → `SUP:SUP-W03`；`PO-2026-04278 這張單的供應商靠得住嗎？` → `SUP:SUP-T01`。
  - B 類：`載板廠整體跟晶圓代工比起來誰比較準時` → `光阻廠整體跟矽晶圓廠比起來誰比較準時`（`SUM:otd_by_supplier_type`）。其餘不變。
  - C 類：`封裝測試廠的交期靠得住嗎` → `特殊氣體廠的交期靠得住嗎`，可接受 `SUM:otd_by_supplier_type`、`SUM:supplier_otd_ranking`、`SUP:SUP-G01`、`SUP:SUP-G02`；`做光罩的廠商表現怎樣` 不變；`哪家做載板的最讓人擔心` → `哪家做光阻的最讓人擔心`，可接受 `SUM:supplier_otd_ranking` 與**實際資料中準交率最差的光阻廠**（Task 10 重建資料後以 `supplier_stats.supplier_performance()` 確認是 SUP-R01 或 SUP-R02，填入正確的那家；標準答案必須能從資料還原）。先填 `SUP:SUP-R02`，Task 10 核對。
- [ ] **Step 4: 提示詞**：`extract_eta.md` 的「生管」全部改「物料企劃」（第 7 行改為「領域同仁（物料企劃／採購）」）；`rag_answer.md` 第 7 行改為「你是晶圓廠供應鏈部門的內部查詢助理，服務對象是物料企劃與採購同仁。」提示詞裡的範例單號 `PO-2026-04417` 保留。
- [ ] **Step 5: 介面**（`app.py`，只做本計畫需要的部分）：
  - 檢索輸入框 placeholder 改 `例：PR-ArF-1088 有沒有第二家可以買？`。
  - 行動清單表格在「保守到料日」後加「可投產日」（`available_date`）。
  - 資料流程：`actions = pipeline.retriage(result["all"], gr_map, cfg["triage"])`，其中 `gr_map` 由 `planner_settings.load_overrides()` 與料號主檔組成 `{material_id: (days, source)}`（只放有覆寫的料號即可，`retriage` 會替其餘料號補預設說明）。
  - 新分頁「🛠 收貨處理天數」（放在「⚖️ 供應商歷史」之後）：
    1. 說明文字：「料到廠後要幾天才能投產（進料檢驗、入庫、光阻回溫等）。預設值依料別，你可以依實際情況逐料號調整；調整會立刻反映在行動清單。**這是工具內的設定，不會寫回 ERP 料號主檔**；雲端展示環境重新啟動後會回到預設。」
    2. 表格：料號、料別（中文）、主檔預設天數、目前使用天數、來源。
    3. 表單（`st.form`）：選料號（`selectbox`，可搜尋）、天數（`number_input` 0–30）、原因（必填）、姓名（必填）→ 送出後呼叫 `planner_settings.set_override(..., default_days=主檔值)`，成功 `st.success` 並 `st.rerun()`；`ValueError` 以 `st.error` 顯示。
    4. 已調整的料號可「恢復預設」（同樣要填原因與姓名）。
    5. 調整紀錄表（`change_log`，新到舊）。
- [ ] **Step 6**：`tests/test_rag.py` 的料號、供應商代號依本計畫的對應換掉（`SUB-FCCSP-1088`→`PR-ArF-1088`、`WF-N6-XR3390`→`SW-300-E-3390`、`SUP-F03`→`SUP-W03`、`SUP-S01`→`SUP-R02`、`SUP-F02`→`SUP-W02`，PO 單號不變），只換字串、不改斷言邏輯。
- [ ] **Step 7: 跑** `tests/test_rag.py -v`（`LLM_PROVIDER=none`）。**Step 8: Commit**：`feat(ui): 新增收貨處理天數設定分頁；檢索與提示詞改為晶圓廠與物料企劃用語`。

---

### Task 10: 重建資料、驗證、回測與設計決策 17（離線）

**Files:** `output/回測結果.md`（重新產生）、`docs/設計決策.md`

- [ ] **Step 1: 依序重建真實資料**（`LLM_PROVIDER=none`）：刪除 `data/erp_sim.db`、`data/*.csv`、`data/inbox/`、`data/planner_settings.db` 後執行 `generate_data.py` → `build_erp_db.py` → `generate_history.py`，貼出每一步的輸出。
- [ ] **Step 2: 世界檢查**：印出各料別料號數、各供應商未結單數與歷史單數、SUP-W03／SUP-G01 漂移前後延遲比例；確認準交率最差的光阻廠，核對 Task 9 評估題目的標準答案（不對就改題目的可接受卡片，並在報告註明）。
- [ ] **Step 3: 全部測試** `LLM_PROVIDER=none py -X utf8 -m pytest tests -q` 全過，記下實際數量。
- [ ] **Step 4: 主流程**：`LLM_PROVIDER=none py -X utf8 src/pipeline.py`，把統計與前 6 筆（含理由與動作）貼給使用者檢查；至少應看到一筆理由含「收貨處理」。
- [ ] **Step 5: 回測**：`LLM_PROVIDER=none py -X utf8 src/backtest.py`，原樣呈現結果（沒贏就寫沒贏，不調參數）。
- [ ] **Step 6: App 離線冒煙測試**：AppTest 無例外；另用 AppTest 在「收貨處理天數」分頁送出一次調整（姓名、原因、天數），確認行動清單對應料號的可投產日改變、紀錄表多一列；測試結束刪除 `data/planner_settings.db`。
- [ ] **Step 7: 設計決策 17**（插在「已知限制」之前）：情境由 Fabless 改晶圓廠的原因；收貨處理天數的設計（仿 SAP MARC-WEBAZ／Oracle 後處理前置時間）、各料別預設與依據、把握程度（光罩最低）；企劃逐料號調整的設計（原因必填、紀錄、不寫回 ERP、雲端不持久）；被否決的方案：全公司單一天數、依料別寫死不可調、由工具直接改 ERP 主檔、為各料別編造更精細的天數。已知限制第 4 點保留，另把決策 16 限制中「收貨處理時間尚未納入」一句改為「已於決策 17 納入」。
- [ ] **Step 8: Commit**：`docs: 新增決策 17；以晶圓廠世界重新產生回測結果`（只 add `output/回測結果.md`、`docs/設計決策.md`）。

---

### Task 11: 重跑評估與種子快取（會用到 Gemini 額度，**執行前先問使用者**）

**先停下來問使用者**：「接下來要重跑解析評估、檢索評估，並重新匯出種子快取，會用到 Gemini 免費額度（解析約 25 封信、知識卡向量約 350 張、檢索評估 24 題 × 6 種配置、範例問答 6 題）。可以開始嗎？」取得同意後才執行。

- [ ] **Step 1**：`py -X utf8 src/evaluate.py`，檢查 `output/實驗結果.md`；若 LLM 欄位大面積掛零，**先看原始錯誤**（HTTP 429 配額 vs 模型下線 404），不要重試灌額度。
- [ ] **Step 2**：`py -X utf8 src/rag/evaluate_rag.py`，檢查 `output/RAG檢索評估.md`；結果若推翻先前結論（例如語意＋釘選不再最好），如實記錄。
- [ ] **Step 3**：`py -X utf8 scripts/export_demo_cache.py` 更新 `demo_cache/`。
- [ ] **Step 4: 種子快取驗證（網路呼叫必須為 0）**：依 CLAUDE.md 的方法，在短路徑 `C:/Users/User/AppData/Local/Temp/claude/cs` 建立不含 `.cache/`、`data/` 的乾淨副本，把 `requests.post/get` 換成會拋錯的函式，跑主流程＋語意索引＋範例問答。
- [ ] **Step 5: Commit**：`chore: 以晶圓廠世界重跑評估並更新部署用種子快取`（`output/*.md`、`demo_cache/`）。README 表格同步留給 Plan 3，commit body 註明。
- [ ] **Step 6: 推上 GitHub**（`feat/planner-triage`），告訴使用者：可以把 Streamlit Cloud 的部署分支改成 `feat/planner-triage` 來看新版。

---

## 自我檢查

| 已確認的需求 | 任務 |
|---|---|
| 世界設定改為晶圓廠（料別、供應商、信件） | 1、2、3、5 |
| 收貨處理天數依料別預設（查證後的數字） | 1、2、4 |
| 企劃可逐料號調整、原因必填、有紀錄、不寫回 ERP | 6、9 |
| 缺料天數與回測都納入收貨處理天數 | 7、8 |
| 缺料在收貨處理天數內 → 建議 IQC 優先檢驗 | 7 |
| 提示詞用語統一（避免之後再重跑一次評估） | 9 |
| 用到 API 前先問使用者 | 11 |

**型別一致性**：`effective_gr_days` 回傳 `(int, str)`；`retriage` 的 `gr_days_by_material` 值為同一型別；`triage.evaluate` 讀 `material["gr_processing_days"]`、`material["gr_source"]`，輸出新增 `available_date`；`rolling_backtest(..., gr_days_by_category=...)` 為 keyword 參數，預設行為不變。

**已知風險**：
- Task 1 之後到 Task 4 之前，部分既有測試會因 enum 改名而失敗（中間狀態），每個 Task 的 commit body 需註明。
- 漂移供應商換人後，資料庫層級的方向性測試可能樣本不足（Task 5 已寫處理方式：回報，不降門檻）。
- 規則層的原因碼 regex 可能抓不到新用語，解析評估數字會變；這是改寫信件的真實代價，如實記錄。
