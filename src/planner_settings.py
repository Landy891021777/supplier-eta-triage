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
