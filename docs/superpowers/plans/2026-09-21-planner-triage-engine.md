# 物料企劃追料引擎（Plan 1／3）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 以「預估缺料天數」取代十條加權分數，讓物料企劃每天看到的排序可以用兩個日期驗算，並用時間切分回測驗證方法。

**Architecture:** 保守到料日 = 供應商說的日期 + 這家供應商「改期過的單」歷史落差的百分位。分級（P1/P2/P3）只用「有沒有缺料」與三個事實旗標，沒有權重。歷史資料加入供應商表現隨時間變化，回測才有意義。

**Tech Stack:** Python 3.13、pandas、SQLite、Streamlit、pytest（全部離線，不呼叫 LLM）。

---

## 範圍與拆分

已與使用者達成共識（見對話紀錄）。全部工作拆成三份計畫，**各自可獨立交付、可運行**：

| 計畫 | 內容 | 是否呼叫 API |
|---|---|---|
| **Plan 1（本檔）** | 核心引擎：歷史資料時間變化、到料估計、預估缺料天數分級、建議動作、回測、移除舊權重與校準、UI 最小改動讓 App 仍可用 | 否 |
| Plan 2 | 物料企劃介面：兩區（企劃工作區／專案說明區）、稱呼改為「物料企劃」、匯出明日追料清單與供應商月度績效表、企劃確認紀錄（工具自己的表，不寫 ERP） | 否 |
| Plan 3 | 分批交貨（含信件抽取 prompt 變更）、prompt 稱呼統一、重跑 `evaluate.py`／`evaluate_rag.py`、更新 `demo_cache/`、README／CLAUDE.md／設計決策收尾、部署 | **是（用 Gemini 額度，執行前先問使用者）** |

Plan 2、Plan 3 要等 Plan 1 完成後，依實際程式與回測數字才寫得出具體程式碼，因此現在不寫。

**Plan 1 完成後的已知狀態（要誠實記錄）：**
- README、`CLAUDE.md` 仍描述舊的權重與校準，Plan 3 統一更新。本計畫只新增設計決策 16。
- 重新產生歷史資料會改變供應商統計，RAG 知識卡的內容會跟著變。**Plan 1 不重跑 RAG 評估與種子快取**，驗證時一律用 `LLM_PROVIDER=none` 避免呼叫 embedding API。
- 分批交貨仍只取最晚一筆，Plan 3 處理。

## 檔案結構

| 檔案 | 動作 | 責任 |
|---|---|---|
| `src/generate_history.py` | 修改 | 加入供應商表現隨時間變化；`build_history` 可指定資料庫路徑（測試用副本） |
| `src/supplier_stats.py` | 修改 | 歷史查詢多帶幾個欄位；新增 `estimate_delay`；改寫 `conservative_eta` |
| `src/triage.py` | 新增 | 預估缺料天數、P1/P2/P3 分級、建議動作（取代 `impact.py`） |
| `src/backtest.py` | 新增 | 時間切分回測（不偷看未來）、輸出 `output/回測結果.md` |
| `src/pipeline.py` | 修改 | 改呼叫 `triage`；移除 `rescore` 與權重 |
| `src/benefit.py`、`src/draft.py`、`app.py` | 修改 | 只做讓程式仍可運行的最小改動，Plan 2 再重排 |
| `config.yaml` | 修改 | 移除 `impact_weights`、`priority_thresholds`，新增 `triage` |
| `src/impact.py`、`src/calibrate.py`、`tests/test_calibration.py`、`output/權重校準.md` | 刪除 | 舊權重與校準 |
| `tests/test_generate_history.py`、`tests/test_supplier_stats.py`、`tests/test_triage.py`、`tests/test_backtest.py`、`tests/test_pipeline_triage.py` | 新增 | 對應各模組 |
| `docs/設計決策.md` | 修改 | 新增決策 16 |

## 通用規則（每個 Task 都適用）

- 一律 `py -X utf8`；檔案讀寫 `encoding="utf-8"`。
- 註解寫「為什麼」，繁體中文；領域假設一律標 `# 領域假設：`。
- 測試 docstring 要寫出「錯了會怎樣」。
- Commit 用中文 conventional commits，結尾加：
  `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`
- 所有涉及 API 的動作在本計畫都不會發生。若任何步驟開始呼叫 Gemini，立刻停下。

---

### Task 0: 建立工作分支並確認基準線

**Files:** 無

- [ ] **Step 1: 建立分支**

```bash
cd /c/Users/User/Desktop/supplier-eta-triage
git switch -c feat/planner-triage
```
Expected: `Switched to a new branch 'feat/planner-triage'`

- [ ] **Step 2: 確認基準線全過**

```bash
py -X utf8 -m pytest tests -q
```
Expected: `81 passed`

- [ ] **Step 3: 確認 LLM 可以被環境變數關掉（之後的驗證都靠這個避免用額度）**

```bash
cd /c/Users/User/Desktop/supplier-eta-triage/src && LLM_PROVIDER=none py -X utf8 -c "from llm.provider import get_provider; print(type(get_provider()).__name__)"
```
Expected: `NullProvider`。若印出別的名稱，停下來，`_load_dotenv` 覆寫了環境變數，之後所有 App 驗證都要改成先暫時移開 `.env`。

---

### Task 1: 歷史資料加入「供應商表現隨時間變化」

> **執行後修正（2026-09-21）：** 審查發現兩個資料庫層級的漂移測試在每邊約 35 筆樣本下，
> 換亂數種子約 21% 會失敗。已另加與種子無關的測試（直接對 `_simulate_outcome` 抽兩萬次），
> 資料庫層級測試降為方向性檢查。見 commit `02bb548`。

**為什麼先做：** 回測的意義在於「過去的落差能不能預測未來」。若整年都是同一個固定分布，涵蓋率必然接近設定值，驗證等於白送。

**Files:**
- Modify: `src/generate_history.py`
- Test: `tests/test_generate_history.py`（新增）

- [ ] **Step 1: 寫失敗的測試**

建立 `tests/test_generate_history.py`：

```python
# -*- coding: utf-8 -*-
"""
歷史單據產生器的測試。

全部在資料庫的「副本」上執行，不會動到 data/erp_sim.db。
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

DB = ROOT / "data" / "erp_sim.db"
pytestmark = pytest.mark.skipif(
    not DB.exists(), reason="需先執行 py src/build_erp_db.py")


@pytest.fixture(scope="module")
def rebuilt(tmp_path_factory):
    import generate_history
    dst = tmp_path_factory.mktemp("erp") / "erp_copy.db"
    shutil.copy(DB, dst)
    generate_history.build_history(verbose=False, db_path=dst)
    return dst


def _late_rate(con, vendor: str, lo: str, hi: str) -> tuple[int, float]:
    n, late = con.execute("""
        SELECT COUNT(*),
               SUM(CASE WHEN g.receipt_date > s.committed_date THEN 1 ELSE 0 END)
        FROM goods_receipt g
        JOIN po_header   h ON h.po_no = g.po_no
        JOIN po_schedule s ON s.po_no = g.po_no AND s.item_no = g.item_no
        WHERE h.vendor_id = ? AND s.committed_date >= ? AND s.committed_date < ?
    """, (vendor, lo, hi)).fetchone()
    return n, (late or 0) / n if n else float("nan")


def test_degrading_supplier_gets_worse_over_time(rebuilt):
    """
    SUP-S02 從 2026-03-01 起表現變差。

    若產生器整年都是固定分布，時間切分回測的涵蓋率必然接近設定值，
    「驗證」就成了自己驗自己。這個測試守住「表現真的會隨時間變」。
    """
    with sqlite3.connect(rebuilt) as con:
        n_before, before = _late_rate(con, "SUP-S02", "2025-01-01", "2026-03-01")
        n_after, after = _late_rate(con, "SUP-S02", "2026-03-01", "2027-01-01")
    assert min(n_before, n_after) >= 15, (n_before, n_after)
    assert after - before >= 0.15, (before, after)


def test_improving_supplier_gets_better_over_time(rebuilt):
    with sqlite3.connect(rebuilt) as con:
        n_before, before = _late_rate(con, "SUP-F03", "2025-01-01", "2026-04-01")
        n_after, after = _late_rate(con, "SUP-F03", "2026-04-01", "2027-01-01")
    assert min(n_before, n_after) >= 15, (n_before, n_after)
    assert before - after >= 0.15, (before, after)


def test_generator_is_idempotent(rebuilt):
    """
    回歸測試：歷史產生器必須冪等，跑一次跟跑十次結果要一樣。

    早期版本直接 INSERT 變更文件，重跑一次就寫入第二遍，
    po_change_log 從 904 筆變成 1599 筆，改期次數憑空翻倍。
    這種 bug 不會報錯，資料照樣跑得出來，只是悄悄地錯。
    """
    import generate_history

    def snapshot():
        with sqlite3.connect(rebuilt) as con:
            return {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                    for t in ("po_header", "po_item", "po_schedule",
                              "po_change_log", "goods_receipt", "purchase_req")}

    first = snapshot()
    generate_history.build_history(verbose=False, db_path=rebuilt)
    assert snapshot() == first


def test_no_duplicate_change_log_rows(rebuilt):
    """變更文件不可有內容完全相同的重複列。"""
    with sqlite3.connect(rebuilt) as con:
        total = con.execute("SELECT COUNT(*) FROM po_change_log").fetchone()[0]
        distinct = con.execute(
            "SELECT COUNT(*) FROM (SELECT DISTINCT po_no, item_no, old_value,"
            " new_value, changed_at FROM po_change_log)").fetchone()[0]
    assert total == distinct, f"變更文件有 {total - distinct} 筆重複"
```

- [ ] **Step 2: 確認測試失敗**

```bash
py -X utf8 -m pytest tests/test_generate_history.py -v
```
Expected: 全部 FAIL，錯誤為 `TypeError: build_history() got an unexpected keyword argument 'db_path'`

- [ ] **Step 3: 實作**

`src/generate_history.py` 做四處修改。

(a) 模組 docstring（第 1–28 行）整段換成：

```python
# -*- coding: utf-8 -*-
"""
產生「歷史單據與實際結果」，寫入模擬 ERP 資料庫。

先前的模擬 ERP 只有「未結採購單」。`goods_receipt`（收貨紀錄）代表
「後來到底怎麼了」，沒有它就無法從歷史推導供應商準交率與到料估計。
這支程式補上過去 12 個月的歷史單據：請購 → 採購 → 交貨排程 →
改期紀錄 → 實際收貨。

===========================  必須先說的話  ===========================
**這些歷史結果是我用一組因果規則產生的，不是真實資料。**

它的用途是讓「歷史落差 → 保守到料日 → 回測」這條流程有資料可跑。
回測（src/backtest.py）驗證的是方法有沒有偷看未來、估計有沒有校準，
**不能證明真實供應商會照這種分布行動。**

為了讓時間切分回測有意義，部分供應商的表現會隨時間變好或變差
（見 SUPPLIER_DRIFT）。若整年都是同一個固定分布，涵蓋率必然接近設定值，
驗證就沒有意義。
=====================================================================
"""
```

