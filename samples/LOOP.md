# LOOP.md — 給 agent 的迴圈控制檔

## 目標（done 條件）
- `po_api.feature` 全部情境通過
- ABAP Unit 全綠
- ATC 無 P1

## 每輪規則
1. 先跑驗證，看目前哪條情境失敗，再決定做什麼
2. 一輪只改一件事（一個方法、一個分支、一個測試）
3. 改完必跑：語法檢查 → ABAP Unit → ATC → feature

## 停止條件
- 全綠 → 停，產出摘要
- 連續 3 輪通過數沒增加 → 寫 BLOCKED.md，停
- 第 20 輪 → 停，不管結果

## 禁止
- 修改 feature file 或 openapi.yaml（要改請回閘門 2）
- 修改 SAP 標準物件
- 跳過寫入審批直接進 D01
- 為了讓測試過而硬編碼期望值

## 卡住時
寫 BLOCKED.md：卡住的情境、期望值、實際值、你的判斷（規格錯還是程式錯），然後停。

## ABAP Unit（每輪必做）
- 每個新增或修改的 method 都要有對應的 test method，命名 `given_<前提>_when_<動作>_then_<結果>`
- 測試寫在 class 的 local test class（CCAU include），不建獨立全域 class
- `RISK LEVEL HARMLESS DURATION SHORT`，其他 risk level 一律不接受
- DB 存取用 `cl_osql_test_environment` 隔離（EKKO / EKPO / EKET / Z-table），介面呼叫用 `cl_abap_testdouble`；測試不准真的寫表
- 一條 Gherkin 業務規則對一個 test method；HTTP 層由 API 情境驗，規則層由 ABAP Unit 驗
- Verify 順序：語法 → ABAP Unit → ATC → API 情境；ABAP Unit 不綠不進 ATC
