# -*- coding: utf-8 -*-
"""
首次啟動時補齊可重新生成的資料：合成資料 → 模擬 ERP 資料庫 → 歷史單據。

===========================  為什麼獨立成檔  ===========================
原本寫在 app.py 的 `_ensure_data`，用「資料庫檔案存在」當作「資料準備好了」。
但 build_erp_db 是先建檔、建表，再慢慢寫入資料。雲端第一次載入時若同時有兩個
工作階段（重新整理、分頁重連），第二個看到檔案存在就跳過建置，直接去產生
歷史單據，這時「來源清單」還是空的，於是報：

    RuntimeError: 來源清單是空的，無法產生歷史單據

整個 App 打不開。本機用兩個執行緒錯開 0.02 秒即可重現。

修正兩件事：
  1. 一把鎖，同一個伺服器行程內的工作階段排隊，不會同時建置。
  2. 「準備好了」改看**內容**（關鍵表格都有資料），不看檔案存在。
     這樣即使上次建置中途被中斷、留下半成品，下次也會判定為未完成並重建，
     而不是永遠卡在壞掉的資料庫上。
=====================================================================
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import closing, nullcontext
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent

# 這些表格都有資料，才算「基礎資料」建好了。
BASE_TABLES = ("vendor_master", "material_master", "source_list", "po_header",
               "po_item", "po_schedule", "purchase_req")
CSV_FILES = ("po_master.csv", "materials.csv", "suppliers.csv")
# 資料格式的版本標記：料號主檔加入收貨處理天數（≈ MARC-WEBAZ）之前產生的資料
# 沒有這一欄。舊格式不能「默默補 0 天」繼續用 —— 畫面會把 0 天說成料別預設，
# 所以判定為未備妥、整批重建。
MATERIAL_COLUMN = "gr_processing_days"

# Streamlit 的每個工作階段是同一個行程裡的執行緒，所以一把行程內的鎖就夠。
_LOCK = threading.Lock()


def _no_step(label: str):
    return nullcontext()


def _csv_ready(root: Path) -> bool:
    data = root / "data"
    files = [data / n for n in CSV_FILES] + [data / "inbox" / "_index.json"]
    if not all(p.exists() and p.stat().st_size > 0 for p in files):
        return False
    with open(data / "materials.csv", encoding="utf-8-sig") as f:
        return MATERIAL_COLUMN in f.readline()


def db_state(db: Path) -> tuple[bool, bool]:
    """回傳 (基礎資料是否完整, 歷史單據是否已產生)。讀不了就當作未完成。"""
    if not db.exists():
        return False, False
    try:
        with closing(sqlite3.connect(db)) as con:
            def count(table: str) -> int:
                try:
                    return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                except sqlite3.OperationalError:  # 表格不存在 = 建置未完成
                    return 0
            cols = {r[1] for r in con.execute("PRAGMA table_info(material_master)")}
            base = MATERIAL_COLUMN in cols and all(count(t) > 0 for t in BASE_TABLES)
            return base, base and count("goods_receipt") > 0
    except sqlite3.DatabaseError:
        return False, False


def _is_ready(root: Path) -> bool:
    return _csv_ready(root) and db_state(root / "data" / "erp_sim.db") == (True, True)


def _gen_data() -> None:
    import generate_data
    generate_data.main()


def _build_db() -> None:
    import build_erp_db
    build_erp_db.build(verbose=False)


def _gen_history() -> None:
    import generate_history
    generate_history.build_history(verbose=False)


def ensure_data(root: Path = ROOT, *,
                step: Callable[[str], object] = _no_step,
                gen_data: Callable[[], None] = _gen_data,
                build_db: Callable[[], None] = _build_db,
                gen_history: Callable[[], None] = _gen_history) -> None:
    """
    補齊缺少的資料。`step(標題)` 回傳一個 context manager（UI 傳入 st.spinner）。

    `root` 只用來判斷完成度；實際寫到哪裡由各產生器決定，預設兩者相同。
    三個產生器可以注入，測試才能用假的重現「建置到一半」的時序。
    """
    if _is_ready(root):      # 每次互動都會重跑腳本，備妥時要幾乎零成本
        return

    with _LOCK:
        if not _csv_ready(root):
            with step("首次啟動：正在產生合成資料…"):
                gen_data()

        db = root / "data" / "erp_sim.db"
        base, has_history = db_state(db)
        if not base:
            with step("首次啟動：正在建立模擬 ERP 資料庫…"):
                build_db()
            base, has_history = db_state(db)
            if not base:
                raise RuntimeError(
                    "模擬 ERP 資料庫建置後仍不完整（有表格沒有資料），請檢查 data/ 下的 CSV。")

        if not has_history:
            with step("首次啟動：正在產生歷史單據與收貨紀錄…"):
                gen_history()
            if not db_state(db)[1]:
                raise RuntimeError("歷史單據產生後仍是空的，請檢查 generate_history 的輸出。")
