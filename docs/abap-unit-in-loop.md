# 迴圈內即時產生 ABAP Unit

2026-10-27

## 結論
是的：SAP Loop Workbench 的設計是由 agent 在迴圈裡**即時產生 ABAP Unit**，與 handler 同批寫入、同一次審批，公司不需要先有 ABAP Unit practice。ABAP Unit 是 kernel 內建功能，S/4HANA 2021 上沒有任何 switch 要開；要準備的只有跑測試的 MCP tool 與幾條給 agent 的規則。對目前沒有 Unit Test 文化的團隊，這個 pilot 正好是把它帶進來的低成本路徑：人不寫測試，人審測試。

## 迴圈裡誰寫、何時寫
測試類別由 agent 寫，與 handler 同步產生，人只在 G3 審。

1. **第 1 輪一起建**：agent 建物件時送審的是 `ZCL_ZAPI_PO_HTTP + ZPO_NOTE + ZCL_ZAPI_PO_HTTP_TEST`，handler 與測試類別同一批產生、同一次 G3 審批。
2. **每輪同步改**：「一輪改一件事」時，對應的 test method 一起改。Verify 階段的順序是語法 → **ABAP Unit** → ATC → API 情境。
3. **放在 local test class**：寫在 class 的 CCAU include，不是獨立的全域 class，跟著 handler 一起傳輸、不會散落。

LOOP.md 的規則（已補進 `samples/LOOP.md`）：每個新增或修改的 method 都要有對應的 test method，命名 `given_xxx_when_yyy_then_zzz`，RISK LEVEL HARMLESS、DURATION SHORT，DB 用 `cl_osql_test_environment` 隔離，不准真的寫表。

## 系統端準備事項
沒有要「啟用」的東西，只有五項要準備或確認。

| 項目 | 說明 | 負責 |
| --- | --- | --- |
| 跑測試的 MCP tool | `/tools/abap_unit` 對到 ADT 的 `POST /sap/bc/adt/abapunit/testruns`（XML 指定 class），或 ABAP 端呼叫 `CL_AUNIT_API`；回傳 passed / total / 失敗訊息（契約見 `executor.py`） | 平台 |
| Test double 可用 | `cl_osql_test_environment`（7.52+，2021 有）隔離 EKKO / EKPO / ZPO_NOTE；`cl_abap_testdouble` 隔離介面呼叫 | Basis 確認 |
| Risk level 限制 | 只允許 HARMLESS；agent 產生 DANGEROUS / CRITICAL 一律在 G3 拒絕 | G3 審批者 |
| ATC 設定 | ATC 變體若勾「ABAP Unit 執行」會重跑一次；pilot 階段先關掉，由迴圈控制（若變體由 Basis 統一管不好動，改成並行跑、以迴圈結果為準） | Basis / QA |
| 傳輸 | CCAU include 跟 class 同一個 TR，不需額外處理 | — |

## 對沒有 Unit Test 文化的團隊，這樣切比較順
切入點是「審」不是「寫」，同仁從第一天就看得到 Gherkin 與 ABAP Unit 的對應。

1. **人不寫測試，人審測試**：同仁在 G3 看到的是 agent 寫的 test class，審「這個 given / when / then 有沒有對到 Gherkin 情境」，比從零學寫 test double 容易得多。
2. **Gherkin → ABAP Unit 的對應明確化**：一條 Scenario 的業務規則（例如 status 計算）對一個 test method；API 情境驗 HTTP 層，ABAP Unit 驗規則層。
3. **匯入既有服務時從 0 開始**：extractor 會報「既有 ABAP Unit：無」，基準迴圈第一件事就是幫既有 handler 補 characterization 的 unit test，這一步也是人審、不改程式。
4. **指標上看得到**：儀表板本來就有「ABAP Unit x/y」那格，retro 時可以看從 0 到多少。

## 後續工作項
- [x] LOOP.md 補 unit test 規則 → `samples/LOOP.md`
- [x] mock LLM 第 1 輪 Action 片段加 local test class 範例（含 `cl_osql_test_environment`）→ `llm.py`
- [x] `executor.py` 的 `/tools/abap_unit` 契約寫清楚 ADT XML 格式與回傳欄位
