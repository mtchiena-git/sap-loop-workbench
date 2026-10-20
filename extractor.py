"""
Extractor — pull an existing SAP RESTful service (SICF node / handler class)
and reverse it into endpoint-table + field-table drafts for the spec stage.

Sources (pick with EXTRACT_MODE=mock|abap|adt):
  MockSource      bundled sample dump (samples/extract_po.json) — offline demo
  AbapHttpSource  GET {SAP_EXTRACT_URL}?node=... or ?class=...   (ZCL_LOOP_EXTRACT_HTTP)
                  env: SAP_EXTRACT_URL, SAP_USER, SAP_PASS  (basic auth)
  AdtSource       ADT REST — class source + DDIC structures, no RTTI expansion
                  env: SAP_ADT_URL (…/sap/bc/adt), SAP_USER, SAP_PASS

Pipeline:  fetch(dump) → parse_dump(dump) → {endpoints, structs, prd_skeleton, tech_spec}
The parse is heuristic (regex over the handler source + the RTTI/DDIC type
dump); it produces a DRAFT that the agent refines (llm) and humans review at G2.
"""
import base64
import json
import os
import re
import urllib.parse
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------- sources
class MockSource:
    name = "mock"

    def fetch(self, node: str = "", clazz: str = "") -> dict:
        with open(os.path.join(BASE, "samples", "extract_po.json"), encoding="utf-8") as f:
            d = json.load(f)
        if node:
            d["node"] = node
        return d


