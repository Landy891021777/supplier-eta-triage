# 物料企劃介面（Plan 2）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 介面分成「企劃工作區」與「專案說明」兩區，並加上物料企劃每天真的會用的三件事：確認交期後留下紀錄並立即生效、匯出明日追料清單（Excel）、供應商月度績效表。

**Architecture:** `app.py` 改成導覽外殼（`st.navigation` 分組），每個頁面一個檔案放在 `views/`，共用的資料載入與分級放在 `ui_state.py`。企劃確認交期存進工具自己的 `data/planner_settings.db`（新表），由 `pipeline.retriage()` 套用，**永不寫回 ERP**。匯出與月度績效是純函式，放 `src/exports.py`，可單獨測試。

**Tech Stack:** Python 3.13、Streamlit 1.63（`st.navigation`／`st.Page`）、pandas、openpyxl、pytest。**全程離線，不用 Gemini 額度。**

---

## 已確認的決定

- 使用者是晶圓廠**物料企劃**；企劃工作區只放企劃每天用的頁面，評估與效益放「專案說明」給面試官看。
- 企劃確認紀錄：對需人工確認的單（暫估、僅意向、沒給日期），企劃向供應商要到確切日期後，在工具裡登錄「確認後的日期、備註、姓名」。登錄後該單以確認日期重算分級、不再標需人工確認，理由第一條寫明誰在哪天確認；畫面標明「尚未寫回 ERP，請依公司流程更新交貨排程行」。
- 匯出明日追料清單：Excel，含 P1、P2、待查，欄位見 Task 2。
- 供應商月度績效：依**承諾月份**統計每家供應商的交貨筆數、準交率、改期比例、延遲時中位數、P80 延遲；該月樣本少於 5 筆標「樣本不足」、不給比例。品質與配合度沒有資料，不做。
- 全站「生管」改稱「物料企劃」；建議動作裡的「通知生管」保留（那是真的要通知生管）。

## 不可動的東西（否則要重跑評估、會用到 Gemini 額度）

- `src/llm/prompts/`、`src/handcrafted_emails.py`、`src/generate_data.py` 的信件內容、`src/draft.py` 的 PROMPT。
  （手寫信件裡「我們生管確認過」是**供應商那邊的生管**，本來就該保留。）
- `docs/操作指引.md`、`docs/設計決策.md`、`docs/ERP欄位對應.md`、`src/rag/knowledge.py`、`src/rag/eval_questions.py`、`src/rag/answer.py` 的 `EXAMPLE_QUESTIONS`。
  操作指引已經提到的名稱照舊沿用：「今日行動清單」「🔎 物料智能檢索」「🛠 收貨處理天數」「⬇️ 匯出行動清單 CSV」「產生回信草稿」。新功能的說明在 Plan 3 重跑評估時補進操作指引。
- 例外：`docs/設計決策.md` 在 Task 6 新增決策 18，這會改變知識卡；**Task 6 不重跑評估**，Plan 3 收尾時一次重跑（屆時分批交貨也會改提示詞）。

## 檔案結構

| 檔案 | 動作 | 責任 |
|---|---|---|
| `src/planner_settings.py` | 修改 | 新增企劃確認交期的存取與紀錄 |
| `src/pipeline.py` | 修改 | `retriage()` 套用企劃確認 |
| `src/exports.py` | 新增 | 明日追料清單 Excel、供應商月度績效（純函式） |
| `ui_state.py` | 新增 | 共用：確保資料、快取載入主流程、套用覆寫與確認後的清單、側邊欄 |
| `views/actions.py` | 新增 | 📋 今日行動清單（含確認表單、兩種匯出） |
| `views/search.py` | 新增 | 🔎 物料智能檢索（搬移） |
| `views/suppliers.py` | 新增 | 🏭 供應商績效（歷史統計＋月度績效＋匯出） |
| `views/receiving.py` | 新增 | 🛠 收貨處理天數（搬移） |
| `views/erp.py` | 新增 | 📄 ERP 單據（搬移） |
| `views/emails.py` | 新增 | 📨 信件與解析軌跡（原「原始信件」＋「解析軌跡」合併） |
| `views/about.py` | 新增 | 📖 專案簡介與三步導覽 |
| `views/validation.py` | 新增 | 🧪 方法驗證（回測＋解析評估＋檢索評估） |
| `views/benefit.py` | 新增 | 📊 效益估算（搬移，K 參數移到頁內） |
| `app.py` | 改寫 | 導覽外殼 |
| `tests/test_confirmations.py`、`tests/test_exports.py`、`tests/test_app_pages.py` | 新增 | |

