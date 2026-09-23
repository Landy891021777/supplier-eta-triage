# 分批交貨與收尾（Plan 3）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓一個採購項次的**每一筆交貨排程行分開追蹤**（目前只看最晚一筆），然後把整個專案收乾淨：文件全面改寫、重跑評估與種子快取、合併到 `main`、把公開網址換成新版。

**Architecture:** 追蹤單位從「採購單」改成「採購單 × 交貨排程行」。資料層讓 `po_schedule` 真的有多筆排程行，`PO_SQL` 每行一列；抽取層允許同一張單抽出多筆交期（帶數量）；對位時把每筆抽取結果對到排程行，分級與畫面都以排程行為單位。企劃的確認紀錄與收貨處理天數覆寫跟著改成以排程行為鍵。

**Tech Stack:** Python 3.13、SQLite、pandas、Streamlit、pytest；Task 5 會呼叫 Gemini（**執行前必須先問使用者**）。

---

## 為什麼要做分批交貨

物料企劃實務上最常見的回覆之一就是「這批先出 2000，剩下的下個月」。目前工具只取最晚那筆，
等於把「先到的那批」當成不存在：明明有一部分準時到、可以先投料，工具卻報整張單缺料；
反過來，如果最晚那筆準時、早的那筆延了，工具會完全看不到缺料。

`docs/設計決策.md` 的已知限制第 1 條、`src/adapters/sqlite_source.py` 的註解都標了這個簡化，
手寫案例 HC-010 也是為它準備的（分批交貨：8 片原日期、12 片延到 11/15）。

## 已確認的範圍與取捨

- **歷史單（`generate_history.py`）維持單一排程行**：歷史只用來估「說定日期後還晚幾天」，
  拆成多行不會讓估計更好，卻會讓回測與統計的單位變得混亂。在文件標明。
- **抽取層允許同一張單多筆交期**，每筆可帶數量；對不到排程行時的退路見 Task 3。
- **企劃的確認與收貨處理天數覆寫**：確認改成綁「採購單 × 排程行 × 信件」；收貨處理天數
  仍然綁料號（那是料號主檔的屬性，與排程行無關）。
- **不做**：把分批交貨的邏輯套到歷史與回測；多幣別、價格、運費。

## 文件現況（Task 4 要改的東西，先列清楚）

| 檔案 | 現在還在講的舊東西 |
|---|---|
| `README.md` | 十條加權規則與權重表、權重校準與 AUC、`impact.py`／`calibrate.py` 的專案結構、Fabless 料別、舊的數字 |
| `CLAUDE.md` | 模組表寫 `impact.py`／`calibrate.py`、「重跑 calibrate.py」的必做事項、舊狀態數字（52 封信、P1 10、898 張歷史單、81 項測試）、候選清單前三項已完成 |
| `docs/操作指引.md` | 沒有「企劃確認交期」「匯出明日追料清單」「供應商月度績效」「兩區導覽」的說明；分頁名稱已改 |
| `docs/部署指引.md` | 檢查清單寫「供應商歷史與權重」分頁、舊的首頁數字 |
| `docs/ERP欄位對應.md` | 分批交貨那幾句要改成「已實作」 |

## 通用規則

- `py -X utf8`；utf-8；**Task 1–4、6 一律 `LLM_PROVIDER=none`**；出現 gemini／embedding／429 立刻停（Task 5 除外）。
- 註解寫「為什麼」，繁體中文；領域假設標 `# 領域假設：`；測試 docstring 寫「錯了會怎樣」。
- Commit：中文 conventional commits，結尾 `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`。
- 不可弱化既有測試；計畫與程式對不上就回報 NEEDS_CONTEXT。
- 本機 port 8512 可能有使用者在看的預覽，不要關；要自己起伺服器請用 8513。
- **文件數字一律引用實際跑出來的結果**（`output/*.md`、`pipeline.run()` 的統計），不可憑印象寫。

---

### Task 1: 資料層 —— 讓交貨排程行真的有多筆

**Files:** Modify `src/generate_data.py`、`src/build_erp_db.py`、`src/adapters/sqlite_source.py`、`src/adapters/csv_source.py`、`src/adapters/base.py`；Test `tests/test_adapters.py`、`tests/test_world.py`

