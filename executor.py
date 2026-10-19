"""
Executor abstraction — the "Verify" step of the loop.

The loop never grades itself: verification comes from these executors.

- MockSapExecutor : simulates ABAP syntax check / ABAP Unit / ATC / API
                    scenario run with a deterministic-ish progression so a
                    demo loop visibly converges (and can be made to block).
- AbapHttpExecutor: calls your ABAP HTTP handler / MCP BFF. Endpoints are
                    placeholders — wire them to ZCL_MCP_* or ADT REST.
                    Env: SAP_BFF_URL, SAP_BFF_TOKEN
"""
import json
import os
import random
import urllib.request


class VerifyResult(dict):
    """Keys: syntax_ok, unit_pass, unit_total, atc_p1, atc_p2,
    scen_pass, scen_total, failures[list of {scenario, expected, actual}]"""


class MockSapExecutor:
    name = "mock"

    def __init__(self, seed: int = 7):
        self.rnd = random.Random(seed)

    def verify(self, iteration: int, scenarios: list, state: dict) -> VerifyResult:
        total = len(scenarios)
        # Progress model: roughly +1..2 scenarios per iteration, with a
        # chance of regression early on; a 'poison' scenario never passes
        # (represents a contradictory Gherkin step) so the loop must stop.
        poison = state.get("poison_scenario")
        prev = state.get("scen_pass", 0)
        gain = self.rnd.choice([1, 1, 2, 2, 0]) if iteration > 1 else 1
        if iteration == 3 and prev > 1:
            gain = -1  # a regression the loop has to recover from
        passed = max(0, min(total - (1 if poison else 0), prev + gain))
        failures = []
        for i, s in enumerate(scenarios):
            if i >= passed:
                if s == poison:
                    failures.append({"scenario": s,
                                     "expected": "status 200 且 404 同時成立",
                                     "actual": "矛盾的 Then — 無法同時滿足"})
                else:
                    failures.append({"scenario": s,
                                     "expected": "HTTP 200 / 欄位值符合",
                                     "actual": self.rnd.choice(
                                         ["HTTP 500 — 未處理例外",
                                          "HTTP 404 — 分支漏 RETURN",
                                          "status = OPEN（期望 PARTIAL）",
                                          "items 為空"])})
        unit_total = 6 + total
        unit_pass = min(unit_total, 3 + passed + iteration // 2)
        atc_p1 = max(0, 2 - iteration // 2)
        atc_p2 = max(0, 5 - iteration)
        return VerifyResult(syntax_ok=iteration > 1 or self.rnd.random() > 0.3,
                            unit_pass=unit_pass, unit_total=unit_total,
                            atc_p1=atc_p1, atc_p2=atc_p2,
                            scen_pass=passed, scen_total=total,
                            failures=failures)

    def write(self, obj: dict) -> dict:
        return {"ok": True, "message": f"(mock) wrote {obj.get('name')}"}


class AbapHttpExecutor:
    name = "abap-http"

    def __init__(self):
        self.base = os.environ.get("SAP_BFF_URL", "").rstrip("/")
        self.token = os.environ.get("SAP_BFF_TOKEN", "")
        if not self.base:
            raise RuntimeError("SAP_BFF_URL not set")

    def _post(self, path: str, body: dict) -> dict:
        req = urllib.request.Request(
            f"{self.base}{path}", data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.token}"})
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.loads(r.read().decode())

    def verify(self, iteration: int, scenarios: list, state: dict) -> VerifyResult:
        # Expected BFF contract (adjust to your MCP tools):
        #   POST /tools/syntax_check  {objects:[...]}      -> {ok:bool}
        #   POST /tools/abap_unit     {class:...}          -> {passed:int,total:int}
        #   POST /tools/atc           {package:...}        -> {p1:int,p2:int}
        #   POST /tools/run_feature   {feature:...}        -> {passed:int,total:int,failures:[...]}
        syn = self._post("/tools/syntax_check", {"objects": state.get("objects", [])})
        unit = self._post("/tools/abap_unit", {"class": state.get("test_class")})
        atc = self._post("/tools/atc", {"package": state.get("package")})
        scen = self._post("/tools/run_feature", {"feature": state.get("feature")})
        return VerifyResult(syntax_ok=syn.get("ok", False),
                            unit_pass=unit.get("passed", 0), unit_total=unit.get("total", 0),
                            atc_p1=atc.get("p1", 0), atc_p2=atc.get("p2", 0),
                            scen_pass=scen.get("passed", 0), scen_total=scen.get("total", 0),
                            failures=scen.get("failures", []))

    def write(self, obj: dict) -> dict:
        # Only called AFTER the human approval gate.
        return self._post("/tools/write_object", obj)


def get_executor():
    if os.environ.get("SAP_MODE", "mock").lower() == "abap":
        return AbapHttpExecutor()
    return MockSapExecutor()