**命名注意**：頁面資料夾叫 `views/`，不可叫 `pages/`（Streamlit 會把 `pages/` 當成舊式多頁自動載入，和 `st.navigation` 衝突）。`ui_state.py` 放在專案根目錄（與 `app.py` 同層）。

## 通用規則

- `py -X utf8`；utf-8；**一律 `LLM_PROVIDER=none`**；輸出出現 gemini、embedding、429 立刻停。
- 註解寫「為什麼」，繁體中文；領域假設標 `# 領域假設：`；測試 docstring 寫「錯了會怎樣」。
- Commit：中文 conventional commits，結尾 `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`；只 add 本 Task 的檔案。
- 不重建 `data/`（目前已是晶圓廠世界）。測試若會寫 `data/planner_settings.db`，一律改用 `tmp_path`，測完不可殘留。
- 本機 port 8512 可能有使用者在看的預覽，不要關。

---

### Task 1: 企劃確認交期（儲存與套用）

**Files:** Modify `src/planner_settings.py`、`src/pipeline.py`；Test `tests/test_confirmations.py`

- [ ] **Step 1: 寫失敗的測試** `tests/test_confirmations.py`：

```python
# -*- coding: utf-8 -*-
"""
企劃確認交期的測試。

守住：確認要有日期與姓名；確認後分級立即改用確認日期；
供應商之後又來新信時，舊的確認不能蓋掉新資訊；永不寫回 ERP。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pipeline  # noqa: E402
import planner_settings as ps  # noqa: E402

TCFG = {"tight_buffer_days": 3, "percentile_confirmed": .8,
        "percentile_estimated": .9, "percentile_intent_only": .95, "min_samples": 20}


@pytest.fixture
def db(tmp_path):
    return tmp_path / "planner_settings.db"


def _row(**kw):
    base = dict(po_no="A", email_id="E1", matched=True, change_type="delay",
                commitment_strength="intent_only", new_eta="2026-10-10",
                committed_date="2026-10-01", need_date="2026-10-20",
                downstream_scheduled=False, has_second_source=True, is_bottleneck=False,
                alt_material_id="", material_id="PR-ArF-1088", category="PHOTORESIST",
                gr_processing_days=0, delay_days_est=0, delay_basis="改期過的單",
                delay_n=40, delay_percentile=.95, estimate_available=True,
                estimate_reason="", needs_human_review=True, supplier_id="SUP-R01",
                received_at=pd.Timestamp("2026-09-08"))
    base.update(kw)
    return base


def test_confirmation_requires_date_and_name(db):
    with pytest.raises(ValueError, match="日期"):
        ps.confirm_eta(db, "A", "E1", "", "電話確認", "王小明")
    with pytest.raises(ValueError, match="姓名"):
        ps.confirm_eta(db, "A", "E1", "2026-10-25", "電話確認", " ")


def test_confirmed_date_is_used_and_review_flag_cleared(db):
    ps.confirm_eta(db, "A", "E1", "2026-10-25", "電話確認", "王小明", now="2026-09-22 10:00")
    out = pipeline.retriage(pd.DataFrame([_row()]), {}, TCFG,
                            confirmations=ps.load_confirmations(db))
    r = out.set_index("po_no").loc["A"]
    assert r["new_eta"] == "2026-10-25" and r["commitment_strength"] == "confirmed"
    assert not r["needs_human_review"]
    assert "王小明" in r["reasons"][0] and "尚未寫回 ERP" in r["reasons"][0]
    assert r["gap_days"] == 5          # 10/25 + 延遲估計 0 − 需求日 10/20


def test_newer_supplier_email_supersedes_old_confirmation(db):
    """
    企劃 9/22 依 E1 確認了 10/25；隔天供應商又寄 E2 說要延到 11/10。
    若舊確認蓋掉新信，工具會把一個已經作廢的日期當真。
    """
    ps.confirm_eta(db, "A", "E1", "2026-10-25", "電話確認", "王小明")
    out = pipeline.retriage(pd.DataFrame([_row(email_id="E2", new_eta="2026-11-10")]), {},
                            TCFG, confirmations=ps.load_confirmations(db))
    r = out.set_index("po_no").loc["A"]
    assert r["new_eta"] == "2026-11-10" and r["needs_human_review"]
    assert not any("王小明" in s for s in r["reasons"])


def test_confirmation_log_keeps_every_entry(db):
    ps.confirm_eta(db, "A", "E1", "2026-10-25", "電話", "王小明", now="2026-09-22 10:00")
    ps.confirm_eta(db, "A", "E1", "2026-10-28", "改口", "王小明", now="2026-09-22 15:00")
    assert [c["confirmed_date"] for c in ps.confirmation_log(db)] == ["2026-10-25", "2026-10-28"]
    assert ps.load_confirmations(db)["A"]["confirmed_date"] == "2026-10-28"


def test_no_confirmations_changes_nothing():
    df = pd.DataFrame([_row()])
    a = pipeline.retriage(df, {}, TCFG)
    b = pipeline.retriage(df, {}, TCFG, confirmations={})
    assert a[["po_no", "priority", "gap_days"]].equals(b[["po_no", "priority", "gap_days"]])
```

