# -*- coding: utf-8 -*-
"""
回信草稿生成。

定位：**草稿，不是自動回信。**
工具永遠不會替使用者寄出任何信件 —— 交期溝通牽涉商務關係，
一封語氣不對的信造成的損害，遠大於省下的那三分鐘。
工具的責任是把「查資料 + 打字」這段省掉，決定權留給人。

兩種模式：
    有 LLM   → 依案件情境生成貼合語氣的草稿
    無 LLM   → 以規則式模板生成（欄位由系統填入，語氣固定但正確）

模板模式刻意保留而非移除，因為它有兩個真實用途：
    1. 同仁還沒申請到金鑰時，工具照樣可用
    2. 需要「每次都一模一樣」的正式通知時，模板比 LLM 更適合
"""
from __future__ import annotations

from llm.provider import BaseProvider, get_provider

PROMPT = """你是半導體公司的物料企劃，正要回信給供應商窗口，追一張交期有變的採購單。

案件資訊：
- 採購單號：{po_no}
- 料號：{material_id}
- 供應商：{supplier_name}
- 原承諾交期：{committed_date}
- 供應商新回覆交期：{new_eta}
- 供應商承諾強度：{strength}
- 下游需求日：{need_date}
- 系統判定：{priority}（預估缺料天數 {gap}；正數代表預估來不及，負數代表尚有緩衝）
- 主要原因：{reasons}

請寫一封繁體中文的回信草稿，要求：
1. 語氣專業、對事不對人，不要指責。這是長期合作關係，不是一次性交易。
2. 如果供應商的承諾強度不是「已確認」，**必須明確要求對方給出可承諾的確切日期**，
   並說明我方需要據此排定下游產能。
3. 如果緩衝天數已經不足，說明我方的需求日，請對方評估是否有部分提前交付的可能。
4. 結尾請對方在特定期限前回覆。
5. 全文控制在 200 字以內，主管與供應商都沒時間讀長信。

只輸出信件內文，不要加任何說明或標題。"""


def _template_draft(row: dict) -> str:
    """無 LLM 時的規則式模板。欄位由系統填入，內容正確但語氣固定。"""
    strength_map = {
        "confirmed": "貴司已確認之交期",
        "estimated": "貴司初步預估之交期",
        "intent_only": "貴司目前尚未承諾之預計交期",
        "none": "（信中未提供明確日期）",
    }
    desc = strength_map.get(str(row.get("commitment_strength")), "貴司回覆之交期")
    eta = row.get("new_eta") or "（未提供）"
    ask = ""
    if row.get("commitment_strength") != "confirmed":
        ask = ("\n由於此日期尚未確認，煩請於本週內提供可承諾之確切出貨日，"
               "以利我方排定後續產能。")
    return (
        f"您好，\n\n"
        f"關於採購單 {row.get('po_no')}（料號 {row.get('material_id')}），"
        f"原承諾交期為 {row.get('committed_date')}，"
        f"目前{desc}為 {eta}。\n"
        f"我方此料之下游需求日為 {row.get('need_date')}，"
        f"本案經系統評估為 {row.get('priority')}。{ask}\n\n"
        f"若有部分數量可提前交付，也煩請一併告知，我方可據此調整投料順序。\n\n"
        f"感謝協助。\n\n"
        f"（本草稿由供應商交期回覆解析工具產生，寄出前請自行確認內容與語氣）"
    )


def generate(row: dict, provider: BaseProvider | None = None) -> tuple[str, str]:
    """回傳 (草稿內容, 來源說明)。來源必須顯示在 UI 上，使用者有權知道這是誰寫的。"""
    provider = provider or get_provider()
    if not provider.available:
        return _template_draft(row), "規則式模板（未接 LLM）"

    reasons = row.get("reasons") or []
    prompt = PROMPT.format(
        po_no=row.get("po_no"), material_id=row.get("material_id"),
        supplier_name=row.get("supplier_name"), committed_date=row.get("committed_date"),
        new_eta=row.get("new_eta") or "（信中未提供明確日期）",
        strength=row.get("commitment_strength"), need_date=row.get("need_date"),
        priority=row.get("priority"), gap=row.get("gap_days"),
        reasons="；".join(reasons) if isinstance(reasons, list) else str(reasons),
    )
    # json_mode=False：這是要給人讀的信，不是要進資料表的結構化資料。
    resp = provider.complete(prompt, temperature=0.3, json_mode=False)
    if resp.ok and resp.text.strip():
        return resp.text.strip(), f"{resp.provider} / {resp.model}（{resp.latency_ms} ms）"
    return _template_draft(row), f"LLM 呼叫失敗，已降級為模板：{resp.error[:120]}"
