# -*- coding: utf-8 -*-
"""
建立「模擬 ERP」資料庫。

===========================  這支程式在解決什麼問題  ===========================
我手上沒有 ERP，也不可能拿到真實資料。但直接把
`reschedule_count`（改過幾次期）這種欄位寫在 CSV 裡，
等於偷偷跳過了整合工作中真正困難的部分 ——

    真實 ERP 裡根本沒有「改期次數」這一欄。
    它藏在變更文件表裡，你得自己 COUNT 出來。

同理：
    承諾日  不在採購單頭，在「交貨排程行」
    需求日  不在採購單，在「請購單」
    有無二源 不是一個布林欄位，要去「來源清單」看合格供應商有幾家

所以這支程式把先前產生的扁平 CSV，拆解成一組**結構貼近 SAP MM 的
關聯式資料表**，讓工具必須用真正的 SQL JOIN 去推導那些欄位。

這樣做的價值：
    1. 證明這個工具是「為了接 ERP 而設計」，不是嘴上說說
    2. 把整合時真正會遇到的困難（欄位不是現成的）先做過一遍
    3. 之後真要接公司 ERP，只需要換掉 SQL 的來源，邏輯不用動

誠實聲明：表名與結構是參考 SAP ECC/S4 的公開資料整理的近似版本，
不是任何公司的真實 schema。各家客製化程度差異很大。
我沒有實際串接過生產環境的 ERP。
==============================================================================
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DB_PATH = DATA / "erp_sim.db"

# 表名後面的括號是 SAP 的對應表，方便熟悉 SAP 的人快速對照。
SCHEMA = """
DROP TABLE IF EXISTS vendor_master;
DROP TABLE IF EXISTS material_master;
DROP TABLE IF EXISTS material_alternate;
DROP TABLE IF EXISTS source_list;
DROP TABLE IF EXISTS purchase_req;
DROP TABLE IF EXISTS po_header;
DROP TABLE IF EXISTS po_item;
DROP TABLE IF EXISTS po_schedule;
DROP TABLE IF EXISTS po_change_log;
DROP TABLE IF EXISTS goods_receipt;

-- 供應商主檔 (≈ SAP LFA1)
CREATE TABLE vendor_master (
    vendor_id   TEXT PRIMARY KEY,
    vendor_name TEXT NOT NULL,
    vendor_type TEXT NOT NULL,
    otd_rate    REAL
);

-- 物料主檔 (≈ SAP MARA + MARC)
CREATE TABLE material_master (
    material_id        TEXT PRIMARY KEY,
    category           TEXT,
    std_lead_time_days INTEGER,
    is_bottleneck      INTEGER DEFAULT 0,
    criticality        TEXT
);

-- 替代料關係 (≈ BOM 替代群組 / 主檔替代關係)
CREATE TABLE material_alternate (
    material_id     TEXT NOT NULL,
    alt_material_id TEXT NOT NULL,
    PRIMARY KEY (material_id, alt_material_id)
);

-- 來源清單 (≈ SAP EORD)。「有沒有二源」的權威來源：
-- 不是一個布林欄位，是「這顆料有幾家合格供應商」。
CREATE TABLE source_list (
    material_id  TEXT NOT NULL,
    vendor_id    TEXT NOT NULL,
    is_qualified INTEGER DEFAULT 1,
    valid_from   TEXT,
    PRIMARY KEY (material_id, vendor_id)
);

-- 請購單 (≈ SAP EBAN)。下游需求日的來源，不在採購單上。
CREATE TABLE purchase_req (
    pr_no                TEXT PRIMARY KEY,
    material_id          TEXT NOT NULL,
    req_qty              INTEGER,
    need_date            TEXT,
    period_demand_qty    INTEGER,   -- 該料號當期總需求，用來算本單佔比
    downstream_scheduled INTEGER DEFAULT 0
);

-- 採購單頭 (≈ SAP EKKO)
CREATE TABLE po_header (
    po_no        TEXT PRIMARY KEY,
    vendor_id    TEXT NOT NULL,
    created_date TEXT
);

