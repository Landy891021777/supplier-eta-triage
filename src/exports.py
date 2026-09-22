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
=========================================================================
"""
from __future__ import annotations

import io
from datetime import datetime

import pandas as pd

import triage
from domain import CATEGORY_LABEL_ZH

FOLLOWUP_COLUMNS = ["優先級", "預估缺料天數", "採購單號", "料號", "料別", "供應商", "數量",
                    "新交期", "保守到料日", "可投產日", "需求日", "承諾強度", "需人工確認",
                    "建議動作", "理由"]

# 一個月只有幾筆單時，準交率／改期比例會因為一兩筆單大幅跳動，
# 算出來的比例會誤導評核，寧可標「樣本不足」也不給一個看似精確的假數字。
MIN_MONTHLY_SAMPLES = 5


# ---------------------------------------------------------------------------
# 明日追料清單
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


def _yn(v) -> str:
    return "是" if bool(v) else "否"


def _category_label(v) -> str:
    v = triage._clean_str(v)
    return CATEGORY_LABEL_ZH.get(v, v)


def followup_workbook(actions: pd.DataFrame, as_of: str) -> bytes:
    """
    明日追料清單，給物料企劃明天早上追料、或帶去缺料檢討會用。

    只保留 P1、P2、待查——P3 沒有缺料風險，不必印出來讓人多看一眼；
    順序沿用輸入（actions 已經依分級與缺料天數排序過，這裡不重排，
    重排的邏輯只該有一份，在 pipeline._sort_by_urgency）。
    """
    df = actions[actions["priority"].isin(("P1", "P2", "待查"))].reset_index(drop=True)

    out = pd.DataFrame({
        "優先級": df["priority"],
        "預估缺料天數": _col(df, "gap_days").map(_int_or_blank),
        "採購單號": df["po_no"],
        "料號": _col(df, "material_id").map(triage._clean_str),
        "料別": _col(df, "category").map(_category_label),
        "供應商": _col(df, "supplier_name").map(triage._clean_str),
        "數量": [_qty_text(q, u) for q, u in zip(_col(df, "qty"), _col(df, "base_uom"))],
        "新交期": _col(df, "new_eta").map(triage._clean_str),
        "保守到料日": _col(df, "conservative_eta").map(triage._clean_str),
        "可投產日": _col(df, "available_date").map(triage._clean_str),
        "需求日": _col(df, "need_date").map(triage._clean_str),
        "承諾強度": _col(df, "commitment_strength").map(triage._clean_str),
        "需人工確認": _col(df, "needs_human_review").map(_yn),
        "建議動作": _col(df, "actions").map(_joined),
        "理由": _col(df, "reasons").map(_joined),
    })[FOLLOWUP_COLUMNS]

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        out.to_excel(writer, sheet_name="追料清單", index=False)
        ws = writer.sheets["追料清單"]
        ws.freeze_panes = "A2"
        widths = {"優先級": 8, "預估缺料天數": 12, "採購單號": 12, "料號": 16, "料別": 10,
                  "供應商": 16, "數量": 10, "新交期": 12, "保守到料日": 12, "可投產日": 12,
                  "需求日": 12, "承諾強度": 10, "需人工確認": 10, "建議動作": 40, "理由": 50}
        for i, col in enumerate(FOLLOWUP_COLUMNS, start=1):
            ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = widths[col]

        note = pd.DataFrame({"說明": [
            f"產生時間：{datetime.now().strftime('%Y-%m-%d %H:%M')}",
            f"模擬基準日（as_of）：{as_of}",
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
def supplier_monthly(outcomes: pd.DataFrame) -> pd.DataFrame:
    """
    依承諾月份統計每家供應商的交期表現。

    輸入是 supplier_stats.load_outcomes() 的欄位（至少 supplier_id、
    committed_date、delay_days、reschedule_count；有 supplier_name 就
    帶上顯示用）。延遲時中位數只算真的延遲的單（delay_days > 0），
    P80 延遲則跟 src/supplier_stats.py 的 supplier_performance() 一致，
    對全部單取 P80（interpolation="higher"，取實際出現過的天數，
    不做內插）——同一件事只有一套算法，兩處對不上會讓人不知道信哪個。
    """
    if outcomes.empty:
        return pd.DataFrame(columns=["供應商", "承諾月份", "交貨筆數", "準交率", "改期比例",
                                     "延遲時中位數(天)", "P80 延遲(天)", "樣本"])

    df = outcomes.copy()
    df["承諾月份"] = pd.to_datetime(df["committed_date"]).dt.strftime("%Y-%m")
    df["_準交"] = df["delay_days"] <= 0
    df["_有改期"] = df["reschedule_count"] >= 1

    out = df.groupby(["supplier_id", "承諾月份"]).agg(
        交貨筆數=("delay_days", "size"),
        準交率=("_準交", "mean"),
        改期比例=("_有改期", "mean"),
        **{"P80 延遲(天)": ("delay_days",
                           lambda s: s.quantile(0.8, interpolation="higher"))},
    ).reset_index().rename(columns={"supplier_id": "供應商"})

    late_median = (df[df["delay_days"] > 0]
                   .groupby(["supplier_id", "承諾月份"])["delay_days"].median().to_dict())
    out["延遲時中位數(天)"] = [late_median.get((s, m), float("nan"))
                            for s, m in zip(out["供應商"], out["承諾月份"])]

    if "supplier_name" in df.columns:
        names = df.drop_duplicates("supplier_id").set_index("supplier_id")["supplier_name"]
        out.insert(1, "供應商名稱", out["供應商"].map(names))

    small = out["交貨筆數"] < MIN_MONTHLY_SAMPLES
    out["樣本"] = small.map({True: "樣本不足", False: "足夠"})
    for col in ("準交率", "改期比例", "延遲時中位數(天)", "P80 延遲(天)"):
        out.loc[small, col] = float("nan")

    cols = ["供應商"] + (["供應商名稱"] if "供應商名稱" in out.columns else []) + \
           ["承諾月份", "交貨筆數", "準交率", "改期比例", "延遲時中位數(天)", "P80 延遲(天)", "樣本"]
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
