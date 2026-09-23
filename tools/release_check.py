#!/usr/bin/env python3
"""Offline release gate for Multi-Script."""
from pathlib import Path
import json, re, subprocess, sys
ROOT=Path(__file__).resolve().parents[1]
VERSION="5.3.1"
errors=[]
def need(ok,msg):
    if not ok: errors.append(msg)
def load(path):
    try:return json.loads((ROOT/path).read_text(encoding='utf-8'))
    except Exception as e: errors.append(f'{path}: {e}'); return {}
manifest=load('extension/manifest.json')
need(manifest.get('manifest_version')==3,'manifest must be v3')
need(manifest.get('version')==VERSION,'manifest version mismatch')
bridge=(ROOT/'runtime/bridge.py').read_text(encoding='utf-8')
need(f'BRIDGE_VERSION = "{VERSION}"' in bridge,'bridge version mismatch')
notion=(ROOT/'extension/providers/notion.js').read_text(encoding='utf-8')
need(f'dataset.zsNotionVer = "{VERSION}"' in notion,'Notion adapter version mismatch')
required=['README.md','LICENSE','CHANGELOG.md','CONTRIBUTING.md','docs/INSTALL.md','docs/PRIVACY.md','docs/SECURITY.md','docs/SUPPORT.md','docs/THIRD_PARTY_NOTICES.md','docs/RELEASE_CHECKLIST.md','runtime/config.example.json','runtime/studio-standard.json','runtime/quality-presets.json','roblox-plugin/MultiScriptCompanion.server.lua','docs/ROBLOX_PLUGIN.md']
for x in required: need((ROOT/x).is_file(),f'missing {x}')
for x in ['runtime/elevenlabs_audio.py','runtime/configure_elevenlabs.py','runtime/.env.example']: need((ROOT/x).is_file(),f'missing {x}')
need((ROOT/'docs/MODEL_WATCH.md').is_file(),'missing model watch')
need((ROOT/'tests/test_notion_prompt_only_routing.js').is_file(),'missing prompt-only routing test')
need((ROOT/'tests/test_cross_provider_tool_matrix.js').is_file(),'missing cross-provider tool matrix')
need((ROOT/'tests/test_arena_roblox_json_protocol.js').is_file(),'missing Arena Roblox JSON protocol test')
need((ROOT/'extension/core/tool-routing.js').is_file(),'missing exact engine tool resolver')
need((ROOT/'tests/test_engine_tool_routing.js').is_file(),'missing engine routing regression')
need((ROOT/'tests/test_settings_customization.js').is_file(),'missing customization UI test')
need((ROOT/'tests/test_elevenlabs_settings.py').is_file(),'missing ElevenLabs settings test')
need((ROOT/'tests/test_roblox_companion.py').is_file(),'missing Roblox companion test')
need((ROOT/'tests/test_tool_parameter_contracts.py').is_file(),'missing parameter contract test')
need('ms_roblox_capability_audit' in direct_names if 'direct_names' in locals() else True,'missing Roblox capability audit')
need((ROOT/'tests/test_arena_human_verification.js').is_file(),'missing Arena verification test')
arena=(ROOT/'extension/providers/arena.js').read_text(encoding='utf-8')
for x in ['waitForHumanVerification','humanVerificationRequired','does not bypass verification challenges']: need(x in arena,f'missing Arena verification guard: {x}')
notion=(ROOT/'extension/providers/notion.js').read_text(encoding='utf-8')
for x in ['opus55','Claude Opus 5.5','awaiting-notion',"Notion Auto's best eligible model",'never claim it was selected unless Notion itself confirms that selection','directPickerInteraction: false']: need(x in notion,f'missing Opus 5.5 profile guard: {x}')
for section in manifest.get('content_scripts',[]):
    for rel in section.get('js',[])+section.get('css',[]): need((ROOT/'extension'/rel).is_file(),f'manifest references missing extension/{rel}')