(b) 在 `CATEGORY_RISK` 定義之後（`_simulate_outcome` 之前）新增：

```python
# 領域假設：供應商的交付表現不是固定不變的。
# 現實中會因為產能重分配、換廠、良率改善而變好或變差。
# from：從這天起（以承諾日計）表現改變；p_late：延遲機率的加減量；
# delay_mult：延遲時的天數倍率。
SUPPLIER_DRIFT = {
    "SUP-S02": {"from": date(2026, 3, 1), "p_late": +0.25, "delay_mult": 1.4},  # 變差
    "SUP-F03": {"from": date(2026, 4, 1), "p_late": -0.22, "delay_mult": 0.8},  # 改善
}
```

(c) `_simulate_outcome` 簽名加 `vendor_id`，並套用時間變化。簽名改為：

```python
def _simulate_outcome(rng: random.Random, *, vendor_id: str, otd_rate: float,
                      category: str, is_bottleneck: bool, reschedule_count: int,
                      committed: date, qty: int) -> int:
```
在 `if qty >= 5000:` 那兩行之後、`p_late = max(0.02, min(0.92, p_late))` 之前插入：

```python
    drift = SUPPLIER_DRIFT.get(vendor_id)
    drift_on = bool(drift and committed >= drift["from"])
    if drift_on:
        p_late += drift["p_late"]
```
在函式最後 `return max(1, int(round(base)))` 之前插入：

```python
    if drift_on:
        base *= drift["delay_mult"]
```
呼叫處（`delta = _simulate_outcome(` 那段）在 `rng,` 後加 `vendor_id=vid,`。

(d) `build_history` 支援指定資料庫路徑。簽名改為 `def build_history(verbose: bool = True, db_path: Path | str | None = None) -> dict:`；函式開頭改成：

```python
    path = Path(db_path or DB_PATH)
    if not path.exists():
        raise FileNotFoundError(
            f"找不到 {path}，請先執行： py src/build_erp_db.py")
```
並把函式內其餘的 `sqlite3.connect(DB_PATH)` 改為 `sqlite3.connect(path)`、`DB_PATH.name` 改為 `path.name`。函式最後兩行提醒文字改為：

```python
        print("       提醒：這些結果由因果模型產生，非真實資料。")
        print("       回測只能證明方法可行，不能證明真實供應商的行為。")
```

- [ ] **Step 4: 確認測試通過**

```bash
py -X utf8 -m pytest tests/test_generate_history.py -v
```
Expected: 4 passed。

**若 drift 測試失敗：** 先印出 `(before, after)` 與樣本數看是樣本太少還是漂移量太小。只能調整 `SUPPLIER_DRIFT` 的數值（那是我們的假設參數），**不可降低測試的 0.15 門檻或樣本數門檻**。

- [ ] **Step 5: Commit**

```bash
git add src/generate_history.py tests/test_generate_history.py
git commit -F - <<'EOF'
feat(history): 歷史資料加入供應商表現隨時間變化

回測要驗證「過去的落差能否預測未來」，若整年都是固定分布，涵蓋率必然
接近設定值，驗證等於白送。SUP-S02 從 2026-03 起變差、SUP-F03 從 2026-04
起變好；build_history 可指定資料庫路徑，測試在副本上跑，不動真實資料庫。

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
```

---

### Task 2: 到料落差估計（只用改期過的單）

**Files:**
- Modify: `src/supplier_stats.py`
- Test: `tests/test_supplier_stats.py`（新增）

- [ ] **Step 1: 寫失敗的測試**

建立 `tests/test_supplier_stats.py`：

```python
# -*- coding: utf-8 -*-
"""
供應商歷史統計與到料落差估計的測試。

估計函式用小型手造資料驗證邏輯；讀真實資料庫的測試放在檔案後半。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import supplier_stats  # noqa: E402


def _outcomes(rows: list[tuple[str, int, int]]) -> pd.DataFrame:
    """rows = [(supplier_id, delay_days, reschedule_count), ...]"""
    return pd.DataFrame(rows, columns=["supplier_id", "delay_days", "reschedule_count"])


def test_uses_only_rescheduled_orders_when_enough_samples():
    """
    生管收到的是「已經跳票、剛給新日期」的單。
    若把從沒改期、準時到的單一起算，會系統性低估這種單的風險。
    """
    df = _outcomes([("A", 0, 0)] * 30 + [("A", 10, 2)] * 25)
    est = supplier_stats.estimate_delay(df, "A", percentile=0.80)
    assert est["available"] and est["basis"] == "改期過的單"
    assert est["delay_days"] == 10 and est["n"] == 25


def test_falls_back_to_all_orders_and_says_so():
    df = _outcomes([("A", 0, 0)] * 30 + [("A", 10, 1)] * 5)
    est = supplier_stats.estimate_delay(df, "A", percentile=0.80)
    assert est["available"]
    assert est["basis"] == "全部單（改期單樣本不足）"
    assert est["n"] == 35


def test_refuses_when_samples_insufficient():
    """只有 10 筆的統計不該被當成依據，寧可說樣本不足。"""
    df = _outcomes([("A", 5, 1)] * 10)
    est = supplier_stats.estimate_delay(df, "A", percentile=0.80)
    assert est["available"] is False
    assert "樣本不足" in est["reason"]


def test_unknown_supplier_is_unavailable_not_an_error():
    est = supplier_stats.estimate_delay(_outcomes([("A", 5, 1)] * 30), "Z", percentile=0.8)
    assert est["available"] is False


def test_delay_is_never_negative():
    """供應商全都提早到，保守到料日也不能比他說的日期還早。"""
    df = _outcomes([("A", -2, 1)] * 30)
    est = supplier_stats.estimate_delay(df, "A", percentile=0.95)
    assert est["delay_days"] == 0


def test_higher_percentile_is_never_smaller():
    df = _outcomes([("A", d, 1) for d in range(0, 40)])
    lo = supplier_stats.estimate_delay(df, "A", percentile=0.80)["delay_days"]
    hi = supplier_stats.estimate_delay(df, "A", percentile=0.95)["delay_days"]
    assert hi >= lo


def test_conservative_eta_adds_delay_to_promised_date():
    df = _outcomes([("A", 9, 1)] * 30)
    r = supplier_stats.conservative_eta("A", "2026-10-29", df)
    assert r["available"]
    assert r["conservative"] == "2026-11-07"
    assert r["conservative"] >= r["promised"]


def test_conservative_eta_rejects_unparseable_date():
    df = _outcomes([("A", 9, 1)] * 30)
    r = supplier_stats.conservative_eta("A", "下週", df)
    assert r["available"] is False


# ---------------------------------------------------------------------------
# 讀真實（模擬）資料庫
# ---------------------------------------------------------------------------
DB = ROOT / "data" / "erp_sim.db"
needs_db = pytest.mark.skipif(
    not DB.exists(),
    reason="需先執行 py src/build_erp_db.py 與 py src/generate_history.py")


@needs_db
def test_load_outcomes_has_columns_the_backtest_needs():
    df = supplier_stats.load_outcomes()
    assert {"po_no", "supplier_id", "committed_date", "receipt_date", "need_date",
            "notice_date", "vendor_otd", "delay_days", "reschedule_count"} <= set(df.columns)
    assert len(df) > 100


@needs_db
def test_notice_date_only_exists_for_rescheduled_orders():
    df = supplier_stats.load_outcomes()
    assert df.loc[df["reschedule_count"] >= 1, "notice_date"].notna().all()
    assert df.loc[df["reschedule_count"] == 0, "notice_date"].isna().all()


@needs_db
def test_supplier_performance_flags_small_samples():
    """
    樣本太少的統計會誤導人。只有三張單的供應商算出「準交率 33%」，
    那個數字不該拿去做決策，工具寧可說「樣本不足」也不要給假訊號。
    """
    perf = supplier_stats.supplier_performance(min_samples=1000)
    assert not perf["樣本是否足夠"].any()
    perf = supplier_stats.supplier_performance(min_samples=1)
    assert perf["樣本是否足夠"].all()
    assert perf["準交率"].between(0, 1).all()


@needs_db
def test_conservative_eta_is_never_earlier_than_promised_on_real_history():
    outcomes = supplier_stats.load_outcomes()
    for sid in outcomes["supplier_id"].unique():
        r = supplier_stats.conservative_eta(sid, "2026-10-29", outcomes)
        if r["available"]:
            assert r["conservative"] >= r["promised"], sid


@needs_db
def test_reschedule_reliability_supports_using_rescheduled_orders_only():
    """
    這張表是「只用改期過的單」的依據：改期越多次，最終仍延遲的比例應越高。
    若這個關係不成立，估計就不該把改期單獨立出來。
    """
    t = supplier_stats.reschedule_reliability().set_index("改期情形")
    if {"未改期", "改期 3 次以上"} <= set(t.index):
        assert t.loc["改期 3 次以上", "最終仍延遲比例"] > t.loc["未改期", "最終仍延遲比例"]
```

- [ ] **Step 2: 確認失敗**

```bash
py -X utf8 -m pytest tests/test_supplier_stats.py -v
```
Expected: 手造資料的測試 FAIL（`AttributeError: ... no attribute 'estimate_delay'`）。

- [ ] **Step 3: 實作**

`src/supplier_stats.py`：

(a) 檔案最上方 `import pandas as pd` 之前加入 `import numpy as np`（與其他 import 依字母序排列）。

(b) 整段換掉 `HISTORY_SQL`：

```python
# 已結案單的實際表現。delay_days > 0 代表比承諾日晚到。
# notice_date：最後一次「承諾日被改」的變更日，也就是企劃收到改期通知的時點。
#   回測用它決定「當時已經知道哪些歷史」。
HISTORY_SQL = """
SELECT
    i.po_no                                                       AS po_no,
    h.vendor_id                                                   AS supplier_id,
    m.category                                                    AS category,
    s.committed_date                                              AS committed_date,
    g.receipt_date                                                AS receipt_date,
    r.need_date                                                   AS need_date,
    v.otd_rate                                                    AS vendor_otd,
    CAST(julianday(g.receipt_date) - julianday(s.committed_date)
         AS INTEGER)                                              AS delay_days,
    (SELECT COUNT(*) FROM po_change_log c
      WHERE c.po_no = i.po_no AND c.item_no = i.item_no
        AND c.field_name = 'committed_date')                      AS reschedule_count,
    (SELECT MAX(c.changed_at) FROM po_change_log c
      WHERE c.po_no = i.po_no AND c.item_no = i.item_no
        AND c.field_name = 'committed_date')                      AS notice_date
FROM goods_receipt g
JOIN po_item      i ON i.po_no = g.po_no AND i.item_no = g.item_no
JOIN po_header    h ON h.po_no = i.po_no
JOIN po_schedule  s ON s.po_no = i.po_no AND s.item_no = i.item_no
JOIN material_master m ON m.material_id = i.material_id
LEFT JOIN purchase_req   r ON r.pr_no = i.pr_no
LEFT JOIN vendor_master  v ON v.vendor_id = h.vendor_id
"""
```

