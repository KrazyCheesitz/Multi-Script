import json
from pathlib import Path
r=Path(__file__).resolve().parents[1];s=json.load(open(r/'runtime/skills.json'));ids=[k for k in s if '-mastery-' in k]
assert len(s)==800 and len(ids)==120
for x in ids:
 v=s[x];assert len(v['steps'])==8 and len(v['qualityGates'])==6 and len(v['relatedSkills'])==4 and v['deliverables']
assert {s[x]['engine'] for x in ids}=={'roblox','unity','godot','blender','figma','general'}
print('PASS 120 universal mastery skills across six production domains')
