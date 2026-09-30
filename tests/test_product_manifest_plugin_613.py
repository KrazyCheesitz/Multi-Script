import json,re
from pathlib import Path
R=Path(__file__).resolve().parents[1]
m=json.load(open(R/"runtime/product-manifest.json"));assert m["version"]=="6.17.3"
sk=json.load(open(R/"runtime/skills.json"));vt=json.load(open(R/"runtime/virtual-tools.json"))["tools"]
assert len(sk)==m["catalogue"]["skills"]==800 and len(vt)==m["catalogue"]["virtualSpecialists"]==300
assert all(x.get("engine") and x.get("engineExecutionContract") for x in sk.values())
assert all(x.get("engine") and x.get("engineExecutionContract") for x in vt.values())
p=(R/"roblox-plugin/MultiScriptCompanion.server.lua").read_text()
for x in ['PLUGIN_VERSION = "6.17.3"',"DIAGNOSTIC_VERSION = 2","readinessScore","highIssueCount","scanDurationMs","Selection.SelectionChanged","task.wait()","Sanitized diagnostic report"]:assert x in p,x
for x in m["robloxCompanion"]["forbiddenExecutionSurfaces"]:assert x not in p,x
assert all(x in p for x in ["/status","/catalog","/plugin/heartbeat"])
b=(R/"runtime/bridge.py").read_text();assert all(x in b for x in ['_hb_text("placeName", 120)','_hb_int("readinessScore", 0, 100)','_hb_int("issueCount", 0, 100)'])
std=json.load(open(R/"runtime/studio-standard.json"));assert std["version"]=="12.0" and len(std["robloxCompanionDiagnostics"])==8
print("PASS canonical product manifest, explicit catalogue contracts, Roblox companion v6.13 diagnostics and strict no-execution boundary")
