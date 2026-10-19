# PRD：採購單狀態查詢 API（Pilot）

## 1. 背景
外部物流系統需要用採購單號查詢交貨狀態，目前靠人工在 ME23N 查後回報。

## 2. 目標
提供 RESTful API：GET /zapi/po/{ebeln} 回傳抬頭、項目、交貨狀態；POST /zapi/po/{ebeln}/note 寫入備註。

## 3. 使用者
物流系統（技術使用者）、採購 PM（查詢備註）。

## 4. 範圍
- 做：讀 EKKO/EKPO，計算狀態 OPEN / PARTIAL / DELIVERED / CLOSED；備註寫入 Z-table
- 不做：OData、Fiori、修改標準程式、效能調校

## 5. 業務規則
- 所有項目完成交貨（ELIKZ）→ DELIVERED；部分收貨 → PARTIAL；未收貨 → OPEN；刪除旗標 → CLOSED
- 查詢需要 M_BEST_EKO 顯示權限；寫備註需要變更權限
- 備註 1–255 字，空白拒絕

## 6. 驗收條件（用範例寫，不用形容詞）
- 給一張項目 10 已收、項目 20 未收的單，查詢回 status=PARTIAL、items 有 2 筆
- 查一張不存在的單，回 404 與 error=PO_NOT_FOUND
- 單號非 10 碼數字，回 400
- 沒權限的使用者查，回 403
- 寫一筆備註「請加速交貨」，回 201，ZPO_NOTE 多一筆
- 寫空白備註，回 400，ZPO_NOTE 不增加

## 7. 風險與依賴
- 測試資料：需要固定的測試採購單 4500000001 / 4500000002
- 授權：測試使用者需要兩組角色（有權 / 無權）