for rel in list(manifest.get('icons',{}).values())+list(manifest.get('action',{}).get('default_icon',{}).values()): need((ROOT/'extension'/rel).is_file(),f'missing icon {rel}')
skills=load('runtime/skills.json'); need(len(skills)==800,f'expected 800 skills, found {len(skills)}')
need(len(set(skills))==len(skills),'duplicate skill ids')
direct_names=re.findall(r"\{[\"']name[\"']:\s*[\"'](ms_[^\"']+)[\"']",bridge)
need(len(direct_names)==151 and len(set(direct_names))==151,f'expected 151 direct tools, found {len(direct_names)}')
for x in ['ms_roblox_capability_audit','ms_camera_system_design','ms_shader_production','ms_security_abuse_review','ms_regression_plan']: need(x in direct_names,f'missing expanded direct tool {x}')
packs=load('runtime/skill-packs.json'); need(packs.get('totalSkills')==len(skills),'skill-packs totalSkills mismatch')
capabilities=load('runtime/capability-index.json'); need(capabilities.get('totalSkills')==len(skills),'capability-index totalSkills mismatch')
virtual=load('runtime/virtual-tools.json'); vtools=virtual.get('tools',{}) if isinstance(virtual,dict) else {}; need(virtual.get('total')==200 and len(vtools)==200,'expected 200 virtual tools'); need(virtual.get('engineCounts')=={'roblox':50,'unity':50,'godot':50,'blender':50},'virtual tool engine counts mismatch')
for tid,v in vtools.items():
    need(v.get('id')==tid and v.get('engine') in {'roblox','unity','godot','blender'},f'invalid virtual tool {tid}')
    need(len(v.get('stages') or [])==8 and len(v.get('qualityGates') or [])==8,f'incomplete virtual tool {tid}')
need(all(len(v.get('relatedSkills') or [])>=4 for v in skills.values()),'every skill must have four related collaborators')
for pack_name,pack in packs.items():
    if not isinstance(pack,dict): continue
    ids=pack.get('skillIds',pack.get('skills'))
    if ids is None: continue
    need(len(ids)==len(set(ids)),f'duplicate ids in pack {pack_name}')
    if 'count' in pack: need(pack['count']==len(ids),f'count mismatch in pack {pack_name}')
    for sid in ids: need(sid in skills,f'unknown skill {sid} in pack {pack_name}')
for sid,v in skills.items():
    need(bool(v.get('name')) and bool(v.get('description')),f'incomplete skill {sid}')
    need(len(v.get('steps') or [])>=1,f'skill lacks steps {sid}')
for p in ROOT.rglob('*'):
    if p.is_file():
        rel=p.relative_to(ROOT).as_posix()
        if '__pycache__' in p.parts or rel.endswith('.pyc'): continue
        if p.stat().st_size>15_000_000: errors.append(f'oversized file: {rel}')
for p in list((ROOT/'runtime').glob('*.py'))+list((ROOT/'tests').glob('*.py'))+list((ROOT/'tools').glob('*.py')):
    try: compile(p.read_text(encoding='utf-8'),str(p),'exec')
    except Exception as e: errors.append(f'python syntax {p.relative_to(ROOT)}: {e}')
js=list((ROOT/'extension').glob('*.js'))+list((ROOT/'extension/core').glob('*.js'))+list((ROOT/'extension/providers').glob('*.js'))+list((ROOT/'tests').glob('*.js'))
for p in js:
    r=subprocess.run(['node','--check',str(p)],capture_output=True,text=True)
    if r.returncode: errors.append(f'javascript syntax {p.relative_to(ROOT)}: {r.stderr.strip()}')
# Block common accidental secret forms without printing values.
secret_re=re.compile(r'(?i)(api[_-]?key|access[_-]?token|client[_-]?secret|private[_-]?key)\s*[=:]\s*["\'][A-Za-z0-9_\-]{16,}')
for p in ROOT.rglob('*'):
    if p.is_file() and p.suffix.lower() in {'.py','.js','.json','.md','.yml','.yaml','.html','.css','.bat','.command'}:
        try:text=p.read_text(encoding='utf-8')
        except Exception:continue
        if secret_re.search(text): errors.append(f'possible hardcoded secret in {p.relative_to(ROOT)}')
if errors:
    print('RELEASE CHECK FAILED')
    for e in errors: print(' -',e)
    sys.exit(1)
print(f'PASS Multi-Script {VERSION} release check: {len(skills)} skills, {len(js)} JavaScript files, manifest references and public docs verified')