(c) `supplier_performance` 在 `out["樣本是否足夠"] = ...` 這行之前加入兩欄（讓畫面上的數字與估計用的母體一致）：

```python
    resched = df[df["reschedule_count"] >= 1].groupby("supplier_id")["delay_days"]
    out["改期單樣本數"] = resched.size().reindex(out.index).fillna(0).astype(int)
    out["改期單 P80 延遲(天)"] = (
        resched.quantile(0.80, interpolation="higher").reindex(out.index))
```

(d) 在 `supplier_performance` 之後、`reschedule_reliability` 之前新增：

```python
def estimate_delay(outcomes: pd.DataFrame, supplier_id: str, *,
                   percentile: float, min_samples: int = 20) -> dict:
    """
    這家供應商「說定日期後」實際還會晚幾天（歷史百分位）。

    母體只取**曾改期過的單**：企劃收到的是已經跳票、剛給新日期的通知，
    拿「全部單」（含從沒改期、準時到的）去估，會系統性低估這種單的風險。
    改期單樣本不足 min_samples 時退回全部單，並在 basis 標明；
    全部單也不足就回報樣本不足，不給一個看起來很精確的假數字。

    刻意不再往下切（例如再依料別）：898 張歷史單分給 12 家供應商，
    每家改期單只有幾十筆，再切每格只剩個位數。

    這是歷史統計，不是預測模型。percentile 用 "higher"：取實際出現過的
    天數，不做內插，說出來的「N 天」一定是真的發生過的落差。
    """
    mine = outcomes[outcomes["supplier_id"] == supplier_id]
    rescheduled = mine[mine["reschedule_count"] >= 1]
    if len(rescheduled) >= min_samples:
        pool, basis = rescheduled, "改期過的單"
    elif len(mine) >= min_samples:
        pool, basis = mine, "全部單（改期單樣本不足）"
    else:
        return {"available": False, "n": int(len(mine)),
                "reason": f"歷史樣本不足（{len(mine)} 筆），不提供保守估計"}
    days = int(np.quantile(pool["delay_days"].to_numpy(), percentile, method="higher"))
    return {"available": True, "delay_days": max(0, days),
            "percentile": percentile, "basis": basis, "n": int(len(pool))}
```

(e) 整段換掉 `conservative_eta`：

```python
def conservative_eta(supplier_id: str, promised: str | date,
                     outcomes: pd.DataFrame, *, percentile: float = 0.80,
                     min_samples: int = 20) -> dict:
    """
    把「供應商說的日期」翻譯成「保守到料日」。

    回傳的是給排程參考的第二個日期，不是預測：
    意思是「歷史上這類單有八成在這天以前到」。
    刻意不取平均：平均會被少數超長延遲拉高，
    而且排程要的是「幾成把握」的語言，不是期望值。
    """
    try:
        promised_d = (promised if isinstance(promised, date)
                      else date.fromisoformat(str(promised)[:10]))
    except (ValueError, TypeError):
        return {"available": False, "reason": "承諾日無法解析"}

    est = estimate_delay(outcomes, supplier_id, percentile=percentile,
                         min_samples=min_samples)
    if not est["available"]:
        return est
    return {
        **est,
        "promised": promised_d.isoformat(),
        "conservative": (promised_d + timedelta(days=est["delay_days"])).isoformat(),
        "note": (f"歷史{est['basis']}共 {est['n']} 筆，"
                 f"{int(round(percentile * 100))}% 在說定日期後 {est['delay_days']} 天內到"),
    }
```

(f) 檔案底部 `if __name__ == "__main__":` 區塊中「保守到料日示例」那段（`perf = supplier_performance(data)` 起至結尾）改為：

```python
    print("\n=== 保守到料日示例 ===")
    for sid, promised in [("SUP-F03", "2026-10-29"), ("SUP-S01", "2026-10-14")]:
        r = conservative_eta(sid, promised, data)
        if r["available"]:
            print(f"  {sid}  說定 {r['promised']} -> 保守 {r['conservative']}  （{r['note']}）")
        else:
            print(f"  {sid}  {r['reason']}")
```

- [ ] **Step 4: 確認通過**

```bash
py -X utf8 -m pytest tests/test_supplier_stats.py -v
```
Expected: 全部 passed（資料庫相關測試在資料存在時執行；歷史尚未加入 drift，不影響這些測試）。

- [ ] **Step 5: 確認 RAG 知識卡沒被新欄位改動**

```bash
py -X utf8 -m pytest tests/test_rag.py -q
```
Expected: passed。並用 `grep -n "perf\[" src/rag/knowledge.py` 確認知識卡只取具名欄位（準交率、P80 延遲等），沒有用 `to_string()` 或迭代所有欄位。若有，停下並回報：新欄位會改變知識卡文字，牽動 Plan 3 的 RAG 評估。

- [ ] **Step 6: Commit**

```bash
git add src/supplier_stats.py tests/test_supplier_stats.py
git commit -F - <<'EOF'
feat(supplier-stats): 到料落差只用改期過的單估計

企劃收到的是已跳票、剛給新日期的通知，用全部單（含準時的）估計會低估
這種單的風險。改期單樣本不足 20 筆時退回全部單並標明母體，全部單也不足
就回報樣本不足。歷史查詢多帶通知日與需求日，供回測使用。

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
```

---

### Task 3: 分級與建議動作（`triage.py`）

> **執行後修正（2026-09-21）：** 審查找到四個邏輯漏洞並已修正，實際程式以 repo 為準，
> 下方程式碼是原始版本：(1) 提前交貨但仍晚於需求日要照常分級；
> (2) 非延遲且讀不出新日期 → 待查；(3) 沒給新日期時理由寫「原承諾日」、緩衝註明不能當真；
> (4) 每張 P1/P2 至少一個建議動作，沒給日期一律先追日期，「通知生管」提前。

**Files:**
- Create: `src/triage.py`
- Test: `tests/test_triage.py`

- [ ] **Step 1: 寫失敗的測試**

建立 `tests/test_triage.py`：

```python
# -*- coding: utf-8 -*-
"""
交期風險分級的測試。

守住的是「判斷方向」與「不亂建議」：
  - 缺料與否由日期相減決定，不是分數
  - 建議動作遵守 AVL：替代料未驗證前不可寫成可用
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from domain import ChangeType, CommitmentStrength  # noqa: E402
from triage import (PRIORITY_RANK, _clean_str, evaluate,  # noqa: E402
                    percentile_for)

CFG = {"tight_buffer_days": 3, "percentile_confirmed": 0.80,
       "percentile_estimated": 0.90, "percentile_intent_only": 0.95}

PO = {"committed_date": "2026-10-01", "need_date": "2026-10-20",
      "downstream_scheduled": False}
MAT = {"has_qualified_second_source": True, "is_bottleneck": False,
       "alt_material_id": ""}


def _est(days: int) -> dict:
    return {"available": True, "delay_days": days, "percentile": 0.80,
            "basis": "改期過的單", "n": 40}


def _run(new_eta="2026-10-10", est=None, po=None, mat=None, rec=None):
    record = {"new_eta": new_eta, "change_type": ChangeType.DELAY.value,
              "commitment_strength": CommitmentStrength.CONFIRMED.value,
              **(rec or {})}
    return evaluate(record, {**PO, **(po or {})}, {**MAT, **(mat or {})}, CFG,
                    _est(0) if est is None else est)


# ---------------------------------------------------------------------------
# 預估缺料天數
# ---------------------------------------------------------------------------
def test_history_delay_turns_a_safe_looking_order_into_a_shortage():
    """
    供應商說 10/10、需求日 10/20，表面上還有 10 天緩衝。
    但這家供應商說定後通常再晚 15 天 → 保守到料日 10/25，缺 5 天。
    只看供應商說的日期會漏掉這張單。
    """
    r = _run(est=_est(15))
    assert r["conservative_eta"] == "2026-10-25"
    assert r["gap_days"] == 5
    assert r["priority"] == "P2"


def test_without_history_falls_back_to_stated_date_and_says_so():
    r = _run(est={"available": False, "n": 3,
                  "reason": "歷史樣本不足（3 筆），不提供保守估計"})
    assert r["gap_days"] == -10
    assert any("樣本不足" in s for s in r["reasons"])


def test_percentile_for_picks_more_conservative_when_not_confirmed():
    assert percentile_for("confirmed", CFG) == 0.80
    assert percentile_for("estimated", CFG) == 0.90
    assert percentile_for("intent_only", CFG) == 0.95
    assert percentile_for("none", CFG) == 0.95
    assert percentile_for(None, CFG) == 0.95


# ---------------------------------------------------------------------------
# 分級
# ---------------------------------------------------------------------------
def test_shortage_with_downstream_scheduled_is_p1():
    r = _run(est=_est(15), po={"downstream_scheduled": True})
    assert r["priority"] == "P1"


def test_shortage_on_bottleneck_material_is_p1():
    assert _run(est=_est(15), mat={"is_bottleneck": True})["priority"] == "P1"


def test_shortage_without_critical_flags_is_p2():
    assert _run(est=_est(15))["priority"] == "P2"


def test_tight_buffer_on_single_source_is_p2():
    r = _run(new_eta="2026-10-18", est=_est(0),
             mat={"has_qualified_second_source": False})
    assert r["gap_days"] == -2 and r["priority"] == "P2"


def test_tight_buffer_with_qualified_second_source_is_p3():
    r = _run(new_eta="2026-10-18", est=_est(0))
    assert r["priority"] == "P3"


def test_plenty_of_buffer_is_p3_even_for_single_source():
    r = _run(est=_est(0), mat={"has_qualified_second_source": False})
    assert r["priority"] == "P3"


def test_priority_rank_orders_tiers():
    assert PRIORITY_RANK["P1"] < PRIORITY_RANK["P2"] < PRIORITY_RANK["P3"]


def test_no_change_leaves_action_list():
    r = evaluate({"change_type": ChangeType.NO_CHANGE.value, "new_eta": None},
                 PO, MAT, CFG, _est(9))
    assert r["priority"] == "—" and r["gap_days"] is None


def test_pull_in_is_p3_with_warehouse_note():
    """提前交貨要處理倉容與付款，不該和斷料排在一起。"""
    r = evaluate({"change_type": ChangeType.PULL_IN.value, "new_eta": "2026-09-20"},
                 PO, {**MAT, "is_bottleneck": True}, CFG, _est(9))
    assert r["priority"] == "P3" and "倉容" in r["note"]


def test_missing_need_date_is_flagged_not_guessed():
    r = _run(po={"need_date": None})
    assert r["priority"] == "待查" and r["gap_days"] is None


def test_delay_without_new_date_is_never_left_at_p3():
    """
    供應商說會延、卻沒給新日期時，只能拿原承諾日去算，缺料天數一定被低估。
    這種單最需要企劃立刻追日期，不能因為「看起來還有緩衝」就掉到 P3。
    """
    r = _run(new_eta=None, est=_est(0),
             rec={"commitment_strength": CommitmentStrength.NONE.value})
    assert r["priority"] in ("P1", "P2")
    assert "未給新日期" in r["reasons"][0]
    assert "確切日期" in r["actions"][0]


# ---------------------------------------------------------------------------
# 建議動作（遵守 AVL）
# ---------------------------------------------------------------------------
def test_alternate_material_is_never_called_usable():
    """
    替代料要過客戶與品保驗證（AVL），不能想換就換。
    資料表只知道有替代料，不知道它驗證過沒有，所以只能請品保確認。
    """
    r = _run(est=_est(15), mat={"alt_material_id": "WF-N7-KL2211"})
    text = "；".join(r["actions"])
    assert "WF-N7-KL2211" in text and "品保" in text
    assert "可換料" not in text and "可直接" not in text


def test_nan_alternate_is_treated_as_none():
    """
    回歸測試：料號主檔的替代料欄位在 CSV 讀進 pandas 會變成 NaN，
    str(NaN) == "nan" 是非空字串，早期版本因此對企劃說「有替代料 nan」。
    """
    for empty in (float("nan"), None, "", "  ", "NaN", "None"):
        r = _run(est=_est(15), mat={"alt_material_id": empty})
        assert "替代料" not in "；".join(r["actions"]), repr(empty)
        assert "nan" not in "；".join(r["actions"]).lower()


def test_second_source_action_goes_through_procurement():
    r = _run(est=_est(15))
    assert any("採購" in a and "二源" in a for a in r["actions"])


def test_single_source_action_states_the_limit():
    r = _run(est=_est(15), mat={"has_qualified_second_source": False})
    assert any("單一來源" in a for a in r["actions"])


def test_downstream_scheduled_notifies_production_control():
    r = _run(est=_est(15), po={"downstream_scheduled": True})
    assert any("生管" in a for a in r["actions"])


def test_unconfirmed_date_asks_for_a_firm_date_first():
    r = _run(est=_est(15),
             rec={"commitment_strength": CommitmentStrength.INTENT_ONLY.value})
    assert "確切日期" in r["actions"][0]


def test_p3_has_no_actions():
    assert _run(est=_est(0))["actions"] == []


def test_actions_do_not_claim_cost_or_feasibility():
    r = _run(est=_est(15))
    hand = [a for a in r["actions"] if "空運" in a][0]
    assert "確認" in hand and "$" not in hand and "元" not in hand


def test_clean_str_normalises_empty_values():
    assert _clean_str(float("nan")) == ""
    assert _clean_str(None) == ""
    assert _clean_str("nan") == ""
    assert _clean_str("  WF-1  ") == "WF-1"
```