- [ ] **Step 2: 確認失敗**。
- [ ] **Step 3: 實作**
  - `planner_settings.py`：`_SCHEMA` 加入

```sql
CREATE TABLE IF NOT EXISTS eta_confirmation (
    confirm_id INTEGER PRIMARY KEY AUTOINCREMENT, po_no TEXT NOT NULL,
    email_id TEXT NOT NULL, confirmed_date TEXT NOT NULL, note TEXT,
    confirmed_by TEXT NOT NULL, confirmed_at TEXT NOT NULL);
```
    新增 `confirm_eta(db, po_no, email_id, confirmed_date, note, user, *, now=None)`：日期用 `date.fromisoformat` 驗證（空或格式錯 → `ValueError("請填寫確認後的交期日期（YYYY-MM-DD）")`），姓名必填（沿用既有訊息「請填寫姓名」），只 INSERT（保留完整紀錄）。
    `load_confirmations(db=None) -> dict[po_no, dict]`：每張單取 `confirm_id` 最大的一筆。`confirmation_log(db=None) -> list[dict]` 依 `confirm_id` 排序。模組 docstring 補一段：確認紀錄與收貨處理天數一樣存在工具自己的資料庫，不寫回 ERP；它記錄的是「企劃向供應商要到的確切日期」，這是 ERP 結構上不會有的資料（誰、何時、怎麼確認的）。
  - `pipeline.retriage(all_df, gr_days_by_material, tcfg, confirmations=None)`：對 matched 列，若 `confirmations` 有這張單**且 `email_id` 相同**，則在重建 `record` 前把 `new_eta` 換成確認日期、`commitment_strength` 設為 `"confirmed"`；`change_type` 依確認日期與 `committed_date` 重新判定（晚於→delay、早於→pull_in、相同→no_change，與 `run()` 的規則一致，抽成共用小函式）；估計改用「已確認」的百分位（`percentile_for("confirmed")`）。**`retriage` 手上沒有歷史資料，無法重算估計**，所以 `run()` 要一次把三個百分位的估計都存進每一列：`delay_days_p80`、`delay_days_p90`、`delay_days_p95`（各自呼叫 `estimate_delay`；樣本不足時為 NaN），`retriage` 依要用的百分位取對應欄；這些欄位不存在時（舊格式或測試資料）退回 `delay_days_est`。評估後把 `needs_human_review` 設為 False，並在 `reasons` 最前面插入 `f"企劃 {by} {at[5:10]} 向供應商確認交期 {date}（{note}）；尚未寫回 ERP，請依公司流程更新交貨排程行"`（note 空白時省略括號）。輸出的列也要帶更新後的 `new_eta`、`commitment_strength`、`needs_human_review`。docstring 寫明 email_id 必須相同的原因（新信優先）。