class AbapHttpSource:
    name = "abap-http"

    def __init__(self):
        self.url = os.environ.get("SAP_EXTRACT_URL", "").rstrip("/")
        if not self.url:
            raise RuntimeError("SAP_EXTRACT_URL not set")
        self.auth = base64.b64encode(f"{os.environ.get('SAP_USER','')}:{os.environ.get('SAP_PASS','')}".encode()).decode()

    def fetch(self, node: str = "", clazz: str = "") -> dict:
        q = urllib.parse.urlencode({"node": node} if node else {"class": clazz})
        req = urllib.request.Request(f"{self.url}?{q}", headers={"Authorization": f"Basic {self.auth}",
                                                                 "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read().decode())


class AdtSource:
    """Fallback when the ABAP extractor is not deployed: ADT gives the class
    source; TYPES are parsed from the text (no RTTI), DDIC structures via
    /ddic/structures/{name} when reachable."""
    name = "adt"

    def __init__(self):
        self.url = os.environ.get("SAP_ADT_URL", "").rstrip("/")
        if not self.url:
            raise RuntimeError("SAP_ADT_URL not set")
        self.auth = base64.b64encode(f"{os.environ.get('SAP_USER','')}:{os.environ.get('SAP_PASS','')}".encode()).decode()

    def _get(self, path: str, accept: str = "text/plain") -> str:
        req = urllib.request.Request(f"{self.url}{path}", headers={"Authorization": f"Basic {self.auth}", "Accept": accept})
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.read().decode("utf-8", "replace")

    def fetch(self, node: str = "", clazz: str = "") -> dict:
        classes = [clazz] if clazz else self._handlers_of_node(node)
        out = {"node": node, "handlers": []}
        for c in classes:
            src = self._get(f"/oo/classes/{c.lower()}/source/main")
            try:
                src += "\n\" ===== LOCAL TEST CLASSES (CCAU) =====\n" + self._get(f"/oo/classes/{c.lower()}/includes/testclasses")
            except Exception:
                pass
            h = {"class": c.upper(), "source": src}
            h.update(scan_source(src))                   # same heuristics as the ABAP side
            h["types"] = parse_types_from_source(src)    # text-level TYPES expansion
            h["ddic"] = []
            out["handlers"].append(h)
        return out

    def _handlers_of_node(self, node: str) -> list:
        # ADT has no public SICF endpoint on 2021; use the discovery URL if your
        # system exposes one, otherwise ask for the class explicitly.
        raise RuntimeError("ADT source needs ?class=…; SICF node lookup requires ZCL_LOOP_EXTRACT_HTTP")


def get_source():
    mode = os.environ.get("EXTRACT_MODE", "mock").lower()
    return {"abap": AbapHttpSource, "adt": AdtSource}.get(mode, MockSource)()


# ---------------------------------------------------------------- text heuristics
def scan_source(src: str) -> dict:
    """Python twin of ZCL_LOOP_EXTRACT_HTTP->scan_source for the ADT path."""
    h = {"methods": [], "unit_tests": [], "auth_checks": [], "status_codes": [], "form_fields": [],
         "header_fields": [], "tables_read": [], "tables_written": [], "bapis": [], "messages": []}
    add = lambda k, v: (v and v not in h[k] and h[k].append(v))
    for i, raw in enumerate(src.splitlines(), 1):
        u = raw.strip().upper()
        if not u or u[0] in "*\"":
            continue
        if u.startswith("METHOD ") and not u.startswith("METHODS"):
            add("methods", u[7:].rstrip(".").strip())
        m = re.match(r"CLASS\s+(\w+).*FOR TESTING", u)
        if m:
            add("unit_tests", m.group(1))
        m = re.search(r"AUTHORITY-CHECK OBJECT '([^']+)'", u)
        if m:
            h["auth_checks"].append({"object": m.group(1), "line": i, "actvt": ""})
        m = re.search(r"ID 'ACTVT' FIELD\s+(\S+)", u)
        if m and h["auth_checks"]:
            h["auth_checks"][-1]["actvt"] = m.group(1).rstrip(".")
        m = re.search(r"SET_STATUS\(.*CODE\s*=\s*(\d{3})", u) or re.search(r"\b(?:IV_|LV_)?CODE\s*=\s*(\d{3})\b", u)
        if m:
            h["status_codes"].append({"code": int(m.group(1)), "line": i})
        for m in re.finditer(r"GET_FORM_FIELD\(\s*'([^']+)'", u):
            add("form_fields", m.group(1).lower())
        for m in re.finditer(r"GET_HEADER_FIELD\(\s*'([^']+)'", u):
            add("header_fields", m.group(1).lower())
        m = re.search(r"\bFROM\s+(\w+)", u)
        if m and ("SELECT" in u or u.startswith("FROM")) and "SCREEN" not in u:
            add("tables_read", m.group(1))
        m = re.match(r"(INSERT|UPDATE|MODIFY|DELETE)\s+(?:FROM\s+)?(Z\w+)", u)
        if m:
            add("tables_written", m.group(2))
        m = re.search(r"CALL FUNCTION\s+'([^']+)'", u)
        if m:
            add("bapis", m.group(1))
        m = re.search(r"MESSAGE\s+[EWISAX](\d{3})\((\w+)\)", u)
        if m:
            h["messages"].append({"class": m.group(2), "number": m.group(1), "line": i})
    return h


ABAP_TYPE_MAP = {"C": "string", "STRING": "string", "N": "string", "D": "string", "T": "string",
                 "I": "integer", "INT4": "integer", "INT8": "integer", "P": "number", "F": "number",
                 "DEC": "number", "QUAN": "number", "CURR": "number", "ABAP_BOOL": "boolean",
                 "XSDBOOLEAN": "boolean", "FLAG": "boolean", "BOOLE_D": "boolean", "X": "string"}


def parse_types_from_source(src: str) -> list:
    """TYPES: BEGIN OF … END OF … and TABLE OF … from the class text."""
    types, cur = [], None
    for raw in src.splitlines():
        u = raw.strip().rstrip(",.").strip()
        if not u or u[0] in "*\"":
            continue
        m = re.match(r"(?:TYPES:?\s+)?BEGIN OF\s+(\w+)", u, re.I)
        if m:
            cur = {"name": m.group(1).upper(), "kind": "struct", "fields": []}
            continue
        m = re.match(r"END OF\s+(\w+)", u, re.I)
        if m and cur:
            types.append(cur)
            cur = None
            continue
        m = re.match(r"(?:TYPES:?\s+)?(\w+)\s+TYPE\s+(?:STANDARD|SORTED|HASHED)?\s*TABLE OF\s+(\w+)", u, re.I)
        if m:
            types.append({"name": m.group(1).upper(), "kind": "table", "line": m.group(2).upper()})
            continue
        if cur:
            m = re.match(r"(\w+)\s+TYPE\s+(\w+)(?:\s+LENGTH\s+(\d+))?(?:\s+DECIMALS\s+(\d+))?", u, re.I)
            if m:
                cur["fields"].append({"name": m.group(1).upper(), "type": m.group(2).upper(),
                                      "len": int(m.group(3) or 0), "dec": int(m.group(4) or 0),
                                      "ddic": "" if m.group(2).upper() in ABAP_TYPE_MAP else m.group(2).upper(),
                                      "label": ""})
    return types


# ---------------------------------------------------------------- dump → spec drafts
def _json_name(abap_name: str, style: str) -> str:
    n = abap_name.lower()
    if style == "camel":
        parts = n.split("_")
        return parts[0] + "".join(p.capitalize() for p in parts[1:])
    return n


def _struct_to_fields(t: dict, style: str, table_lines: dict) -> list:
    out = []
    for f in t.get("fields", []):
        at = (f.get("type") or "").upper()
        ddic = (f.get("ddic") or "").upper()
        jt = ABAP_TYPE_MAP.get(at) or ABAP_TYPE_MAP.get(ddic) or "string"
        ref = ""
        # a component whose type is a known table type → list of <line struct>
        if at in table_lines:
            jt, ref = "list of", table_lines[at]
        elif at and at not in ABAP_TYPE_MAP and not ddic and at.startswith("TY_"):
            jt, ref = "object", _pascal(at)
        row = {"label": f.get("label") or "", "name": _json_name(f["name"], style), "type": jt,
               "len": str(f.get("len") or "") if jt == "string" else "", "req": True, "ex": "",
               "rule": "", "ref": ref, "src": f"{t['name']}-{f['name']}" + (f" ({ddic})" if ddic else "")}
        if ddic in ("ELIKZ", "LOEKZ") or at in ("ABAP_BOOL", "FLAG"):
            row["type"], row["len"] = "boolean", ""
        out.append(row)
    return out


def _pascal(ty_name: str) -> str:
    n = re.sub(r"^TY_", "", ty_name.upper())
    return "".join(p.capitalize() for p in n.split("_"))


def _routes_from_source(src: str, prefix: str = "") -> list:
    """Find the routing branches: method + path pattern pairs, plus the name
    the handler gives to each path segment (CONV ebeln( lt_seg[ 1 ] ) → {ebeln})."""
    routes, cur_method, seg_names = [], None, {}
    for raw in src.splitlines():
        u = raw.strip().upper()
        m = re.search(r"CONV\s+(\w+)\(.*?SEG\w*\[\s*(\d+)", u)
        if m:
            seg_names[int(m.group(2))] = m.group(1).lower()
        m = re.search(r"REQUEST_METHOD\W*=\W*'(GET|POST|PUT|PATCH|DELETE)'", u) or \
            re.search(r"\bWHEN\s+'(GET|POST|PUT|PATCH|DELETE)'", u)
        if m:
            cur_method = m.group(1)
        m = re.search(r"(?:PATH_INFO|PATH)\W*(?:CP|=|CS)\W*'(/[^']*)'", u) or re.search(r"\bWHEN\s+'(/[^']*)'", u)
        if m:
            segs, n = [], 0
            for seg in m.group(1).strip("/").split("/"):
                if seg in ("*", "+", "{id}"):
                    n += 1
                    segs.append("{" + seg_names.get(n, f"id{n if n > 1 else ''}") + "}")
                else:
                    segs.append(seg.lower())
            routes.append({"method": cur_method or "GET", "path": prefix + "/" + "/".join(segs)})
    seen, out = set(), []
    for r in routes:
        k = (r["method"], r["path"])
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out


def parse_dump(dump: dict, json_style: str = "snake") -> dict:
    """Turn the raw dump into endpoint/field table drafts + PRD skeleton + tech spec."""
    endpoints, structs, tech, prd_caps = [], [], [], []
    node = dump.get("node", "")
    for h in dump.get("handlers", []):
        src = h.get("source", "")
        types = h.get("types", []) + h.get("ddic", [])
        table_lines = {t["name"].upper(): _pascal(t["line"]) for t in types if t.get("kind") == "table" and t.get("line")}
        for t in types:
            if t.get("kind") != "struct":
                continue
            structs.append({"name": _pascal(t["name"]), "role": f"反推自 {t['name']}" + ("（DDIC）" if t in h.get("ddic", []) else ""),
                            "src": t["name"], "fields": _struct_to_fields(t, json_style, table_lines)})
        auth = h.get("auth_checks", [])
        codes = sorted({c["code"] for c in h.get("status_codes", []) if c["code"] >= 400 and c["code"] != 405})
        ok_codes = sorted({c["code"] for c in h.get("status_codes", []) if 200 <= c["code"] < 300})
        prefix = "/" + node.rsplit("/", 1)[-1] if node else ""
        routes = _routes_from_source(src, prefix) or [{"method": "GET", "path": f"{prefix}/{{id}}"}]
        ff = h.get("form_fields", [])
        for r in routes:
            path_params = re.findall(r"\{(\w+)\}", r["path"])
            params = [{"name": p, "in": "path", "type": "string", "len": "", "req": True, "desc": "反推自 PATH_INFO"} for p in path_params]
            params += [{"name": q, "in": "query", "type": "string", "len": "", "req": False, "desc": "反推自 get_form_field"} for q in ff if q not in path_params]
            is_write = r["method"] in ("POST", "PUT", "PATCH")
            body = next((s["name"] for s in structs if is_write and re.search(r"REQ|IN|NOTE|CREATE", s["src"])), "") if is_write else ""
            resp = next((s["name"] for s in structs if not is_write and re.search(r"STATUS|RES|OUT|HEAD", s["src"])), "") if not is_write else ""
            a = next((x for x in auth if (is_write and "02" in x.get("actvt", "")) or (not is_write and "03" in x.get("actvt", ""))), auth[0] if auth else None)
            endpoints.append({"method": r["method"], "path": r["path"], "summary": f"反推自 {h['class']}",
                              "params": params, "body": body, "ok": str((201 if is_write else 200) if not ok_codes else (max(ok_codes) if is_write else min(ok_codes))),
                              "resp": resp, "errors": ",".join(str(c) for c in codes) or "400,403,404",
                              "auth": f"{a['object']} ACTVT {a.get('actvt','')}".strip() if a else "", "src": h["class"]})
            prd_caps.append(f"{'寫入' if is_write else '查詢'}：{r['method']} {r['path']}（由 {h['class']} 提供）")
        tech.append(f"""## {h['class']}
- SICF：{node}
- 方法：{', '.join(h.get('methods', [])[:12])}
- 讀取表：{', '.join(h.get('tables_read', [])) or '-'}
- 寫入表：{', '.join(h.get('tables_written', [])) or '-'}
- BAPI / FM：{', '.join(h.get('bapis', [])) or '-'}
- 授權：{', '.join(f"{a['object']} {a.get('actvt','')}" for a in auth) or '-'}
- 訊息：{', '.join(f"{m['class']} {m['number']}" for m in h.get('messages', [])) or '-'}
- 既有 ABAP Unit：{', '.join(h.get('unit_tests', [])) or '無'}""")
    prd = f"""# PRD（反推骨架，請 PM 補業務意義）

## 1. 背景
既有服務 {node} 匯入。以下由程式反推，**業務意義、使用者、不回傳清單**需由 PM 補齊。

## 2. 目標（現況能力）
{chr(10).join('- ' + c for c in prd_caps) or '- （未能從路由反推，請手動補）'}

## 3. 使用者
（PM 補）

## 4. 範圍
- 現況基準：先讓 characterization 情境全綠，再談改動

## 5. 業務規則
（PM 補；程式中的規則見 Tech Spec 的訊息與授權）

## 6. 資料需求
見欄位表（反推自 TYPES / DDIC；業務名與規則欄留空待補）

## 7. 驗收條件
第一輪＝現況行為；PM 標出「現況錯、期望應為 X」的項目即為第一個變更需求
"""
    return {"endpoints": endpoints, "structs": structs, "prd": prd,
            "tech_spec": "# Tech Spec（反推）\n\n" + "\n\n".join(tech),
            "stats": {"handlers": len(dump.get("handlers", [])), "endpoints": len(endpoints), "structs": len(structs)}}


def extract(node: str = "", clazz: str = "", json_style: str = "snake") -> dict:
    src = get_source()
    dump = src.fetch(node=node, clazz=clazz)
    out = parse_dump(dump, json_style)
    out["source"] = src.name
    out["raw"] = dump
    return out


if __name__ == "__main__":
    import sys
    r = extract(node=sys.argv[1] if len(sys.argv) > 1 else "/sap/bc/zapi/po")
    print(json.dumps({k: v for k, v in r.items() if k != "raw"}, ensure_ascii=False, indent=1))