- [ ] **Step 2: 確認失敗**

```bash
py -X utf8 -m pytest tests/test_triage.py -v
```
Expected: 收集階段 `ModuleNotFoundError: No module named 'triage'`。

- [ ] **Step 3: 實作**

建立 `src/triage.py`：

```python
# -*- coding: utf-8 -*-
"""
交期風險分級：預估缺料天數 ＋ P1/P2/P3 ＋ 建議動作。

===========================  為什麼沒有權重  ===========================
舊版用十條規則各給 0~1 分、再依權重加成 0~100 分。那組權重是人手填的，
拿去「校準」時用的又是自己產生的歷史資料，等於用答案驗答案
（見 docs/設計決策.md 決策 16）。

這一版只問物料企劃真正在意的一件事：這張單到料時，需求日已經過了幾天？

    預估缺料天數 = 保守到料日 − 下游需求日
    保守到料日   = 供應商說的日期 + 這家供應商過去「說定後仍延遲」的天數

那是兩個日期相減，可以在會議上逐項驗算，不是分數。
分級只用「有沒有缺料」與三個事實旗標，沒有任何可調權重。
=====================================================================

領域假設：`need_date` 視為 MRP 的淨需求日（已扣庫存與在途），
因此不另外建庫存表。若接的是未扣庫存的需求日，缺料天數會被高估。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from domain import ChangeType

# 排序用：分級由前到後，同級內再依預估缺料天數由大到小。
PRIORITY_RANK = {"P1": 0, "P2": 1, "P3": 2, "待查": 3, "—": 4}


def _d(v) -> date | None:
    """
    寬鬆的日期正規化。

    來源可能是 ISO 字串、datetime.date、或 pandas Timestamp。
    Timestamp 與 datetime 都是 date 的子類，但直接相減型別會出錯，
    所以必須先降階成純 date。
    """
    if v is None or v != v:  # None 或 NaN
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except (ValueError, TypeError):
        return None


def _clean_str(v) -> str:
    """
    把「空值」正規化成空字串。

    這個函式是因為一個真實的 bug 而生的：料號主檔的替代料欄位在 CSV 裡
    是空字串，pandas 讀進來變成 float NaN，而 str(NaN) 是字串 "nan"，
    它是非空字串，於是通過 `if alt:`，工具就對企劃說「有替代料 nan」。
    空值處理在資料型工具裡不是細節，是正確性問題。
    """
    if v is None or v != v:
        return ""
    s = str(v).strip()
    return "" if s.lower() in {"nan", "none", "nat", "null", "-"} else s


def percentile_for(strength: str | None, cfg: dict) -> float:
    """
    承諾強度越弱，取越保守的歷史百分位。

    領域假設：暫估與僅意向的日期比確認的日期更不可靠，
    「沒有給日期」視為最不可靠，與僅意向同級。
    百分位數字本身是假設，回測只能檢驗它們的涵蓋率，不能證明它們「對」。
    """
    key = {"confirmed": "percentile_confirmed",
           "estimated": "percentile_estimated"}.get(strength or "",
                                                    "percentile_intent_only")
    return float(cfg[key])


def _tier(gap: int, po: dict, material: dict, tight_days: int) -> str:
    critical = bool(po.get("downstream_scheduled")) or bool(material.get("is_bottleneck"))
    single_source = not bool(material.get("has_qualified_second_source"))
    if gap > 0:
        return "P1" if critical else "P2"
    if single_source and gap >= -tight_days:
        return "P2"
    return "P3"


def suggest_actions(priority: str, gap: int, record: dict, po: dict,
                    material: dict) -> list[str]:
    """
    給物料企劃「自己能做」的下一步。

    詢價與下單是採購的工作、驗證替代料是品保的工作，所以動作寫成
    「與採購確認」「請品保確認」，不寫成企劃自己去做。
    成本與可行性資料庫裡沒有，因此不寫。
    """
    if priority not in ("P1", "P2"):
        return []
    acts: list[str] = []
    if record.get("commitment_strength") != "confirmed":
        acts.append("先向供應商追一個可承諾的確切日期（目前日期尚未確認）")
    if gap > 0:
        acts.append("可評估的手段：催貨、拉貨、分批交、空運"
                    "（成本與可行性需與採購、供應商確認）")
    if material.get("has_qualified_second_source"):
        acts.append("與採購確認能否向已認證二源詢價備案")
    elif gap > 0:
        acts.append("單一來源，無已認證二源可轉單，只能追供應商")
    alt = _clean_str(material.get("alt_material_id"))
    if alt:
        acts.append(f"替代料 {alt}：請品保確認是否已通過驗證，或能否走特採；"
                    "未確認前不可視為可用")
    if po.get("downstream_scheduled"):
        acts.append("通知生管：這張單可能缺料，需確認下游排程")
    return acts


def evaluate(record: dict, po: dict, material: dict, triage_cfg: dict,
             estimate: dict | None) -> dict:
    """
    對一筆已對位的變更算預估缺料天數、分級與建議動作。

    estimate 是 supplier_stats.estimate_delay 的結果（由呼叫端依承諾強度
    選好百分位後傳入），本函式不讀資料庫，方便單獨測試。
    """
    change = record.get("change_type")
    out = {"gap_days": None, "conservative_eta": None, "delay_days_est": 0,
           "delay_basis": "", "delay_n": 0, "actions": [], "note": ""}

    if change == ChangeType.NO_CHANGE.value:
        # 確認不變的信不進行動清單，但要留下紀錄（證明這封信已被處理過）。
        return {**out, "priority": "—", "reasons": ["供應商確認照原計畫，無需行動"],
                "note": "供應商確認照原計畫，無需行動"}
    if change == ChangeType.PULL_IN.value:
        # 提前交貨要處理的是倉容與付款，不是缺料。
        return {**out, "priority": "P3", "reasons": ["供應商提前交貨"],
                "note": "提前交貨：請確認倉容與付款排程"}

    eta = _d(record.get("new_eta")) or _d(po.get("committed_date"))
    need = _d(po.get("need_date"))
    if eta is None or need is None:
        return {**out, "priority": "待查",
                "reasons": ["缺少交期或需求日，無法計算預估缺料天數"]}

    available = bool(estimate and estimate.get("available"))
    delay = int(estimate["delay_days"]) if available else 0
    conservative = eta + timedelta(days=delay)
    gap = (conservative - need).days

    reasons: list[str] = []
    if available:
        pct = int(round(float(estimate["percentile"]) * 100))
        reasons.append(
            f"供應商說 {eta.isoformat()}；這家供應商過去{estimate['basis']}"
            f"（{estimate['n']} 筆）有 {pct}% 在說定日期後 {delay} 天內到，"
            f"保守到料日 {conservative.isoformat()}")
    else:
        why = (estimate or {}).get("reason", "沒有歷史收貨紀錄")
        reasons.append(f"{why}；暫以供應商說的日期 {eta.isoformat()} 計算")
    if gap > 0:
        reasons.append(f"比下游需求日 {need.isoformat()} 晚 {gap} 天，預估缺料")
    else:
        reasons.append(f"距下游需求日 {need.isoformat()} 尚有 {-gap} 天緩衝")
    if gap > 0 and po.get("downstream_scheduled"):
        reasons.append("下游已排定產能或已對客戶承諾，缺料會連動整串排程")
    if gap > 0 and material.get("is_bottleneck"):
        reasons.append("瓶頸料，延誤難以補回")

    tight = int(triage_cfg["tight_buffer_days"])
    priority = _tier(gap, po, material, tight)
    if priority == "P2" and gap <= 0:
        reasons.append(f"單一來源且緩衝只剩 {-gap} 天（門檻 {tight} 天）")

    # 供應商說會延但沒給日期：上面只能拿原承諾日算，缺料天數必然被低估。
    # 這種單最需要企劃立刻追日期，至少排 P2，不能因為「看似有緩衝」而被放掉。
    if change == ChangeType.DELAY.value and _d(record.get("new_eta")) is None:
        reasons.insert(0, "供應商表示會延遲但未給新日期；以下以原承諾日估計，實際可能更晚")
        if priority == "P3":
            priority = "P2"

    return {**out, "priority": priority, "gap_days": gap,
            "conservative_eta": conservative.isoformat(),
            "delay_days_est": delay,
            "delay_basis": estimate["basis"] if available else "",
            "delay_n": int(estimate["n"]) if available else 0,
            "reasons": reasons,
            "actions": suggest_actions(priority, gap, record, po, material)}
```