- [ ] **Step 1: 寫失敗的測試**（`tests/test_adapters.py` 追加，沿用現有在 `tmp_path` 重建新世界的 fixture）：
  1. 產生的採購單裡，有 **10%～25%** 的單被拆成 2 筆交貨排程行（數量相加等於項次數量、日期不同、`sched_line` 為 1、2）。
  2. `SqliteSource().purchase_orders()` **每筆排程行一列**，欄位含 `sched_line`、`sched_qty`、`committed_date`；同一張單的兩列 `po_no` 相同、`sched_line` 不同。
  3. 單一排程行的單，行為與現在完全一樣（`sched_line` 為 1、`sched_qty` 等於 `qty`）。
  4. CSV 來源也有 `sched_line`／`sched_qty`（CSV 版本一張單一列時 `sched_line=1`），兩個來源的 `(po_no, sched_line)` 集合一致。
  5. 手寫案例 HC-010 用到的 `PO-2026-04188` 一定要是分批的單（固定資料，不能靠隨機），且兩筆分別是 8 與 12（對應信中的數量）。

- [ ] **Step 2: 實作**
  - `generate_data.py`：`build_pos` 產生的每張單新增欄位 `schedule`：`[(sched_line, committed_date, qty), ...]`。
    - 領域假設：約 20% 的單分兩批交貨；第二批比第一批晚 7～30 天；數量拆法為 30/70 或 50/50（取整數，兩批相加等於總量）。光罩（數量 1）不拆。
    - `FIXED_POS` 增加一個可選欄位描述固定的拆批；`PO-2026-04188`（TG-Cu-90，共 20 片）固定拆成 8（2026-09-30）與 12（2026-11-15），與 HC-010 的信件內容一致。
    - CSV 輸出：`po_master.csv` 改為**每筆排程行一列**，新增 `sched_line`、`sched_qty` 欄位；`qty` 保留為該項次總量。
  - `build_erp_db.py`：`po_schedule` 依 `schedule` 寫多列；`po_item.qty` 仍為總量。
  - `adapters/sqlite_source.py`：`PO_SQL` 的 `latest_sched` CTE 拿掉，改成直接 JOIN `po_schedule`，每行一列，`SELECT ... s.sched_line, s.qty AS sched_qty, s.committed_date`；「只取在途」的條件改成 **該排程行還沒有對應收貨**（`goods_receipt` 目前以 `(po_no, item_no)` 為鍵，歷史單只有一行，維持 JOIN 即可；註解寫明真實 ERP 會以排程行對收貨）。`share_of_period_demand` 的分子改用 `sched_qty`。
  - `adapters/csv_source.py`、`base.py`：資料合約加 `sched_line`、`sched_qty`。
  - 舊註解「本版取最晚的一筆，並在 README 標為已知簡化」整段改寫成現在的做法。
- [ ] **Step 3**：跑 `tests/test_adapters.py tests/test_world.py -v`；**此時 pipeline 尚未支援，多排程行的單會在後續 Task 才正確**。全套測試可能有失敗，列出並確認都屬「pipeline 尚未對位到排程行」這一類。
- [ ] **Step 4: Commit**：`feat(erp): 交貨排程行改為每行一列，支援分批交貨`。

---

### Task 2: 抽取層 —— 一封信可以講同一張單的多批交期

**Files:** Modify `src/llm/prompts/extract_eta.md`、`src/domain.py`、`src/extract_rules.py`、`src/extract_llm.py`、`src/handcrafted_emails.py`（只改 HC-010 的標準答案）、`src/evaluate.py`；Test `tests/test_extraction.py`

- [ ] **Step 1: 決定資料形狀**：`ExtractedRecord` 新增兩個欄位：
  - `qty: int | None` —— 信中提到這批的數量（沒提就是 None）
  - `batch_note: str` —— 例如「分批交貨的其中一批」；沒有就空字串
  同一張單可以有多筆 record（同一個 `po_no`、不同 `new_eta`／`qty`）。
- [ ] **Step 2: 提示詞**（`extract_eta.md`）
  - 新增一節「分批交貨」：供應商說「2000 先出、其餘延到 X」時，**輸出兩筆**，各自帶 `qty` 與 `new_eta`；照原日期的那批 `change_type` 為 `no_change`、`commitment_strength` 依語氣；延後的那批為 `delay`。輸出範例要完整。
  - 修掉一個因為全域改字造成的錯誤：第 53 行的例句「我們物料企劃確認過」應為**供應商那邊的生管**，改回「我們生管確認過」（信件原文就是這句）。第 14 行的角色改為「晶圓廠的資深物料企劃」。
  - JSON schema 說明加上 `qty`（整數或 null）與 `batch_note`。