-- 採購單項次 (≈ SAP EKPO)
CREATE TABLE po_item (
    po_no       TEXT NOT NULL,
    item_no     INTEGER NOT NULL,
    material_id TEXT NOT NULL,
    qty         INTEGER,
    pr_no       TEXT,
    PRIMARY KEY (po_no, item_no)
);

-- 交貨排程行 (≈ SAP EKET)。承諾日在這裡，不在單頭。
-- 一個項次可以有多筆排程行 —— 這就是「分批交貨」的資料結構基礎。
CREATE TABLE po_schedule (
    po_no          TEXT NOT NULL,
    item_no        INTEGER NOT NULL,
    sched_line     INTEGER NOT NULL,
    committed_date TEXT,
    qty            INTEGER,
    PRIMARY KEY (po_no, item_no, sched_line)
);

-- 變更文件 (≈ SAP CDHDR/CDPOS)。「改期次數」的權威來源。
CREATE TABLE po_change_log (
    change_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    po_no      TEXT NOT NULL,
    item_no    INTEGER NOT NULL,
    field_name TEXT NOT NULL,
    old_value  TEXT,
    new_value  TEXT,
    changed_at TEXT,
    changed_by TEXT
);

-- 收貨紀錄 (≈ SAP MKPF/MSEG)。
-- 目前是空的 —— 這是刻意的：它代表「工具上線後才會累積的真實結果」，
-- 也是未來用資料校準規則權重的唯一來源。詳見 README 第十節。
CREATE TABLE goods_receipt (
    gr_no        TEXT PRIMARY KEY,
    po_no        TEXT NOT NULL,
    item_no      INTEGER NOT NULL,
    qty          INTEGER,
    receipt_date TEXT
);