- [ ] **Step 4: 確認通過**

```bash
py -X utf8 -m pytest tests/test_triage.py -v
```
Expected: 全部 passed。

- [ ] **Step 5: Commit**

```bash
git add src/triage.py tests/test_triage.py
git commit -F - <<'EOF'
feat(triage): 以預估缺料天數分級並給出建議動作

舊的十條加權分數權重無依據，且校準用的是自己產生的資料。新版排序鍵
是保守到料日減需求日，可用兩個日期驗算；分級只看有無缺料與三個事實
旗標。建議動作遵守 AVL：替代料未驗證前只寫「請品保確認」，詢價寫成
「與採購確認」，不寫成本與可行性。

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
```

---

### Task 4: 時間切分回測（`backtest.py`）

**為什麼：** 履歷寫的是「用過去收貨紀錄推導準交率與到料估計，來驗證優先排序的標準」。合成資料上不能用「學權重」驗證（循環論證），能驗證的是**方法**：估計有沒有校準、有沒有偷看未來、排序有沒有贏簡單做法。

**Files:**
- Create: `src/backtest.py`
- Test: `tests/test_backtest.py`

- [ ] **Step 1: 寫失敗的測試**

建立 `tests/test_backtest.py`：

```python
# -*- coding: utf-8 -*-
"""
回測的測試。守住的只有一件事：**不可以偷看未來。**

偷看未來的回測數字會漂亮得不像話，而上線時根本沒有那些資料。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import backtest  # noqa: E402


def _row(po, sup, delay, resched, committed, receipt, need, notice, otd=0.85):
    return {"po_no": po, "supplier_id": sup, "delay_days": delay,
            "reschedule_count": resched, "committed_date": committed,
            "receipt_date": receipt, "need_date": need,
            "notice_date": notice, "vendor_otd": otd}


def _toy() -> pd.DataFrame:
    """A 供應商：上半年 30 張改期單全都準時；下半年才開始變糟（延 20 天）。"""
    rows = [_row(f"E{i}", "A", 0, 1, "2026-01-20", "2026-01-20", "2026-02-20",
                 "2026-01-10") for i in range(30)]
    rows += [_row(f"L{i}", "A", 20, 1, "2026-07-01", "2026-07-21", "2026-08-01",
                  "2026-06-20") for i in range(30)]
    # 唯一的測試單：通知日 2026-05-01，此時只知道上半年那些全準時的單
    rows.append(_row("T1", "A", 20, 1, "2026-05-10", "2026-05-30", "2026-05-25",
                     "2026-05-01"))
    return pd.DataFrame(rows)


def test_training_slice_only_contains_orders_already_received():
    df = _toy()
    train = backtest.training_slice(df, "2026-05-01")
    assert (pd.to_datetime(train["receipt_date"]) < pd.Timestamp("2026-05-01")).all()
    assert "T1" not in set(train["po_no"])
    assert not any(p.startswith("L") for p in train["po_no"])


def test_rolling_backtest_does_not_see_the_future():
    """
    回歸測試（防偷看）：T1 在 5/1 收到改期通知，當時 A 供應商過去的單全都
    準時，估計應為 0 天。若誤把下半年那 30 張延 20 天的單算進去，
    估計會變成 20 天，涵蓋率因此看起來很漂亮 —— 但那是作弊。
    """
    bt = backtest.rolling_backtest(_toy(), test_from="2026-05-01", min_samples=20)
    t1 = bt[bt["po_no"] == "T1"]
    assert len(t1) == 1
    assert t1.iloc[0]["est_80"] == 0


def test_coverage_counts_only_orders_with_an_estimate():
    bt = pd.DataFrame({"delay_days": [0, 5, 30], "est_80": [4, 4, None],
                       "est_90": [4, 4, None], "est_95": [4, 4, None]})
    rate, n = backtest.coverage(bt, 0.80)
    assert n == 2 and rate == 0.5


def test_precision_at_k_uses_the_ranking_given():
    bt = pd.DataFrame({"gap_ours": [9, 1, 5, 3], "actual_short": [True, False, True, False]})
    assert backtest.precision_at_k(bt, "gap_ours", k=2) == 1.0
    assert backtest.precision_at_k(bt, "gap_ours", k=2, ascending=True) == 0.0


def test_ranking_table_always_reports_the_random_baseline():
    bt = pd.DataFrame({"gap_ours": [3, 2, 1, 0], "gap_buffer": [0, 1, 2, 3],
                       "gap_vendor": [0.1, 0.2, 0.3, 0.4],
                       "actual_short": [True, False, True, False]})
    t = backtest.ranking_table(bt, frac=0.5)
    assert any("隨機" in s for s in t["方法"])
    assert t["前段命中率"].between(0, 1).all()
```

- [ ] **Step 2: 確認失敗**

```bash
py -X utf8 -m pytest tests/test_backtest.py -v
```
Expected: `ModuleNotFoundError: No module named 'backtest'`

- [ ] **Step 3: 實作**

建立 `src/backtest.py`：

```python
# -*- coding: utf-8 -*-
"""
時間切分回測：驗證「用過去的落差估計保守到料日」這個方法。

===========================  這驗證什麼、不驗證什麼  ===========================
驗證：
  1. 保守到料日的涵蓋率 —— 實際到貨日落在估計日期以內的比例，
     是否接近設定的百分位（80% 就該約 80%）。
  2. 排序前段的命中率 —— 排在前面的單，實際上真的缺料的比例，
     是否高於「只看供應商說的日期」「只看供應商整體準交率」這兩個簡單做法。

不驗證：
  合成資料由我們自己的因果模型產生，這裡的數字**只能說明方法沒有偷看未來、
  估計有校準**，不能說明真實供應商會照這種分布行動。
  沒贏基準就如實寫沒贏，不調參數讓它好看。

防偷看的做法：每一張測試單，只用「它收到改期通知那天以前就已收貨」的
歷史來估計（見 training_slice）。
=============================================================================
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from supplier_stats import estimate_delay, load_outcomes

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "output" / "回測結果.md"

PERCENTILES = (0.80, 0.90, 0.95)
TEST_MONTHS = 4     # 最近幾個月當測試期，之前的歷史只拿來訓練
TOP_FRACTION = 0.2  # 排序前段：取前 20%


def training_slice(outcomes: pd.DataFrame, notice_date) -> pd.DataFrame:
    """
    「通知當天已經知道」的歷史：只含通知日以前已收貨的單。

    通知日之後才收的貨，在當天是未來。把它們算進去，涵蓋率會漂亮得
    不像話，而上線時根本沒有那些資料。
    """
    return outcomes[pd.to_datetime(outcomes["receipt_date"]) < pd.Timestamp(notice_date)]


def rolling_backtest(outcomes: pd.DataFrame, test_from, *, min_samples: int = 20,
                     percentiles=PERCENTILES) -> pd.DataFrame:
    """對測試期內每一張改期過的單，用當時已知的歷史算估計，再對照實際結果。"""
    o = outcomes.copy()
    o["_notice"] = pd.to_datetime(o["notice_date"])
    tests = o[(o["reschedule_count"] >= 1) & (o["_notice"] >= pd.Timestamp(test_from))]

    rows = []
    for _, t in tests.iterrows():
        train = training_slice(outcomes, t["_notice"])
        row = {"po_no": t["po_no"], "supplier_id": t["supplier_id"],
               "delay_days": int(t["delay_days"]),
               "committed": pd.Timestamp(t["committed_date"]),
               "need": pd.Timestamp(t["need_date"]),
               "vendor_otd": float(t["vendor_otd"]),
               "actual_short": bool(pd.Timestamp(t["receipt_date"])
                                    > pd.Timestamp(t["need_date"]))}
        for p in percentiles:
            est = estimate_delay(train, t["supplier_id"], percentile=p,
                                 min_samples=min_samples)
            row[f"est_{int(round(p * 100))}"] = est["delay_days"] if est["available"] else None
        rows.append(row)

    bt = pd.DataFrame(rows)
    if bt.empty:
        return bt
    bt["buffer_days"] = (bt["need"] - bt["committed"]).dt.days
    # 越大越該排前面（越缺）。
    bt["gap_ours"] = bt["est_80"].fillna(0) - bt["buffer_days"]   # 樣本不足時退回只看說定日期
    bt["gap_buffer"] = -bt["buffer_days"]                          # 基準 A：只看緩衝
    bt["gap_vendor"] = 1 - bt["vendor_otd"]                        # 基準 B：只看整體準交率
    return bt


def coverage(bt: pd.DataFrame, percentile: float) -> tuple[float | None, int]:
    """實際到貨落差 ≤ 估計天數的比例；只計有估計的單。"""
    col = f"est_{int(round(percentile * 100))}"
    sub = bt[bt[col].notna()]
    if sub.empty:
        return None, 0
    return float((sub["delay_days"] <= sub[col]).mean()), int(len(sub))


def precision_at_k(bt: pd.DataFrame, score_col: str, k: int,
                   ascending: bool = False) -> float:
    """依 score_col 排序後，前 k 張單真的缺料的比例。"""
    top = bt.sort_values(score_col, ascending=ascending, kind="mergesort").head(k)
    return float(top["actual_short"].mean())


def ranking_table(bt: pd.DataFrame, frac: float = TOP_FRACTION) -> pd.DataFrame:
    k = max(1, int(round(len(bt) * frac)))
    rows = [
        ("本工具：預估缺料天數", precision_at_k(bt, "gap_ours", k)),
        ("基準 A：只看供應商說的日期（緩衝天數）", precision_at_k(bt, "gap_buffer", k)),
        ("基準 B：只看供應商整體準交率（低者優先）", precision_at_k(bt, "gap_vendor", k)),
        ("隨機（等於這批單實際缺料的比例）", float(bt["actual_short"].mean())),
    ]
    out = pd.DataFrame(rows, columns=["方法", "前段命中率"])
    out.attrs["k"] = k
    return out


def run(as_of: date | None = None) -> dict:
    import yaml
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    if as_of is None:
        as_of = date.fromisoformat(cfg["data_generation"]["as_of_date"])
    min_samples = int(cfg["triage"]["min_samples"])
    test_from = as_of - timedelta(days=30 * TEST_MONTHS)
    outcomes = load_outcomes()
    bt = rolling_backtest(outcomes, test_from, min_samples=min_samples)
    return {"bt": bt, "test_from": test_from, "as_of": as_of,
            "n_history": len(outcomes)}


def write_report(res: dict, path: Path = OUT) -> str:
    bt = res["bt"]
    cov_rows = []
    for p in PERCENTILES:
        rate, n = coverage(bt, p)
        cov_rows.append((f"P{int(round(p * 100))}", f"{p:.0%}",
                         "—" if rate is None else f"{rate:.1%}", n))
    cov = pd.DataFrame(cov_rows, columns=["百分位", "預期涵蓋率", "實際涵蓋率", "有估計的單數"])
    rank = ranking_table(bt)
    rank["前段命中率"] = rank["前段命中率"].map(lambda v: f"{v:.1%}")
    no_est = int(bt["est_80"].isna().sum())

    text = f"""# 時間切分回測結果

> ⚠️ **合成資料。** 歷史結果由 `src/generate_history.py` 的因果模型產生。
> 這份回測驗證的是**方法**（估計有沒有校準、有沒有偷看未來），
> **不能證明真實供應商會照這種分布行動。**

## 設定

- 歷史已結案單：**{res['n_history']}** 張
- 測試期：通知日 ≥ {res['test_from']}（最近 {TEST_MONTHS} 個月），
  測試單 **{len(bt)}** 張（都是曾改期過的單，其中 {no_est} 張當時樣本不足、退回只看說定日期）
- 防偷看：每張測試單只用「它收到改期通知那天以前就已收貨」的單來估計
- 實際缺料的定義：實際收貨日 > 下游需求日；這批測試單的缺料比例為 **{bt['actual_short'].mean():.1%}**

## 一、保守到料日的涵蓋率

實際到貨日落在估計日期以內的比例，應該接近預期。差太多代表估計不準。

{cov.to_markdown(index=False)}

## 二、排序前段的命中率（前 {int(TOP_FRACTION * 100)}%，共 {rank.attrs['k']} 張）

排在前面的單，實際上真的缺料的比例。**沒贏基準就是沒贏。**

{rank.to_markdown(index=False)}

## 怎麼讀這份結果

- 涵蓋率在測試期沒有偏離預期，只說明「用過去落差估計」這個方法在**有供應商表現隨時間變化**
  的合成資料上仍然站得住腳；那些變化是我們自己寫進產生器的，見 `SUPPLIER_DRIFT`。
- 前段命中率是把測試期的單放在一起排序，不是逐日模擬企劃每天面對的清單。
- 兩個基準都是實務上真的會用的簡單做法，不是刻意做弱的稻草人。
- 基準 A 與本工具的差別只在「有沒有加上這家供應商過去的落差」，
  所以兩者的差距就是歷史資料的貢獻。
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return text


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(write_report(run()))
    print(f"\n[OK] 已寫入 {OUT}")
```

