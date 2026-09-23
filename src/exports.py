# -*- coding: utf-8 -*-
"""
物料企劃真的會用的兩份輸出：明日追料清單、供應商月度績效。

===========================  為什麼獨立成檔  ===========================
兩者都是「拿 actions／歷史收貨紀錄，轉成一份 Excel」的純函式：
不碰 Streamlit、不讀資料庫，方便單獨測試——企劃拿去開會的東西，
欄位跟數字錯一個都不行，靠畫面點一遍測不出這種錯，只有把輸出
讀回來逐格檢查才靠得住（見 tests/test_exports.py）。

===========================  月度績效的範圍  ===========================
只做「交期面」：依承諾月份統計每家供應商的交貨筆數、準交率、改期比例、
延遲天數。供應商的「品質」與「配合度」這裡沒有可信的資料來源
（沒有驗退紀錄、沒有溝通紀錄），刻意不做，不編造一個數字充數。

一個月樣本太少時，比例會因為一兩筆單大幅跳動，算出來的「準交率」
反而會誤導評核，所以少於 MIN_MONTHLY_SAMPLES 筆的月份只給筆數、
不給比例，標示「樣本不足」。

===========================  在途逾期單會低估近期月份  ===========================
只算「已收貨」的單，近期月份的準交率會被系統性高估：一張單只要還沒
收貨，不管承諾日已經過了多久，都不會出現在 outcomes 裡——最近一兩個
月本來就有更高比例的單還在途，算出來的準交率會比真實情況好看，而且
月份越新，這個偏誤越大。

supplier_monthly() 可以選擇性地帶入 open_pos（在途未收貨的單，
至少要有 supplier_id、committed_date）與 as_of（模擬基準日）：承諾日
已過、還沒收貨的單，會被當成「延遲中」的一筆計入，延遲天數＝到基準日
為止（這是低估——實際到貨一定比這個數字更晚或持平，不會更早）。
這些筆數額外算進「逾期未收（筆）」欄，讓看的人知道那個月有多少筆數字
是用低估值撐出來的，不是真的已經結案的準確數字。
=========================================================================
"""
from __future__ import annotations

import io
from datetime import date, datetime

import pandas as pd

import triage
from domain import CATEGORY_LABEL_ZH, COMMITMENT_LABEL_ZH

# 企劃看得懂的欄位順序：CSV（行動清單）與 Excel（追料清單）共用同一套，
# 同一件事只有一套轉換規則，兩處欄位才不會對不上（見 to_planner_rows）。
PLANNER_COLUMNS = ["優先級", "預估缺料天數", "採購單號", "料號", "料別", "供應商", "數量",
                   "新交期", "原承諾日", "保守到料日", "可投產日", "需求日", "承諾強度",
                   "需人工確認", "建議動作", "理由"]
FOLLOWUP_COLUMNS = PLANNER_COLUMNS  # 舊名沿用，避免其他地方 import 這個名字時炸掉

# 追料清單裡要寫成真正 Excel 日期（而不是文字）的欄位：Excel 才能排序、
# 篩選、做日期運算，企劃拿去做樞紐分析表時不必再自己轉型別。
_DATE_COLUMNS = {"新交期", "原承諾日", "保守到料日", "可投產日", "需求日"}

# 一個月只有幾筆單時，準交率／改期比例會因為一兩筆單大幅跳動，
# 算出來的比例會誤導評核，寧可標「樣本不足」也不給一個看似精確的假數字。
MIN_MONTHLY_SAMPLES = 5


# ---------------------------------------------------------------------------
# 共用的欄位清洗
# ---------------------------------------------------------------------------
def _col(df: pd.DataFrame, name: str) -> pd.Series:
    """欄位整個不存在時（例如舊格式或呼叫端組的精簡測試資料）回傳全空
    的 Series，讓下面的清洗函式統一用同一套邏輯處理缺值，不必到處判斷
    "有沒有這個欄位"。"""
    return df[name] if name in df.columns else pd.Series([None] * len(df), index=df.index)


def _int_or_blank(v):
    """
    預估缺料天數要寫整數；NaN／None 留空。

    7.0 這種字企劃看了會問「是不是算錯了」，nan 更是連是不是數字都
    看不出來——待查的單本來就沒有缺料天數可言，空白才是誠實的表示法。
    """
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else int(f)


