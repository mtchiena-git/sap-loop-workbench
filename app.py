"""
SAP Loop Workbench — PRD → Spec → Dev/Test loop → Release
FastAPI + SQLite + vanilla JS. No build tools, no CDN.

Run:  uvicorn app:app --host 0.0.0.0 --port 8080
Env:  LLM_MODE=mock|gateway   SAP_MODE=mock|abap   (see llm.py / executor.py)
"""
import json
import os
import queue
import sqlite3
import threading
import time
import uuid
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from executor import get_executor
from extractor import extract, get_source
from llm import get_llm

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.environ.get("LOOP_DB", os.path.join(BASE, "loop.db"))
STAGES = ["prd", "spec", "dev", "release", "done"]
GATE_NAMES = {1: "PRD 定稿（Three Amigos）", 2: "規格核准", 3: "寫入 D01 審批", 4: "TR 放行"}

app = FastAPI(title="SAP Loop Workbench")
llm = get_llm()
executor = get_executor()
_lock = threading.Lock()
_subscribers: list[queue.Queue] = []
_runs: dict[str, dict] = {}   # run_id -> control flags


# ---------------------------------------------------------------- db
def db():
    c = sqlite3.connect(DB, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS project(
          id TEXT PRIMARY KEY, name TEXT, prd TEXT DEFAULT '', glossary TEXT DEFAULT '',
          stage TEXT DEFAULT 'prd', settings TEXT DEFAULT '{}', created TEXT);
        CREATE TABLE IF NOT EXISTS spec(
          project_id TEXT PRIMARY KEY, openapi TEXT DEFAULT '', feature TEXT DEFAULT '',
          tech_spec TEXT DEFAULT '', frozen INTEGER DEFAULT 0, updated TEXT,
          endpoints TEXT DEFAULT '[]', fields TEXT DEFAULT '[]', origin TEXT DEFAULT '');
        CREATE TABLE IF NOT EXISTS gate(
          id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT, gate INTEGER,
          status TEXT, who TEXT, note TEXT, ts TEXT);
        CREATE TABLE IF NOT EXISTS run(
          id TEXT PRIMARY KEY, project_id TEXT, status TEXT, iteration INTEGER DEFAULT 0,
          max_iter INTEGER, no_progress_limit INTEGER, started TEXT, ended TEXT,
          reason TEXT DEFAULT '', state TEXT DEFAULT '{}');
        CREATE TABLE IF NOT EXISTS iteration(
          id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, n INTEGER, plan TEXT,
          action TEXT, verify TEXT, revise TEXT, tokens INTEGER, seconds REAL, ts TEXT);
        CREATE TABLE IF NOT EXISTS approval(
          id TEXT PRIMARY KEY, run_id TEXT, project_id TEXT, iteration INTEGER,
          object_name TEXT, object_type TEXT, payload TEXT, status TEXT,
          created TEXT, decided TEXT, decided_by TEXT, note TEXT DEFAULT '');
        CREATE TABLE IF NOT EXISTS note(
          id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT, role TEXT,
          text TEXT, ts TEXT);
        """)
        cols = {r[1] for r in c.execute("PRAGMA table_info(spec)")}
        for col, ddl in (("endpoints", "TEXT DEFAULT '[]'"), ("fields", "TEXT DEFAULT '[]'"), ("origin", "TEXT DEFAULT ''")):
            if col not in cols:
                c.execute(f"ALTER TABLE spec ADD COLUMN {col} {ddl}")


init_db()


def now():
    return datetime.now().isoformat(timespec="seconds")


def emit(kind: str, project_id: str, data: dict):
    msg = json.dumps({"kind": kind, "project_id": project_id, "ts": now(), **data}, ensure_ascii=False)
    with _lock:
        for q in list(_subscribers):
            q.put(msg)


def row(r):
    return dict(r) if r else None


# ---------------------------------------------------------------- models
class ProjectIn(BaseModel):
    name: str
    prd: str = ""
    glossary: str = ""


class ProjectPatch(BaseModel):
    name: Optional[str] = None
    prd: Optional[str] = None
    glossary: Optional[str] = None
    settings: Optional[dict] = None


class SpecPatch(BaseModel):
    openapi: Optional[str] = None
    feature: Optional[str] = None
    tech_spec: Optional[str] = None
    endpoints: Optional[list] = None
    fields: Optional[list] = None


class ImportIn(BaseModel):
    name: str = ""
    node: str = ""          # SICF node, e.g. /sap/bc/zapi/po
    clazz: str = ""         # or a handler class name
    json_style: str = "snake"   # snake | camel  (how /ui2/cl_json names fields)


class GateIn(BaseModel):
    who: str = ""
    note: str = ""
    approve: bool = True


class Decision(BaseModel):
    approve: bool
    who: str = ""
    note: str = ""


class NoteIn(BaseModel):
    role: str = "observer"
    text: str


class LoopStart(BaseModel):
    max_iter: int = 20
    no_progress_limit: int = 3
    approval_mode: str = "first"   # first | every | auto
    poison_scenario: str = ""      # name of a scenario that can never pass (demo)
    seconds_per_iter: float = 2.0  # pacing for the mock executor


# ---------------------------------------------------------------- projects
@app.get("/api/projects")
def list_projects():
    with db() as c:
        return [row(r) for r in c.execute("SELECT id,name,stage,created FROM project ORDER BY created DESC")]


@app.post("/api/projects")
def create_project(p: ProjectIn):
    pid = uuid.uuid4().hex[:8]
    with db() as c:
        c.execute("INSERT INTO project(id,name,prd,glossary,stage,settings,created) VALUES(?,?,?,?,?,?,?)",
                  (pid, p.name, p.prd, p.glossary, "prd", "{}", now()))
        c.execute("INSERT INTO spec(project_id,updated) VALUES(?,?)", (pid, now()))
    emit("project", pid, {"stage": "prd"})
    return get_project(pid)


@app.get("/api/projects/{pid}")
def get_project(pid: str):
    with db() as c:
        p = row(c.execute("SELECT * FROM project WHERE id=?", (pid,)).fetchone())
        if not p:
            raise HTTPException(404)
        p["settings"] = json.loads(p["settings"] or "{}")
        p["spec"] = row(c.execute("SELECT * FROM spec WHERE project_id=?", (pid,)).fetchone())
        if p["spec"]:
            for k in ("endpoints", "fields"):
                try:
                    p["spec"][k] = json.loads(p["spec"][k] or "[]")
                except Exception:
                    p["spec"][k] = []
        p["gates"] = [row(r) for r in c.execute("SELECT * FROM gate WHERE project_id=? ORDER BY id", (pid,))]
        p["runs"] = [row(r) for r in c.execute("SELECT * FROM run WHERE project_id=? ORDER BY started", (pid,))]
        p["approvals"] = [row(r) for r in c.execute(
            "SELECT * FROM approval WHERE project_id=? ORDER BY created DESC", (pid,))]
        p["notes"] = [row(r) for r in c.execute("SELECT * FROM note WHERE project_id=? ORDER BY id", (pid,))]
        p["scenarios"] = parse_scenarios(p["spec"]["feature"] if p["spec"] else "")
        return p


@app.patch("/api/projects/{pid}")
def patch_project(pid: str, p: ProjectPatch):
    with db() as c:
        cur = row(c.execute("SELECT * FROM project WHERE id=?", (pid,)).fetchone())
        if not cur:
            raise HTTPException(404)
        settings = json.loads(cur["settings"] or "{}")
        if p.settings is not None:
            settings.update(p.settings)
        c.execute("UPDATE project SET name=?,prd=?,glossary=?,settings=? WHERE id=?",
                  (p.name if p.name is not None else cur["name"],
                   p.prd if p.prd is not None else cur["prd"],
                   p.glossary if p.glossary is not None else cur["glossary"],
                   json.dumps(settings, ensure_ascii=False), pid))
    return get_project(pid)


@app.delete("/api/projects/{pid}")
def delete_project(pid: str):
    with db() as c:
        for t in ("project", "spec", "gate", "run", "approval", "note"):
            col = "id" if t == "project" else "project_id"
            c.execute(f"DELETE FROM {t} WHERE {col}=?", (pid,))
        c.execute("DELETE FROM iteration WHERE run_id NOT IN (SELECT id FROM run)")
    return {"ok": True}


# ---------------------------------------------------------------- gates
def set_stage(pid: str, stage: str):
    with db() as c:
        c.execute("UPDATE project SET stage=? WHERE id=?", (stage, pid))
    emit("stage", pid, {"stage": stage})


@app.post("/api/projects/{pid}/gate/{gate}")
def pass_gate(pid: str, gate: int, g: GateIn):
    p = get_project(pid)
    status = "APPROVED" if g.approve else "REJECTED"
    with db() as c:
        c.execute("INSERT INTO gate(project_id,gate,status,who,note,ts) VALUES(?,?,?,?,?,?)",
                  (pid, gate, status, g.who, g.note, now()))
    emit("gate", pid, {"gate": gate, "status": status, "who": g.who, "note": g.note})
    if not g.approve:
        return get_project(pid)
    if gate == 1 and p["stage"] == "prd":
        set_stage(pid, "spec")
        if not (p["spec"] and p["spec"]["feature"]):   # keep an existing spec after a rollback
            threading.Thread(target=generate_spec, args=(pid,), daemon=True).start()
    elif gate == 2 and p["stage"] == "spec":
        with db() as c:
            c.execute("UPDATE spec SET frozen=1 WHERE project_id=?", (pid,))
        set_stage(pid, "dev")
    elif gate == 4 and p["stage"] == "release":
        set_stage(pid, "done")
    return get_project(pid)


# ---------------------------------------------------------------- spec
def generate_spec(pid: str):
    p = get_project(pid)
    emit("spec", pid, {"status": "generating"})
    system = ("你是 SAP S/4HANA 技術架構師。根據 PRD 與詞彙表，輸出 JSON，鍵為 openapi（OpenAPI 3 yaml）、"
              "feature（Gherkin，8–12 條情境，Then 驗資料不驗畫面）、tech_spec（Markdown：SICF、物件、授權、錯誤碼、測試資料）。"
              "只輸出 JSON。")
    user = f"PRD:\n{p['prd']}\n\n詞彙表:\n{p['glossary']}"
    try:
        raw = llm.complete(system, user, purpose="spec")
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
        data = json.loads(raw)
    except Exception as e:  # keep the demo alive
        data = {"openapi": "", "feature": "", "tech_spec": f"# 產生失敗\n{e}"}
    with db() as c:
        c.execute("UPDATE spec SET openapi=?,feature=?,tech_spec=?,updated=? WHERE project_id=?",
                  (data.get("openapi", ""), data.get("feature", ""), data.get("tech_spec", ""), now(), pid))
    emit("spec", pid, {"status": "ready"})


@app.post("/api/projects/{pid}/spec/regenerate")
def regenerate_spec(pid: str):
    p = get_project(pid)
    if p["spec"]["frozen"]:
        raise HTTPException(400, "規格已凍結（閘門 2 已通過）。要改請先退回閘門 2。")
    threading.Thread(target=generate_spec, args=(pid,), daemon=True).start()
    return {"ok": True}


@app.patch("/api/projects/{pid}/spec")
def patch_spec(pid: str, s: SpecPatch):
    p = get_project(pid)
    if p["spec"]["frozen"]:
        raise HTTPException(400, "規格已凍結：迴圈的終止條件不能在迴圈中更動。")
    with db() as c:
        cur = p["spec"]
        c.execute("UPDATE spec SET openapi=?,feature=?,tech_spec=?,endpoints=?,fields=?,updated=? WHERE project_id=?",
                  (s.openapi if s.openapi is not None else cur["openapi"],
                   s.feature if s.feature is not None else cur["feature"],
                   s.tech_spec if s.tech_spec is not None else cur["tech_spec"],
                   json.dumps(s.endpoints if s.endpoints is not None else cur["endpoints"], ensure_ascii=False),
                   json.dumps(s.fields if s.fields is not None else cur["fields"], ensure_ascii=False), now(), pid))
    return get_project(pid)["spec"]


# ---------------------------------------------------------------- import existing service
@app.post("/api/projects/import")
def import_service(i: ImportIn):
    """Reverse an existing SICF service into a project at the SPEC stage:
    endpoint table + field table + PRD skeleton + tech spec, all marked as drafts."""
    if not i.node and not i.clazz:
        raise HTTPException(400, "請給 SICF 節點（node）或 handler class（clazz）")
    try:
        ex = extract(node=i.node, clazz=i.clazz, json_style=i.json_style)
    except Exception as e:
        raise HTTPException(502, f"抽取失敗（{type(e).__name__}）：{e}")
    name = i.name or (i.node.rsplit("/", 1)[-1] or i.clazz).upper() + "（匯入）"
    pid = uuid.uuid4().hex[:8]
    origin = {"mode": "import", "source": ex["source"], "node": i.node, "class": i.clazz,
              "handlers": [h["class"] for h in ex["raw"].get("handlers", [])], "stats": ex["stats"], "ts": now()}
    with db() as c:
        c.execute("INSERT INTO project(id,name,prd,glossary,stage,settings,created) VALUES(?,?,?,?,?,?,?)",
                  (pid, name, ex["prd"], "", "spec", json.dumps({"baseline": True}, ensure_ascii=False), now()))
        c.execute("INSERT INTO spec(project_id,openapi,feature,tech_spec,endpoints,fields,origin,updated) VALUES(?,?,?,?,?,?,?,?)",
                  (pid, "", "", ex["tech_spec"], json.dumps(ex["endpoints"], ensure_ascii=False),
                   json.dumps(ex["structs"], ensure_ascii=False), json.dumps(origin, ensure_ascii=False), now()))
        c.execute("INSERT INTO gate(project_id,gate,status,who,note,ts) VALUES(?,?,?,?,?,?)",
                  (pid, 1, "IMPORTED", "agent", f"從 {i.node or i.clazz} 反推：{ex['stats']['endpoints']} 端點、{ex['stats']['structs']} 結構（{ex['source']}）", now()))
    emit("project", pid, {"stage": "spec"})
    threading.Thread(target=generate_characterization, args=(pid,), daemon=True).start()
    return get_project(pid)


@app.get("/api/extract/preview")
def extract_preview(node: str = "", clazz: str = "", json_style: str = "snake"):
    """Dry run: show what the extractor would produce, without creating a project."""
    ex = extract(node=node, clazz=clazz, json_style=json_style)
    return {k: v for k, v in ex.items() if k != "raw"} | {"handlers": [h["class"] for h in ex["raw"].get("handlers", [])]}


def generate_characterization(pid: str):
    """Agent writes characterization scenarios (current behaviour) from the
    reversed endpoint/field tables + tech spec. Marked @characterization."""
    p = get_project(pid)
    emit("spec", pid, {"status": "generating"})
    system = ("你是 SAP 測試工程師。根據既有服務反推出的端點表、欄位表與 Tech Spec，"
              "寫 Gherkin characterization 情境：描述『現在的行為』，不是期望行為。"
              "每個端點至少：正常、404、400、403 各一條；寫入端點加一條驗 Z-table。每條加 tag @characterization。只輸出 feature 文字。")
    user = json.dumps({"endpoints": p["spec"]["endpoints"], "fields": p["spec"]["fields"],
                       "tech_spec": p["spec"]["tech_spec"]}, ensure_ascii=False)
    try:
        feature = llm.complete(system, user, purpose="characterize")
    except Exception as e:
        feature = f"# 產生失敗：{e}"
    with db() as c:
        c.execute("UPDATE spec SET feature=?,updated=? WHERE project_id=?", (feature, now(), pid))
    emit("spec", pid, {"status": "ready"})


PREV_STAGE = {"spec": "prd", "dev": "spec", "release": "dev", "done": "release"}
STAGE_GATE = {"prd": 1, "spec": 2, "dev": 3, "release": 4}


class RollbackIn(BaseModel):
    who: str
    note: str
    to: Optional[str] = None   # default: previous stage


@app.post("/api/projects/{pid}/rollback")
def rollback(pid: str, r: RollbackIn):
    """Go back one stage (or to an earlier one). Content is preserved; reason is logged."""
    p = get_project(pid)
    frm = p["stage"]
    to = r.to or PREV_STAGE.get(frm)
    if not to:
        raise HTTPException(400, "已在第一個階段，無法再退回。")
    if STAGES.index(to) >= STAGES.index(frm):
        raise HTTPException(400, "只能退回到更早的階段。")
    if not r.who.strip() or not r.note.strip():
        raise HTTPException(400, "退回需要填「誰」與原因。")
    for run in p["runs"]:
        if run["status"] in ("running", "waiting_approval"):
            raise HTTPException(400, "迴圈執行中，請先停止再退回。")
    with db() as c:
        if to in ("prd", "spec"):
            c.execute("UPDATE spec SET frozen=0 WHERE project_id=?", (pid,))
        c.execute("INSERT INTO gate(project_id,gate,status,who,note,ts) VALUES(?,?,?,?,?,?)",
                  (pid, STAGE_GATE.get(to, 0), "ROLLBACK", r.who,
                   f"{frm} → {to}：{r.note}", now()))
    set_stage(pid, to)
    emit("gate", pid, {"gate": STAGE_GATE.get(to, 0), "status": "ROLLBACK", "who": r.who, "note": r.note})
    return get_project(pid)


@app.post("/api/projects/{pid}/spec/unfreeze")
def unfreeze_spec(pid: str):
    """Backward-compatible alias: dev → spec without a reason form."""
    with db() as c:
        c.execute("UPDATE spec SET frozen=0 WHERE project_id=?", (pid,))
        c.execute("INSERT INTO gate(project_id,gate,status,who,note,ts) VALUES(?,?,?,?,?,?)",
                  (pid, 2, "ROLLBACK", "", "dev → spec：規格解凍，回到閘門 2", now()))
    set_stage(pid, "spec")
    return get_project(pid)


def parse_scenarios(feature: str) -> list:
    out = []
    for line in (feature or "").splitlines():
        s = line.strip()
        for kw in ("Scenario Outline:", "Scenario:", "情境:", "場景:"):
            if s.startswith(kw):
                out.append(s[len(kw):].strip())
                break
    return out


# ---------------------------------------------------------------- loop
@app.post("/api/projects/{pid}/loop/start")
def start_loop(pid: str, cfg: LoopStart):
    p = get_project(pid)
    if p["stage"] != "dev":
        raise HTTPException(400, "要先通過閘門 2（規格核准）才能啟動迴圈。")
    for r in p["runs"]:
        if r["status"] in ("running", "waiting_approval"):
            raise HTTPException(400, "已有迴圈在執行。")
    rid = uuid.uuid4().hex[:8]
    state = {"scen_pass": 0, "poison_scenario": cfg.poison_scenario or None,
             "approval_mode": cfg.approval_mode, "seconds_per_iter": cfg.seconds_per_iter,
             "feature": p["spec"]["feature"], "objects": [], "history": []}
    with db() as c:
        c.execute("INSERT INTO run(id,project_id,status,iteration,max_iter,no_progress_limit,started,state) "
                  "VALUES(?,?,?,?,?,?,?,?)",
                  (rid, pid, "running", 0, cfg.max_iter, cfg.no_progress_limit, now(),
                   json.dumps(state, ensure_ascii=False)))
    _runs[rid] = {"stop": False}
    threading.Thread(target=run_loop, args=(rid, pid), daemon=True).start()
    emit("run", pid, {"run_id": rid, "status": "running"})
    return {"run_id": rid}


@app.post("/api/runs/{rid}/stop")
def stop_loop(rid: str):
    if rid in _runs:
        _runs[rid]["stop"] = True
    return {"ok": True}


@app.get("/api/runs/{rid}")
def get_run(rid: str):
    with db() as c:
        r = row(c.execute("SELECT * FROM run WHERE id=?", (rid,)).fetchone())
        if not r:
            raise HTTPException(404)
        r["state"] = json.loads(r["state"] or "{}")
        r["iterations"] = []
        for it in c.execute("SELECT * FROM iteration WHERE run_id=? ORDER BY n", (rid,)):
            d = row(it)
            d["verify"] = json.loads(d["verify"] or "{}")
            r["iterations"].append(d)
        return r


def _update_run(rid, **kw):
    sets = ",".join(f"{k}=?" for k in kw)
    with db() as c:
        c.execute(f"UPDATE run SET {sets} WHERE id=?", (*kw.values(), rid))


def request_approval(rid, pid, n, name, otype, payload) -> str:
    aid = uuid.uuid4().hex[:8]
    with db() as c:
        c.execute("INSERT INTO approval(id,run_id,project_id,iteration,object_name,object_type,payload,status,created) "
                  "VALUES(?,?,?,?,?,?,?,?,?)", (aid, rid, pid, n, name, otype, payload, "PENDING", now()))
    emit("approval", pid, {"id": aid, "status": "PENDING", "object": name, "iteration": n})
    return aid


def wait_approval(aid, rid, timeout=900) -> str:
    t0 = time.time()
    while time.time() - t0 < timeout:
        if _runs.get(rid, {}).get("stop"):
            return "STOPPED"
        with db() as c:
            st = c.execute("SELECT status FROM approval WHERE id=?", (aid,)).fetchone()[0]
        if st != "PENDING":
            return st
        time.sleep(0.5)
    with db() as c:
        c.execute("UPDATE approval SET status='EXPIRED',decided=? WHERE id=?", (now(), aid))
    return "EXPIRED"


def run_loop(rid: str, pid: str):
    r = get_run(rid)
    state = r["state"]
    p = get_project(pid)
    scenarios = p["scenarios"]
    no_progress = 0
    best = 0
    n = 0
    reason = ""
    status = "running"
    system = ("你是 ABAP 開發 agent。每輪只做一件事。先看驗證結果再改程式。"
              "不可修改 feature file，不可修改標準程式，卡住就寫 BLOCKED 說明原因並停止。")
    try:
        while n < r["max_iter"]:
            if _runs.get(rid, {}).get("stop"):
                status, reason = "stopped", "人工停止"
                break
            n += 1
            t0 = time.time()
            ctx = json.dumps({"iteration": n, "last_verify": state.get("last_verify"),
                              "scenarios": scenarios}, ensure_ascii=False)
            emit("iter", pid, {"run_id": rid, "n": n, "phase": "plan"})
            plan = llm.complete(system, "規劃本輪：\n" + ctx, purpose="plan")
            emit("iter", pid, {"run_id": rid, "n": n, "phase": "act"})
            code = llm.complete(system, "實作：\n" + ctx, purpose="code")
            obj_name = "ZCL_ZAPI_PO_HTTP" if n > 1 else "ZCL_ZAPI_PO_HTTP + ZPO_NOTE + ZCL_ZAPI_PO_HTTP_TEST"
            needs_approval = state["approval_mode"] == "every" or (state["approval_mode"] == "first" and n == 1)
            revise = ""
            if needs_approval:
                _update_run(rid, status="waiting_approval", iteration=n)
                emit("run", pid, {"run_id": rid, "status": "waiting_approval"})
                aid = request_approval(rid, pid, n, obj_name, "CLAS", code)
                decision = wait_approval(aid, rid)
                _update_run(rid, status="running")
                emit("run", pid, {"run_id": rid, "status": "running"})
                if decision == "STOPPED":
                    status, reason = "stopped", "人工停止（審批等待中）"
                    break
                if decision in ("REJECTED", "EXPIRED"):
                    with db() as c:
                        note = c.execute("SELECT note FROM approval WHERE id=?", (aid,)).fetchone()[0]
                    revise = f"寫入被{ '拒絕' if decision=='REJECTED' else '逾時' }：{note or '（無說明）'}。本輪不寫入，依意見調整後下一輪重送。"
                    verify = {"skipped": True, "scen_pass": state.get("scen_pass", 0),
                              "scen_total": len(scenarios), "failures": []}
                    no_progress += 1
                    _record(rid, n, plan, code, verify, revise, t0)
                    if no_progress >= r["no_progress_limit"]:
                        status, reason = "blocked", f"連續 {no_progress} 輪無進展（寫入持續被拒）"
                        break
                    continue
                executor.write({"name": obj_name, "type": "CLAS", "source": code})
            else:
                executor.write({"name": obj_name, "type": "CLAS", "source": code})
            emit("iter", pid, {"run_id": rid, "n": n, "phase": "verify"})
            time.sleep(state.get("seconds_per_iter", 2.0))
            verify = executor.verify(n, scenarios, state)
            state["scen_pass"] = verify["scen_pass"]
            state["last_verify"] = {k: v for k, v in verify.items() if k != "failures"}
            state["history"].append(verify["scen_pass"])
            if verify["failures"]:
                revise = llm.complete(system, "修正：\n" + json.dumps(verify["failures"][:2], ensure_ascii=False),
                                      purpose="revise")
            _record(rid, n, plan, code, verify, revise, t0)
            _update_run(rid, iteration=n, state=json.dumps(state, ensure_ascii=False))
            emit("iter", pid, {"run_id": rid, "n": n, "phase": "done", "scen_pass": verify["scen_pass"],
                               "scen_total": verify["scen_total"], "atc_p1": verify["atc_p1"]})
            # ---- stop conditions
            if verify["scen_pass"] == verify["scen_total"] and verify["atc_p1"] == 0 and verify["syntax_ok"]:
                status, reason = "done", f"全綠：{verify['scen_total']}/{verify['scen_total']} 情境、ATC P1=0"
                break
            if verify["scen_pass"] > best:
                best, no_progress = verify["scen_pass"], 0
            else:
                no_progress += 1
            if no_progress >= r["no_progress_limit"]:
                stuck = verify["failures"][0]["scenario"] if verify["failures"] else "?"
                status = "blocked"
                reason = (f"BLOCKED：連續 {no_progress} 輪通過數未增加。卡住的情境：「{stuck}」。"
                          f"期望：{verify['failures'][0]['expected'] if verify['failures'] else ''}；"
                          f"實際：{verify['failures'][0]['actual'] if verify['failures'] else ''}。"
                          "判斷為規格與程式矛盾或缺少資訊，需要人判定要改規格還是改程式。")
                break
        else:
            status, reason = "stopped", f"達到回合上限 {r['max_iter']}"
    except Exception as e:
        status, reason = "error", f"{type(e).__name__}: {e}"
    _update_run(rid, status=status, reason=reason, ended=now(), iteration=n,
                state=json.dumps(state, ensure_ascii=False))
    emit("run", pid, {"run_id": rid, "status": status, "reason": reason})
    if status == "done":
        set_stage(pid, "release")
    _runs.pop(rid, None)


def _record(rid, n, plan, action, verify, revise, t0):
    tokens = (len(plan) + len(action) + len(revise)) // 2 + 800   # rough estimate
    with db() as c:
        c.execute("INSERT INTO iteration(run_id,n,plan,action,verify,revise,tokens,seconds,ts) VALUES(?,?,?,?,?,?,?,?,?)",
                  (rid, n, plan, action, json.dumps(verify, ensure_ascii=False), revise, tokens,
                   round(time.time() - t0, 1), now()))


# ---------------------------------------------------------------- approvals
@app.get("/api/approvals")
def list_approvals(status: str = "PENDING"):
    with db() as c:
        return [row(r) for r in c.execute("SELECT * FROM approval WHERE status=? ORDER BY created", (status,))]


@app.post("/api/approvals/{aid}")
def decide(aid: str, d: Decision):
    st = "APPROVED" if d.approve else "REJECTED"
    with db() as c:
        a = row(c.execute("SELECT * FROM approval WHERE id=?", (aid,)).fetchone())
        if not a:
            raise HTTPException(404)
        if a["status"] != "PENDING":
            raise HTTPException(400, f"已是 {a['status']}")
        c.execute("UPDATE approval SET status=?,decided=?,decided_by=?,note=? WHERE id=?",
                  (st, now(), d.who, d.note, aid))
        c.execute("INSERT INTO gate(project_id,gate,status,who,note,ts) VALUES(?,?,?,?,?,?)",
                  (a["project_id"], 3, st, d.who, f"{a['object_name']}：{d.note}", now()))
    emit("approval", a["project_id"], {"id": aid, "status": st, "who": d.who})
    return {"ok": True}


# ---------------------------------------------------------------- notes / report
@app.post("/api/projects/{pid}/notes")
def add_note(pid: str, n: NoteIn):
    with db() as c:
        c.execute("INSERT INTO note(project_id,role,text,ts) VALUES(?,?,?,?)", (pid, n.role, n.text, now()))
    emit("note", pid, {})
    return get_project(pid)["notes"]


@app.get("/api/projects/{pid}/report")
def report(pid: str):
    p = get_project(pid)
    runs = [get_run(r["id"]) for r in p["runs"]]
    gates = p["gates"]
    human_touches = len([g for g in gates if g["gate"] == 3]) + len([g for g in gates if g["gate"] in (1, 2, 4)])
    out = {"project": p["name"], "stage": p["stage"], "scenarios": len(p["scenarios"]),
           "runs": [{"id": r["id"], "status": r["status"], "iterations": r["iteration"], "reason": r["reason"],
                     "history": r["state"].get("history", []),
                     "tokens": sum(i["tokens"] for i in r["iterations"]),
                     "seconds": round(sum(i["seconds"] for i in r["iterations"]), 1),
                     "final": (r["iterations"][-1]["verify"] if r["iterations"] else {})} for r in runs],
           "gates": gates, "human_touches": human_touches, "notes": p["notes"]}
    return out


# ---------------------------------------------------------------- SSE
@app.get("/api/events")
def events():
    q: queue.Queue = queue.Queue()
    with _lock:
        _subscribers.append(q)

    def gen():
        try:
            yield "data: {\"kind\":\"hello\"}\n\n"
            while True:
                try:
                    msg = q.get(timeout=15)
                    yield f"data: {msg}\n\n"
                except queue.Empty:
                    yield ": keepalive\n\n"
        finally:
            with _lock:
                if q in _subscribers:
                    _subscribers.remove(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/config")
def config():
    try:
        ext = get_source().name
    except Exception as e:
        ext = f"unavailable ({e})"
    return {"llm": llm.name, "executor": executor.name, "extractor": ext, "gates": GATE_NAMES, "stages": STAGES}


# ---------------------------------------------------------------- samples / static
@app.get("/api/samples")
def samples():
    d = os.path.join(BASE, "samples")
    out = {}
    for f in sorted(os.listdir(d)):
        with open(os.path.join(d, f), encoding="utf-8") as fh:
            out[f] = fh.read()
    return out


app.mount("/static", StaticFiles(directory=os.path.join(BASE, "static")), name="static")


@app.get("/")
def index():
    return FileResponse(os.path.join(BASE, "static", "index.html"))
