import json
from pathlib import Path
r=Path(__file__).resolve().parents[1];s=json.load(open(r/'runtime/skills.json'));ids=[k for k in s if '-studio-' in k]
assert len(s)==800 and len(ids)==180
for x in ids:
 v=s[x];assert len(v['steps'])==10 and len(v['qualityGates'])==8 and len(v['relatedSkills'])==4 and len(v['deliverables'])==3
assert {s[x]['engine'] for x in ids}=={'roblox','unity','godot','blender','figma','general'}
print('PASS 180 senior studio craft skills across six domains')