def _date_or_blank(v):
    """
    交期欄位寫成真正的 date 物件（不是文字），Excel 才能排序、篩選、
    做日期運算；觸發 triage._d() 解析不出來（None／格式錯）就留空，
    不寫成 "nan" 或 "None" 這種字。CSV 匯出一樣可以用（str(date) 剛好
    就是 YYYY-MM-DD，跟原本文字格式一致）。
    """
    return triage._d(v)


def _qty_text(qty, uom) -> str:
    """數量寫成「80 GAL」；缺單位（NaN／空字串）只寫數量，不留一個懸空的空格。"""
    uom = triage._clean_str(uom)
    try:
        f = float(qty)
    except (TypeError, ValueError):
        return ""
    if f != f:
        return ""
    q = int(f) if f == int(f) else f
    return f"{q} {uom}".strip() if uom else str(q)


def _joined(v) -> str:
    """理由與建議動作原本是 list；Excel 儲存格裡出現 ['a1', 'a2'] 這種
    字比空白更糟——那看起來像資料壞了，不是「這裡沒有東西」。"""
    return "；".join(str(x) for x in v) if isinstance(v, list) else ""


def _actions_text(priority, v) -> str:
    """
    待查的單常常沒有建議動作（triage 早退，連 suggest_actions 都沒跑），
    但「什麼都沒寫」會讓企劃以為這張單不用管——待查是「讀不出來」，
    不是「沒事」，比 P1/P2 更需要人去看一眼原信，所以給一個明確的預設
    動作，不留空。P1/P2/P3 的單如果本來就有建議動作，不動它。
    """
    joined = _joined(v)
    if not joined and priority == "待查":
        return "請人工看原信，確認單號與新交期"
    return joined


def _yn(v) -> str:
    return "是" if bool(v) else "否"


def _category_label(v) -> str:
    v = triage._clean_str(v)
    return CATEGORY_LABEL_ZH.get(v, v)


def _commitment_label(v) -> str:
    """承諾強度顯示中文；不認得的值（未知格式）原樣顯示，不隱藏異常資料。"""
    v = triage._clean_str(v)
    return COMMITMENT_LABEL_ZH.get(v, v)


def to_planner_rows(df: pd.DataFrame) -> pd.DataFrame:
    """
    把 actions 的原始欄位（英文欄名、內部代碼）轉成企劃看得懂的中文欄位。

    CSV（行動清單，views/actions.py）與 Excel（明日追料清單，見下面的
    followup_workbook）共用這支函式：企劃看到的欄位定義只有一份，不會
    CSV 一套、Excel 又是另一套、哪天改了欄位卻只改到一邊。
    不篩優先級——篩選是呼叫端的事（CSV 匯出的是使用者當下篩好的
    view；Excel 只留 P1／P2／待查，見 followup_workbook）。
    """
    return pd.DataFrame({
        "優先級": df["priority"],
        "預估缺料天數": _col(df, "gap_days").map(_int_or_blank),
        "採購單號": df["po_no"],
        "料號": _col(df, "material_id").map(triage._clean_str),
        "料別": _col(df, "category").map(_category_label),
        "供應商": _col(df, "supplier_name").map(triage._clean_str),
        "數量": [_qty_text(q, u) for q, u in zip(_col(df, "qty"), _col(df, "base_uom"))],
        "新交期": _col(df, "new_eta").map(_date_or_blank),
        "原承諾日": _col(df, "committed_date").map(_date_or_blank),
        "保守到料日": _col(df, "conservative_eta").map(_date_or_blank),
        "可投產日": _col(df, "available_date").map(_date_or_blank),
        "需求日": _col(df, "need_date").map(_date_or_blank),
        "承諾強度": _col(df, "commitment_strength").map(_commitment_label),
        "需人工確認": _col(df, "needs_human_review").map(_yn),
        "建議動作": [_actions_text(p, v) for p, v in zip(df["priority"], _col(df, "actions"))],
        "理由": _col(df, "reasons").map(_joined),
    })[PLANNER_COLUMNS]