- [ ] **Step 4: 確認 `to_markdown` 可用**

```bash
py -X utf8 -c "import tabulate; print(tabulate.__version__)"
```
若 `ModuleNotFoundError`：`pandas.DataFrame.to_markdown` 需要 `tabulate`。先檢查 `requirements.txt` 是否已有（舊的校準報表就用了 `to_markdown`，應該已在）。若沒有，加入 `requirements.txt` 並 `py -m pip install tabulate`。

- [ ] **Step 5: 確認通過**

```bash
py -X utf8 -m pytest tests/test_backtest.py -v
```
Expected: 5 passed。

- [ ] **Step 6: Commit**

```bash
git add src/backtest.py tests/test_backtest.py
git commit -F - <<'EOF'
feat(backtest): 時間切分回測，驗證估計方法而非學權重

每張測試單只用「收到改期通知那天以前已收貨」的歷史來估計，避免偷看
未來。報涵蓋率（P80/P90/P95）與排序前段命中率，並對照兩個簡單基準與
隨機。合成資料上只能驗證方法，報告開頭已聲明；沒贏基準就如實寫沒贏。

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
```

---

### Task 5: 切換 —— 以新分級取代舊權重

**這一步一次動到 pipeline、config、UI、benefit、draft 並刪除舊檔**，因為它們彼此耦合，拆開會讓 App 在中途無法啟動。做完後一次 commit。

**Files:**
- Modify: `config.yaml`、`src/pipeline.py`、`src/benefit.py`、`src/draft.py`、`app.py`、`tests/test_extraction.py`
- Delete: `src/impact.py`、`src/calibrate.py`、`tests/test_calibration.py`、`output/權重校準.md`
- Test: `tests/test_pipeline_triage.py`（新增）

- [ ] **Step 1: 寫失敗的流程測試**

建立 `tests/test_pipeline_triage.py`：

```python
# -*- coding: utf-8 -*-
"""
端到端：信件 → 對位 → 預估缺料天數 → 分級排序。

只跑規則層（use_llm=False），完全離線，不呼叫任何 API。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

pytestmark = pytest.mark.skipif(
    not ((ROOT / "data" / "erp_sim.db").exists()
         and (ROOT / "data" / "inbox" / "_index.json").exists()),
    reason="需先執行 generate_data.py、build_erp_db.py、generate_history.py")


@pytest.fixture(scope="module")
def result():
    import pipeline
    return pipeline.run(use_llm=False)


def test_weighted_score_is_gone(result):
    cols = set(result["actions"].columns)
    assert "impact_score" not in cols and "rule_details" not in cols
    assert {"gap_days", "conservative_eta", "reasons", "actions"} <= cols


def test_list_is_sorted_by_tier_then_shortage_days(result):
    from triage import PRIORITY_RANK
    df = result["actions"]
    ranks = df["priority"].map(PRIORITY_RANK).tolist()
    assert ranks == sorted(ranks), "分級必須由 P1 排到 P3"
    for tier, g in df.groupby("priority"):
        gaps = g["gap_days"].dropna().tolist()
        assert gaps == sorted(gaps, reverse=True), f"{tier} 內須依缺料天數由大到小"


def test_p1_means_shortage_and_a_critical_flag(result):
    p1 = result["actions"][result["actions"]["priority"] == "P1"]
    assert (p1["gap_days"] > 0).all()
    critical = p1["downstream_scheduled"].astype(bool) | p1["is_bottleneck"].astype(bool)
    assert critical.all()


def test_every_row_explains_itself(result):
    df = result["actions"]
    assert df["reasons"].map(len).gt(0).all()


def test_unconfirmed_dates_still_need_human_review(result):
    """承諾強度非 confirmed 的一律不寫回，這道閘門不能因為改版而消失。"""
    df = result["actions"]
    weak = df["commitment_strength"].isin(["estimated", "intent_only"])
    assert df.loc[weak, "needs_human_review"].all()
```

- [ ] **Step 2: 確認失敗**

```bash
py -X utf8 -m pytest tests/test_pipeline_triage.py -v
```
Expected: FAIL（`impact_score` 仍在欄位中）。

- [ ] **Step 3: 改 `config.yaml`**

檔頭註解（第 1–8 行）的「設計原則」段改為：

```yaml
# =====================================================================
# 供應商交期回覆智能解析與追料排序工具 — 參數設定
#
# 設計原則：所有領域判斷都外部化到這個檔案，不寫死在程式裡。
#   理由：內部工具的第一版一定會被使用者挑戰「你憑什麼這樣排」。
#   排序本身沒有權重（預估缺料天數 = 保守到料日 − 需求日，可以驗算）；
#   這裡放的是百分位與門檻等領域假設，物料企劃可以質疑並調整。
# =====================================================================
```

把「影響評估規則：10 條規則的權重」整段（從 `# ------...` 分隔線起，到 `# 其餘為 P3：留意即可` 為止，共 `impact_weights:` 與 `priority_thresholds:` 兩個區塊）換成：

```yaml
# ---------------------------------------------------------------------
# 交期風險分級（取代舊的十條加權分數，見 docs/設計決策.md 決策 16）
#   預估缺料天數 = 保守到料日 − 下游需求日
#   保守到料日   = 供應商說的日期 + 這家供應商過去「改期過的單」的落差百分位
#   P1 = 預估缺料，且下游已排定或為瓶頸料
#   P2 = 預估缺料；或緩衝 ≤ tight_buffer_days 且無已認證二源
#   P3 = 其餘
# ---------------------------------------------------------------------
triage:
  min_samples: 20               # 歷史單少於此數，不提供保守估計（不給假精確）
  # 領域假設：承諾越不可靠，取越保守的歷史百分位。數字是假設，
  # 回測只檢驗涵蓋率，不能證明這三個數字「對」。
  percentile_confirmed: 0.80
  percentile_estimated: 0.90
  percentile_intent_only: 0.95  # 「沒給日期」也歸這一級
  # 領域假設：緩衝只剩 3 天內視為緊；請依實際經驗調整。
  tight_buffer_days: 3
```

- [ ] **Step 4: 改 `src/pipeline.py`**

(a) import 區：把 `from impact import evaluate` 換成

```python
import triage
from supplier_stats import estimate_delay, load_outcomes
```
並在 `from domain import ...` 之後保留原有 import。模組 docstring 第 1–3 條裡的「影響評估」字樣改為「分級」即可（不需改動其他敘述）。

(b) 在 `load_ground_truth` 之後新增：

```python
_OUTCOME_COLUMNS = ["supplier_id", "delay_days", "reschedule_count"]


def _load_outcomes() -> pd.DataFrame:
    """
    歷史收貨紀錄。讀不到時回傳空表 —— 之後每一筆都會明確標示
    「歷史樣本不足」，而不是悄悄假裝有估計。
    """
    try:
        return load_outcomes()
    except (FileNotFoundError, RuntimeError):
        return pd.DataFrame(columns=_OUTCOME_COLUMNS)


def _sort_by_urgency(df: pd.DataFrame) -> pd.DataFrame:
    """先依分級，同級內依預估缺料天數由大到小，最後依收信時間。"""
    rank = df["priority"].map(triage.PRIORITY_RANK).fillna(9)
    return (df.assign(_rank=rank)
              .sort_values(["_rank", "gap_days", "received_at"],
                           ascending=[True, False, True], na_position="last")
              .drop(columns="_rank").reset_index(drop=True))
```

(c) `run` 的簽名與開頭（原第 124–130 行）改為：

```python
def run(cfg: dict | None = None, use_llm: bool = True) -> dict:
    """執行完整流程，回傳可直接餵給 UI 的結果集。"""
    cfg = cfg or load_config()
    tcfg = cfg["triage"]
    as_of = date.fromisoformat(cfg["data_generation"]["as_of_date"])
```
在 `sup_index = ...` 那行之後加入：

```python
    outcomes = _load_outcomes()
```

(d) 對不到 PO 的那筆 `rows.append({...})` 中，把 `"impact_score": 0.0, "priority": "待查", "top_reasons": [...]` 換成：

```python
                             "gap_days": None, "priority": "待查",
                             "reasons": ["信中的 PO 號對不到主檔，需人工確認"],
                             "actions": [],
```

(e) 把 `result = evaluate(rec, po, material, supplier, weights, thresholds)` 換成：

```python
            # 信裡沒給新日期時，不論模型把承諾強度判成什麼，都取最保守的百分位。
            strength = (rec.get("commitment_strength") if rec.get("new_eta")
                        else "none")
            pct = triage.percentile_for(strength, tcfg)
            estimate = estimate_delay(outcomes, po["supplier_id"], percentile=pct,
                                      min_samples=int(tcfg["min_samples"]))
            result = triage.evaluate(rec, po, material, tcfg, estimate)
```