- [ ] **Step 3: 規則層**（`extract_rules.py`）：表格型信件已可抽多列；新增對「split / 分批 / 其餘 / remaining / pcs on the original date」這類句型的處理——抓得到就輸出兩筆（帶 qty），抓不到維持現狀（一筆、信心不變）。**不要為了讓 HC-010 過關而寫死字串**：用一般化的樣式，並在測試裡用另一句不同寫法驗證。
- [ ] **Step 4: 標準答案**：`handcrafted_emails.py` 的 HC-010 `ground_truth` 改成兩筆：
  - `{"po_no": "PO-2026-04188", "qty": 8, "new_eta": "2026-09-30", "commitment_strength": "confirmed", "change_type": "no_change", "reason_code": "customer_priority"}`
  - `{"po_no": "PO-2026-04188", "qty": 12, "new_eta": "2026-11-15", "commitment_strength": "confirmed", "change_type": "delay", "reason_code": "customer_priority"}`
  信件內文不動（已經寫著 8 與 12）。原本註解裡「本版以最晚那批為準，屬已知簡化」改寫。
- [ ] **Step 5: 評估計分**（`evaluate.py`）：`score_email` 目前以 `po_no` 集合比對；改為以 `(po_no, qty)` 為鍵（`qty` 為 None 時退回只比 `po_no`），讓分批的兩筆能分別計分。`po_hit` 的定義改為「信中提到的每一筆交期都有對應輸出」。docstring 寫明改動原因。
- [ ] **Step 6: 測試**（`tests/test_extraction.py`）：
  - HC-010 規則層抽出兩筆，數量分別是 8 與 12、日期正確、`change_type` 一為 `no_change` 一為 `delay`。
  - 另一句不同寫法的分批（自己造一句，例如「1,000 pcs ship on 10/05, the remaining 500 pcs will follow on 10/26」）也要抽出兩筆。
  - 沒有分批的信仍然只抽一筆（回歸）。
  - `score_email` 對分批的計分正確（自己造 pred／truth）。
- [ ] **Step 7: Commit**：`feat(extract): 一封信可抽出同一張單的多批交期；修正提示詞誤改的例句`。

---

### Task 3: 對位、分級、畫面與匯出都以排程行為單位

**Files:** Modify `src/pipeline.py`、`src/triage.py`（如需要）、`src/planner_settings.py`、`src/exports.py`、`views/actions.py`、`views/erp.py`、`ui_state.py`；Test `tests/test_pipeline_triage.py`、`tests/test_confirmations.py`、`tests/test_exports.py`、`tests/test_retriage.py`

- [ ] **Step 1: 對位規則**（`pipeline.py`，寫成獨立函式 `match_schedule_line(record, lines)` 方便測試）：
  1. 抽取結果有 `qty`，且某一行的 `sched_qty` 與它相同 → 對到那一行。
  2. 沒有 qty，但信中提到的「原日期」等於某一行的 `committed_date` → 對到那一行。
  3. 該單只有一行 → 對到那一行。
  4. 以上都不成立 → **對到最早的未交行**，並在該列加註「信中未指明是哪一批，工具對到最早的一批」，`needs_human_review` 設為 True。
  註解寫明：這是刻意保守的退路，寧可要求人工確認，也不要把一個日期套到錯的批次。
- [ ] **Step 2: 分級與去重**：`run()` 與 `retriage()` 的鍵由 `po_no` 改為 `(po_no, sched_line)`；同一封信對同一行的多筆以最新信件為準（原本的去重邏輯照搬，鍵改掉）。`gap_days` 等欄位不變，但每行各自算。
- [ ] **Step 3: 企劃確認**：`planner_settings` 的 `eta_confirmation` 加 `sched_line` 欄（舊資料 NULL 視為第 1 行），`confirm_eta(...)` 與 `load_confirmations(...)` 以 `(po_no, sched_line)` 為鍵；`retriage` 比對時加上 `sched_line`。既有測試同步更新。
- [ ] **Step 4: 畫面**：
  - 行動清單表格在「採購單號」後加「批次」欄（`sched_line`，只有一行時顯示「—」），數量顯示該批數量。
  - 逐案展開的標題加上批次，例如 `PO-2026-04188（第 2 批 / 共 2 批）`。
  - 確認表單的 key 加上 `sched_line`。
  - ERP 單據頁的第 ④ 張表（交貨排程行）加一句：**這就是分批交貨的資料結構，工具現在每一行分開追蹤。**
