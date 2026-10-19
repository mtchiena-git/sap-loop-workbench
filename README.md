# SAP Loop Workbench

讓同仁真實體驗 AI Agent 迴圈：PRD → 系統規格 → 開發/測試迴圈 → 放行，人只在 4 個閘門出手。
FastAPI + SQLite + vanilla JS，無 build tools、無 CDN（Menlo 友善）。

## 啟動
```
pip install fastapi uvicorn
uvicorn app:app --host 0.0.0.0 --port 8080
```
開 http://localhost:8080 ，按「＋ 新專案」會自動載入範例 PRD 與詞彙表。

預設 **mock 模式**：LLM 與 SAP executor 都是模擬，不碰 D01，迴圈會「看起來」逐輪收斂，
可以先拿來跑體驗工作坊。

## 四個閘門
| 閘門 | 誰 | 效果 |
|---|---|---|
| G1 PRD 定稿 | PM + 開發 + Key User | 通過後 agent 自動產生 OpenAPI / Gherkin / Tech Spec |
| G2 規格核准 | 開發審 tech spec、PM 審 feature | 通過後 feature **凍結**＝迴圈的 done 條件 |
| G3 寫入 D01 審批 | 開發 | 每次寫入先 PENDING，核准才寫；拒絕時意見會餵回 agent |
| G4 TR 放行 | Team Lead | 全綠 + ATC + 情境結果 |

## 退回上一步
每個階段都可退回前一階段（spec→prd、dev→spec、release→dev、done→release）。
退回需填「誰」與原因，記錄在閘門紀錄為 `ROLLBACK`；內容一律保留（PRD、欄位表、Gherkin、迴圈紀錄），
退回到 spec/prd 會自動解凍規格。迴圈執行中不能退回，要先停止。
API：`POST /api/projects/{id}/rollback {who, note, to?}`。

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
static/index.html  單頁 UI
samples/        範例 PRD、詞彙表、LOOP.md
loop.db         SQLite（自動建立）
```