(f) 主要 `rows.append({...})` 中，刪掉「以下欄位是為了讓 UI 能在使用者調整權重時就地重算…」那三行註解，並把

```python
                "impact_score": result["impact_score"],
                "priority": result["priority"],
                "top_reasons": result["top_reasons"],
                "rule_details": result["rule_details"],
                "note": result["note"],
```
換成：

```python
                "gap_days": result["gap_days"],
                "conservative_eta": result["conservative_eta"],
                "delay_days_est": result["delay_days_est"],
                "delay_basis": result["delay_basis"],
                "delay_n": result["delay_n"],
                "priority": result["priority"],
                "reasons": result["reasons"],
                "actions": result["actions"],
                "note": result["note"],
```

(g) 把

```python
    actionable = actionable.sort_values(
        ["impact_score", "received_at"], ascending=[False, True]).reset_index(drop=True)
```
換成 `actionable = _sort_by_urgency(actionable)`。

(h) 刪掉整個 `rescore` 函式（原第 269–302 行）。`__main__` 區塊的 `cols` 中 `"impact_score"` 換成 `"gap_days"`。

- [ ] **Step 5: 刪除舊檔並整理測試**

```bash
git rm src/impact.py src/calibrate.py tests/test_calibration.py output/權重校準.md
```

`tests/test_extraction.py`：
- 刪掉 `from impact import evaluate  # noqa: E402` 那一行。
- 刪掉從 `# 影響評估：方向性必須正確` 分隔線起到檔尾的全部內容（`WEIGHTS`、`THRESH`、`BASE_PO`、`_score`、以及所有 `test_*_raises_impact`、`test_no_change_scores_zero…`、`test_pull_in_is_capped`、`test_nan_alt_material…`、`test_clean_str…`）。這些行為已由 `tests/test_triage.py` 覆蓋。
- 若 `date`、`ChangeType`、`CommitmentStrength`、`pytest` 在剩餘內容中已不再使用，刪除對應 import：

```bash
grep -n "date\b\|ChangeType\|CommitmentStrength\|pytest\." tests/test_extraction.py
```
只留仍有使用者。

- [ ] **Step 6: 改 `src/benefit.py`**

(a) 模組 docstring 的「關於 Recall@K 的循環性」整段（第 17–25 行）換成：

```python
關於 Recall@K 的循環性（必須說明，不可省略）：
    下面用「新預計到料日 − 需求日 > 0」當作 outcome，這是兩個日期相減的
    客觀事實。但排序鍵「預估缺料天數」是同一個式子再加上歷史落差，
    所以這個比較對本工具有利，只能說明排序有把缺料訊號推到前段。

    真正的驗證是 src/backtest.py 的時間切分回測（用實際收貨日當 outcome、
    不偷看未來），結果見 output/回測結果.md。
```

(b) `compare_strategies` 內 `{"策略": "本工具（影響分數排序）", "Recall@K": recall_at_k(df, "impact_score", k)},` 換成：

```python
        {"策略": "本工具（預估缺料天數排序）", "Recall@K": recall_at_k(df, "gap_days", k)},
```
（`gap_days` 為 NaN 的列，`sort_values` 預設排在最後，符合預期。）

- [ ] **Step 7: 改 `src/draft.py`**

(a) `PROMPT` 第一行改為 `你是半導體公司的物料企劃，正要回信給供應商窗口，追一張交期有變的採購單。`
(b) `- 系統判定影響程度：{priority}（影響分數 {impact}）` 換成：

```
- 系統判定：{priority}（預估缺料天數 {gap}；正數代表預估來不及，負數代表尚有緩衝）
```
(c) `generate` 中：`reasons = row.get("top_reasons") or []` 改成 `row.get("reasons") or []`；`priority=row.get("priority"), impact=row.get("impact_score"),` 改成 `priority=row.get("priority"), gap=row.get("gap_days"),`。
(d) `_template_draft` 中 `f"本案經系統評估影響程度為 {row.get('priority')}。{ask}\n\n"` 改成 `f"本案經系統評估為 {row.get('priority')}。{ask}\n\n"`；頁尾 `（本草稿由供應商交期回覆解析工具產生，寄出前請自行確認內容與語氣）` 保留。

- [ ] **Step 8: 改 `app.py`（最小改動，Plan 2 再重排）**

建立暫存腳本 `C:\Users\User\AppData\Local\Temp\claude\C--Users-User-Desktop-supplier-eta-triage\468e31c6-6cae-4e7f-b1be-bcaf8330d656\scratchpad\migrate_app.py`：

```python
from pathlib import Path

p = Path(r"C:\Users\User\Desktop\supplier-eta-triage\app.py")
s = p.read_text(encoding="utf-8")


def cut(text, start, end, new=""):
    i = text.index(start)
    j = text.index(end, i)
    return text[:i] + new + text[j:]


def sub(text, old, new, count=1):
    assert text.count(old) == count, (text.count(old), old[:50])
    return text.replace(old, new)


# 1. 側邊欄：移除權重與門檻滑桿
s = cut(s, '    st.sidebar.divider()\n    st.sidebar.subheader("① 影響評估權重")',
        '    st.sidebar.divider()\n    st.sidebar.subheader("③ 效益試算參數")')
s = sub(s, 'st.sidebar.subheader("③ 效益試算參數")', 'st.sidebar.subheader("效益試算參數")')
s = sub(s, "生管每日可仔細追的案件數 (K)", "物料企劃每日可仔細追的案件數 (K)")
s = sub(s, '    thresholds = {"P1": p1, "P2": max(p2, 0)}\n\n', "")

# 2. 不再就地重算分數
s = sub(s, '    actions = pipeline.rescore(result["all"], weights, thresholds)\n'
           '    actions = actions[actions["priority"] != "—"].reset_index(drop=True)\n',
        '    actions = result["actions"].copy()\n')

# 3. 分頁名稱
s = sub(s, '"⚖️ 供應商歷史與權重"', '"⚖️ 供應商歷史"')
s = sub(s, "t_calib", "t_history", count=2)

# 4. 行動清單分頁
s = sub(s, 'st.caption("依影響分數排序。展開任一筆可看到十條規則各拿幾分，以及回信草稿。")',
        'st.caption("依預估缺料天數排序（缺越多天越前面）。展開任一筆可看到為什麼、建議動作，以及回信草稿。")')
s = sub(s, 'view[["priority", "impact_score", "po_no", "material_id",',
        'view[["priority", "gap_days", "conservative_eta", "po_no", "material_id",')
s = sub(s, '"priority": "優先級", "impact_score": "影響分數", "po_no": "採購單號",',
        '"priority": "優先級", "gap_days": "預估缺料天數", "conservative_eta": "保守到料日",\n'
        '                    "po_no": "採購單號",')
s = sub(s, "（影響分數 {row['impact_score']}）{flag}\")",
        "（{_fmt_gap(row['gap_days'])}）{flag}\")")
s = sub(s, 'for r in (row["top_reasons"] or []):', 'for r in (row["reasons"] or []):')
s = cut(s, '                        st.markdown("**十條規則明細**")', '                    with b:',
        '                        if row["actions"]:\n'
        '                            st.markdown("**建議動作**")\n'
        '                            for a in row["actions"]:\n'
        '                                st.markdown(f"- {a}")\n')
s = cut(s, '                        # 供應商歷史表現：把 ERP 收貨紀錄變成當下用得到的判斷依據。',
        '                        st.markdown("**原始信件**")')
s = sub(s, 'view.drop(columns=["rule_details", "top_reasons"], errors="ignore")\n'
           '                    .to_csv(index=False).encode("utf-8-sig"),',
        'view.assign(reasons=view["reasons"].map("；".join),\n'
        '                            actions=view["actions"].map("；".join))\n'
        '                    .to_csv(index=False).encode("utf-8-sig"),')

# 5. 權重校準分頁 → 供應商歷史分頁
s = cut(s, '    # ==================== 分頁 7：權重校準 ====================',
        '    # ==================== 物料智能檢索（RAG） ====================',
'''    # ==================== 分頁 7：供應商歷史 ====================
    with t_history:
        st.subheader("供應商歷史表現")
        st.caption(
            "行動清單上的「保守到料日」來自這裡：這家供應商說定日期之後，"
            "過去實際還會晚幾天。只用「曾改期過的單」估計，因為你收到的都是已經跳票的通知。")
        st.warning(
            "⚠️ **本頁使用模擬歷史資料。** 歷史結果由 `src/generate_history.py` 的因果模型產生，"
            "非真實資料；在真實環境，輸入應該是 ERP 的收貨紀錄。")
        try:
            pcol, rcol = st.columns([3, 2])
            with pcol:
                st.markdown("**各供應商歷史表現**")
                st.dataframe(_supplier_performance(), width="stretch",
                             hide_index=True, height=300)
            with rcol:
                st.markdown("**改期次數 vs 最終是否延遲**")
                st.dataframe(_reschedule_reliability(), width="stretch",
                             hide_index=True)
                st.caption(
                    "改期越多次的單，最終仍延遲的比例是否越高？這是保守到料日"
                    "只用「改期過的單」的依據。若資料顯示無關，估計就不該把改期單獨立出來。")
        except (FileNotFoundError, RuntimeError) as e:
            st.info(f"{e}\\n\\n請先執行： `py src/generate_history.py`")

''')

# 6. 移除校準與舊的單筆歷史查詢；加入缺料天數格式化
s = cut(s, '@st.cache_data(show_spinner="校準中…")',
        '@st.cache_data(show_spinner=False)\ndef _supplier_performance():')
s = cut(s, 'def _supplier_history(supplier_id, promised):', 'if __name__ == "__main__":',
'''def _fmt_gap(gap) -> str:
    """預估缺料天數的人話：缺 N 天／尚有 N 天緩衝。"""
    if gap is None or gap != gap:
        return "無法估計"
    g = int(gap)
    return f"預估缺料 {g} 天" if g > 0 else f"尚有 {-g} 天緩衝"


''')

p.write_text(s, encoding="utf-8")
print("app.py migrated")
```

執行並檢查：

```bash
py -X utf8 "C:/Users/User/AppData/Local/Temp/claude/C--Users-User-Desktop-supplier-eta-triage/468e31c6-6cae-4e7f-b1be-bcaf8330d656/scratchpad/migrate_app.py"
grep -n "labels\|weights\|thresholds\|impact\|rescore\|calibrate\|rule_details\|top_reasons" app.py
py -X utf8 -c "import ast,sys; ast.parse(open('app.py',encoding='utf-8').read()); print('syntax ok')"
```
Expected：`app.py migrated`；grep **沒有任何輸出**（若有，逐一處理，多半是剩下的 `labels` 引用）；`syntax ok`。

- [ ] **Step 9: 跑全部測試**