- [ ] **Step 4: 通過**；全套測試通過。
- [ ] **Step 5: Commit**：`feat(confirm): 企劃確認交期後留下紀錄並立即以確認日期重算，不寫回 ERP`。

---

### Task 2: 匯出與月度績效（`src/exports.py`）

**Files:** Create `src/exports.py`；Test `tests/test_exports.py`

- [ ] **Step 1: 寫失敗的測試** `tests/test_exports.py`：

```python
# -*- coding: utf-8 -*-
"""匯出與月度績效的測試：企劃拿去開會的東西，數字與欄位不能錯。"""
from __future__ import annotations

import io
import sys
from pathlib import Path

import pandas as pd
from openpyxl import load_workbook

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import exports  # noqa: E402


def _actions():
    common = dict(material_id="PR-ArF-1088", category="PHOTORESIST", supplier_name="Resist-Echo",
                  qty=80, base_uom="GAL", new_eta="2026-10-10", conservative_eta="2026-10-20",
                  available_date="2026-10-22", need_date="2026-10-15",
                  commitment_strength="estimated", needs_human_review=True)
    return pd.DataFrame([
        {**common, "priority": "P1", "gap_days": 7.0, "po_no": "A",
         "reasons": ["r1", "r2"], "actions": ["a1"]},
        {**common, "priority": "P3", "gap_days": -9.0, "po_no": "B",
         "reasons": ["r"], "actions": []},
        {**common, "priority": "待查", "gap_days": float("nan"), "po_no": "C",
         "reasons": ["讀不出"], "actions": []},
    ])


def test_followup_workbook_keeps_p1_p2_and_pending_only():
    wb = load_workbook(io.BytesIO(exports.followup_workbook(_actions(), "2026-09-08")))
    ws = wb["追料清單"]
    header = [c.value for c in ws[1]]
    assert header[:4] == ["優先級", "預估缺料天數", "採購單號", "料號"]
    pos = [r[header.index("採購單號")] for r in ws.iter_rows(min_row=2, values_only=True)]
    assert pos == ["A", "C"]
    assert "說明" in wb.sheetnames


def test_followup_workbook_writes_integers_and_joined_text():
    """Excel 裡出現 7.0、nan 或 ['a1'] 這種字，企劃拿去開會會被笑。"""
    wb = load_workbook(io.BytesIO(exports.followup_workbook(_actions(), "2026-09-08")))
    ws = wb["追料清單"]
    header = [c.value for c in ws[1]]
    rows = list(ws.iter_rows(min_row=2, values_only=True))
    assert rows[0][header.index("預估缺料天數")] == 7
    assert rows[1][header.index("預估缺料天數")] in (None, "")
    assert rows[0][header.index("建議動作")] == "a1"
    assert rows[0][header.index("數量")] == "80 GAL"


def _outcomes():
    rows = []
    for i in range(6):   # 2026-05：6 筆，2 筆延遲
        rows.append(dict(supplier_id="S1", committed_date=f"2026-05-{i+1:02d}",
                         delay_days=5 if i < 2 else 0, reschedule_count=1 if i < 3 else 0))
    for i in range(3):   # 2026-06：3 筆 → 樣本不足
        rows.append(dict(supplier_id="S1", committed_date=f"2026-06-{i+1:02d}",
                         delay_days=0, reschedule_count=0))
    return pd.DataFrame(rows)


def test_monthly_performance_counts_and_rates():
    m = exports.supplier_monthly(_outcomes()).set_index(["供應商", "承諾月份"])
    may = m.loc[("S1", "2026-05")]
    assert may["交貨筆數"] == 6
    assert abs(may["準交率"] - 4 / 6) < 1e-9
    assert abs(may["改期比例"] - 0.5) < 1e-9
    assert may["延遲時中位數(天)"] == 5


def test_monthly_performance_refuses_small_samples():
    """只有 3 筆的月份算出「準交率 100%」會誤導評核，寧可標樣本不足。"""
    jun = exports.supplier_monthly(_outcomes()).set_index(["供應商", "承諾月份"]).loc[("S1", "2026-06")]
    assert jun["樣本"] == "樣本不足" and pd.isna(jun["準交率"])
```