- [ ] **Step 5: 匯出**：`exports.followup_workbook` 欄位加「批次」與「本批數量」；`FOLLOWUP_COLUMNS` 順序調整（批次放採購單號後）。
- [ ] **Step 6: 測試**：
  - `match_schedule_line` 四條規則各一個測試（含退路會標需人工確認）。
  - 端到端：HC-010 的 `PO-2026-04188` 在行動清單裡是**兩列**，第 1 批不缺料、第 2 批缺料。
  - 確認紀錄綁到正確的批次：對第 2 批確認不影響第 1 批。
  - 匯出 Excel 有批次欄、數量是該批數量。
- [ ] **Step 7: Commit**：`feat(triage): 分批交貨改為每一批各自追蹤、各自分級`。

---

### Task 4: 文件全面改寫（離線，不碰程式）

**Files:** `README.md`、`CLAUDE.md`、`docs/操作指引.md`、`docs/部署指引.md`、`docs/ERP欄位對應.md`

**先取得真實數字**（跑完把輸出貼進報告，文件只引用這些）：

```bash
LLM_PROVIDER=none py -X utf8 -m pytest tests -q
LLM_PROVIDER=none py -X utf8 src/pipeline.py
py -X utf8 -c "import sqlite3;c=sqlite3.connect('data/erp_sim.db');print({t:c.execute(f'select count(*) from {t}').fetchone()[0] for t in ['vendor_master','material_master','po_header','goods_receipt','po_schedule']})"
```
（`output/實驗結果.md`、`output/RAG檢索評估.md`、`output/回測結果.md` 的數字在 Task 5 重跑後才是最終版；Task 4 先寫結構與說法，數字留 `TBD-待 Task 5`，Task 5 完成後補上——**這是計畫裡唯一允許出現待補標記的地方，且必須在 Task 5 清掉**。）

- [ ] **Step 1: README 改寫**（保留第一段誠實聲明的精神，整份重寫）：
  1. 一句話定位：給晶圓廠物料企劃的交期風險排序工具。
  2. 誠實聲明（合成資料、未串接真實 ERP、數字皆由程式跑出）。
  3. 四個模組：讀信、排序、ERP 與歷史、物料智能檢索（表格，每格一句話）。
  4. **② 排序那一節整段重寫**：刪掉十條權重表，改成
     `預估缺料天數 = 保守到料日 + 收貨處理天數 − 下游需求日`、P1/P2/P3 的條件、
     承諾強度如何決定百分位、分批交貨每批各自追蹤、建議動作遵守 AVL。
  5. **評估**：解析（含「誤判為已確認」筆數的意義）、檢索、**回測**（取代原本的「權重校準」整節）。
     照實寫本工具與基準打平、P95 涵蓋率偏低。
  6. 專案結構：刪 `impact.py`、`calibrate.py`，補 `triage.py`、`backtest.py`、`planner_settings.py`、
     `exports.py`、`ui_state.py`、`views/`。
  7. 已知限制：更新為現在真正的限制（歷史單不分批、收貨處理天數是領域假設、
     雲端設定不持久、沒有預測模型、逾期未收的延遲是低估值…）。
  8. 導入真實環境的下一步：沿用原本精神，改用現在的用語。
- [ ] **Step 2: CLAUDE.md 改寫**：模組表、必做事項表（拿掉 calibrate，加入「改 prompt／信件 → 重跑 evaluate.py 與種子快取」「改 docs 或知識卡 → 重跑 evaluate_rag.py」「改分級或歷史 → 重跑 backtest.py」）、目前狀態（以實際數字）、候選清單（刪掉已完成的三項，保留其餘並重新排序）、踩過的坑（新增本輪的：全域改字改壞提示詞例句、報告寫死結論句、歷史單數量沒依料別、確認交期沒重判變更類型）。
- [ ] **Step 3: 操作指引**：補「企劃確認交期」「匯出明日追料清單」「供應商月度績效」「兩區導覽」「分批交貨怎麼看」；分頁名稱與實際一致；常見問題補「確認之後會寫回 ERP 嗎？」「供應商又寄新信，我先前的確認還算數嗎？」。
- [ ] **Step 4: 部署指引**：檢查清單改成現在的分頁與數字；補「私人 repo 部署」「換分支要重新部署一個 App」「公開網址會用到金鑰，請設用量上限」。
- [ ] **Step 5: ERP 欄位對應**：分批交貨從「已知簡化」改為「已實作：每一行各自追蹤」；`goods_receipt` 與排程行的對應寫成待確認事項。
- [ ] **Step 6: Commit**：`docs: 全面改寫 README 與說明文件，對齊預估缺料天數與分批交貨`。

