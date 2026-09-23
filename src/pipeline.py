# -*- coding: utf-8 -*-
"""
主流程編排：信件 → 分層解析 → 對位 PO → 分級 → 行動清單。

流程設計的三個關鍵決定：

1. **分層解析**：先跑免費的規則層，只有信心不足的才升級呼叫 LLM。
   理由是成本與延遲 —— 一天幾百封信全丟 API，同仁會嫌慢也會被 IT 關切。

2. **對位之後才判定 delay / pull_in**。
   單看信件無法知道新日期是提前還是延後，必須跟 PO 主檔的原承諾日比對。
   這是「解析」與「判斷」分工的具體體現：LLM 負責讀懂信，系統負責比對事實。

3. **人工確認閘門**：非 confirmed 的解析結果，一律不覆寫系統承諾日。
   工具只提出建議，寫回 ERP 必須由人按下確認。
   交期資料錯了，下游整條排程都會跟著錯 —— 這個代價不能由工具自動承擔。
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pandas as pd
import yaml

import extract_llm
import extract_rules
import planner_settings
import triage
from adapters import get_source
from domain import ChangeType, CommitmentStrength, coalesce_batch_key, coalesce_sched_line
from llm.provider import get_provider
from supplier_stats import estimate_delay, load_outcomes

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
INBOX = DATA / "inbox"


def load_config() -> dict:
    with open(ROOT / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def get_data_source(cfg: dict | None = None):
    """
    依設定建立資料來源。

    刻意不在這裡做「找不到就退回 CSV」的容錯 ——
    使用者以為在讀 ERP、實際卻在讀 CSV，是最糟的失敗方式。
    寧可大聲失敗。
    """
    cfg = cfg or load_config()
    source = get_source(cfg.get("data_source", "csv"))
    problems = source.validate()
    if problems:
        raise ValueError(
            f"資料來源 '{source.name}' 不符合資料合約：" + "；".join(problems))
    return source


def load_reference_data(cfg: dict | None = None):
    """回傳 (採購單, 料號主檔, 供應商主檔)。來源由 config.yaml 決定。"""
    src = get_data_source(cfg)
    return src.purchase_orders(), src.materials(), src.suppliers()


def load_emails() -> list[dict]:
    idx = INBOX / "_index.json"
    if not idx.exists():
        raise FileNotFoundError(
            "找不到 data/inbox/_index.json，請先執行： py src/generate_data.py")
    return json.loads(idx.read_text(encoding="utf-8"))


def load_ground_truth() -> list[dict]:
    p = INBOX / "_ground_truth.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []


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


def _strength_for_percentile(rec: dict) -> str | None:
    """
    決定要用哪個百分位的承諾強度依據。

    只看「new_eta 是否解析得出有效日期」，不是只看欄位有沒有值 ——
    解析失敗的髒日期（例如 LLM 抽到 "2026-13-45" 這種不存在的日期）
    跟完全沒給日期一樣不可靠，都該走 triage 裡最保守的 "none" 百分位，
    不能因為欄位有字串內容就誤判成真的有一個可用的承諾日期。
    用 triage._d 而不是自己重寫一次日期解析，是因為 triage.evaluate
    最終也是用它判斷有沒有新日期，兩處標準不一致才是真正的風險。
    """
    has_date = triage._d(rec.get("new_eta")) is not None
    return rec.get("commitment_strength") if has_date else "none"


# 排程行層級欄位：同一張單的每一批各自不同，跟 _build_po_index() 的
# common_cols（PO 層級欄位）互斥。
_LINE_COLUMNS = ["sched_line", "sched_qty", "committed_date", "share_of_period_demand"]


def _row_sched_line(row: dict) -> int:
    """
    列上的排程行號：欄位不存在，或是 NaN／None（舊格式 all_df、
    retriage 測試自己組的精簡資料、Task 3 之前存的確認紀錄），
    一律視為第 1 行——這正是「只有一筆排程行的單，行為完全不變」
    這條相容規則在 retriage()／confirmations 這一側的實作。
    """
    return coalesce_sched_line(row.get("sched_line"))


def match_schedule_line(record: dict, lines: list[dict]) -> tuple[int, str, bool]:
    """
    決定一筆抽取結果對到 PO 的哪一筆交貨排程行。

    分批交貨時，同一張單有多筆排程行（sched_line），抽取結果通常只帶
    「這批的數量、日期」，不會明講是第幾批。依序試四條規則：

      1. 抽取結果有 qty，且某一行的 sched_qty 與它相同 → 對到那一行
         （最可靠：供應商信件通常會講清楚「這批 N 件」）。
      2. 沒有 qty，但信中的日期跟某一行的 committed_date 相同 → 對到那一行
         （例如「照原日期出貨」只講日期沒講數量，日期本身就能定位到某一
         行——前提是兩行的 committed_date 不同，不然這條規則本來就分不出）。
      3. 這張單只有一行 → 沒什麼好比對的，直接對到它（單一排程行的單，
         行為要跟改版前完全一樣）。
      4. 以上都不成立（例如同一張單兩行的 committed_date 剛好相同、
         或抽取結果的 qty／日期兩個都對不上任何一行）→ **保守退路**：
         對到最早的未交行，附註工具是用哪個保守假設猜的，並強制
         needs_human_review=True。寧可讓企劃多看一眼、確認是不是猜對，
         也不要把一個日期悄悄套到錯的批次——套錯批次比要求人工確認的
         代價高得多（見 Plan 3 Task 3：分批交貨對位規則）。

    回傳 (sched_line, note, needs_human_review)：note 是要插進 reasons
    的說明，沒有用到退路時是空字串。
    """
    if not lines:
        raise ValueError("match_schedule_line 需要至少一筆排程行")
    ordered = sorted(lines, key=lambda l: int(l["sched_line"]))

    # 規則 3：只有一行，不必比對。放在最前面純粹是效能考量（多數單都
    # 只有一行），結果跟放在規則 1、2 之後完全一樣——單一行時，任何
    # qty／日期的比對就算不比對也只會對到這唯一一行。
    if len(ordered) == 1:
        return int(ordered[0]["sched_line"]), "", False

    # 規則 1：qty 對得上 sched_qty。
    qty = record.get("qty")
    if qty is not None:
        try:
            qty_int = int(qty)
        except (TypeError, ValueError):
            qty_int = None
        if qty_int is not None:
            for line in ordered:
                if int(line["sched_qty"]) == qty_int:
                    return int(line["sched_line"]), "", False

    # 規則 2：沒有 qty，日期對得上某一行的 committed_date。
    if qty is None:
        new_eta = record.get("new_eta")
        if new_eta:
            target = str(new_eta)[:10]
            for line in ordered:
                if str(line["committed_date"])[:10] == target:
                    return int(line["sched_line"]), "", False

    # 規則 4：保守退路。
    earliest = min(ordered, key=lambda l: str(l["committed_date"]))
    note = "信中未指明是哪一批，工具對到最早的一批"
    return int(earliest["sched_line"]), note, True


def _build_po_index(pos_df: pd.DataFrame) -> dict[str, dict]:
    """
    把「每筆排程行一列」的 purchase_orders() 依 po_no 分組回 PO 層級索引。

    PO 層級欄位（料號、供應商、需求日、下游是否已排定、改期次數…）同一張
    單的每一行都相同，取第一列就好；排程行層級欄位（sched_line、
    sched_qty、committed_date、share_of_period_demand）各自帶一份
    list，交給 match_schedule_line() 對位——run() 才知道這封信的內容
    該套用哪一行的承諾日，不是整張單共用同一個承諾日。
    """
    index: dict[str, dict] = {}
    common_cols = [c for c in pos_df.columns if c not in _LINE_COLUMNS]
    for po_no, g in pos_df.groupby("po_no", sort=False):
        common = g.iloc[0][common_cols].to_dict()
        lines = (g[_LINE_COLUMNS].sort_values("sched_line")
                 .to_dict("records"))
        index[po_no] = {**common, "lines": lines}
    return index


def _resolve_record_batches(records: list[dict], po: dict) -> list[dict]:
    """
    同一封信裡，針對同一張 PO 抽出的所有記錄，各自決定要對到哪個排程行、
    彼此有沒有撞在同一行上。

    每筆記錄先各自呼叫一次 match_schedule_line()（四條規則不變），
    再依算出來的 sched_line 分組。同一組（同一封信、同一張單、同一個
    排程行）出現一筆以上的記錄時，分兩種情況，講法完全不同：

      - PO 目前只有一筆排程行（is_single_line）：供應商是在**提議**把
        這一行拆成好幾批——ERP 那邊還沒拆行，工具卻已經從信裡讀出不只
        一批。這是本函式主要要處理的情境：`is_proposed_split=True`，
        reasons 要講「供應商提議拆批」，不能沿用規則 4「對到最早的
        一批」的說法——那句話的意思是「工具讀不出是哪一批，用猜的」，
        但這裡工具其實讀出了兩批，只是 ERP 結構還沒跟上，兩件事不一樣，
        用同一句話會誤導企劃以為工具沒讀懂信。
      - PO 本來就有不只一筆排程行，只是這幾筆記錄剛好都落回規則 4 的
        保守退路（同一個「最早的未交行」）：`is_proposed_split=False`，
        維持各自原本規則 4 的「對到最早的一批」說明。

    不管是哪一種情況，只要同一封信裡有超過一筆記錄落在同一個排程行，
    都要給每一筆一個彼此不同的 batch_key（優先用 qty 當識別依據，
    qty 缺或剛好重複時退回「第幾筆」），run() 的去重與 planner_settings
    的確認紀錄才不會把後面那筆蓋掉前面那筆——改版前只用 (po_no,
    sched_line) 當鍵，同一封信裡兩筆記錄撞在同一行時，後面那筆會悄悄
    蓋掉前面那筆：供應商信裡明明讀出了「8 片準時、12 片延遲」兩批，
    行動清單卻只看得到延遲那一批，準時的 8 片憑空消失。

    回傳跟 records 等長、位置一一對應的 list[dict]，每個元素含
    sched_line／match_note／needs_line_review／batch_key／
    is_proposed_split／batch_index／batch_total。
    """
    lines = po["lines"]
    matched = [match_schedule_line(rec, lines) for rec in records]

    groups: dict[int, list[int]] = {}
    for i, (sched_line, _, _) in enumerate(matched):
        groups.setdefault(sched_line, []).append(i)

    is_single_line = len(lines) == 1
    out: list[dict | None] = [None] * len(records)
    for sched_line, idxs in groups.items():
        collided = len(idxs) > 1
        is_proposed_split = collided and is_single_line
        qty_seen: dict = {}
        for order, i in enumerate(idxs, start=1):
            _, match_note, needs_line_review = matched[i]
            if not collided:
                batch_key = ""
            else:
                qty = records[i].get("qty")
                qty_seen[qty] = qty_seen.get(qty, 0) + 1
                # (qty, 第幾次看到這個 qty)：qty 是主要的識別依據，加一個
                # 出現次數當保險——50/50 對分的兩批 qty 剛好相同時，這個
                # 保險才不會讓兩批的 batch_key 又撞在一起。
                if qty is None:
                    batch_key = f"idx{order}"
                elif qty_seen[qty] == 1:
                    batch_key = f"qty{int(qty)}"
                else:
                    batch_key = f"qty{int(qty)}-{qty_seen[qty]}"
            out[i] = {
                "sched_line": sched_line,
                "match_note": "" if is_proposed_split else match_note,
                "needs_line_review": True if is_proposed_split else needs_line_review,
                "batch_key": batch_key,
                "is_proposed_split": is_proposed_split,
                "batch_index": order,
                "batch_total": len(idxs),
            }
    return out


def _keep_latest_email_per_schedule_line(df: pd.DataFrame) -> pd.DataFrame:
    """
    同一個排程行只留「最新一封信」貢獻的列。

    用「同一組（po_no, sched_line）裡 received_at 等於這一組最大值」來
    篩，不是單純 drop_duplicates(keep="last")：同一封（最新的那封）信
    如果對同一個排程行給了不只一筆記錄（供應商提議拆批，或規則 4 的
    保守退路剛好讓兩筆記錄都落到同一行，見 _resolve_record_batches），
    這幾筆都屬於「最新一封信」，要一起留下——drop_duplicates 只用
    (po_no, sched_line) 當鍵會把同一封信自己的另一批也當成「舊資料」
    濾掉，等於「準時的那批被延遲那批悄悄蓋掉」在另一個層級重演。
    換成舊一點的信，不管原本有幾批，整組一起淘汰（真實情境：供應商先
    寄信提議拆成兩批，企劃還沒處理，供應商又寄新信改口說「其實照單一
    日期」——舊信的兩批不能跟新信的一筆並存，見
    tests/test_pipeline_triage.py 的「latest email wins」迴歸測試）。

    獨立成一支函式方便單獨測試，也是因為這支函式只需要
    po_no／sched_line／received_at 三個欄位就能驗證行為，不必每次都
    跑一次完整的 run()。

    groupby(..., dropna=False)：未對到主檔的列 sched_line 是 None，
    pandas 的 groupby 預設會直接丟掉 NaN／None 鍵的整組（不納入計算），
    沒有 dropna=False 這些列會被 transform 判成 NaN、跟自己的
    received_at 永遠比不相等，整批消失。
    """
    latest_at = df.groupby(["po_no", "sched_line"], dropna=False)["received_at"].transform("max")
    return df[df["received_at"] == latest_at].reset_index(drop=True)


def _proposed_split_note(qty, uom: str, batch_index: int, batch_total: int) -> str:
    """
    供應商提議把一筆還沒拆行的排程行拆成好幾批時，要插進 reasons 的說明。

    跟 match_schedule_line 規則 4「信中未指明是哪一批，工具對到最早的
    一批」是兩件不同的事：規則 4 是工具讀不出是哪一批、用猜的；這裡是
    工具讀出了不只一批，只是 ERP 那筆排程行還沒真的拆開——講清楚
    「提議」兩個字，企劃才知道這是供應商單方面說的，還沒反映在系統裡，
    要靠人工確認後請採購維護排程行（不能自動寫回 ERP，見決策 11）。
    """
    if qty is not None:
        try:
            portion = f"本批 {int(qty)}{(' ' + uom) if uom else ''}"
        except (TypeError, ValueError):
            portion = f"第 {batch_index} 批"
    else:
        portion = f"第 {batch_index} 批"
    return (f"供應商提議把這一行拆成 {batch_total} 批（{portion}）；"
            "ERP 的交貨排程行尚未拆行，請確認後由採購維護")


def _derive_change_type(new_eta: str | None, committed_date, current: str | None, *,
                        force: bool = False) -> str:
    """
    依新日期與原承諾日重新判定變更類型（delay／pull_in／no_change）。

    run() 解析信件之後、retriage() 套用企劃確認交期之後，都要重判一次
    change_type——兩個來源（供應商信件、企劃人工確認）都可能給出早於、
    晚於、或等於原承諾日的日期，判斷規則沒有理由分成兩套，抽成共用
    函式才不會有一天兩邊各自改壞而對不上（見 retriage() 的說明）。

    只有「目前不是 no_change 而且有新日期」才需要重判：供應商已經明講
    「確認不變」，不該因為日期字串剛好能解析就被誤判成別的類型；
    完全沒有新日期、或新日期解析不出來，維持原本的 change_type，
    不能瞎猜一個新的出來。這條短路只適用於 run() 解析信件的路徑。

    force=True（企劃確認交期的路徑專用）：略過上面那條短路。企劃向
    供應商要到的確認日期是**比信件更新的事實**：如果它跟原承諾日不同，
    就是變了，不能讓信件當時「說不變」的舊結論繼續蓋著新事實。
    這是一個真實發生過的 bug：供應商信件寫「照原計畫」被解析成
    no_change，企劃事後跟供應商要到一個明顯延後的確認日期，
    change_type 卻因為短路而原地不動，gap_days 沒有跟著重算，
    needs_human_review 還被清掉——企劃以為登錄了新日期，
    分級卻完全沒變，甚至可能整筆從清單消失（見 tests/test_confirmations.py）。
    """
    if not force and (current == ChangeType.NO_CHANGE.value or not new_eta):
        return current
    if not new_eta:
        return current
    try:
        new_eta_d = date.fromisoformat(str(new_eta)[:10])
        committed_d = date.fromisoformat(str(committed_date)[:10])
    except (ValueError, TypeError):
        return current
    if new_eta_d < committed_d:
        return ChangeType.PULL_IN.value
    if new_eta_d > committed_d:
        return ChangeType.DELAY.value
    return ChangeType.NO_CHANGE.value


# 百分位到 run() 存好的欄名對應，鍵跟 triage.percentile_for() 一致
# （沒對到的承諾強度，包含 "none"，一律走最保守的 p95）。
_PERCENTILE_COLUMNS = {"confirmed": "delay_days_p80", "estimated": "delay_days_p90"}


def _percentile_column(strength: str | None) -> str:
    return _PERCENTILE_COLUMNS.get(strength or "", "delay_days_p95")


def _delay_estimates(outcomes: pd.DataFrame, supplier_id: str, tcfg: dict) -> dict[str, dict]:
    """
    對 confirmed／estimated／intent_only 三個百分位各算一次延遲估計。

    retriage() 手上沒有歷史收貨資料，沒辦法重算估計——企劃調完收貨
    處理天數或登錄確認交期時，只想立刻看到新的分級，不該（也不能）
    為此重新掃一次歷史資料庫。所以 run() 一次把三個百分位都存進
    every matched 列（delay_days_p80/p90/p95），retriage() 之後只要
    依承諾強度挑對應欄，不用重算。
    """
    min_samples = int(tcfg["min_samples"])
    return {strength: estimate_delay(outcomes, supplier_id,
                                     percentile=triage.percentile_for(strength, tcfg),
                                     min_samples=min_samples)
            for strength in ("confirmed", "estimated", "intent_only")}


def _pct_delay_days(estimate: dict) -> float:
    """三個百分位欄位的值：有估計就是天數，樣本不足就是 NaN（不是 0）。"""
    return float(estimate["delay_days"]) if estimate.get("available") else float("nan")


def _select_estimate(row: dict, strength: str, tcfg: dict) -> dict | None:
    """
    retriage() 依承諾強度，從 run() 存好的 delay_days_p80/p90/p95 挑一欄。

    欄位不存在（舊格式的 all_df，或測試自己組的資料，例如
    tests/test_retriage.py）：退回 Task 1 之前的邏輯——用單一欄位
    delay_days_est／estimate_available，必要時從 delay_n 反推有沒有
    估計，行為與之前完全相同。

    欄位存在但是 NaN：代表 run() 當初對這個百分位就判斷「樣本不足」，
    不能因為 delay_days_est 剛好有值就當作有估計去套——那個值是另一個
    百分位（row 原本的承諾強度）算出來的，套在這裡是錯的估計，比
    「沒有估計」更危險。只有欄位整個不存在，才退回 delay_days_est。

    「這家供應商過去（N 筆）」這句話裡的 N 與 basis，不能讀 row 上的
    delay_n／delay_basis——那兩個欄位是 triage.evaluate() 對「這一列
    原本的承諾強度」算出來的，待查／提前交貨／確認不變會提早 return，
    帶著預設值 0／""，不代表這個供應商真的沒有歷史樣本（這正是一個真實
    bug：企劃確認一張原本是「待查」的單後，明明 delay_days_p80 有值，
    訊息卻說「過去（0 筆）」）。要知道供應商真正的歷史筆數與依據，
    只能讀 run() 另外存的 delay_n_hist／delay_basis_hist——那是直接從
    supplier_stats.estimate_delay() 來的，不會被 triage.evaluate() 的
    早退路徑污染。欄位不存在（舊格式）才退回 row 上的 delay_n／delay_basis。
    """
    col = _percentile_column(strength)
    if col in row:
        val = row.get(col)
        hist_n = row.get("delay_n_hist")
        if hist_n is None:  # 舊格式：沒有 *_hist 欄位，退回原本（可能被污染的）欄位
            hist_n = row.get("delay_n", 0)
            hist_basis = row.get("delay_basis", "")
            hist_reason = row.get("estimate_reason") or ""
        else:
            hist_basis = row.get("delay_basis_hist", "")
            hist_reason = row.get("estimate_reason_hist") or ""
        if triage._missing(val):
            return {"available": False, "n": hist_n,
                    "reason": hist_reason or "歷史樣本不足，不提供保守估計"}
        return {"available": True, "delay_days": int(val),
                "percentile": triage.percentile_for(strength, tcfg),
                "basis": hist_basis, "n": hist_n}

    if "estimate_available" not in row:
        # 相容舊格式的 all_df：用 delay_n > 0 反推有沒有估計——有估計
        # 一定有算過至少一筆歷史樣本，delay_n 才會是正的。百分位優先讀
        # delay_percentile，沒有（或是 NaN）就照承諾強度現算。
        n = row.get("delay_n", 0)
        try:
            n = int(n) if not triage._missing(n) else 0
        except (TypeError, ValueError):
            n = 0
        if n > 0:
            pct = row.get("delay_percentile")
            if triage._missing(pct):
                pct = triage.percentile_for(row.get("commitment_strength"), tcfg)
            return {"available": True, "delay_days": row.get("delay_days_est", 0),
                    "percentile": pct, "basis": row.get("delay_basis", ""), "n": n}
        return {"available": False, "n": n,
                "reason": row.get("estimate_reason") or "沒有歷史收貨紀錄"}
    if row.get("estimate_available"):
        return {"available": True, "delay_days": row.get("delay_days_est", 0),
                "percentile": row.get("delay_percentile"),
                "basis": row.get("delay_basis", ""), "n": row.get("delay_n", 0)}
    if triage._clean_str(row.get("estimate_reason")):
        return {"available": False, "n": row.get("delay_n", 0),
                "reason": row.get("estimate_reason")}
    return None


# ---------------------------------------------------------------------------
def extract_one(email: dict, cfg: dict, known_pos: list[str],
                provider=None, use_llm: bool = True) -> tuple[list[dict], dict]:
    """
    對單封信執行分層解析。

    回傳 (records, trace)。trace 記錄這封信走過哪幾層、為什麼升級、
    LLM 呼叫的耗時與成敗 —— 沒有 trace 的 AI 工具無法被稽核，也不會被信任。
    """
    threshold = float(cfg["extraction"]["confidence_threshold"])
    rule_records = [r.to_dict() for r in extract_rules.extract(email)]
    rule_conf = max([r["confidence"] for r in rule_records], default=0.0)

    trace = {
        "email_id": email["email_id"],
        "rule_confidence": round(rule_conf, 2),
        "escalated": False,
        "llm_ok": False,
        "llm_latency_ms": 0,
        "llm_error": "",
        "final_layer": "rule",
    }

    if not use_llm or rule_conf >= threshold:
        trace["reason"] = ("未啟用 LLM" if not use_llm
                           else f"規則層信心 {rule_conf:.2f} ≥ 門檻 {threshold}，未升級")
        return rule_records, trace

    trace["escalated"] = True
    trace["reason"] = f"規則層信心 {rule_conf:.2f} < 門檻 {threshold}，升級至 LLM"
    llm_records, meta = extract_llm.extract(
        email, cfg["data_generation"]["as_of_date"], known_pos, provider)
    trace.update(llm_ok=meta["ok"], llm_latency_ms=meta["latency_ms"],
                 llm_error=meta["error"])

    if meta["ok"] and llm_records:
        trace["final_layer"] = "llm"
        return [r.to_dict() for r in llm_records], trace

    # LLM 不可用或失敗 —— 退回規則層結果，並在 UI 標示可信度低。
    trace["final_layer"] = "rule(fallback)"
    return rule_records, trace


def retriage(all_df: pd.DataFrame, gr_days_by_material: dict, tcfg: dict,
            confirmations: dict | None = None) -> pd.DataFrame:
    """
    只重算分級，不重跑讀信。

    企劃在「收貨處理天數」分頁調完某個料號的天數後，只需要重新跑這支函式
    就能立刻看到新的優先序——不必、也不該為了一個天數調整再去呼叫 LLM
    重新解析一次信件（成本與延遲都划不來，何況信件內容根本沒變）。這也是
    run() 唯一的分級路徑：run() 把抽取與對位的原始欄位寫進 all_df，
    分級一律交給這支函式，避免兩套邏輯各自演化到對不上。

    gr_days_by_material：{material_id: (天數, 來源說明)}，只放企劃覆寫過的
    料號。沒被覆寫的料號，用列上原本的 gr_processing_days／category 呼叫
    planner_settings.effective_gr_days 取得料別預設與說明文字。

    confirmations：planner_settings.load_confirmations() 的回傳值，
    {(po_no, sched_line, batch_key): {confirmed_date, email_id, note,
    confirmed_by, confirmed_at}}——鍵是「採購單 × 排程行 × 批次」而不是
    單純 po_no，分批交貨時企劃可能只跟供應商確認了其中一批，不能讓那筆
    確認套到同一張單的另一批（見 Plan 3 Task 3）。batch_key 是為了「一
    封信對同一個排程行講了不只一批」這種情況才存在的（供應商提議拆批、
    或規則 4 的保守退路剛好讓兩筆記錄撞在同一行，見 run() 的
    _resolve_record_batches）——多數列同一封信對同一行只有一筆記錄，
    batch_key 就是空字串，等於只用 (po_no, sched_line) 當鍵，跟這個機制
    出現以前完全一樣。列上沒有 sched_line／batch_key 欄位（舊格式
    all_df、測試自己組的精簡資料）分別視為第 1 行、空字串
    （_row_sched_line()／coalesce_batch_key()），對應「只有一筆排程行、
    沒有撞批的單，行為跟改版前完全一樣」。
    套用的條件是 (po_no, sched_line, batch_key) 對得上**而且 email_id 也對得上**——email_id 對不上
    代表供應商在企劃確認之後又寄了新信（例如改口延到更晚），這種情況
    舊確認已經作廢，不能讓它蓋掉新信的內容，寧可讓這張單回到「需人工
    確認」，也不能顯示一個已經不算數的日期；這種「作廢」不能默默發生，
    要在 reasons 第一條寫明是哪個舊確認、被哪封新信作廢，企劃才知道
    「這張單本來已經確認過」而不是以為工具漏看了自己的確認紀錄。
    套用後 new_eta／commitment_strength／change_type 都改用確認後的值
    （change_type 用跟 run() 相同的 _derive_change_type，但傳 force=True：
    企劃的確認是比信件更新的事實，不能被「信件當時說不變」的舊結論
    短路掉，見 _derive_change_type 的說明），needs_human_review 設為
    False，並在 reasons 最前面插入一條寫明「誰、哪天確認、尚未寫回 ERP」
    的說明；這條說明裡把 triage.evaluate() 原本寫的「供應商說 {日期}」
    換成「確認交期 {日期}」——那句話原本假設日期一定是供應商信件說的，
    確認交期的路徑上日期是企劃自己登錄的，用詞要對得上，不能讓企劃以為
    那是供應商剛講的話。

    對不到 PO 主檔（matched=False）的列，即使 confirmations 裡剛好有
    同樣 po_no 的確認（理論上不該發生，防禦用），也完全不套用：不到主檔
    的列沒有 committed_date 可比對，也沒有 triage.evaluate() 可以重算，
    這裡直接 continue 略過，等同從不查 confirmations。
    """
    if all_df.empty:
        return all_df

    rows: list[dict] = []
    confirmations = confirmations or {}
    for _, series in all_df.iterrows():
        row = series.to_dict()
        if not row.get("matched"):
            # 對不到 PO 主檔的列本來就沒有可以重算的東西，原樣保留，
            # 也不查 confirmations（見上面的說明）。
            rows.append(row)
            continue

        po_no = row.get("po_no")
        sched_line = _row_sched_line(row)
        batch_key = coalesce_batch_key(row.get("batch_key"))
        confirmation = confirmations.get((po_no, sched_line, batch_key))
        confirmed = bool(confirmation) and confirmation.get("email_id") == row.get("email_id")
        # 有確認紀錄、但 email_id 對不上目前這封信：舊確認已經作廢
        # （見上面的說明），這裡先記下來，等 reasons 算完後插在最前面。
        superseded = bool(confirmation) and not confirmed
        if confirmed:
            row["change_type"] = _derive_change_type(
                confirmation["confirmed_date"], row.get("committed_date"),
                row.get("change_type"), force=True)
            row["new_eta"] = confirmation["confirmed_date"]
            row["commitment_strength"] = CommitmentStrength.CONFIRMED.value

        material_id = row.get("material_id")
        override = gr_days_by_material.get(material_id)
        if override is not None:
            gr_days, gr_source = override
        else:
            gr_days, gr_source = planner_settings.effective_gr_days(
                material_id, row.get("gr_processing_days"), row.get("category"), {})

        record = {"new_eta": row.get("new_eta"), "change_type": row.get("change_type"),
                  "commitment_strength": row.get("commitment_strength")}
        po = {"committed_date": row.get("committed_date"), "need_date": row.get("need_date"),
              "downstream_scheduled": row.get("downstream_scheduled")}
        material = {"has_qualified_second_source": row.get("has_second_source"),
                    "is_bottleneck": row.get("is_bottleneck"),
                    "alt_material_id": row.get("alt_material_id"),
                    "gr_processing_days": gr_days, "gr_source": gr_source}

        strength = _strength_for_percentile(record)
        estimate = _select_estimate(row, strength, tcfg)

        result = triage.evaluate(record, po, material, tcfg, estimate)
        reasons = result["reasons"]
        # 對位到排程行時如果用了保守退路（見 pipeline.match_schedule_line
        # 規則 4），run() 會把那句說明存成 sched_match_note 欄位——這裡要
        # 重新插回 reasons，因為 triage.evaluate() 剛剛是從頭算的，完全
        # 不知道這件事。這條規則跟 run() 直接組 all_df 那一列時的做法
        # 必須完全一致，否則同一列在「剛解析完」跟「企劃調完天數後
        # retriage 重算」會顯示不同的理由（見
        # tests/test_pipeline_triage.py 的一致性測試）。
        match_note = row.get("sched_match_note") or ""
        if match_note:
            reasons = [match_note] + reasons
        if confirmed:
            by = confirmation.get("confirmed_by", "")
            at = confirmation.get("confirmed_at", "")
            note = confirmation.get("note") or ""
            note_part = f"（{note}）" if note else ""
            conf_date = confirmation["confirmed_date"]
            prefix = (f"企劃 {by} {at[5:10]} 向供應商確認交期 "
                      f"{conf_date}{note_part}；"
                      "尚未寫回 ERP，請依公司流程更新交貨排程行")
            # triage.evaluate() 寫死假設日期是供應商信件說的（「供應商說
            # {日期}」／「供應商說的日期 {日期}」）；這裡日期是企劃自己
            # 登錄的確認日期，換個對得上事實的說法，不改其餘文字。
            reasons = [r.replace(f"供應商說的日期 {conf_date}", f"確認交期 {conf_date}")
                       .replace(f"供應商說 {conf_date}", f"確認交期 {conf_date}")
                       for r in reasons]
            reasons = [prefix, *reasons]
        elif superseded:
            by = confirmation.get("confirmed_by", "")
            at = confirmation.get("confirmed_at", "")
            note_prefix = (f"企劃 {by} {at[5:10]} 依 {confirmation.get('email_id', '')} 確認的 "
                          f"{confirmation.get('confirmed_date', '')} 已因供應商新信 "
                          f"{row.get('email_id', '')} 作廢，請重新確認")
            reasons = [note_prefix, *reasons]

        row.update(gap_days=result["gap_days"], conservative_eta=result["conservative_eta"],
                   available_date=result["available_date"], priority=result["priority"],
                   reasons=reasons, actions=result["actions"], note=result["note"],
                   gr_processing_days=gr_days, gr_source=gr_source)
        if confirmed:
            row["needs_human_review"] = False
        rows.append(row)

    out = pd.DataFrame(rows)
    out = out[out["priority"] != "—"].reset_index(drop=True)
    return _sort_by_urgency(out)


def run(cfg: dict | None = None, use_llm: bool = True) -> dict:
    """執行完整流程，回傳可直接餵給 UI 的結果集。"""
    cfg = cfg or load_config()
    tcfg = cfg["triage"]
    as_of = date.fromisoformat(cfg["data_generation"]["as_of_date"])

    source = get_data_source(cfg)
    pos_df, mats_df, sups_df = (source.purchase_orders(), source.materials(),
                                source.suppliers())
    emails = load_emails()
    po_index = _build_po_index(pos_df)
    mat_index = mats_df.set_index("material_id").to_dict("index")
    sup_index = sups_df.set_index("supplier_id").to_dict("index")
    outcomes = _load_outcomes()

    provider = get_provider() if use_llm else None
    llm_available = bool(provider and provider.available)

    rows: list[dict] = []
    traces: list[dict] = []
    # 依供應商快取三個百分位的估計，同一供應商名下多張單不用各自重算一次
    # （_delay_estimates 每次都要掃一輪 outcomes，供應商名單通常遠比信件少）。
    estimate_cache: dict[str, dict[str, dict]] = {}

    for email in emails:
        # 只把該供應商名下的 PO 帶進 prompt，縮短 prompt 並降低錯配機會。
        # 帶的是完整情境（含原承諾日）而非只有單號 —— 模型要能推算相對日期。
        known = pos_df.loc[
            pos_df["supplier_id"] == email["supplier_id"],
            ["po_no", "material_id", "committed_date", "need_date"]
        ].to_dict("records")
        records, trace = extract_one(email, cfg, known, provider, use_llm)
        traces.append(trace)

        # ---- 對位到排程行：一封信裡同一張單可能有好幾筆記錄，先分組
        # 算好每一筆要對到哪個排程行、彼此有沒有撞在同一行上（見
        # _resolve_record_batches：這是「供應商提議拆批」與「規則 4
        # 猜到同一行」兩種情境的判斷都在這裡完成，讓底下逐筆組列只要
        # 讀結果，不必重複判斷邏輯）----
        by_po: dict[str, list[int]] = {}
        for i, rec in enumerate(records):
            po_no = rec.get("po_no")
            if po_no and po_no in po_index:
                by_po.setdefault(po_no, []).append(i)
        batch_info: dict[int, dict] = {}
        for po_no, idxs in by_po.items():
            resolved = _resolve_record_batches([records[i] for i in idxs], po_index[po_no])
            for local_i, i in enumerate(idxs):
                batch_info[i] = resolved[local_i]

        for i, rec in enumerate(records):
            po_no = rec.get("po_no")
            if not po_no:
                continue
            rec["email_id"] = email["email_id"]
            rec["received_at"] = email["received_at"]
            rec["supplier_id"] = email["supplier_id"]
            rec["subject"] = email["subject"]
            rec["email_tags"] = ",".join(email.get("tags", []))

            po = po_index.get(po_no)
            if po is None:
                # 對不到 PO 主檔 —— 不能靜默丟掉，這通常代表 PO 號打錯或
                # 是別的單位的單，必須讓人看到。沒有主檔就沒有排程行可比對，
                # sched_line 留 None——_row_sched_line() 會把它當成第 1 行，
                # 同一張（打錯的）單號的多封信仍然會被去重成一列。
                rows.append({**rec, "po_no": po_no, "sched_line": None,
                             "sched_lines_total": None, "sched_qty": None,
                             "batch_key": "", "is_proposed_split": False,
                             "proposed_batch_index": None, "proposed_batch_total": None,
                             "matched": False,
                             "gap_days": None, "priority": "待查",
                             "reasons": ["信中的 PO 號對不到主檔，需人工確認"],
                             "actions": [],
                             "needs_human_review": True, "note": ""})
                continue

            material = mat_index.get(po["material_id"], {})
            supplier = sup_index.get(po["supplier_id"], {})

            # ---- 對位到排程行（分批交貨：先決定是哪一批，才知道要跟
            # 哪一行的承諾日比對）----
            lines = po["lines"]
            info = batch_info[i]
            sched_line = info["sched_line"]
            needs_line_review = info["needs_line_review"]
            line = next(l for l in lines if int(l["sched_line"]) == sched_line)
            sched_lines_total = len(lines)

            # ---- 對位之後才判定變更類型（單看信件做不到）----
            rec["change_type"] = _derive_change_type(
                rec.get("new_eta"), line["committed_date"], rec.get("change_type"))

            # 信裡沒給新日期（或給的日期解析不出來）時，不論模型把承諾強度
            # 判成什麼，都取最保守的百分位。
            strength = _strength_for_percentile(rec)
            pct = triage.percentile_for(strength, tcfg)
            supplier_id = po["supplier_id"]
            if supplier_id not in estimate_cache:
                estimate_cache[supplier_id] = _delay_estimates(outcomes, supplier_id, tcfg)
            estimates_by_strength = estimate_cache[supplier_id]
            # "none"（信裡沒給新日期）沒有對應欄——跟 intent_only 同一個
            # 百分位（見 triage.percentile_for），拿 intent_only 那組即可。
            estimate = estimates_by_strength.get(strength, estimates_by_strength["intent_only"])

            # 收貨處理天數：run() 這裡還沒有企劃的覆寫（覆寫只在使用者按
            # 「收貨處理天數」分頁的表單時才會有），所以永遠傳空 overrides，
            # 取到的就是料號主檔＋料別預設。之後 retriage() 用同一支函式，
            # 分級只有這一套邏輯，不會兩邊各自算一次而對不上。
            gr_days, gr_source = planner_settings.effective_gr_days(
                po["material_id"], material.get("gr_processing_days"),
                material.get("category"), {})
            material_for_eval = {**material, "gr_processing_days": gr_days,
                                 "gr_source": gr_source}
            # triage.evaluate() 讀的 po["committed_date"] 必須是「這一批」的
            # 承諾日，不是整張單共用一個日期——po 本身（_build_po_index()
            # 分出來的 PO 層級欄位）沒有 committed_date，這裡從對位到的
            # 那一行補上。
            po_for_eval = {**po, "committed_date": line["committed_date"]}
            result = triage.evaluate(rec, po_for_eval, material_for_eval, tcfg, estimate)

            # ---- 人工確認閘門 ----
            gate = set(cfg["extraction"]["require_human_review_when"])
            needs_review = (
                rec.get("commitment_strength") in gate
                or rec.get("confidence", 0) < 0.6
                or (rec.get("change_type") == ChangeType.DELAY.value
                    and not rec.get("new_eta"))
                # 對位到排程行用了保守退路（規則 4，工具是用猜的），或者
                # 這一列是供應商提議拆批（ERP 還沒拆行，需要人工確認後
                # 由採購維護排程行）：兩種情況都不能讓企劃以為這是有
                # 把握、已經反映在系統裡的結果。
                or needs_line_review
            )

            # 保守退路／提議拆批的說明要讓企劃看到，插在 triage.evaluate()
            # 算出的理由最前面，寫法跟 retriage() 插入確認說明的方式一致。
            #
            # 「提議拆批」（is_proposed_split）跟規則 4 的「對到最早的
            # 一批」是兩件不同的事（見 _resolve_record_batches 的說明），
            # 這裡才第一次拿得到組說明文字要用的 material（單位）跟
            # info 裡的 batch_index／batch_total，所以在這裡才組出最終
            # 文字，_resolve_record_batches 只回傳 is_proposed_split 這個
            # 判斷、不組文字。
            if info["is_proposed_split"]:
                match_note = _proposed_split_note(
                    rec.get("qty"), triage._clean_str(material.get("base_uom")),
                    info["batch_index"], info["batch_total"])
            else:
                match_note = info["match_note"]

            # match_note 同時存成 sched_match_note 欄位（不是只塞進
            # reasons 字串裡）：retriage() 重算分級時是從 row 的原始欄位
            # 重建 record/po/material 再呼叫一次 triage.evaluate()，那支
            # 函式完全不知道「對位到排程行時用了保守退路／供應商提議
            # 拆批」這件事，算出來的 reasons 天生就不會有這句話。如果
            # 不把它存成獨立欄位，retriage() 重算出的 reasons 會少這一
            # 條，跟 run() 剛算出來的版本對不上（tests/test_pipeline_triage.py
            # 的一致性測試就是為了抓這種問題）。
            reasons = ([match_note] if match_note else []) + result["reasons"]

            # 「本批數量」：一般情況（ERP 已經拆好行，或單一排程行）就是
            # 這一行 ERP 記錄的數量；供應商提議拆批時，ERP 那筆排程行的
            # 數量還是項次總量（20），不是這一批自己的量（8／12）——這裡
            # 要顯示的是供應商提議的那個數字，不是 ERP 還沒更新的舊數字，
            # 否則企劃會看到兩批的「本批數量」都寫著同一個總量，看不出
            # 8 片跟 12 片的差別。信裡沒提到數量（batch_key 退回用第幾筆
            # 識別）才退回 ERP 的行數量，好過完全留白。
            sched_qty = (int(rec["qty"]) if info["is_proposed_split"]
                        and rec.get("qty") is not None else int(line["sched_qty"]))

            rows.append({
                **rec,
                "matched": True,
                "sched_line": sched_line,
                "sched_lines_total": sched_lines_total,
                "sched_qty": sched_qty,
                "sched_match_note": match_note,
                "batch_key": info["batch_key"],
                "is_proposed_split": info["is_proposed_split"],
                "proposed_batch_index": info["batch_index"] if info["is_proposed_split"] else None,
                "proposed_batch_total": info["batch_total"] if info["is_proposed_split"] else None,
                "material_id": po["material_id"],
                "category": material.get("category", ""),
                "committed_date": line["committed_date"],
                "need_date": po["need_date"],
                "qty": po["qty"],
                "downstream_scheduled": po["downstream_scheduled"],
                "reschedule_count": po["reschedule_count"],
                "has_second_source": material.get("has_qualified_second_source", False),
                "is_bottleneck": material.get("is_bottleneck", False),
                "share_of_period_demand": line.get("share_of_period_demand"),
                "criticality": material.get("criticality", ""),
                "std_lead_time_days": material.get("std_lead_time_days", 0),
                "alt_material_id": material.get("alt_material_id", ""),
                "supplier_name": supplier.get("supplier_name", ""),
                "supplier_otd": supplier.get("historical_otd_rate", None),
                "gr_processing_days": gr_days,
                "gr_source": gr_source,
                "estimate_available": bool(estimate and estimate.get("available")),
                "estimate_reason": "" if (estimate and estimate.get("available")) \
                    else (estimate or {}).get("reason", ""),
                "delay_percentile": (float(estimate["percentile"])
                                     if estimate and estimate.get("available") else pct),
                # retriage() 沒有歷史資料，沒辦法重算估計——把三個百分位都
                # 存起來，retriage() 依它要用的承諾強度挑對應欄
                # （見 pipeline._select_estimate）。樣本不足時是 NaN，
                # 不是 0：0 天會被誤讀成「歷史上準時到」。
                "delay_days_p80": _pct_delay_days(estimates_by_strength["confirmed"]),
                "delay_days_p90": _pct_delay_days(estimates_by_strength["estimated"]),
                "delay_days_p95": _pct_delay_days(estimates_by_strength["intent_only"]),
                # 供應商真正的歷史筆數／依據——不受 triage.evaluate() 早退路徑
                # 污染，見 _select_estimate() 與 C2 的說明。三個百分位共用
                # 同一個 supplier_stats.estimate_delay() 篩樣本池的邏輯
                # （改期單樣本夠不夠、退回全部單），樣本池的選擇跟百分位
                # 門檻無關，所以 confirmed/estimated/intent_only 這三次呼叫
                # 對同一個供應商一定算出相同的 n／basis／是否可用，
                # 拿哪一個都一樣，這裡固定拿 "confirmed" 那組。
                "delay_n_hist": int(estimates_by_strength["confirmed"]["n"]),
                "delay_basis_hist": estimates_by_strength["confirmed"].get("basis", ""),
                "estimate_available_hist": bool(estimates_by_strength["confirmed"]["available"]),
                "estimate_reason_hist": estimates_by_strength["confirmed"].get("reason", ""),
                "gap_days": result["gap_days"],
                "conservative_eta": result["conservative_eta"],
                "available_date": result["available_date"],
                "delay_days_est": result["delay_days_est"],
                "delay_basis": result["delay_basis"],
                "delay_n": result["delay_n"],
                "priority": result["priority"],
                "reasons": reasons,
                "actions": result["actions"],
                "note": result["note"],
                "needs_human_review": needs_review,
            })

    df = pd.DataFrame(rows)
    if df.empty:
        return {"actions": df, "traces": pd.DataFrame(traces),
                "llm_available": llm_available, "stats": {}, "as_of": as_of,
                "data_source": source.describe()}

    # ---- 去重：同一個排程行只留「最新一封信」貢獻的列 ----
    # （對應手寫案例 HC-001 / HC-009：同一天內供應商又補了一封確認信；
    # 詳細規則見 _keep_latest_email_per_schedule_line 的說明）。
    df["received_at"] = pd.to_datetime(df["received_at"], errors="coerce")
    df = _keep_latest_email_per_schedule_line(df)

    # 分級交給 retriage()，跟企劃事後調整天數走同一套邏輯（不重覆一份
    # 過濾＋排序），這裡沒有任何覆寫，結果等同於直接濾掉「—」再排序。
    actionable = retriage(df, {}, tcfg)

    stats = {
        "emails_processed": len(emails),
        "records_extracted": len(rows),
        "unique_pos": int(df["po_no"].nunique()),
        "no_change_filtered": int((df["priority"] == "—").sum()),
        "actionable": int(len(actionable)),
        "p1": int((actionable["priority"] == "P1").sum()),
        "p2": int((actionable["priority"] == "P2").sum()),
        "p3": int((actionable["priority"] == "P3").sum()),
        "unmatched": int((~df["matched"]).sum()),
        "needs_review": int(actionable["needs_human_review"].sum()),
        "escalated": int(sum(t["escalated"] for t in traces)),
        "llm_ok": int(sum(t["llm_ok"] for t in traces)),
    }

    return {"actions": actionable, "all": df, "traces": pd.DataFrame(traces),
            "llm_available": llm_available, "stats": stats, "as_of": as_of,
            "data_source": source.describe()}


if __name__ == "__main__":
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    out = run(use_llm=True)
    print(json.dumps(out["stats"], ensure_ascii=False, indent=2))
    cols = ["priority", "gap_days", "po_no", "material_id", "supplier_name",
            "new_eta", "commitment_strength", "needs_human_review"]
    print(out["actions"][cols].head(15).to_string(index=False))
