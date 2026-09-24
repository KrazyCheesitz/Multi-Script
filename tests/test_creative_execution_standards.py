import json,pathlib
R=pathlib.Path(__file__).resolve().parents[1]
s=json.load(open(R/"runtime/skills.json"));v=json.load(open(R/"runtime/virtual-tools.json"))["tools"];st=json.load(open(R/"runtime/studio-standard.json"))
assert st["version"]=="12.0" and len(st["animationNativeStandard"])==6 and len(st["vfxNativeStandard"])==6 and len(st["textureArtifactStandard"])==6
assert sum("animationNativeStandard" in x for x in s.values())>=20 and sum("animationNativeStandard" in x for x in v.values())>=20
assert sum("vfxNativeStandard" in x for x in s.values())>=5 and sum("vfxNativeStandard" in x for x in v.values())>=5
assert sum("textureArtifactStandard" in x for x in s.values())>=20 and sum("textureArtifactStandard" in x for x in v.values())>=20
for x in list(s.values())+list(v.values()):
 if "animationNativeStandard" in x: assert any("runtime" in q or "playback" in q for q in x["animationNativeStandard"])
print("PASS 6.8 cross-engine animation, VFX, texture and real-artifact execution standards")