CREATE INDEX idx_po_item_po      ON po_item(po_no);
CREATE INDEX idx_po_sched_po     ON po_schedule(po_no, item_no);
CREATE INDEX idx_change_po       ON po_change_log(po_no, item_no, field_name);
CREATE INDEX idx_source_material ON source_list(material_id);
"""


def build(verbose: bool = True) -> Path:
    for f in ("po_master.csv", "materials.csv", "suppliers.csv"):
        if not (DATA / f).exists():
            raise FileNotFoundError(
                f"找不到 data/{f}，請先執行： py src/generate_data.py")

    pos = pd.read_csv(DATA / "po_master.csv", encoding="utf-8-sig")
    mats = pd.read_csv(DATA / "materials.csv", encoding="utf-8-sig")
    sups = pd.read_csv(DATA / "suppliers.csv", encoding="utf-8-sig")

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists():
        DB_PATH.unlink()
    con = sqlite3.connect(DB_PATH)
    con.executescript(SCHEMA)

    # ---------------- 主檔 ----------------
    con.executemany(
        "INSERT INTO vendor_master VALUES (?,?,?,?)",
        sups[["supplier_id", "supplier_name", "supplier_type",
              "historical_otd_rate"]].itertuples(index=False, name=None))

    con.executemany(
        "INSERT INTO material_master VALUES (?,?,?,?,?)",
        [(r.material_id, r.category, int(r.std_lead_time_days),
          int(bool(r.is_bottleneck)), r.criticality)
         for r in mats.itertuples(index=False)])

    # 替代料：CSV 裡的空字串在此被正確地「不寫入」，
    # 而不是變成一列 alt_material_id = ''。關聯式模型天然避免了這個問題。
    alts = [(r.material_id, str(r.alt_material_id).strip())
            for r in mats.itertuples(index=False)
            if str(r.alt_material_id).strip() not in ("", "nan", "None")]
    con.executemany("INSERT OR IGNORE INTO material_alternate VALUES (?,?)", alts)

    # ---------------- 來源清單 ----------------
    # 把布林欄位 has_qualified_second_source 還原成它真正的樣子：
    # 該料號在來源清單裡有幾家合格供應商。
    sup_by_type: dict[str, list[str]] = {}
    for r in sups.itertuples(index=False):
        sup_by_type.setdefault(r.supplier_type, []).append(r.supplier_id)
    type_of_cat = {"WAFER": "FOUNDRY", "MASK": "MASK_SHOP",
                   "SUBSTRATE": "SUBSTRATE", "ASSEMBLY": "OSAT"}

    primary_vendor = (pos.drop_duplicates("material_id")
                      .set_index("material_id")["supplier_id"].to_dict())
    src_rows = []
    for r in mats.itertuples(index=False):
        cands = sup_by_type.get(type_of_cat.get(r.category, ""), [])
        if not cands:
            continue
        first = primary_vendor.get(r.material_id, cands[0])
        chosen = [first]
        if bool(r.has_qualified_second_source):
            for c in cands:
                if c != first:
                    chosen.append(c)
                    break
        for v in chosen:
            src_rows.append((r.material_id, v, 1, "2025-01-01"))
    con.executemany("INSERT OR IGNORE INTO source_list VALUES (?,?,?,?)", src_rows)

    # ---------------- PR / PO / 排程行 / 變更文件 ----------------
    pr_rows, hdr_rows, item_rows, sched_rows, chg_rows = [], [], [], [], []
    for i, r in enumerate(pos.itertuples(index=False), start=1):
        pr_no = f"PR-{i:06d}"
        share = float(r.share_of_period_demand) or 1.0
        period_qty = max(int(r.qty), int(round(int(r.qty) / share)))
        pr_rows.append((pr_no, r.material_id, int(r.qty), str(r.need_date),
                        period_qty, int(bool(r.downstream_scheduled))))

        hdr_rows.append((r.po_no, r.supplier_id, str(r.po_created_date)))
        item_rows.append((r.po_no, 10, r.material_id, int(r.qty), pr_no))
        # 單一排程行；分批交貨在此資料結構下是「多筆排程行」，
        # 屬已知未實作項目（見 README 已知限制）。
        sched_rows.append((r.po_no, 10, 1, str(r.committed_date), int(r.qty)))

        # 依 reschedule_count 產生對應筆數的變更文件。
        # 工具之後必須自己 COUNT 這張表，而不是讀一個現成欄位。
        n = int(r.reschedule_count or 0)
        committed = date.fromisoformat(str(r.committed_date)[:10])
        for k in range(n):
            old = committed - timedelta(days=7 * (n - k))
            new = committed - timedelta(days=7 * (n - k - 1))
            chg_rows.append((r.po_no, 10, "committed_date",
                             old.isoformat(), new.isoformat(),
                             (old - timedelta(days=3)).isoformat(), "VENDOR_EDI"))

    con.executemany("INSERT INTO purchase_req VALUES (?,?,?,?,?,?)", pr_rows)
    con.executemany("INSERT INTO po_header VALUES (?,?,?)", hdr_rows)
    con.executemany("INSERT INTO po_item VALUES (?,?,?,?,?)", item_rows)
    con.executemany("INSERT INTO po_schedule VALUES (?,?,?,?,?)", sched_rows)
    con.executemany(
        "INSERT INTO po_change_log (po_no,item_no,field_name,old_value,new_value,"
        "changed_at,changed_by) VALUES (?,?,?,?,?,?,?)", chg_rows)

    con.commit()

    if verbose:
        counts = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in ("vendor_master", "material_master", "material_alternate",
                            "source_list", "purchase_req", "po_header", "po_item",
                            "po_schedule", "po_change_log", "goods_receipt")}
        print(f"[OK] 模擬 ERP 資料庫： {DB_PATH}")
        for t, c in counts.items():
            print(f"       {t:<20} {c:>6} 筆")
        print("       goods_receipt 刻意為空：它代表工具上線後才會累積的真實結果，")
        print("       也是未來用資料校準規則權重的唯一來源。")
    con.close()
    return DB_PATH


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    build()
