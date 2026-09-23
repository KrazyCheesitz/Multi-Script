import importlib.util,json,sys,types
from pathlib import Path
R=Path(__file__).resolve().parents[1];w=types.ModuleType("websockets");w.ConnectionClosed=Exception;sys.modules.setdefault("websockets",w)
s=importlib.util.spec_from_file_location("q",R/"runtime"/"bridge.py");b=importlib.util.module_from_spec(s);s.loader.exec_module(b)
class M:
 def health(self):return []
def run(q,e="roblox"):return json.loads(b._builtin_call("ms_studio_director",{"request":q,"engine":e},M())["text"])
for q,want in [("Make me a classic shedletsky model","roblox-classic-character"),("a classic roblox gui","roblox-classic-gui"),("a high quallity stud texture ui panel","stud-texture-ui-panel"),("high quality walk, dash and punch animation","locomotion-combat-animation-set")]:
 x=run(q);assert want in [p["id"] for p in x["matchedQualityPresets"]],(q,x);assert x["automaticEnhancement"]["simplePromptIsEnough"]
p=json.load(open(R/"runtime"/"quality-presets.json"));assert len(p["presets"])>=12
print("PASS Quality Autopilot examples and 12 cross-discipline production presets")