- [ ] **Step 2: 確認失敗**。
- [ ] **Step 3: 實作 `src/exports.py`**：
  - `FOLLOWUP_COLUMNS`：`優先級、預估缺料天數、採購單號、料號、料別、供應商、數量、新交期、保守到料日、可投產日、需求日、承諾強度、需人工確認、建議動作、理由`。料別用 `domain.CATEGORY_LABEL_ZH`；數量＝`f"{qty} {base_uom}"`（缺單位只寫數量）；缺料天數寫整數、NaN 留空；建議動作與理由以「；」串接（非 list 時為空）；需人工確認寫「是／否」。只保留 `priority in ("P1", "P2", "待查")`，順序沿用輸入（已排序）。
  - `followup_workbook(actions, as_of) -> bytes`：用 `pd.ExcelWriter(buf, engine="openpyxl")` 寫兩個工作表：「追料清單」與「說明」（產生時間、模擬基準日 `as_of`、缺料天數公式、「合成資料，僅供展示」、「工具不寫回 ERP」）。欄寬依內容粗略設定（`ws.column_dimensions[...].width`），首列凍結。
  - `MIN_MONTHLY_SAMPLES = 5`，註解：一個月只有幾筆時比例跳動極大，評核會誤導。
  - `supplier_monthly(outcomes) -> DataFrame`：輸入 `supplier_stats.load_outcomes()` 的欄位（至少 supplier_id、committed_date、delay_days、reschedule_count；有 supplier_name 就帶上）。依 `承諾月份`（`committed_date[:7]`）與供應商分組：`交貨筆數`、`準交率`（delay_days ≤ 0 的比例）、`改期比例`（reschedule_count ≥ 1）、`延遲時中位數(天)`、`P80 延遲(天)`（`quantile(0.8, interpolation="higher")`，與 `supplier_stats` 一致）、`樣本`（筆數 < 5 → "樣本不足"，否則 "足夠"）；樣本不足時四個比例／天數欄設為 NaN。欄名 `供應商` 放 supplier_id（有名稱則另加 `供應商名稱`）。依供應商、月份排序。
  - `monthly_workbook(monthly) -> bytes`：一個工作表「月度績效」，比例欄以百分比格式輸出（`number_format = "0.0%"`）。
  - 模組 docstring：月度績效只有交期面（準交率、改期）；品質與配合度沒有資料，刻意不做，不編造。
- [ ] **Step 4: 通過**；**Step 5: Commit**：`feat(exports): 明日追料清單 Excel 與供應商月度績效`。

---

### Task 3: 介面改成兩區導覽（只搬移，不改行為）

**Files:** Create `ui_state.py`、`views/*.py`（除 `about.py`、`validation.py` 之外的頁面）；Modify `app.py`；Test `tests/test_app_pages.py`

**原則：這一步只重新組織，畫面內容與行為和現在一樣**（評估與效益暫時各自成頁，Task 5 再整理）。方便審查時逐頁比對。

- [ ] **Step 1: 寫失敗的測試** `tests/test_app_pages.py`：

