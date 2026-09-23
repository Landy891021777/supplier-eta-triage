# CLAUDE.md — 專案交接說明

給接手這個專案的 Claude Code session。先讀完這份，再動任何程式。

## 專案是什麼

**Supply Chain AI Tool: Delivery Risk Prioritization（供應鏈 AI 工具：交期風險優先排序）**

使用者是**晶圓廠的物料企劃（Material Planner）**。工具讀懂供應商的交期回覆信 →
對回採購單的交貨排程行 → 算出預估缺料天數 → 排出今天該追的單，附理由與建議動作，
並讓企劃看到這家供應商過去說話算不算數。以 Streamlit 交付的內部工具。

四個模組：

| 模組 | 主要檔案 | 核心設計 |
|---|---|---|
| ① 讀信 | `src/extract_rules.py`、`src/extract_llm.py`、`src/llm/` | 規則層優先，信心 < 0.75 才升級 LLM；抽取「承諾強度」（confirmed／estimated／intent_only）；一封信可抽出同一張單的多批交期 |
| ② 排序 | `src/triage.py`、`src/pipeline.py`、`src/draft.py`、`src/backtest.py` | `預估缺料天數 = 保守到料日 + 收貨處理天數 − 需求日`；P1/P2/P3 只看事實旗標，**沒有任何權重**；時間切分回測驗證方法 |
| ③ ERP 與歷史 | `src/build_erp_db.py`、`src/adapters/`、`src/generate_history.py`、`src/supplier_stats.py`、`src/planner_settings.py`、`src/exports.py` | SQLite 10 張表（結構參考 SAP MM）；保守到料日＝改期過的單的落差 P80；企劃的設定與確認存工具自己的 DB，**不寫回 ERP** |
| ④ 物料智能檢索 | `src/rag/` | 依資料形狀切塊＋摘要卡；語意檢索＋識別碼釘選；驗證引用出處 |

介面：`app.py`（導覽外殼）＋ `ui_state.py`（共用狀態）＋ `views/*.py`（一頁一檔），
分「企劃工作區」（今日行動清單、物料智能檢索、供應商績效、收貨處理天數、ERP 單據、
信件與解析軌跡）與「專案說明」（專案簡介與導覽、方法驗證、效益估算）兩區。
設計取捨的完整紀錄：`docs/設計決策.md`（決策 1–18，決策 19 待寫）。

## 不可違反的原則

1. **全部是合成資料**，沒有任何真實公司資料，未串接真實 ERP。README 第一段是誠實聲明，不可移除或弱化。
2. **不編造數字**。文件裡的每個數字都必須由程式實際跑出來；改了會影響結果的東西就要重跑並同步。
3. **不宣稱沒做到的事**。沒有交期預測模型；保守到料日是歷史統計；ERP 結構是「參考 SAP」而非真實結構。
4. **評估必須容許推翻假設**。回測顯示本工具與簡單基準打平、P95 涵蓋率偏低，就照實寫；報告的結論句要依實際數字產生，不可寫死。
5. **工具永不自動寫回 ERP、永不自動寄信**。企劃的調整與確認都存在 `data/planner_settings.db`。
6. **避免寫「由資料產生器設定而來」的比例**當成果（例如「幾成信件不需 LLM」——信件風格比例是自己設定的）。

## 環境與執行

- Windows 11、Python 3.13（bash 裡用 `py`，`python` 不在 PATH）
- **一律用 `py -X utf8`**，檔案讀寫一律 `encoding="utf-8"`（系統預設是 cp950）
- **離線開發一律加 `LLM_PROVIDER=none`**；看到 gemini／embedding／429 就停下來看原因
- LLM：`.env` 內 `LLM_PROVIDER=gemini`、`GEMINI_MODEL=gemini-3.5-flash-lite`、`GEMINI_EMBED_MODEL=gemini-embedding-001`（免費層，額度有限）

資料不進版控，新環境先依序產生：

