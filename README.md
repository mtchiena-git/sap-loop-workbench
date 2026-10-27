# SAP Loop Workbench

讓同仁真實體驗 AI Agent 迴圈：PRD → 系統規格 → 開發/測試迴圈 → 放行，人只在 4 個閘門出手。
FastAPI + SQLite + vanilla JS，無 build tools、無 CDN（Menlo 友善）。

## 快速開始
```powershell
git clone https://github.com/mtchiena-git/sap-loop-workbench.git
cd sap-loop-workbench
python -m venv .venv ; .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
.\run.ps1            # Linux/macOS: ./run.sh
```
開 http://127.0.0.1:8080 ，按「＋ 新專案」會自動載入範例 PRD 與詞彙表。
用 Claude Code 安裝：在專案目錄開 `claude`，說「照 CLAUDE.md 把這個平台裝起來並啟動」即可。
`prototype/index.html` 可直接用瀏覽器打開，不需後端。

預設 **mock 模式**：LLM 與 SAP executor 都是模擬，不碰 D01，迴圈會「看起來」逐輪收斂，
可以先拿來跑體驗工作坊。

## 四個閘門
| 閘門 | 誰 | 效果 |
|---|---|---|
| G1 PRD 定稿 | PM + 開發 + Key User | 通過後 agent 自動產生 OpenAPI / Gherkin / Tech Spec |
| G2 規格核准 | 開發審端點表 / 欄位表 / tech spec、PM 審 feature | 通過後規格 **凍結**＝迴圈的 done 條件 |
| G3 寫入 D01 審批 | 開發 | 每次寫入先 PENDING，核准才寫；拒絕時意見會餵回 agent |
| G4 TR 放行 | Team Lead | 全綠 + ATC + 情境結果 |

## 退回上一步
每個階段都可退回前一階段（spec→prd、dev→spec、release→dev、done→release）。
退回需填「誰」與原因，記錄在閘門紀錄為 `ROLLBACK`；內容一律保留（PRD、欄位表、Gherkin、迴圈紀錄），
退回到 spec/prd 會自動解凍規格。迴圈執行中不能退回，要先停止。
API：`POST /api/projects/{id}/rollback {who, note, to?}`。

## 規格的分工（schema-first）
- **PRD（PM）**：只寫業務資料需求——要輸入什麼、回什麼、業務意義、範例、規則。不寫 GET/POST、路徑、欄位英文名。
- **端點表（agent 產生，開發審）**：方法、路徑、參數位置（path/query/header）、請求 body 結構、回應結構、錯誤碼、授權物件。
- **欄位表（agent 產生，開發審）**：結構與欄位——英文名、型別、長度、必填、enum、巢狀。
- **OpenAPI**：由端點表（paths）＋欄位表（schemas）生成，唯讀；ABAP TYPES 與路由骨架同樣生成。
改欄位或端點＝改表、重生成；G2 之後要改須退回 G2。

## 從既有服務匯入（brownfield）
左欄「⇩ 從既有服務匯入」：填 SICF 節點或 handler class → 預覽 → 匯入。
專案直接從「系統規格」開始，閘門紀錄為 `G1 IMPORTED`，並帶：
- **端點表**：方法、路徑（路徑參數名從 `CONV ebeln( lt_seg[1] )` 推）、query 參數（`get_form_field`）、body / 回應結構、錯誤碼（`set_status` / `iv_code =`）、授權（`AUTHORITY-CHECK`）
- **欄位表**：class 內 `TYPES` 與用到的 DDIC 結構，經 RTTI 展開；DDIC 有欄位說明的自動帶入，沒有的標 `?` 待 PM 補
- **PRD 骨架**、**Tech Spec（反推）**：讀寫的表、BAPI、訊息、既有 ABAP Unit
- **Gherkin `@characterization`**：描述現況行為（含現有缺陷），PM 審時標出「現況錯、期望應為 X」