```python
# -*- coding: utf-8 -*-
"""
每一頁都要能在離線、沒有金鑰的情況下打開。

頁面拆成檔案後，最常見的壞法是某頁少 import 一個東西，只有點到那頁才會爆。
"""
from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parent.parent
PAGES = sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / "views").glob("*.py")
               if p.name != "__init__.py")


@pytest.fixture(autouse=True)
def _offline(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_PROVIDER", "none")
    import sys
    sys.path.insert(0, str(ROOT / "src"))
    import planner_settings
    monkeypatch.setattr(planner_settings, "DEFAULT_DB", tmp_path / "ps.db")


def test_home_page_runs():
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=300).run()
    assert not at.exception, at.exception


@pytest.mark.parametrize("page", PAGES)
def test_every_page_runs(page):
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=300).run()
    at.switch_page(page).run()
    assert not at.exception, (page, at.exception)
```

（`switch_page` 的路徑格式請以實際 Streamlit 版本為準；若 `st.navigation` 下 `AppTest.switch_page` 不支援，改為對每個頁面檔直接 `AppTest.from_file("views/xxx.py")`，頁面檔開頭需能獨立執行——即頁面檔自行呼叫 `ui_state` 取得資料，不依賴 `app.py` 先執行。兩種做法擇一，並在報告說明。）

- [ ] **Step 2: 實作**
  - `ui_state.py`：搬移 `_ensure_data`、`load_pipeline`（`@st.cache_data`）、`_load_retriever`、`_llm_budget_left`、`_supplier_performance`、`_reschedule_reliability`、`_materials_df`、`_fmt_gap`、`PRIORITY_COLOR` 等共用函式；新增 `sidebar()`（原側邊欄：LLM 狀態、開關、資料來源；**K 參數不放這裡**，Task 5 移到效益頁；本 Task 先暫留在效益頁內）與 `context()`：確保資料 → 讀 `use_llm`（存在 `st.session_state`）→ 載入主流程 → 讀覆寫（`load_overrides`）→ `retriage`（Task 1 完成後加上 `confirmations=load_confirmations()`）→ 回傳 `dict(result=..., actions=..., stats=..., cfg=..., provider=...)`。
  - `app.py`：只剩頁面設定、`sidebar()`、`st.navigation({...})`、`.run()`：

```python
pg = st.navigation({
    "企劃工作區": [
        st.Page("views/actions.py", title="今日行動清單", icon="📋", default=True),
        st.Page("views/search.py", title="物料智能檢索", icon="🔎"),
        st.Page("views/suppliers.py", title="供應商歷史", icon="⚖️"),
        st.Page("views/receiving.py", title="收貨處理天數", icon="🛠"),
        st.Page("views/erp.py", title="ERP 單據", icon="📄"),
        st.Page("views/emails.py", title="信件與解析軌跡", icon="📨"),
    ],
    "專案說明": [
        st.Page("views/experiments.py", title="評估實驗", icon="🧪"),
        st.Page("views/benefit.py", title="效益量化", icon="📊"),
    ],
})
pg.run()
```
  - 每個 `views/*.py`：把原本 `with t_xxx:` 區塊的內容原樣搬過來，開頭呼叫 `ctx = ui_state.context()`。首頁的五個指標只放在 `views/actions.py`。「原始信件」與「解析軌跡」合併成 `views/emails.py`：上半部是解析軌跡總表與三個指標，下半部選一封信看全文，並顯示該信的解析軌跡那一列。
  - 所有「生管」字樣改「物料企劃」（app 的說明文字、註解；**不含**建議動作的「通知生管」與信件內容）。`src/supplier_stats.py` 的 docstring 同步改用語（只改註解）。
- [ ] **Step 3: 跑** `tests/test_app_pages.py` 與全套；另在 port **8513** 起一個臨時離線伺服器（`LLM_PROVIDER=none`），用 `curl -s localhost:8513` 確認可回應後關掉（不要動 8512）。
- [ ] **Step 4: Commit**：`refactor(ui): 介面拆成企劃工作區與專案說明兩區導覽，頁面各自成檔`。

---

### Task 4: 企劃工作區的新功能

**Files:** Modify `views/actions.py`、`views/suppliers.py`、`ui_state.py`；Test `tests/test_app_pages.py`（追加）