```bash
py -X utf8 -m pytest tests -q
```
Expected: 全部 passed（數量會與 81 不同，因為刪了校準與影響評估測試、新增其他測試）。**記下實際通過數，不要沿用舊的 81。**

- [ ] **Step 10: Commit**

```bash
git add -A
git commit -F - <<'EOF'
refactor: 以預估缺料天數分級取代十條加權分數與權重校準

舊權重是人手填的，校準用的又是自己產生的歷史資料，等於用答案驗答案。
改為兩個日期相減的預估缺料天數，分級只看有無缺料與三個事實旗標。
移除 impact.py、calibrate.py、權重滑桿與校準分頁；App 只做最小改動讓
它仍可運作，介面重排留給下一份計畫。README 與 CLAUDE.md 暫時仍描述
舊做法，之後統一更新。

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
```

---

### Task 6: 重新產生歷史資料、跑回測、離線冒煙測試

**Files:**
- Modify（產出）: `output/回測結果.md`（新增）

- [ ] **Step 1: 重新產生歷史資料（冪等，會套用 drift）**

```bash
py -X utf8 src/generate_history.py
```
Expected: 印出「歷史單據已寫入」與筆數；筆數與 CLAUDE.md 記載的 898 **可能不同**（機率變了），以實際輸出為準。

- [ ] **Step 2: 確認供應商表現真的隨時間改變**

```bash
py -X utf8 - <<'EOF'
import sys; sys.path.insert(0, "src")
import supplier_stats as ss, pandas as pd
df = ss.load_outcomes()
df["late"] = df["delay_days"] > 0
df["half"] = (pd.to_datetime(df["committed_date"]) >= "2026-04-01").map({False: "前期", True: "後期"})
print(df[df.supplier_id.isin(["SUP-S02", "SUP-F03"])].groupby(["supplier_id", "half"])["late"].agg(["size", "mean"]).round(3))
EOF
```
Expected: SUP-S02 後期延遲比例明顯高於前期；SUP-F03 明顯低於前期。

- [ ] **Step 3: 跑全部測試**

```bash
py -X utf8 -m pytest tests -q
```
Expected: 全部 passed。

- [ ] **Step 4: 跑回測，產出報告**

```bash
py -X utf8 src/backtest.py
```
Expected: 印出報告並寫入 `output/回測結果.md`。

- [ ] **Step 5: 如實檢視結果（不可調參數讓它好看）**

讀 `output/回測結果.md`，回答三個問題並**原樣告訴使用者**：
1. 三個百分位的實際涵蓋率，離預期（80/90/95%）差多少？
2. 本工具的前段命中率，有沒有高於基準 A、基準 B？
3. 有估計的單數是否足夠（每個百分位至少 30 張才有意義；不足就明說）。

若本工具沒贏基準，**不改 `SUPPLIER_DRIFT`、不改百分位、不改測試期**。把結果如實留在報告，並在 Task 7 的設計決策裡寫進「回測結果」一節。這正是 CLAUDE.md 原則 4：評估必須容許推翻假設。

- [ ] **Step 6: 離線冒煙測試 App（不呼叫任何 API）**

```bash
cd /c/Users/User/Desktop/supplier-eta-triage && LLM_PROVIDER=none py -X utf8 - <<'EOF'
from streamlit.testing.v1 import AppTest
at = AppTest.from_file("app.py", default_timeout=180).run()
assert not at.exception, at.exception
print("tabs:", len(at.tabs), "| metrics:", [m.label for m in at.metric][:6])
EOF
```
Expected: 無例外；metrics 含「收到信件」「今天要處理 P1」。若出現 `Gemini`、`embedding`、`429` 字樣，代表有東西打到 API：立刻停下並回報。

- [ ] **Step 7: 看一眼行動清單前五名是否合理**

```bash
LLM_PROVIDER=none py -X utf8 src/pipeline.py 2>&1 | head -30
```
Expected: 依 P1 → P2 → P3 排列；P1 的 `gap_days` 全為正數。把前五筆與其 `reasons` 貼給使用者，請他用物料企劃的眼光判斷排序是否合理。

- [ ] **Step 8: Commit 報告**

```bash
git add output/回測結果.md
git commit -F - <<'EOF'
docs: 新增時間切分回測結果

數字由 src/backtest.py 實際跑出，合成資料上只驗證方法。

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
```

---

### Task 7: 設計決策 16

**Files:**
- Modify: `docs/設計決策.md`（在「已知限制」章節之前插入，並更新已知限制第 1 點）

- [ ] **Step 1: 插入決策 16**

在 `## 已知限制（完整清單見 README「已知限制」）` 之前插入（**回測數字不寫進來，只引用 `output/回測結果.md`**，避免文件與報告不一致；若 Task 6 Step 5 的結果值得說明，用文字描述結論，不抄數字）：

```markdown
## 決策 16：放棄十條加權分數，改以「預估缺料天數」排序

**背景**：原本用十條規則各給 0~1 分，再依人手填的權重加成 0~100 的影響分數。
後來用歷史資料做邏輯迴歸校準，發現三條規則「方向相反」。

**問題有兩個**：
1. 權重是人手填的數字（25、10、12…），沒有依據，在會議上被問「25 是怎麼來的」答不出來。
2. 校準用的歷史結果是自己寫的因果模型產生的。學到的係數只是把產生器的假設讀回來，
   這是用答案驗答案。校準報告本身也承認：評分卡混合了「發生機率」與「影響代價」，
   歷史資料只能校準機率那一半。

**採用的做法**：
- 排序鍵改成兩個日期相減：`預估缺料天數 = 保守到料日 − 下游需求日`。
  可以在會議上逐項驗算，不需要解釋任何權重。
- 保守到料日 = 供應商說的日期 + 這家供應商「改期過的單」的歷史落差百分位。
  只用改期過的單，因為企劃收到的是已經跳票的通知；拿全部單（含準時的）估會低估。
  樣本不足 20 筆退回全部單並標明，全部單也不足就回報樣本不足，不給假精確。
- 分級只看「有沒有缺料」與三個事實旗標：
  P1 = 預估缺料且（下游已排定或瓶頸料）；
  P2 = 預估缺料，或緩衝 ≤ 3 天且無已認證二源；P3 = 其餘。
- 承諾強度決定取哪個百分位（確認 P80、暫估 P90、僅意向與未給日期 P95）。
  這三個數字是領域假設，回測只檢驗涵蓋率。
- 舊規則的事實保留為分級條件與建議動作，不再是加分。
- 建議動作遵守 AVL：替代料只寫「請品保確認是否已通過驗證」，詢價寫成「與採購確認」，
  不寫成本與可行性（資料庫沒有）。

**驗證**：`src/backtest.py` 時間切分回測。每張測試單只用「收到改期通知那天以前已收貨」
的歷史，不偷看未來。報保守到料日的涵蓋率，與排序前段命中率對照兩個簡單基準
（只看說定日期、只看供應商整體準交率）與隨機。歷史資料的供應商表現隨時間變化
（`SUPPLIER_DRIFT`），否則涵蓋率必然接近設定值。結果見 `output/回測結果.md`。

**被否決的方案**：
- **保留加權分數，改用邏輯迴歸重學權重**：在合成資料上仍是循環論證，換湯不換藥。
- **保留加權分數當「進階模式」**：兩套並存要雙重維護，也會讓人問「那你的排序到底是哪個」。
- **分級再乘上金額或客戶等級**：資料裡沒有這些欄位，加了只會又變成編出來的權重。
- **供應商 × 料別再往下切分估計落差**：898 張歷史單分給 12 家供應商，每格只剩個位數。

**代價與限制**：
- 歷史資料是合成的，回測只能證明方法沒偷看未來、估計有校準，不能證明真實供應商的行為。
- 落差估計是歷史統計，不是預測模型；它不知道「這一張單」會怎樣。
- `need_date` 視為 MRP 淨需求日（已扣庫存），標為領域假設；未扣庫存的需求日會高估缺料。
- 分批交貨仍只取最晚一筆，下一份計畫處理。

**踩坑紀錄**：舊版 `benefit.py` 的 Recall@K 用「新到料日 − 需求日 > 0」當 outcome，
而舊評分卡規則 1 用的是同一個訊號，比較對本工具有利。現在仍保留那個比較但在文件中
標明循環性；真正的驗證改由回測負責。

---
```

- [ ] **Step 2: 更新已知限制**

`## 已知限制` 三項之後，新增第 4 點：

```markdown
4. **README 與 CLAUDE.md 尚未同步新排序邏輯。** 決策 16 已生效，
   文件其餘部分（README 的「權重校準」、CLAUDE.md 的環境與必做事項）留待收尾統一更新。
```
（此點在 Plan 3 收尾時刪除。）

- [ ] **Step 3: 最終驗證並 commit**

```bash
py -X utf8 -m pytest tests -q
git add docs/設計決策.md
git commit -F - <<'EOF'
docs: 新增決策 16，記錄放棄加權分數的原因與被否決的替代方案

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
git log --oneline | head -8
```
Expected: 測試全過；最近 commit 依序為決策 16、回測結果、切換、回測、分級、落差估計、歷史加入時間變化。

---

## 自我檢查

**共識對照**（使用者已確認的項目 → 對應任務）：

| 共識 | 任務 |
|---|---|
| 主排序鍵換成預估缺料天數（問題 1） | Task 3、5 |
| 只用改期過的單估計、樣本不足退回並標明（問題 2） | Task 2 |
| 時間切分回測＋供應商表現隨時間變化（問題 4） | Task 1、4、6 |
| 直接移除舊權重與校準並記錄決策（問題 5） | Task 5、7 |
| P1/P2/P3 分級條件、3 天門檻在 config（問題 6） | Task 3、5 |
| 建議動作遵守 AVL（問題 9） | Task 3 |
| 承諾強度決定百分位 | Task 3（`percentile_for`）、Task 5（config） |
| 需求日為淨需求日的領域假設 | Task 3 docstring、Task 7 |
| 兩區介面、稱呼改物料企劃、匯出、確認紀錄、專案說明區（問題 3、8、10、11） | **Plan 2** |
| 分批交貨、prompt 統一、重跑評估與種子快取、README/CLAUDE.md、部署（問題 3、7、8） | **Plan 3** |

**已知缺口（刻意留給後續計畫，不是遺漏）**：稱呼「生管」仍出現在 `app.py`（8 處）、
`src/llm/prompts/extract_eta.md` 等；prompt 檔案的修改會使 LLM 快取失效，必須與分批交貨
一起在 Plan 3 一次處理，避免多次重跑評估。

**型別一致性**：`estimate_delay` 回傳鍵 `available/delay_days/percentile/basis/n/reason`
在 `triage.evaluate`、`backtest.rolling_backtest`、`conservative_eta` 中用法一致；
`triage.evaluate` 回傳鍵 `gap_days/conservative_eta/delay_days_est/delay_basis/delay_n/
priority/reasons/actions/note` 與 `pipeline` 寫入 rows 的欄位、`app.py`、`draft.py` 讀取的欄位一致。