---

### Task 5: 重跑評估、種子快取與零網路驗證（**會用 Gemini 額度，先問使用者**）

**先停下來問使用者**：「要重跑解析評估、檢索評估與種子快取了，會用到 Gemini 免費額度（解析約 25 封信、知識卡向量約 355 張、檢索 24 題 × 6 種配置、範例問答 6 題）。可以開始嗎？」

- [ ] **Step 1**：`py -X utf8 src/evaluate.py`；分批交貨會改變 HC-010 的計分，如實記錄與前一版的差異。
- [ ] **Step 2**：`py -X utf8 src/rag/evaluate_rag.py`（知識卡因決策 18、19 與文件改寫而變）。
- [ ] **Step 3**：`py -X utf8 src/backtest.py`（歷史未分批，數字應與現在相近；若有變化要解釋）。
- [ ] **Step 4**：`py -X utf8 scripts/export_demo_cache.py`。
- [ ] **Step 5: 零網路驗證**：短路徑乾淨副本（`C:/Users/User/AppData/Local/Temp/claude/cs`），封鎖 `requests`，跑主流程＋語意索引＋範例問答，網路呼叫必須為 0。
- [ ] **Step 6**：把 Task 4 留下的 `TBD-待 Task 5` 全部換成實際數字（`grep -rn "TBD-待 Task 5" .` 必須沒有輸出）。
- [ ] **Step 7: Commit**：`chore: 重跑評估與種子快取，並補上文件中的實際數字`。

---

### Task 6: 決策 19、合併到 main、切換公開網址

- [ ] **Step 1: 決策 19**（`docs/設計決策.md`）：分批交貨的追蹤單位、對位的四條規則與保守退路、為什麼歷史不分批、確認紀錄改綁排程行；被否決的方案（只追最晚一批、把多批合併成一筆、要求企劃自己指定批次）。
- [ ] **Step 2: 最終檢查**：全套測試；離線 AppTest 逐頁無例外；`git status` 乾淨；`grep -rn "權重\|impact_score\|calibrate" --include=*.py --include=*.md .`（排除 `docs/superpowers/`、設計決策的歷史說明）沒有殘留。
- [ ] **Step 3: 合併到 main**：`git switch main && git merge --no-ff feat/planner-triage`，推上去。舊的 v1 網址（部署在 `main`）會自動變成新版。
- [ ] **Step 4: 告訴使用者要自己做的事**：
  1. 確認 v1 網址已變成新版，然後刪掉 v2 那個臨時 App。
  2. 決定 repo 要不要改成公開（面試官要看程式碼就要公開；公開前再確認一次沒有金鑰）。
  3. Google AI Studio 設金鑰用量上限。
- [ ] **Step 5: Commit & Push**。

---

## 自我檢查

| 需求 | 任務 |
|---|---|
| 分批交貨每批各自追蹤 | 1、2、3 |
| 提示詞誤改的例句修正 | 2 |
| README 不再講十條權重 | 4 |
| CLAUDE.md 反映現況與新的踩坑 | 4 |
| 操作指引補上新功能 | 4 |
| 重跑評估與種子快取、零網路 | 5 |
| 合併 main、切換網址 | 6 |
| 用 API 前先問使用者 | 5 |

**型別一致性**：`ExtractedRecord` 新增 `qty: int | None`、`batch_note: str`；`purchase_orders()` 每列含 `sched_line: int`、`sched_qty: int`；`match_schedule_line(record, lines) -> (sched_line, note, needs_review)`；`confirm_eta(db, po_no, sched_line, email_id, confirmed_date, note, user, *, now=None)`；`load_confirmations()` 的鍵為 `(po_no, sched_line)`。

**已知風險**：
- Task 1 完成到 Task 3 完成之間，主流程會有一段對不上排程行的中間狀態，測試會紅；每個 commit 的 body 要註明。
- 分批交貨改變了 `po_master.csv` 的列數定義（一列一排程行），任何直接讀該檔的地方都要檢查。
- HC-010 的標準答案改變會影響解析評估的數字，這是預期的，要如實記錄。
