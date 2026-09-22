# -*- coding: utf-8 -*-
"""
首次啟動補資料（bootstrap.ensure_data）的測試。

全部用假的產生器在暫存目錄裡跑，不碰真實的 data/。
假產生器刻意模擬真實的時序：先建檔與表格、隔一段時間才寫入資料。
"""
from __future__ import annotations

import sqlite3
import sys
import threading
import time
from contextlib import closing
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import bootstrap  # noqa: E402

TABLES = ("vendor_master", "material_master", "source_list", "po_header",
          "po_item", "po_schedule", "purchase_req", "goods_receipt")


class Fakes:
    """假的三個產生器，並記錄各自被呼叫幾次。"""

    def __init__(self, root: Path, build_delay: float = 0.0,
                 build_inserts: bool = True) -> None:
        self.root, self.build_delay, self.build_inserts = root, build_delay, build_inserts
        self.calls = {"data": 0, "db": 0, "history": 0}

    @property
    def db(self) -> Path:
        return self.root / "data" / "erp_sim.db"

    def gen_data(self) -> None:
        self.calls["data"] += 1
        (self.root / "data" / "inbox").mkdir(parents=True, exist_ok=True)
        for name in ("po_master.csv", "suppliers.csv"):
            (self.root / "data" / name).write_text("x\n1\n", encoding="utf-8")
        (self.root / "data" / "materials.csv").write_text(
            "material_id,gr_processing_days\nM1,1\n", encoding="utf-8")
        (self.root / "data" / "inbox" / "_index.json").write_text("[1]", encoding="utf-8")

    def build_db(self) -> None:
        self.calls["db"] += 1
        self.db.unlink(missing_ok=True)
        with closing(sqlite3.connect(self.db)) as con:
            for t in TABLES:
                extra = ", gr_processing_days INTEGER" if t == "material_master" else ""
                con.execute(f"CREATE TABLE {t} (x INTEGER{extra})")
            con.commit()  # 此刻檔案與表格都已存在，但還沒有任何資料
            time.sleep(self.build_delay)
            if self.build_inserts:
                for t in TABLES[:-1]:
                    vals = "(1, 1)" if t == "material_master" else "(1)"
                    con.execute(f"INSERT INTO {t} VALUES {vals}")
                con.commit()

    def gen_history(self) -> None:
        self.calls["history"] += 1
        with closing(sqlite3.connect(self.db)) as con:
            if con.execute("SELECT COUNT(*) FROM source_list").fetchone()[0] == 0:
                raise RuntimeError("來源清單是空的，無法產生歷史單據")
            con.execute("INSERT INTO goods_receipt VALUES (1)")
            con.commit()

    def run(self) -> None:
        bootstrap.ensure_data(self.root, gen_data=self.gen_data,
                              build_db=self.build_db, gen_history=self.gen_history)


def _state(f: Fakes) -> tuple[bool, bool]:
    return bootstrap.db_state(f.db)


def test_concurrent_sessions_do_not_race(tmp_path):
    """
    回歸測試。雲端首次載入時兩個工作階段幾乎同時進來：
    第一個剛建好資料庫檔案與表格、還沒寫入資料，第二個看到「檔案存在」
    就跳過建置，直接去產生歷史單據，於是報
    「來源清單是空的，無法產生歷史單據」，整個 App 打不開。
    本機用兩個執行緒錯開 0.02 秒即可重現。
    """
    f = Fakes(tmp_path, build_delay=0.3)
    errors: list[BaseException] = []

    def session(delay: float) -> None:
        time.sleep(delay)
        try:
            f.run()
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=session, args=(d,)) for d in (0.0, 0.02)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    assert errors == [], errors
    assert f.calls == {"data": 1, "db": 1, "history": 1}, "只該建置一次"
    assert _state(f) == (True, True)


def test_half_built_database_is_rebuilt_not_trusted(tmp_path):
    """
    上一次建置在寫入資料前被中斷（例如行程被回收），留下只有表格的資料庫。
    只看「檔案存在」會永遠卡在這個壞掉的資料庫上；改看內容才會重建。
    """
    f = Fakes(tmp_path)
    f.gen_data()
    f.calls["data"] = 0
    f.build_inserts = False
    f.build_db()                      # 留下半成品
    f.calls["db"] = 0
    f.build_inserts = True

    f.run()

    assert f.calls["db"] == 1, "半成品必須被判定為未完成並重建"
    assert _state(f) == (True, True)


def test_ready_data_triggers_no_work(tmp_path):
    """已經備妥時不做任何事 —— Streamlit 每次互動都會重跑腳本，這條路徑要很輕。"""
    f = Fakes(tmp_path)
    f.run()
    f.calls.update(data=0, db=0, history=0)

    f.run()

    assert f.calls == {"data": 0, "db": 0, "history": 0}


def test_missing_history_only_generates_history(tmp_path):
    f = Fakes(tmp_path)
    f.gen_data()
    f.build_db()
    f.calls.update(data=0, db=0, history=0)

    f.run()

    assert f.calls == {"data": 0, "db": 0, "history": 1}


def test_build_that_leaves_database_incomplete_fails_loudly(tmp_path):
    """建完仍然不完整，必須大聲報錯，而不是讓後面的步驟去撞一個看不懂的錯誤。"""
    f = Fakes(tmp_path, build_inserts=False)
    with pytest.raises(RuntimeError, match="不完整"):
        f.run()


def test_old_format_data_is_rebuilt_not_silently_used(tmp_path):
    """
    回歸：料號主檔加入收貨處理天數之前的舊資料，曾經被「補 0 天」後繼續使用，
    畫面還把 0 天寫成「料別預設」。舊格式必須判定為未備妥並整批重建。
    """
    f = Fakes(tmp_path)
    f.run()
    (tmp_path / "data" / "materials.csv").write_text("material_id\nM1\n", encoding="utf-8")
    with closing(sqlite3.connect(f.db)) as con:
        con.execute("DROP TABLE material_master")
        con.execute("CREATE TABLE material_master (x INTEGER)")
        con.execute("INSERT INTO material_master VALUES (1)")
        con.commit()
    assert _state(f) == (False, False)
    f.calls.update(data=0, db=0, history=0)

    f.run()

    assert f.calls == {"data": 1, "db": 1, "history": 1}
    assert _state(f) == (True, True)