第一次迴圈用**基準模式**：只跑驗證不改程式，全綠即 baseline；之後的改動走正常流程。

### 抽取來源（`EXTRACT_MODE`）
| 值 | 來源 | 需要 |
|---|---|---|
| `mock`（預設） | `samples/extract_po.json` | 無，離線示範 |
| `abap` | `abap/zcl_loop_extract_http.abap` 掛在 SICF（或當 MCP tool `read_sicf_service`） | `SAP_EXTRACT_URL`、`SAP_USER`、`SAP_PASS` |
| `adt` | ADT REST `/sap/bc/adt/oo/classes/{c}/source/main` | `SAP_ADT_URL`、`SAP_USER`、`SAP_PASS`；只能給 class，TYPES 用文字解析、無 RTTI |

ABAP 端一次回傳：class 原始碼（含 CCAU 測試 include）、methods、RTTI 展開的 TYPES / DDIC、
AUTHORITY-CHECK、status code、form/header field、讀寫表、BAPI、message、unit test 清單。
Python 端 `extractor.py` 的 `parse_dump()` 把它轉成端點表 / 欄位表草稿；`EXTRACT_MODE=adt` 時由
`scan_source()` 在 Python 端做同樣的文字掃描。反推最常錯的是 `/ui2/cl_json` 欄位命名（`json_style`
選 snake / camel）與 optional 欄位，G2 時請特別對。

API：`GET /api/extract/preview?node=…|clazz=…`、`POST /api/projects/import {node|clazz, json_style, name?}`

## ABAP Unit 在迴圈裡怎麼來
agent 與 handler 同批產生 local test class（CCAU），G3 一起審；規則在 `samples/LOOP.md`，
tool 契約在 `executor.py` 的 `ABAP_UNIT_CONTRACT`，完整說明見 [docs/abap-unit-in-loop.md](docs/abap-unit-in-loop.md)。

## 迴圈（LOOP.md）
每輪：Plan → Act → 寫入（經 G3）→ Verify（語法 / ABAP Unit / ATC / 情境）→ Revise。
停止：全綠；連續 N 輪無進展（寫 BLOCKED）；回合上限。

## 工作坊的三個「啊哈」
1. **規格品質決定迴圈品質**：在 G2 刻意把 Gherkin 寫鬆，看 agent 怎麼猜。
2. **迴圈會停**：啟動時選一條「毒情境」（永遠不會過），看它連續無進展後寫 BLOCKED 停下來。
3. **人在控制**：審批模式選「每輪」，拒絕一次並寫意見，看 agent 下一輪的 Revise。

## 接真實環境
| 環境變數 | 值 | 說明 |
|---|---|---|
| `LLM_MODE` | `gateway` | 用 OpenAI-compatible 的內部 LLM gateway |
| `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` | | gateway 連線 |
| `SAP_MODE` | `abap` | 走 ABAP HTTP / MCP BFF |
| `SAP_BFF_URL` / `SAP_BFF_TOKEN` | | BFF 連線 |

`executor.py` 的 `AbapHttpExecutor` 預期 BFF 提供四個 tool endpoint：
`/tools/syntax_check`、`/tools/abap_unit`、`/tools/atc`、`/tools/run_feature`，
以及 `/tools/write_object`（只在 G3 核准後呼叫）。把它們對到你的 ZCL_MCP_* / ADT REST 即可。

## 檔案
```
app.py          FastAPI：專案、閘門、規格、迴圈執行緒、審批、SSE
llm.py          MockLLM / GatewayLLM
executor.py     MockSapExecutor / AbapHttpExecutor（Verify 永遠在迴圈外）
extractor.py    既有服務抽取：Mock / AbapHttp / Adt 來源 + 反推端點表、欄位表
abap/           ZCL_LOOP_EXTRACT_HTTP — SAP 端抽取 handler（SICF / MCP tool）
static/index.html  單頁 UI
samples/        範例 PRD、詞彙表、LOOP.md
loop.db         SQLite（自動建立）
```