```bash
py -X utf8 src/generate_data.py      # 合成資料、52 封信（含 10 封手寫刁鑽案例）
py -X utf8 src/build_erp_db.py       # data/erp_sim.db
py -X utf8 src/generate_history.py   # 1494 張歷史單與收貨紀錄（冪等）
LLM_PROVIDER=none py -X utf8 -m pytest tests -q    # 266 項，全部離線
py -X utf8 -m streamlit run app.py --server.port 8511
```

評估（需金鑰，結果寫到 `output/`，已納入版控作為證據快照）：

```bash
py -X utf8 src/evaluate.py           # 解析：規則層 vs LLM 層
py -X utf8 src/rag/evaluate_rag.py   # 檢索：六種配置
LLM_PROVIDER=none py -X utf8 src/backtest.py   # 回測（不需金鑰）
```

## 改完東西之後必做的事

| 改了什麼 | 必須接著做 |
|---|---|
| prompt、`generate_data.py`、`handcrafted_emails.py` | 重跑 `evaluate.py`，同步 README「解析」 |
| `docs/` 內容、`rag/knowledge.py`、`eval_questions.py`、`EXAMPLE_QUESTIONS` | 重跑 `evaluate_rag.py`，同步 README「檢索」 |
| 上述任一項 | 重跑 `py -X utf8 scripts/export_demo_cache.py` 更新 `demo_cache/`（否則雲端冷啟動會呼叫 API） |
| `triage.py` 的分級、`generate_history.py`、`config.yaml` 的 `triage`／`receiving` | 重跑 `backtest.py`，同步 README「回測」 |
| 任何功能 | 在 `docs/設計決策.md` 新增一條決策（含被否決的替代方案） |

驗證種子快取有效的方式：複製一份不含 `.cache/`、`data/` 的乾淨副本，把 `requests.post/get` 換成會拋錯的函式，跑主流程＋語意索引＋範例問答，網路呼叫必須為 0。**複製目錄要用短路徑**（例如 `C:/Users/User/AppData/Local/Temp/claude/cs`），scratchpad 路徑本身就超過 250 字元，會撞 Windows MAX_PATH。

## 程式慣例

- **註解寫「為什麼」，不寫「做什麼」**，繁體中文。領域假設一律標 `# 領域假設：`。
- 所有可調參數放 `config.yaml`，不寫死在程式裡。
- prompt 獨立成檔放 `src/llm/prompts/`，可版控、可 diff。
- LLM 呼叫一律經過 `src/llm/provider.py`（快取 → 節流 → 呼叫 → 退避重試）。解析用 `json_mode=True`，自由文字用 `json_mode=False`。
- 沒有金鑰時一律**優雅降級**且明確標示，不可偷偷回傳假結果（`NullProvider`）。
- 資料來源經過 `src/adapters/`，分級與介面不直接讀檔。
- 測試聚焦「錯了會出事」的行為；回歸測試的 docstring 要寫出當初的 bug。
- Commit：中文 conventional commits（`feat(rag):`、`fix(llm):`…），body 寫原因與踩坑，結尾 `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`。

## 已經踩過的坑（別再踩）

