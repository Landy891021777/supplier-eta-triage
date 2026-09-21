# CLAUDE.md — 專案交接說明

給接手這個專案的 Claude Code session。先讀完這份，再動任何程式。

## 專案是什麼

**Supply Chain AI Tool: Delivery Risk Prioritization（供應鏈 AI 工具：交期風險優先排序）**

讀懂供應商的交期回覆信 → 對回採購單 → 依領域規則排出生管今天該追的單，
並讓生管看到這家供應商過去說話算不算數。以 Streamlit 交付的內部工具。

四個模組：

| 模組 | 主要檔案 | 核心設計 |
|---|---|---|
| ① 讀信 | `src/extract_rules.py`、`src/extract_llm.py`、`src/llm/` | 規則層優先，信心 < 0.75 才升級 LLM；抽取「承諾強度」（confirmed／estimated／intent_only） |
| ② 排序 | `src/impact.py`、`src/pipeline.py`、`src/draft.py` | 十條加權規則；非 confirmed 的交期不寫回系統（人工確認閘門） |
| ③ ERP 與歷史 | `src/build_erp_db.py`、`src/adapters/`、`src/generate_history.py`、`src/supplier_stats.py`、`src/calibrate.py` | SQLite 10 張表（結構參考 SAP MM）；供應商準交率、保守到料日（P80）；邏輯迴歸校準權重 |
| ④ 物料智能檢索 | `src/rag/` | 依資料形狀切塊＋摘要卡；語意檢索＋識別碼釘選；驗證引用出處 |

介面：`app.py`（8 個具名分頁）。設計取捨的完整紀錄：`docs/設計決策.md`（決策 1–15）。

## 不可違反的原則

1. **全部是合成資料**，沒有任何真實公司資料，未串接真實 ERP。README 第一段是誠實聲明，不可移除或弱化。
2. **不編造數字**。README 與文件裡的每個數字都必須由程式實際跑出來；改了會影響結果的東西就要重跑並同步。
3. **不宣稱沒做到的事**。例如：沒有交期預測模型、邏輯迴歸只用於校準分析、ERP 結構是「參考 SAP」而非真實結構。
4. **評估必須容許推翻假設**。RAG 評估就推翻了原本的混合檢索設計（見決策 12），這種結果要如實記錄，不可為了好看去調參數。
5. **工具永不自動寫回 ERP、永不自動寄信**。
6. **避免寫「由資料產生器設定而來」的比例**當成果（例如「幾成信件不需 LLM」——信件風格比例是自己設定的）。

## 環境與執行

- Windows 11、Python 3.13（bash 裡用 `py`，`python` 不在 PATH）
- **一律用 `py -X utf8`**，檔案讀寫一律 `encoding="utf-8"`（系統預設是 cp950，會亂碼或報錯）
- LLM：`.env` 內 `LLM_PROVIDER=gemini`、`GEMINI_MODEL=gemini-3.5-flash-lite`、`GEMINI_EMBED_MODEL=gemini-embedding-001`（免費層）

資料不進版控，新環境先依序產生：

```bash
py -X utf8 src/generate_data.py      # 合成資料、52 封信（含 10 封手寫刁鑽案例）
py -X utf8 src/build_erp_db.py       # data/erp_sim.db
py -X utf8 src/generate_history.py   # 898 張歷史單與收貨紀錄（冪等）
py -X utf8 -m pytest tests -q        # 81 項，全部離線
py -X utf8 -m streamlit run app.py
```

評估（需金鑰，結果寫到 `output/`，已納入版控作為證據快照）：

```bash
py -X utf8 src/evaluate.py           # 解析：規則層 vs LLM 層
py -X utf8 src/rag/evaluate_rag.py   # 檢索：六種配置
py -X utf8 src/calibrate.py          # 權重校準
```

## 改完東西之後必做的事

| 改了什麼 | 必須接著做 |
|---|---|
| prompt、`generate_data.py`、`handcrafted_emails.py` | 重跑 `evaluate.py`，同步 README「解析」表格 |
| `docs/` 內容、`rag/knowledge.py`、`EXAMPLE_QUESTIONS` | 重跑 `evaluate_rag.py`，同步 README「檢索」表格 |
| 上述任一項 | 重跑 `py -X utf8 scripts/export_demo_cache.py` 更新 `demo_cache/`（否則雲端冷啟動會重新呼叫 API） |
| `impact.py` 權重、`generate_history.py` | 重跑 `calibrate.py`，同步 README「權重校準」 |
| 任何功能 | 在 `docs/設計決策.md` 新增一條決策（含被否決的替代方案） |

