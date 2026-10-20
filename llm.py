"""
LLM provider abstraction.

- MockLLM      : deterministic, offline. Produces plausible spec / code / fixes
                 so the whole loop can be demoed without a gateway.
- GatewayLLM   : OpenAI-compatible chat/completions endpoint (your internal
                 LLM gateway). Configure via env:
                   LLM_BASE_URL   e.g. https://llm-gw.internal/v1
                   LLM_API_KEY
                   LLM_MODEL      e.g. gpt-4o / claude-sonnet / ...
                 Uses urllib only (no extra deps); honours HTTPS_PROXY.
"""
import json
import os
import re
import urllib.request


class MockLLM:
    name = "mock"

    def complete(self, system: str, user: str, purpose: str = "") -> str:
        if purpose == "spec":
            return self._spec(user)
        if purpose == "plan":
            return self._plan(user)
        if purpose == "code":
            return self._code(user)
        if purpose == "revise":
            return self._revise(user)
        if purpose == "characterize":
            return self._characterize(user)
        return "（mock LLM 回應）"

    # ---- spec: PRD -> openapi / feature / tech_spec ----
    def _spec(self, prd: str) -> str:
        resource = "po"
        m = re.search(r"/zapi/(\w+)", prd)
        if m:
            resource = m.group(1)
        openapi = f"""openapi: 3.0.3
info:
  title: Z {resource.upper()} Status API
  version: 0.1.0
servers:
  - url: /sap/bc/zapi
paths:
  /{resource}/{{ebeln}}:
    get:
      summary: 查詢採購單狀態
      parameters:
        - name: ebeln
          in: path
          required: true
          schema: {{ type: string, maxLength: 10 }}
      responses:
        '200':
          description: 抬頭 + 項目 + 交貨狀態
          content:
            application/json:
              schema: {{ $ref: '#/components/schemas/PoStatus' }}
        '400': {{ description: 單號格式錯誤 }}
        '403': {{ description: 無 M_BEST_EKO 權限 }}
        '404': {{ description: 採購單不存在 }}
  /{resource}/{{ebeln}}/note:
    post:
      summary: 新增備註（寫入 ZPO_NOTE）
      requestBody:
        required: true
        content:
          application/json:
            schema:
              type: object
              required: [text]
              properties:
                text: {{ type: string, maxLength: 255 }}
      responses:
        '201': {{ description: 已建立 }}
        '400': {{ description: text 空白或超長 }}
        '403': {{ description: 無權限 }}
        '404': {{ description: 採購單不存在 }}
components:
  schemas:
    PoStatus:
      type: object
      properties:
        ebeln: {{ type: string }}
        lifnr: {{ type: string }}
        bukrs: {{ type: string }}
        status: {{ type: string, enum: [OPEN, PARTIAL, DELIVERED, CLOSED] }}
        items:
          type: array
          items:
            type: object
            properties:
              ebelp: {{ type: string }}
              matnr: {{ type: string }}
              menge: {{ type: number }}
              wemng: {{ type: number }}
              elikz: {{ type: boolean }}
"""
        feature = f"""Feature: 採購單狀態查詢 API
  讓外部系統用單號查詢採購單抬頭、項目與交貨狀態，並可寫入備註。

  Background:
    Given 測試採購單 "4500000001" 存在，供應商 "V-TEST-01"，公司代碼 "1000"
    And 該採購單有 2 個項目，項目 10 已全數收貨，項目 20 尚未收貨

  Scenario: 查詢存在的採購單
    When 呼叫 GET /{resource}/4500000001
    Then 回應狀態碼為 200
    And 回應欄位 status 為 "PARTIAL"
    And 回應包含 2 個 items
    And items[0].elikz 為 true

  Scenario: 查詢不存在的採購單
    When 呼叫 GET /{resource}/4599999999
    Then 回應狀態碼為 404
    And 回應欄位 error 為 "PO_NOT_FOUND"

  Scenario: 單號格式錯誤
    When 呼叫 GET /{resource}/ABC
    Then 回應狀態碼為 400

  Scenario: 無權限的使用者查詢
    Given 呼叫者沒有授權物件 M_BEST_EKO
    When 呼叫 GET /{resource}/4500000001
    Then 回應狀態碼為 403

  Scenario: 新增備註成功
    When 呼叫 POST /{resource}/4500000001/note，內容 {{"text": "請加速交貨"}}
    Then 回應狀態碼為 201
    And 資料表 ZPO_NOTE 有一筆 EBELN = "4500000001" 且 NOTE_TEXT = "請加速交貨"

  Scenario: 備註文字空白
    When 呼叫 POST /{resource}/4500000001/note，內容 {{"text": ""}}
    Then 回應狀態碼為 400
    And 資料表 ZPO_NOTE 沒有新增任何資料

  Scenario: 對不存在的採購單新增備註
    When 呼叫 POST /{resource}/4599999999/note，內容 {{"text": "x"}}
    Then 回應狀態碼為 404

  Scenario: 全數收貨的採購單狀態
    Given 測試採購單 "4500000002" 存在且所有項目已全數收貨
    When 呼叫 GET /{resource}/4500000002
    Then 回應欄位 status 為 "DELIVERED"
"""
        tech = f"""# Tech Spec — Z {resource.upper()} Status API

## SICF
- 節點：/sap/bc/zapi/{resource}
- Handler class：ZCL_ZAPI_{resource.upper()}_HTTP（IF_HTTP_EXTENSION）
- 路由：解析 PATH_INFO，/{{ebeln}} → GET，/{{ebeln}}/note → POST

## 物件
| 物件 | 類型 | 說明 |
|---|---|---|
| ZCL_ZAPI_{resource.upper()}_HTTP | Class | HTTP handler，只做路由與序列化 |
| ZCL_{resource.upper()}_STATUS_SRV | Class | 業務邏輯：讀 EKKO/EKPO，計算 status |
| ZPO_NOTE | Table | EBELN, SEQNO, NOTE_TEXT, ERNAM, ERDAT, ERZET |
| ZCL_ZAPI_{resource.upper()}_HTTP_TEST | Test class | ABAP Unit，CDS/OSQL test double |

## 狀態計算規則
- 所有項目 ELIKZ = X → DELIVERED
- 有任一項目 WEMNG > 0 但非全部 ELIKZ → PARTIAL
- 全部 WEMNG = 0 → OPEN
- EKKO-LOEKZ = L → CLOSED

## 授權
- 讀：AUTHORITY-CHECK OBJECT 'M_BEST_EKO' ACTVT '03' EKORG
- 寫：同上 ACTVT '02'

## 錯誤處理
- JSON body: {{"error": "<CODE>", "message": "<text>"}}
- 錯誤碼：PO_NOT_FOUND / INVALID_EBELN / NO_AUTH / INVALID_TEXT

## 測試資料
- 固定前綴 45000000xx，由 Given 步驟以 BAPI_PO_CREATE1 建立或直接 INSERT
- 每輪 run id 寫入 ZPO_NOTE.ERNAM 供清理
"""
        return json.dumps({"openapi": openapi, "feature": feature, "tech_spec": tech}, ensure_ascii=False)

    def _plan(self, ctx: str) -> str:
        return "本輪只做一件事：針對目前失敗的第一條情境修正 handler；先跑測試確認失敗再改程式。"

    def _code(self, ctx: str) -> str:
        return """CLASS zcl_zapi_po_http IMPLEMENTATION.
  METHOD if_http_extension~handle_request.
    DATA(lv_path) = server->request->get_header_field( '~path_info' ).
    SPLIT lv_path AT '/' INTO TABLE DATA(lt_seg).
    DELETE lt_seg WHERE table_line IS INITIAL.
    DATA(lv_ebeln) = VALUE string( lt_seg[ 1 ] OPTIONAL ).
    IF lv_ebeln NP '++++++++++' OR lv_ebeln CN '0123456789'.
      respond_error( server = server code = 400 err = 'INVALID_EBELN' ).
      RETURN.
    ENDIF.
    ...
  ENDMETHOD.
ENDCLASS."""

    def _characterize(self, ctx: str) -> str:
        try:
            d = json.loads(ctx)
            eps = d.get("endpoints", [])
        except Exception:
            eps = []
        out = ["Feature: 既有服務現況（characterization）",
               "  描述目前的行為，含現有缺陷；PM 審時標出「現況錯、期望應為 X」的條目。", ""]
        for e in eps:
            m, pth = e.get("method", "GET"), e.get("path", "/")
            pp = [p["name"] for p in e.get("params", []) if p["in"] == "path"]
            ok_path = pth
            for n in pp:
                ok_path = ok_path.replace("{" + n + "}", "4500000001")
            bad_path = pth
            for n in pp:
                bad_path = bad_path.replace("{" + n + "}", "4599999999")
            fmt_path = pth
            for n in pp:
                fmt_path = fmt_path.replace("{" + n + "}", "ABC")
            body = '，內容 {"text": "x"}' if e.get("body") else ""
            out += [f"  @characterization", f"  Scenario: {m} {pth} 正常呼叫",
                    f"    When 呼叫 {m} {ok_path}{body}", f"    Then 回應狀態碼為 {e.get('ok', '200')}"]
            if e.get("resp"):
                out += [f"    And 回應符合結構 {e['resp']}"]
            out += [""]
            for code, label, path in (("404", "資料不存在", bad_path), ("400", "格式錯誤", fmt_path)):
                if code in (e.get("errors") or ""):
                    out += [f"  @characterization", f"  Scenario: {m} {pth} {label}",
                            f"    When 呼叫 {m} {path}{body}", f"    Then 回應狀態碼為 {code}", ""]
            if "403" in (e.get("errors") or ""):
                out += [f"  @characterization", f"  Scenario: {m} {pth} 無權限",
                        f"    Given 呼叫者沒有授權 {e.get('auth') or '（見端點表）'}",
                        f"    When 呼叫 {m} {ok_path}{body}", f"    Then 回應狀態碼為 403", ""]
        return "\n".join(out)

    def _revise(self, ctx: str) -> str:
        return "修正：404 分支漏了 RETURN，導致繼續往下序列化空結構。已補上並調整 ABAP Unit 期望值。"


class GatewayLLM:
    name = "gateway"

    def __init__(self):
        self.base = os.environ.get("LLM_BASE_URL", "").rstrip("/")
        self.key = os.environ.get("LLM_API_KEY", "")
        self.model = os.environ.get("LLM_MODEL", "")
        if not self.base:
            raise RuntimeError("LLM_BASE_URL not set")

    def complete(self, system: str, user: str, purpose: str = "") -> str:
        body = json.dumps({
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": 0.2,
        }).encode()
        req = urllib.request.Request(
            f"{self.base}/chat/completions", data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.key}"})
        with urllib.request.urlopen(req, timeout=120) as r:
            data = json.loads(r.read().decode())
        return data["choices"][0]["message"]["content"]


def get_llm():
    if os.environ.get("LLM_MODE", "mock").lower() == "gateway":
        return GatewayLLM()
    return MockLLM()
