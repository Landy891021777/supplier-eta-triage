# -*- coding: utf-8 -*-
"""
手寫的「刁鑽案例」信件庫。

為什麼要手寫？
    程式亂數產生的信件一定長得像模板，規則式 regex 全部都抓得到，
    那樣做出來的對照實驗會得出「不需要 LLM」的假結論。
    真實世界的供應商窗口是活人：他們會用相對日期、會中英夾雜、
    會在一封信裡講三張 PO、會轉寄一整串舊信、會用模糊措辭卸責。

    以下每一封的寫法，都對應我實際看過的溝通型態。
    ground_truth 是人工標註的正確答案，用來量化解析層的表現。

欄位說明：
    tags 標記這封信「難在哪」，評估時可依 tag 分群看表現。
"""
from __future__ import annotations

HANDCRAFTED: list[dict] = [
    {
        "email_id": "HC-001",
        "supplier_id": "SUP-F01",
        "received_at": "2026-09-07 09:14",
        "subject": "RE: PO Confirmation - PO-2026-04417",
        "body": (
            "Hi Jason,\n\n"
            "PO-2026-04417 (WF-N6-XR3390) 這批我們這邊 loading 有點滿，\n"
            "原本 10/15 可能要往後抓個兩週，大概月底前後，我再跟你確認。\n"
            "另外 PO-2026-04452 那顆先照原計畫走沒問題。\n\n"
            "Thanks,\nAmy"
        ),
        "tags": ["relative_date", "vague", "multi_po", "mixed_language", "no_change_trap"],
        "ground_truth": [
            # 「往後抓個兩週，大概月底前後，我再跟你確認」
            # -> 有新日期線索，但明確不是承諾。
            #    抽成確切日期而不標記不確定性，是本工具最危險的錯誤型態。
            {"po_no": "PO-2026-04417", "new_eta": "2026-10-31",
             "commitment_strength": "intent_only", "change_type": "delay",
             "reason_code": "capacity"},
            # 「先照原計畫走沒問題」-> NO_CHANGE，不是延遲。誤判會製造假警報。
            {"po_no": "PO-2026-04452", "new_eta": None,
             "commitment_strength": "confirmed", "change_type": "no_change",
             "reason_code": "not_stated"},
        ],
    },
    {
        "email_id": "HC-002",
        "supplier_id": "SUP-F02",
        "received_at": "2026-09-07 11:02",
        "subject": "Delivery Schedule Update - Week 37",
        "body": (
            "Dear Customer,\n\n"
            "Please be informed of the following schedule revision:\n\n"
            "PO No.         Material        Original ETA   Revised ETA   Remark\n"
            "PO-2026-04390  WF-N7-KL2210    2026-09-25     2026-10-09    Yield excursion at M1\n"
            "PO-2026-04391  WF-N7-KL2210    2026-10-02     2026-10-16    Same lot family\n\n"
            "Revised dates are confirmed and locked in our system.\n\n"
            "Best regards,\nPlanning Dept."
        ),
        "tags": ["formal_table", "multi_po", "confirmed"],
        "ground_truth": [
            {"po_no": "PO-2026-04390", "new_eta": "2026-10-09",
             "commitment_strength": "confirmed", "change_type": "delay",
             "reason_code": "yield"},
            {"po_no": "PO-2026-04391", "new_eta": "2026-10-16",
             "commitment_strength": "confirmed", "change_type": "delay",
             "reason_code": "yield"},
        ],
    },
    {
        "email_id": "HC-003",
        "supplier_id": "SUP-S01",
        "received_at": "2026-09-07 14:31",
        "subject": "Fwd: RE: RE: 載板交期 SUB-FCCSP-1088",
        "body": (
            "------- Forwarded message -------\n"
            "From: Vendor Sales\n"
            "Date: 2026-08-12\n"
            "Subject: RE: 載板交期\n"
            "> PO-2026-04205 我們預計 9/10 出貨，沒有問題。\n"
            "\n"
            "------- Latest -------\n"
            "王先生您好，\n\n"
            "很抱歉上次回覆的 9/10 需要更新。因為上游 ABF 材料供應仍然吃緊，\n"
            "PO-2026-04205 目前最快要到 10 月中，我們排在 10/14。\n"
            "這個日期是我們生管確認過的。\n\n"
            "謝謝\n李"
        ),
        "tags": ["forward_chain", "stale_date_trap", "chinese", "confirmed"],
        "ground_truth": [
            # 陷阱：轉寄串裡的 9/10 是舊資訊。抓到它，就等於把過期承諾當成新承諾。
            {"po_no": "PO-2026-04205", "new_eta": "2026-10-14",
             "commitment_strength": "confirmed", "change_type": "delay",
             "reason_code": "upstream_shortage"},
        ],
    },
    {
        "email_id": "HC-004",
        "supplier_id": "SUP-F01",
        "received_at": "2026-09-07 16:45",
        "subject": "PO-2026-04501 可以提前",
        "body": (
            "Jason 你好，\n\n"
            "PO-2026-04501 這批我們提前完成了，可以在 9/18 出，比原本的 9/30 早。\n"
            "你們那邊倉庫收得下嗎？如果可以我們就安排。\n\n"
            "Amy"
        ),
        "tags": ["pull_in", "chinese"],
        "ground_truth": [
            # 提前交貨不等於沒事：可能要提早付款、佔用倉容、影響現金流。
            # 工具必須列出來，但優先級邏輯與延遲不同。
            {"po_no": "PO-2026-04501", "new_eta": "2026-09-18",
             "commitment_strength": "confirmed", "change_type": "pull_in",
             "reason_code": "internal_reschedule"},
        ],
    },
    {
        "email_id": "HC-005",
        "supplier_id": "SUP-F03",
        "received_at": "2026-09-08 08:20",
        "subject": "Re: Urgent - PO-2026-04333 status",
        "body": (
            "Hi,\n\n"
            "We are trying our best to pull in the schedule. Currently the lot is still\n"
            "at photo stage. I cannot commit a firm date at this moment, but 我們盡量\n"
            "在下個月中之前出貨。Will update you by end of this week.\n\n"
            "Regards,\nDavid"
        ),
        "tags": ["no_firm_date", "relative_date", "mixed_language", "vague"],
        "ground_truth": [
            # 「盡量在下個月中之前」+「cannot commit a firm date」-> intent_only。
            # 這種信最容易被誤抽成一個確切日期，然後被下游當成承諾使用。
            {"po_no": "PO-2026-04333", "new_eta": "2026-10-15",
             "commitment_strength": "intent_only", "change_type": "delay",
             "reason_code": "capacity"},
        ],
    },
    {
        "email_id": "HC-006",
        "supplier_id": "SUP-F02",
        "received_at": "2026-09-08 09:05",
        "subject": "Schedule confirmation",
        "body": (
            "Hi Jason,\n\n"
            "Confirming PO-2026-04466 will be delivered as originally scheduled on\n"
            "30-Sep-2026. No change from our side.\n\n"
            "Rgds"
        ),
        "tags": ["no_change_trap", "date_format_variant"],
        "ground_truth": [
            # 陷阱：信裡有日期、有 PO，但這是「確認不變」。
            # 若誤判為變更，生管的清單會被無意義項目灌爆 —— 內部工具最常見的死法。
            {"po_no": "PO-2026-04466", "new_eta": None,
             "commitment_strength": "confirmed", "change_type": "no_change",
             "reason_code": "not_stated"},
        ],
    },
    {
        "email_id": "HC-007",
        "supplier_id": "SUP-M01",
        "received_at": "2026-09-08 10:12",
        "subject": "光罩交期通知 MSK-N6-XR3390",
        "body": (
            "您好，\n\n"
            "關於 PO-2026-04120 光罩製作，因為前一版 data 有修改，\n"
            "我們需要重新跑一次，交期會從 9 月 12 日順延到 9 月 22 日。\n"
            "造成不便敬請見諒。\n\n"
            "業務部"
        ),
        "tags": ["chinese_date", "short_notice"],
        "ground_truth": [
            # 中文日期格式「9 月 22 日」；且距原承諾只剩幾天才通知 -> 通知時機規則要抓到。
            {"po_no": "PO-2026-04120", "new_eta": "2026-09-22",
             "commitment_strength": "confirmed", "change_type": "delay",
             "reason_code": "internal_reschedule"},
        ],
    },
    {
        "email_id": "HC-008",
        "supplier_id": "SUP-F03",
        "received_at": "2026-09-08 11:40",
        "subject": "RE: Weekly review",
        "body": (
            "Hi team,\n\n"
            "Following up on the three open POs discussed last week:\n"
            "- PO-2026-04277: on track, ETA unchanged.\n"
            "- PO-2026-04278: slipping by about 10 days due to a tool down event.\n"
            "  New target is around Oct 20 but not yet locked.\n"
            "- PO-2026-04279: we will need to push this one out to next quarter.\n"
            "  Sales will follow up separately.\n\n"
            "David"
        ),
        "tags": ["multi_po", "mixed_outcome", "vague"],
        "ground_truth": [
            {"po_no": "PO-2026-04277", "new_eta": None,
             "commitment_strength": "confirmed", "change_type": "no_change",
             "reason_code": "not_stated"},
            {"po_no": "PO-2026-04278", "new_eta": "2026-10-20",
             "commitment_strength": "estimated", "change_type": "delay",
             "reason_code": "capacity"},
            # 「push out to next quarter」沒有可用日期 -> new_eta 應為 None，
            # 但 change_type 仍是 delay 且必須人工處理。硬抽一個日期是錯的。
            {"po_no": "PO-2026-04279", "new_eta": None,
             "commitment_strength": "intent_only", "change_type": "delay",
             "reason_code": "internal_reschedule"},
        ],
    },
    {
        "email_id": "HC-009",
        "supplier_id": "SUP-F01",
        "received_at": "2026-09-08 13:22",
        "subject": "PO-2026-04417 更新",
        "body": (
            "Jason，\n\n"
            "接續早上的信，PO-2026-04417 我們生管確認了，10/29 可以出。\n"
            "這次是確定的日期。\n\n"
            "Amy"
        ),
        "tags": ["supersedes_earlier", "chinese", "confirmed"],
        "ground_truth": [
            # 同一天內針對同一張 PO 的第二封信 —— 必須以最新一封為準。
            # 這考驗的是對位後的去重與時序處理，不是解析本身。
            {"po_no": "PO-2026-04417", "new_eta": "2026-10-29",
             "commitment_strength": "confirmed", "change_type": "delay",
             "reason_code": "capacity"},
        ],
    },
    {
        "email_id": "HC-010",
        "supplier_id": "SUP-S02",
        "received_at": "2026-09-08 15:08",
        "subject": "Q4 allocation notice",
        "body": (
            "Dear valued customer,\n\n"
            "Due to strong demand from other accounts, we regret to inform you that\n"
            "allocation for Q4 has been adjusted. PO-2026-04188 quantity 5,000 will be\n"
            "split: 2,000 pcs on the original date 2026-09-30, remaining 3,000 pcs\n"
            "deferred to 2026-11-15.\n\n"
            "We appreciate your understanding."
        ),
        "tags": ["partial_delivery", "split_shipment", "allocation"],
        "ground_truth": [
            # 分批交貨：部分準時、部分延遲。實務上最常見卻最少被工具處理的型態。
            # 本版以「最晚的那批」為 new_eta 並於 notes 標記分批 —— 屬已知簡化，見 docs/設計決策.md。
            {"po_no": "PO-2026-04188", "new_eta": "2026-11-15",
             "commitment_strength": "confirmed", "change_type": "delay",
             "reason_code": "customer_priority"},
        ],
    },
]