驗證種子快取有效的方式：複製一份不含 `.cache/`、`data/` 的乾淨副本，把 `requests.post/get` 換成會拋錯的函式，跑主流程＋語意索引＋範例問答，網路呼叫必須為 0。**複製目錄要用短路徑**（例如 `C:/Users/User/AppData/Local/Temp/claude/cs`），scratchpad 路徑本身就超過 250 字元，會撞 Windows MAX_PATH。

## 程式慣例

- **註解寫「為什麼」，不寫「做什麼」**，繁體中文。領域假設一律標 `# 領域假設：`。
- 所有可調參數放 `config.yaml`，不寫死在程式裡。
- prompt 獨立成檔放 `src/llm/prompts/`，可版控、可 diff。
- LLM 呼叫一律經過 `src/llm/provider.py`（快取 → 節流 → 呼叫 → 退避重試）。解析用 `json_mode=True`，自由文字（回信草稿、RAG 回答）用 `json_mode=False`。
- 沒有金鑰時一律**優雅降級**且明確標示，不可偷偷回傳假結果（`NullProvider`）。
- 資料來源經過 `src/adapters/`，規則與介面不直接讀檔。
- 測試聚焦「錯了會出事」的行為；回歸測試的 docstring 要寫出當初的 bug。
- Commit：中文 conventional commits（`feat(rag):`、`fix(llm):`…），body 寫原因與踩坑，結尾 `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`。

## 已經踩過的坑（別再踩）

| 坑 | 教訓 |
|---|---|
| LLM 欄位大面積掛零 | 先看原始錯誤。那次是 **HTTP 429 配額**，不是模型不會 |
| Gemini 模型下線回 404 | 模型名稱外部化在 `.env`；`gemini-2.0-flash`、`2.5-flash` 已不可用，`3.6-flash` 每日僅 20 次 |
| embedding 一批 100 筆被 429 | 依**字數**切批（每批 ≤ 6000 字），不是依筆數 |
| 評估題目沒有答案 | 標準答案必須能從信件文字還原；相對日期要帶入原承諾日 |
| `str(NaN) == "nan"` 被當成有替代料 | 空值一律經 `_clean_str()` |
| `itertuples` 把含括號的中文欄名改成 `_3` | 先 rename 成英文欄名再迭代 |
| `generate_history.py` 重跑灌水改期次數 | 產生器必須冪等（先清掉自己的舊產出） |
| 雲端上三個分頁整片空白 | `_ensure_data()` 要連歷史單據一起補 |
| bash heredoc 寫大量中英混合程式碼失敗 | 大檔案改用 Write／Edit 工具 |
| Streamlit 重啟後使用者看到舊畫面 | WebSocket 斷線，請使用者 Ctrl+F5 |

## 目前狀態（2026-09-21）

- 29 個 commit，工作區乾淨，81 項測試通過，**尚未推上 GitHub、尚未部署**
- 首頁：52 封信 → 自動濾除 8 → 行動清單 47 → P1 10、P2 23、需人工確認 15；規則層處理 27 封、升級 LLM 25 封全數成功
- 知識庫 351 張卡；檢索「語意＋識別碼釘選」Hit@3 0.958（24 題）
- 權重校準：394 筆，AUC 評分卡 0.707 vs 學習模型 0.788
- 部署步驟與檢查清單：`docs/部署指引.md`

## 新功能候選清單

依對使用者價值與實作成本大致排序。動手前先跟使用者確認要做哪一個。

1. **推上 GitHub 並部署 Streamlit Cloud**（需使用者登入帳號）
2. **權重調整持久化**：目前同仁調的權重重開就消失；應存檔並記錄「誰調了什麼、為什麼」
3. **機率 × 代價分離評分**：校準發現「下游已排定」等三條衡量的是代價而非機率，應拆成兩個分數相乘（見決策 12 前的校準章節與 README）
4. **分批交貨**：一個採購項次多筆交貨排程行時分開追蹤（目前只取最晚一筆）
5. **承諾強度的自我校準**：記錄「判為 intent_only 的單後來準不準」，這是 ERP 結構上不會有的資料
6. **供應商溝通品質評鑑**：累積通知時機（多晚才講）與改期模式，產出供應商評分
7. **RAG 多跳查詢**：例如「最差的供應商有哪些料號風險」需要先查排名、再查該供應商的料
8. **信件來源串接**：由讀本機檔案改為 IMAP／Microsoft Graph
9. **快取期限（TTL）**與 **token 用量儀表板**
10. **封測段、載板段專屬規則**（目前只有晶圓段）