- [ ] **Step 1: 今日行動清單**
  - `ui_state.context()` 把 `planner_settings.load_confirmations()` 傳給 `retriage`。
  - 展開需人工確認的單時，在警告下方放確認表單（`st.form(key=f"confirm-{po_no}")`）：確認後的交期（`st.date_input`，預設為信中新交期或原承諾日）、備註（選填）、姓名（必填）→ `planner_settings.confirm_eta(..., email_id=row["email_id"])`，成功 `st.success` 並 `st.rerun()`；`ValueError` 以 `st.error` 顯示。說明文字：「向供應商要到確切日期後在這裡登錄，清單會改用這個日期重算。**這裡只記錄在工具內，不會寫回 ERP**；請依公司流程更新交貨排程行。」
  - 已確認的單在理由第一條就會顯示確認資訊（Task 1）；展開區另外列出這張單的確認紀錄（`confirmation_log` 篩該單號，新到舊）。
  - 清單下方兩個按鈕並排：保留「⬇️ 匯出行動清單 CSV」（操作指引已提到），新增「⬇️ 匯出明日追料清單（Excel）」，檔名 `追料清單_{as_of}.xlsx`，內容取自目前篩選前的完整清單（P1、P2、待查）。說明文字：「給明天早上追料、或帶去缺料檢討會用。」
- [ ] **Step 2: 供應商頁**（頁名改為「供應商績效」，icon 🏭；在 `app.py` 同步改）
  - 上半部保留原「各供應商歷史表現」與「改期次數 vs 最終是否延遲」。
  - 下半部「月度績效」：月份選擇（預設最近一個有資料的月份）→ 顯示該月各供應商 `exports.supplier_monthly` 的列；樣本不足的列以文字標示。說明：「供應商評核的交期面：依承諾月份統計。品質與配合度沒有資料，這裡不做。」下載按鈕「⬇️ 匯出供應商月度績效（Excel）」，內容為全部月份。
  - 月度資料以 `@st.cache_data` 快取 `supplier_stats.load_outcomes()` 的結果，並併入供應商名稱。
- [ ] **Step 3: 測試**（追加到 `tests/test_app_pages.py`）：
  1. 在行動清單頁找到一張需人工確認的單，填表單送出 → rerun 後該單不再列為需人工確認，且確認紀錄檔有一筆（用 tmp 的 planner_settings DB）。
  2. 兩個下載按鈕存在；Excel 內容可被 `openpyxl` 讀回（直接呼叫 `exports.followup_workbook(ctx actions)` 驗證即可）。
  3. 供應商績效頁無例外，月份選單有選項。
- [ ] **Step 4: Commit**：`feat(ui): 企劃確認交期、匯出明日追料清單與供應商月度績效`。

---

### Task 5: 專案說明區

**Files:** Create `views/about.py`、`views/validation.py`；Modify `views/benefit.py`、`app.py`；刪除 `views/experiments.py`

- [ ] **Step 1: `views/about.py`（📖 專案簡介與導覽）**，放在「專案說明」第一頁：
  1. 誠實聲明（沿用 README 第一段的意思：全部合成資料、沒有真實公司資料、未串接真實 ERP）。
  2. 一段話說明工具在做什麼（給晶圓廠物料企劃：讀供應商交期回覆 → 對回採購單 → 算預估缺料天數與可投產日 → 排出今天先追哪幾張、建議做什麼）。
  3. **三步導覽**（每步附 `st.page_link` 直接跳到該頁）：
     ① 到「今日行動清單」看 P1 第一張，展開看為什麼排第一、建議動作是什麼；
     ② 到「供應商績效」看那家供應商過去說定日期後通常還晚幾天——保守到料日就是從這裡來的；
     ③ 到「收貨處理天數」把那顆料調多幾天，回清單看排序怎麼變。
  4. 四個模組一覽表（讀信、排序、ERP 與歷史、物料智能檢索），每列一句話＋對應頁面。
  5. 設計原則三條：排序可驗算（兩個日期相減，不是分數）、工具永不寫回 ERP 與寄信、評估容許推翻假設（連到方法驗證頁）。
  文字全部寫在頁面檔內，不引用任何未經程式產生的數字；需要數字的地方（例如 P1 張數）即時從 `ui_state.context()` 取。
