# SAP Loop Workbench — 給 Claude Code 的專案說明

這是一個 FastAPI + SQLite + vanilla JS 的單機工作平台，用來示範 PRD → 系統規格 → AI agent 開發/測試迴圈 → 放行，
人只在 4 個閘門出手。預設 mock 模式，不碰任何 SAP 系統。

## 安裝與啟動（Windows，公司環境）
```powershell
# 1. Python 3.10+（python --version 確認）
# 2. 在專案根目錄
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt          # 若公司 proxy 擋 PyPI：pip install --proxy http://<proxy>:<port> -r requirements.txt
copy .env.example .env                    # 保持 mock 模式即可先跑
.\run.ps1                                 # 或 uvicorn app:app --host 127.0.0.1 --port 8080
# 開 http://127.0.0.1:8080
```
Linux / macOS：`python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt && cp .env.example .env && ./run.sh`

沒有 build step，沒有 CDN，沒有 Node；`static/index.html` 直接由 FastAPI 提供，Menlo 環境可用。
`prototype/index.html` 是獨立的單檔 prototype（含欄位表 / 端點表編輯器、匯入反推），瀏覽器直接打開即可，不需後端。

## 目錄
```
app.py            FastAPI：專案、閘門、規格、迴圈執行緒、審批、SSE、匯入、退回
llm.py            MockLLM / GatewayLLM（OpenAI-compatible chat/completions）
executor.py       MockSapExecutor / AbapHttpExecutor；含 /tools/abap_unit 契約（ADT XML）
extractor.py      既有服務抽取：Mock / AbapHttp / Adt 來源 → 端點表、欄位表、PRD 骨架
abap/             ZCL_LOOP_EXTRACT_HTTP（SICF handler / MCP tool）
samples/          範例 PRD、詞彙表、LOOP.md（含 ABAP Unit 規則）、extract_po.json
static/           伺服器版單頁 UI
prototype/        獨立單檔 prototype
docs/             設計說明（abap-unit-in-loop.md 等）
loop.db           SQLite，第一次啟動自動建立；刪掉即重置
```

## 模式切換（.env）
| 變數 | 值 | 說明 |
|---|---|---|
| LLM_MODE | mock / gateway | gateway 需 LLM_BASE_URL、LLM_API_KEY、LLM_MODEL |
| SAP_MODE | mock / abap | abap 需 SAP_BFF_URL、SAP_BFF_TOKEN；BFF 要提供 /tools/syntax_check、abap_unit、atc、run_feature、write_object |
| EXTRACT_MODE | mock / abap / adt | abap 需 SAP_EXTRACT_URL + SAP_USER/PASS；adt 需 SAP_ADT_URL + SAP_USER/PASS |

`.env` 由 `run.ps1` / `run.sh` 載入；直接跑 uvicorn 時請自行 set 環境變數。

## 給 Claude Code 的工作守則
- 修改後跑 `python -c "import app"` 確認可 import，再用 `python -m pytest -q`（若有 tests/）或手動 curl `/api/config`。
- 前端改動後用 `node --check` 檢查 `<script>` 區塊（抽出來檢查即可），不要引入 build tools 或外部 CDN。
- 不要把 `loop.db`、`.env`、`.venv/` 提交進 git。
- 迴圈規則的單一來源是 `samples/LOOP.md`；改規則先改它。
- 規格的單一來源是端點表 + 欄位表；OpenAPI / ABAP TYPES 是生成物，不要手改。
- commit 訊息用英文、說明「為什麼」。

## 接真實 D01 的順序
1. 部署 `abap/zcl_loop_extract_http.abap`，掛 SICF，`EXTRACT_MODE=abap` 試 `/api/extract/preview?node=...`
2. BFF 補 `/tools/abap_unit`（契約在 `executor.py` 的 `ABAP_UNIT_CONTRACT`）與 `/tools/run_feature`
3. `LLM_MODE=gateway` 接內部 LLM gateway
4. `SAP_MODE=abap`，先用「基準模式」對一個既有 GET 服務跑一輪