| 坑 | 教訓 |
|---|---|
| LLM 欄位大面積掛零 | 先看原始錯誤。那次是 **HTTP 429 配額**，不是模型不會 |
| Gemini 模型下線回 404 | 模型名稱外部化在 `.env` |
| embedding 一批 100 筆被 429 | 依**字數**切批（每批 ≤ 6000 字），不是依筆數 |
| 評估題目沒有答案 | 標準答案必須能從信件文字還原 |
| `str(NaN) == "nan"` 被當成有替代料 | 空值一律經 `_clean_str()`／`_missing()`；pandas 3 會把 None 變 NaN，`or` 退路會失效 |
| `itertuples` 把含括號的中文欄名改成 `_3` | 先 rename 成英文欄名再迭代 |
| 產生器重跑灌水 | 產生器必須冪等（先清掉自己的舊產出） |
| 雲端首次載入整個打不開 | 兩個工作階段搶著建資料：要上鎖，且「備妥」要看**內容**不是檔案存在（`src/bootstrap.py`） |
| 全域把「生管」換成「物料企劃」 | 改壞了 prompt 裡引用**供應商那邊生管**的例句。全域改字前先看每一處的語意 |
| 報告寫死結論句 | 「涵蓋率沒有偏離預期」在新資料上變成不實陳述。結論要依數字產生（`backtest.coverage_verdict`） |
| 歷史單數量沒依料別 | 出現「光罩一次買 8000 片」；數量、前置期、下單日都要依料別 |
| 為了讓測試通過而調資料量 | 歷史單數要有**理由**（每家供應商的改期單 ≥ 20 張），不是湊到門檻就好 |
| 企劃確認交期沒重判變更類型 | 供應商說「照原計畫」的單，確認新日期後沒重算，缺料被低估 |
| 把 HC-010 的拆批預先寫進 ERP | 信件宣布的拆批就變成「沒有變更」，情境消失。ERP 應保留原始單一排程行 |
| bash heredoc 寫大量中英混合程式碼失敗 | 大檔案改用 Write／Edit 工具 |
| Streamlit 重啟後使用者看到舊畫面 | WebSocket 斷線，請使用者 Ctrl+F5 |

## 目前狀態（2026-09-23）

- 分支 `feat/planner-triage`（已推上 GitHub），`main` 還是最舊版本
- **266 項測試通過**，工作區乾淨
- 首頁（關 LLM 的規則層模式）：52 封信 → 自動濾除 13 → 行動清單 43 → P1 15、P2 13、P3 3、需人工確認 18
- 資料：14 家供應商、40 個料號、260 張在途採購單（297 筆交貨排程行，37 張是 ERP 已拆批）、1494 張歷史單
- 回測（`output/回測結果.md`）：本工具與「只看供應商說的日期」打平；P95 涵蓋率 90%，低於預期
- GitHub：`https://github.com/Landy891021777/supplier-eta-triage`（私人）
- 雲端：v2 `https://supplier-eta-triage-v2.streamlit.app/`（部署 `feat/planner-triage`）；v1 部署 `main`，還是舊版

## 進行中：Plan 3（`docs/superpowers/plans/2026-09-23-partial-delivery-and-wrapup.md`）

| 任務 | 狀態 |
|---|---|
| 1 交貨排程行每行一列 | ✅ |
| 2 一封信可抽多批交期、修正 prompt 誤改 | ✅ |
| 3 對位／分級／畫面／匯出改用排程行 | ✅ |
| **4 文件全面改寫（README、CLAUDE.md、操作指引、部署指引、ERP 欄位對應）** | **下一步**（本檔已先更新） |
| 5 重跑評估與種子快取（**會用 Gemini 額度，執行前先問使用者**） | 待辦 |
| 6 決策 19、合併到 `main`、切換公開網址 | 待辦 |

**Task 4 最重要的一件事**：`README.md` 仍在講「十條加權規則」與「權重校準」，那是面試官第一眼看到的東西，必須整段重寫成預估缺料天數、P1/P2/P3 條件、分批交貨、回測結果。數字一律引用實際輸出；`output/*.md` 的數字要等 Task 5 重跑後才是最終版（計畫允許先寫 `TBD-待 Task 5`，Task 5 必須清掉）。

## 新功能候選清單（Plan 3 之後）

動手前先跟使用者確認要做哪一個。

1. **承諾強度的自我校準**：記錄「判為 intent_only 的單後來準不準」，這是 ERP 結構上不會有的資料
2. **供應商溝通品質評鑑**：累積通知時機（多晚才講）與改期模式，產出供應商評分
3. **RAG 多跳查詢**：例如「最差的供應商有哪些料號風險」需要先查排名、再查該供應商的料
4. **信件來源串接**：由讀本機檔案改為 IMAP／Microsoft Graph
5. **快取期限（TTL）**與 **token 用量儀表板**
6. **收貨紀錄對到排程行**：目前 `goods_receipt` 仍以 `(po_no, item_no)` 為鍵，分批交貨的兩行共用一筆收貨判斷
7. **歷史單也支援分批**（目前歷史刻意維持單一排程行）