- [ ] **Step 2: `views/validation.py`（🧪 方法驗證）**：三個 `st.tabs`：「回測：到料估計與排序」（`output/回測結果.md`）、「解析：規則層 vs LLM 層」（`output/實驗結果.md`）、「檢索：六種配置比較」（`output/RAG檢索評估.md`），檔案不存在時顯示對應指令。頁首一句：「每個設計決定都要有證據，而且評估必須容許推翻原本的假設；沒贏的地方照實寫。」
- [ ] **Step 3: `views/benefit.py`（📊 效益估算）**：K 參數（每日可仔細追的件數）從側邊欄移到本頁頂端的 `number_input`；其餘內容照舊。
- [ ] **Step 4: `app.py` 導覽的「專案說明」改為**：`views/about.py`（專案簡介與導覽，📖）、`views/validation.py`（方法驗證，🧪）、`views/benefit.py`（效益估算，📊）。刪除 `views/experiments.py`。
- [ ] **Step 5: 跑** `tests/test_app_pages.py` 與全套。**Step 6: Commit**：`feat(ui): 專案說明區：簡介與三步導覽、方法驗證、效益估算`。

---

### Task 6: 驗證、設計決策 18、推上 GitHub

- [ ] **Step 1**：全套測試（記下數量）；AppTest 逐頁無例外。
- [ ] **Step 2**：用使用者正在看的 8512 預覽（Streamlit 會自動重載；若沒有就提醒使用者重新整理），用內建瀏覽器截圖三張給使用者：側邊欄兩區導覽＋今日行動清單、確認表單、專案簡介頁。
- [ ] **Step 3**：確認沒有留下 `data/planner_settings.db` 的測試資料（`git status` 乾淨、該檔不存在或為使用者自己的操作）。
- [ ] **Step 4: 決策 18**（插在「已知限制」之前）：介面分兩區的理由（企劃每天用的 vs 給面試官看的）；企劃確認紀錄的設計（新信優先、不寫回 ERP、這是 ERP 不會有的資料）；匯出與月度績效的範圍（只有交期面、月樣本少於 5 筆不給比例）；被否決的方案：全部放同一排分頁、確認後直接寫回 ERP、月度績效加入品質與配合度分數、樣本不足也照算比例。**本 Task 不重跑評估**（決策 18 會改變知識卡，於 Plan 3 收尾一次重跑）。
- [ ] **Step 5: Commit** `docs: 新增決策 18（物料企劃介面）`，推上 `feat/planner-triage`。雲端 v2 會自動更新；提醒使用者：決策 18 讓種子快取的知識卡對不上，**雲端冷啟動時「物料智能檢索」會重建索引、用到少量額度**，直到 Plan 3 重跑種子快取為止（主流程與首頁不受影響）。若使用者不接受，改為本 Task 暫不新增決策 18、留到 Plan 3 一併寫。

---

## 自我檢查

| 已確認的需求 | 任務 |
|---|---|
| 兩區：企劃工作區／專案說明 | 3、5 |
| 稱呼改物料企劃（保留「通知生管」） | 3 |
| 企劃確認紀錄（不寫回 ERP） | 1、4 |
| 匯出明日追料清單 Excel | 2、4 |
| 供應商月度績效表（樣本不足不給比例、不做品質與配合度） | 2、4 |
| 專案簡介與三步導覽、方法驗證、效益估算 | 5 |
| 不用 Gemini 額度 | 全部（「不可動的東西」一節） |

**型別一致性**：`confirm_eta(db, po_no, email_id, confirmed_date, note, user, *, now=None)`；`load_confirmations(db=None) -> {po_no: {confirmed_date, email_id, note, confirmed_by, confirmed_at}}`；`retriage(all_df, gr_days_by_material, tcfg, confirmations=None)`；`followup_workbook(actions, as_of) -> bytes`；`supplier_monthly(outcomes) -> DataFrame`；`monthly_workbook(monthly) -> bytes`。