# ---------------------------------------------------------------------------
# 明日追料清單
# ---------------------------------------------------------------------------
def followup_workbook(actions: pd.DataFrame, as_of: str) -> bytes:
    """
    明日追料清單，給物料企劃明天早上追料、或帶去缺料檢討會用。

    只保留 P1、P2、待查——P3 沒有缺料風險，不必印出來讓人多看一眼；
    順序沿用輸入（actions 已經依分級與缺料天數排序過，這裡不重排，
    重排的邏輯只該有一份，在 pipeline._sort_by_urgency）。
    """
    df = actions[actions["priority"].isin(("P1", "P2", "待查"))].reset_index(drop=True)
    out = to_planner_rows(df)

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        out.to_excel(writer, sheet_name="追料清單", index=False)
        ws = writer.sheets["追料清單"]
        ws.freeze_panes = "A2"
        widths = {"優先級": 8, "預估缺料天數": 12, "採購單號": 12, "料號": 16, "料別": 10,
                  "供應商": 16, "數量": 10, "新交期": 12, "原承諾日": 12, "保守到料日": 12,
                  "可投產日": 12, "需求日": 12, "承諾強度": 10, "需人工確認": 10,
                  "建議動作": 40, "理由": 50}
        for i, col in enumerate(PLANNER_COLUMNS, start=1):
            letter = ws.cell(row=1, column=i).column_letter
            ws.column_dimensions[letter].width = widths[col]
            if col in _DATE_COLUMNS:
                # pandas 把 datetime.date 寫進 Excel 時，儲存格型別是日期，
                # 但預設數字格式不一定是 yyyy-mm-dd（會跟著系統地區設定
                # 跑），這裡明講格式，企劃在不同電腦上開都長一樣。
                for row in range(2, ws.max_row + 1):
                    ws.cell(row=row, column=i).number_format = "yyyy-mm-dd"

        note = pd.DataFrame({"說明": [
            f"產生時間：{datetime.now().strftime('%Y-%m-%d %H:%M')}",
            f"模擬基準日：{as_of}",
            "預估缺料天數 = 保守到料日 + 收貨處理天數 − 下游需求日（正數代表預估缺料）。"
            "兩個日期相減，可在會議上逐項驗算。",
            "全部為合成資料，僅供展示，沒有任何真實公司資料，未串接真實 ERP。",
            "本工具永不自動寫回 ERP、永不自動寄信；交期異動仍須依公司流程更新交貨排程行。",
        ]})
        note.to_excel(writer, sheet_name="說明", index=False)
        writer.sheets["說明"].column_dimensions["A"].width = 70

    return buf.getvalue()


# ---------------------------------------------------------------------------
# 供應商月度績效
# ---------------------------------------------------------------------------
_MONTHLY_EMPTY_COLUMNS = ["供應商名稱", "供應商", "承諾月份", "交貨筆數", "準交率",
                          "改期比例", "延遲時中位數(天)", "P80 延遲(天)",
                          "逾期未收（筆）", "樣本"]


