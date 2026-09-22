# -*- coding: utf-8 -*-
"""
物料企劃在工具內調整「收貨處理天數」與「確認交期」的存取層。

===========================  為什麼不寫回 ERP  ===========================
收貨處理天數在 SAP 是料號主檔的欄位（MARC-WEBAZ），實務上改主檔要走核准流程。
本工具的原則是永不寫回 ERP（決策 11），所以企劃的調整存在工具自己的資料庫
data/planner_settings.db，畫面上標明「工具內設定，尚未同步 ERP 主檔」。

每次調整都留下紀錄（誰、何時、原值、新值、原因），原因必填：
同仁半年後才看得懂「為什麼這顆料特別慢」。

限制：雲端展示環境的檔案不持久，App 休眠或重新部署後會回到預設；
真正上線時應改存公司資料庫。

===========================  企劃確認交期  ===========================
非 confirmed 的交期一律不寫回系統（人工確認閘門，見 pipeline.py）。
企劃向供應商要到確切日期後，把「確認後的日期、備註、姓名」登錄在這裡，
跟收貨處理天數一樣存在工具自己的資料庫，一樣不寫回 ERP：畫面上要標明
「尚未寫回 ERP，請依公司流程更新交貨排程行」。

它記錄的是「企劃向供應商要到的確切日期，是誰、何時、怎麼確認的」——
這是 ERP 結構上本來就不會有的資料。只 INSERT、不覆寫，保留每一次確認
的完整歷史；套用時只取每張單最新一筆（confirm_id 最大），而且要跟
email_id 對得上（見 pipeline.retriage() 的說明：供應商之後又來新信，
不能讓一個已經作廢的舊確認蓋掉新資訊）。
=========================================================================
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import date, datetime
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
CREATE TABLE IF NOT EXISTS eta_confirmation (
    confirm_id INTEGER PRIMARY KEY AUTOINCREMENT, po_no TEXT NOT NULL,
    email_id TEXT NOT NULL, confirmed_date TEXT NOT NULL, note TEXT,
    confirmed_by TEXT NOT NULL, confirmed_at TEXT NOT NULL);
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


def _to_int_days(days) -> int:
    """
    嚴格檢查天數：只接受整數字面值（含 "4" 這種可轉整數的字串），
    不接受 2.9 這種小數被 int() 悄悄截斷成 2——企劃輸入 2.9 天，系統卻
    存成 2 天，這種資料錯誤在畫面上看不出來，比直接拒絕更危險。
    NaN／None 也要在這裡擋下來，不能讓呼叫端收到 int() 原生的英文錯誤。
    """
    if days is None:
        raise ValueError("請填寫收貨處理天數")
    try:
        f = float(days)
    except (TypeError, ValueError):
        raise ValueError("收貨處理天數必須是整數") from None
    if f != f:  # NaN：float 的 NaN 不等於自己，是最可靠的判斷方式
        raise ValueError("請填寫收貨處理天數")
    if f != int(f):
        raise ValueError("收貨處理天數必須是整數，不可以有小數")
    return int(f)


def _validate(material_id: str, days, reason: str, user: str) -> tuple[str, int, str, str]:
    if not str(material_id or "").strip():
        raise ValueError("請填寫料號")
    if not str(reason or "").strip():
        raise ValueError("請填寫調整原因")
    if not str(user or "").strip():
        raise ValueError("請填寫姓名")
    d = _to_int_days(days)
    if not 0 <= d <= MAX_DAYS:
        raise ValueError(f"收貨處理天數須介於 0 到 {MAX_DAYS} 天")
    return material_id.strip(), d, reason.strip(), user.strip()


def _now(now: str | None) -> str:
    return now or datetime.now().strftime("%Y-%m-%d %H:%M")


def set_override(db, material_id: str, days: int, reason: str, user: str, *,
                  default_days: int | None = None, now: str | None = None) -> None:
    material_id, d, reason, user = _validate(material_id, days, reason, user)
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
    material_id, d, reason, user = _validate(material_id, default_days, reason, user)
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


def _validate_confirmation(po_no, email_id, confirmed_date, note, user
                           ) -> tuple[str, str, str, str, str]:
    if not str(po_no or "").strip():
        raise ValueError("請填寫採購單號")
    if not str(email_id or "").strip():
        raise ValueError("請填寫信件編號")
    d = str(confirmed_date or "").strip()
    if not d:
        raise ValueError("請填寫確認後的交期日期（YYYY-MM-DD）")
    try:
        date.fromisoformat(d)
    except ValueError:
        raise ValueError("請填寫確認後的交期日期（YYYY-MM-DD）") from None
    if not str(user or "").strip():
        raise ValueError("請填寫姓名")
    return po_no.strip(), email_id.strip(), d, str(note or "").strip(), user.strip()


def confirm_eta(db, po_no: str, email_id: str, confirmed_date: str, note: str, user: str, *,
                now: str | None = None) -> None:
    """
    登錄企劃向供應商確認到的交期。

    只 INSERT、不 UPDATE：跟 set_override 不同，這裡刻意不做「同一張單
    覆寫舊確認」，因為確認紀錄本身就是稽核用的歷史（誰、何時、改口過
    幾次），load_confirmations() 才是取「目前生效的最新一筆」的地方。

    email_id 必須跟著存：套用時要求 email_id 對得上目前這封信，
    否則供應商隔天又來一封改口的新信，舊確認會誤蓋掉新資訊
    （見 pipeline.retriage()）。
    """
    po_no, email_id, confirmed_date, note, user = _validate_confirmation(
        po_no, email_id, confirmed_date, note, user)
    ts = _now(now)
    with closing(_connect(db)) as con:
        con.execute(
            "INSERT INTO eta_confirmation (po_no, email_id, confirmed_date, note,"
            " confirmed_by, confirmed_at) VALUES (?,?,?,?,?,?)",
            (po_no, email_id, confirmed_date, note, user, ts))
        con.commit()


def load_confirmations(db: Path | str | None = None) -> dict[str, dict]:
    """每張單只取最新一筆確認（confirm_id 最大）。"""
    with closing(_connect(db)) as con:
        rows = con.execute(
            "SELECT * FROM eta_confirmation WHERE confirm_id IN "
            "(SELECT MAX(confirm_id) FROM eta_confirmation GROUP BY po_no)").fetchall()
        return {r["po_no"]: dict(r) for r in rows}


def confirmation_log(db: Path | str | None = None) -> list[dict]:
    """完整確認歷史，依時間先後排序，給畫面上「這張單改口過幾次」用。"""
    with closing(_connect(db)) as con:
        return [dict(r) for r in con.execute(
            "SELECT * FROM eta_confirmation ORDER BY confirm_id")]


def _category_label(category) -> str:
    """
    None／NaN 的料別要顯示「未分類」，不是 Python 的 "None" 字樣——
    那個字樣只有寫程式的人看得懂，企劃看到只會覺得是系統壞了。
    """
    if category is None:
        return "未分類"
    try:
        if category != category:  # NaN
            return "未分類"
    except TypeError:
        pass
    return CATEGORY_LABEL_ZH.get(category, category)


def effective_gr_days(material_id: str, default_days, category: str,
                       overrides: dict[str, dict]) -> tuple[int, str]:
    """回傳 (天數, 來源說明)。來源一定要講出來，企劃才知道這個數字能不能信。"""
    o = overrides.get(material_id)
    if o:
        return int(o["days"]), f"企劃 {o['updated_by']} {o['updated_at'][5:10]} 調整：{o['reason']}"
    try:
        d = int(default_days)
    except (TypeError, ValueError):
        # 料號主檔這個欄位是 NaN／None：不是「這個料別收貨處理天數就是
        # 0 天」（那是查過、確實是 0 的意思），是主檔壓根沒維護這筆資料，
        # 兩者混為一談會讓企劃誤以為 0 天是個查證過的結果。
        return 0, "料號主檔未維護收貨處理天數，暫以 0 天計"
    return d, f"{_category_label(category)}料別預設"