def supplier_monthly(outcomes: pd.DataFrame, *, open_pos: pd.DataFrame | None = None,
                     as_of: str | date | None = None) -> pd.DataFrame:
    """
    依承諾月份統計每家供應商的交期表現。

    輸入是 supplier_stats.load_outcomes() 的欄位（至少 supplier_id、
    committed_date、delay_days、reschedule_count；有 supplier_name 就
    帶上顯示用）。延遲時中位數只算真的延遲的單（delay_days > 0），
    P80 延遲則跟 src/supplier_stats.py 的 supplier_performance() 一致，
    對全部單取 P80（interpolation="higher"，取實際出現過的天數，
    不做內插）——同一件事只有一套算法，兩處對不上會讓人不知道信哪個。
    P80 延遲若是負數，代表這個月八成的單其實是提前到，不是延遲。

    open_pos／as_of：見檔頭「在途逾期單會低估近期月份」的說明。兩者都
    給、而且 open_pos 非空時，才會把在途逾期單併入計算；只給其中一個
    (或都不給) 就完全維持原本只看已收貨單的行為，向下相容既有呼叫端
    （例如 tests/test_exports.py 既有的測試）。
    """
    if outcomes.empty and (open_pos is None or open_pos.empty):
        return pd.DataFrame(columns=_MONTHLY_EMPTY_COLUMNS[1:])  # 沒資料時不猜供應商名稱欄要不要放

    frames: list[pd.DataFrame] = []
    if not outcomes.empty:
        base = outcomes[["supplier_id", "committed_date", "delay_days",
                         "reschedule_count"]].copy()
        if "supplier_name" in outcomes.columns:
            base["supplier_name"] = outcomes["supplier_name"]
        base["_逾期未收"] = False
        frames.append(base)

    if open_pos is not None and as_of is not None and not open_pos.empty:
        as_of_d = as_of if isinstance(as_of, date) else date.fromisoformat(str(as_of)[:10])
        op = open_pos.copy()
        op["committed_date"] = pd.to_datetime(op["committed_date"], errors="coerce")
        op = op.dropna(subset=["committed_date"])
        overdue = op[op["committed_date"].dt.date < as_of_d].copy()
        if not overdue.empty:
            # 低估：到基準日為止的天數，不是真正到貨的延遲天數
            # （那一天還沒發生，未來只會更晚，不會更早）。
            overdue["delay_days"] = (pd.Timestamp(as_of_d) - overdue["committed_date"]).dt.days
            if "reschedule_count" not in overdue.columns:
                overdue["reschedule_count"] = 0
            keep = ["supplier_id", "committed_date", "delay_days", "reschedule_count"]
            if "supplier_name" in overdue.columns:
                keep.append("supplier_name")
            overdue = overdue[keep]
            overdue["_逾期未收"] = True
            frames.append(overdue)

    if not frames:
        return pd.DataFrame(columns=_MONTHLY_EMPTY_COLUMNS[1:])

    df = pd.concat(frames, ignore_index=True)
    if df.empty:
        return pd.DataFrame(columns=_MONTHLY_EMPTY_COLUMNS[1:])

    df["承諾月份"] = pd.to_datetime(df["committed_date"]).dt.strftime("%Y-%m")
    df["_準交"] = df["delay_days"] <= 0
    df["_有改期"] = df["reschedule_count"] >= 1

    out = df.groupby(["supplier_id", "承諾月份"]).agg(
        交貨筆數=("delay_days", "size"),
        準交率=("_準交", "mean"),
        改期比例=("_有改期", "mean"),
        **{"P80 延遲(天)": ("delay_days",
                           lambda s: s.quantile(0.8, interpolation="higher"))},
        **{"逾期未收（筆）": ("_逾期未收", "sum")},
    ).reset_index().rename(columns={"supplier_id": "供應商"})
    out["逾期未收（筆）"] = out["逾期未收（筆）"].astype(int)

    late_median = (df[df["delay_days"] > 0]
                   .groupby(["supplier_id", "承諾月份"])["delay_days"].median().to_dict())
    out["延遲時中位數(天)"] = [late_median.get((s, m), float("nan"))
                            for s, m in zip(out["供應商"], out["承諾月份"])]

    if "supplier_name" in df.columns:
        names = df.drop_duplicates("supplier_id").set_index("supplier_id")["supplier_name"]
        out.insert(0, "供應商名稱", out["供應商"].map(names))  # 企劃看名字，不是看代號

    small = out["交貨筆數"] < MIN_MONTHLY_SAMPLES
    out["樣本"] = small.map({True: "樣本不足", False: "足夠"})
    for col in ("準交率", "改期比例", "延遲時中位數(天)", "P80 延遲(天)"):
        out.loc[small, col] = float("nan")

    cols = (["供應商名稱"] if "供應商名稱" in out.columns else []) + \
           ["供應商", "承諾月份", "交貨筆數", "準交率", "改期比例",
            "延遲時中位數(天)", "P80 延遲(天)", "逾期未收（筆）", "樣本"]
    return out[cols].sort_values(["供應商", "承諾月份"]).reset_index(drop=True)


def monthly_workbook(monthly: pd.DataFrame) -> bytes:
    """供應商月度績效，Excel 一張表；準交率、改期比例以百分比格式顯示。"""
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        monthly.to_excel(writer, sheet_name="月度績效", index=False)
        ws = writer.sheets["月度績效"]
        ws.freeze_panes = "A2"
        header = [c.value for c in ws[1]]
        pct_cols = {"準交率", "改期比例"}
        for i, col in enumerate(header, start=1):
            width = max(10, len(str(col)) * 2 + 2)
            ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = width
            if col in pct_cols:
                for row in range(2, ws.max_row + 1):
                    ws.cell(row=row, column=i).number_format = "0.0%"
    return buf.getvalue()
