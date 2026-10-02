#!/usr/bin/env python3
"""Offline release gate for Multi-Script."""
from pathlib import Path
import json, re, subprocess, sys
ROOT=Path(__file__).resolve().parents[1]
VERSION="6.24.0"
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
required=['runtime/product-manifest.json','tools/critic_loop.py','docs/VSCODE_EXTENSION_FUTURE.md','README.md','LICENSE','CHANGELOG.md','CONTRIBUTING.md','docs/INSTALL.md','docs/PRIVACY.md','docs/SECURITY.md','docs/SUPPORT.md','docs/THIRD_PARTY_NOTICES.md','docs/RELEASE_CHECKLIST.md','runtime/config.example.json','runtime/studio-standard.json','roblox-plugin/MultiScriptCompanion.server.lua','docs/ROBLOX_PLUGIN.md']
for x in required: need((ROOT/x).is_file(),f'missing {x}')
product=load('runtime/product-manifest.json')
need(product.get('version')==VERSION,'product manifest version mismatch')
plugin=(ROOT/'roblox-plugin/MultiScriptCompanion.server.lua').read_text(encoding='utf-8')
need(f'PLUGIN_VERSION = "{VERSION}"' in plugin,'Roblox plugin version mismatch')
companion=product.get('robloxCompanion') or {}
need(companion.get('companionTools')==24,'Roblox companion tool count mismatch')
need(companion.get('defaultPermission')=='off','Roblox companion must default Off')
need((companion.get('security') or {}).get('arbitraryCodeEvaluation') is False,'arbitrary plugin code evaluation must remain disabled')
need('loadstring(' not in plugin,'Roblox companion must not evaluate arbitrary Lua')
for route in ['/status','/catalog','/plugin/heartbeat','/plugin/register','/plugin/next','/plugin/result','/plugin/unregister']: need(route in plugin,f'missing plugin route {route}')
companion_tools=load('runtime/roblox-plugin-tools.json').get('tools',[])
need(len(companion_tools)==24 and len({x.get('name') for x in companion_tools})==24,'companion catalog must contain 24 unique tools')
for tool in companion_tools: need(f"handlers.{tool.get('name')}=" in plugin,f"missing companion handler: {tool.get('name')}")
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
need((ROOT/'tests/test_model_improvement_layer.py').is_file(),'missing model-improvement architecture test')
need((ROOT/'tests/test_full_spectrum_skill_mesh.py').is_file(),'missing full-spectrum skill mesh test')
need('ms_roblox_capability_audit' in direct_names if 'direct_names' in locals() else True,'missing Roblox capability audit')
need((ROOT/'tests/test_arena_human_verification.js').is_file(),'missing Arena verification test')
need((ROOT/'tests/test_roblox_studio_id.js').is_file(),'missing Roblox studio_id contract test')
need((ROOT/'tests/test_required_param_contract.js').is_file(),'missing required-parameter contract test')
need((ROOT/'docs/release-notes/RELEASE_NOTES_6.17.3.md').is_file(),'missing 6.17.3 release notes')
need((ROOT/'docs/release-notes/RELEASE_NOTES_6.17.4.md').is_file(),'missing 6.17.4 release notes')
need((ROOT/'docs/release-notes/RELEASE_NOTES_6.23.0.md').is_file(),'missing 6.23.0 release notes')
need((ROOT/'docs/release-notes/RELEASE_NOTES_6.24.0.md').is_file(),'missing 6.24.0 release notes')
arena=(ROOT/'extension/providers/arena.js').read_text(encoding='utf-8')
for x in ['waitForHumanVerification','humanVerificationRequired','does not bypass verification challenges']: need(x in arena,f'missing Arena verification guard: {x}')
# The assisted first-click is the only interaction with a challenge, and the
# release must never ship a bypass. Scan the PROVIDER + CORE for the concrete
# implementations of one (solving services, token fields, execute APIs).
verify=(ROOT/'extension/core/verification.js')
need(verify.is_file(),'missing verification assistant module')
import re as _re
def _strip_comments(s):
    s=_re.sub(r'/\*[\s\S]*?\*/',' ',s)
    s=_re.sub(r'^\s*//.*$',' ',s,flags=_re.M)
    return s
_ship=_strip_comments(arena+_strip_comments((ROOT/'extension/core/verification.js').read_text(encoding='utf-8'))+_strip_comments((ROOT/'extension/core/main.js').read_text(encoding='utf-8'))).lower()
for bad in ['2captcha','anticaptcha','capsolver','capmonster','rucaptcha','deathbycaptcha',
            'grecaptcha.execute','grecaptcha.getresponse','hcaptcha.execute','turnstile.render(',
            'g-recaptcha-response','h-captcha-response','cf-turnstile-response','contentwindow.postmessage']:
    need(bad not in _ship,f'captcha bypass implementation in shipped code: {bad}')
need('assistChallengeClick' in arena,'missing assisted challenge click')
need((ROOT/'tests/test_verification_assistant.js').is_file(),'missing verification assistant test')
need((ROOT/'tests/test_arena_human_verification.js').is_file(),'missing Arena human-verification gate test')
# 6.17.4: the content-script load contract. 6.17.3 shipped a TDZ ReferenceError
# that killed every content script before any UI mounted; node --check cannot see
# it, only evaluating the concatenated chain in one shared scope can. The test is
# required to exist AND to carry its non-vacuous guard.
_lct=(ROOT/'tests/test_content_script_load_contract.js')
need(_lct.is_file(),'missing content-script load contract test (6.17.4 regression gate)')
_lct_src=_lct.read_text(encoding='utf-8')
need('content_scripts' in _lct_src,'load contract test does not walk manifest.content_scripts')
need('TDZ' in _lct_src and 'not vacuous' in _lct_src,'load contract test lost its non-vacuous TDZ guard')
# The bug itself: no top-level assignment to A before `const A = {`.
_main_src=(ROOT/'extension/core/main.js').read_text(encoding='utf-8')
_A_decl=_main_src.find('const A = {')
need(_A_decl>0,'core/main.js lost its `const A = {` declaration')
for _ln,_line in enumerate(_main_src[:_A_decl].splitlines(),1):
    _code=_line.split('//')[0]
    if re.match(r'^\s*A\.[A-Za-z_$][\w$]*\s*=',_code) and _code.startswith('  A.'):
        need(False,f'core/main.js assigns A before `const A` (temporal dead zone) at line {_ln}')
# Engine-registry integrity. The registry in core/engines.js carries the
# action-dispatch vocabulary, the copy-paste REQUIRED SHAPE templates and the
# Roblox place-scope set. A drift between a template and its tool (wrong
# `command`, an action that is not in the tool's vocabulary, a template that
# does not parse) hands the model a shape the server rejects, which is exactly
# the 6.17.1/6.17.2 BAD-JSON failure class. The test is required to exist AND to
# keep asserting JSON round-trip + template/command agreement.
_eri=(ROOT/'tests/test_engine_registry_integrity.js')
need(_eri.is_file(),'missing engine registry integrity test')
_eri_src=_eri.read_text(encoding='utf-8')
need('ENGINE_TEMPLATES' in _eri_src or 'templateFor' in _eri_src,'engine registry test lost its template walk')
need('JSON.parse' in _eri_src and 'actionsOf' in _eri_src,'engine registry test lost template-parse/action checks')
# Refusal feedback honesty. 6.17.5 fixed `subagent` being refused with a
# false "timed out" cause (a permanent block is not a transient failure). The
# helper must exist and be shared, and main.js must not reintroduce a fabricated
# timeout for a permanently disabled command.
_rf=(ROOT/'tests/test_refusal_feedback.js')
need(_rf.is_file(),'missing refusal feedback test')
_cfg_src=(ROOT/'extension/core/config.js').read_text(encoding='utf-8')
need('blockedFeedback' in _cfg_src,'core/config.js lost blockedFeedback')
need('PERMANENTLY_BLOCKED' in _cfg_src,'core/config.js lost the shared PERMANENTLY_BLOCKED set')
need('ZS.blockedFeedback' in _main_src,'core/main.js no longer delegates refusal text to the shared helper')
# Roblox required-shape conformance. The Roblox templates were written by
# guessing parameter names and several were wrong (generate_mesh takes
# textPrompt not prompt; wait_job_finished takes jobId not generation_id;
# screen_capture REQUIRES capture_id; search_game_tree REQUIRES datamodel_type).
# The test pins the real schema facts so an edit cannot reintroduce a guess.
_rsc=(ROOT/'tests/test_roblox_schema_conformance.js')
need(_rsc.is_file(),'missing Roblox schema conformance test')
_rsc_src=_rsc.read_text(encoding='utf-8')
for _x in ['textPrompt','jobId','capture_id','datamodel_type','assetId']:
    need(_x in _rsc_src,f'Roblox schema test lost its {_x} assertion')
_eng=(ROOT/'extension/core/engines.js').read_text(encoding='utf-8')
need('"textPrompt"' in _eng or "'textPrompt'" in _eng,'engines.js lost the real generate_mesh textPrompt field')
need('"jobId"' in _eng,'engines.js lost the real wait_job_finished jobId field')
need('"capture_id"' in _eng,'engines.js lost the required screen_capture capture_id')
need('character_navigation' in _eng,'engines.js lost character_navigation')
notion=(ROOT/'extension/providers/notion.js').read_text(encoding='utf-8')
for x in ['opus55','Claude Opus 5.5','luna','GPT-6 Luna','awaiting-notion',"Notion Auto's best eligible real model",'never imitate ${x.model} through role-play','Never claim ${x.model} was selected unless Notion itself exposes or confirms that selection','directPickerInteraction: false']: need(x in notion,f'missing Notion model starter guard: {x}')
for section in manifest.get('content_scripts',[]):
    for rel in section.get('js',[])+section.get('css',[]): need((ROOT/'extension'/rel).is_file(),f'manifest references missing extension/{rel}')
for rel in list(manifest.get('icons',{}).values())+list(manifest.get('action',{}).get('default_icon',{}).values()): need((ROOT/'extension'/rel).is_file(),f'missing icon {rel}')
skills=load('runtime/skills.json'); need(len(skills)==800,f'expected 800 skills, found {len(skills)}')
need(len(set(skills))==len(skills),'duplicate skill ids')
direct_names=re.findall(r"\{[\"']name[\"']:\s*[\"'](ms_[^\"']+)[\"']",bridge)
roblox_contract_line=next((line for line in bridge.splitlines() if line.startswith('ROBLOX_STUDIO_V3_CONTRACTS = ')),'')
direct_names += re.findall(r"'(ms_roblox_[^']+)': \{",roblox_contract_line)
engine_contract_line=next((line for line in bridge.splitlines() if line.startswith('ENGINE_PRO_V3_CONTRACTS = ')),'')
direct_names += re.findall(r"'(ms_(?:unity|godot|blender)_[^']+)': \{",engine_contract_line)
need(len(direct_names)==230 and len(set(direct_names))==230,f'expected 230 direct tools, found {len(direct_names)}')
for x in ['ms_roblox_capability_audit','ms_camera_system_design','ms_shader_production','ms_security_abuse_review','ms_regression_plan']: need(x in direct_names,f'missing expanded direct tool {x}')
packs=load('runtime/skill-packs.json'); need(packs.get('totalSkills')==len(skills),'skill-packs totalSkills mismatch')
capabilities=load('runtime/capability-index.json'); need(capabilities.get('totalSkills')==len(skills),'capability-index totalSkills mismatch')
virtual=load('runtime/virtual-tools.json'); vtools=virtual.get('tools',{}) if isinstance(virtual,dict) else {}; need(virtual.get('total')==300 and len(vtools)==300,'expected 300 virtual tools'); need(virtual.get('engineCounts')=={'roblox':90,'unity':70,'godot':70,'blender':70},'virtual tool engine counts mismatch')
for tid,v in vtools.items():
    need(v.get('id')==tid and v.get('engine') in {'roblox','unity','godot','blender'},f'invalid virtual tool {tid}')
    need(len(v.get('stages') or [])>=8 and len(v.get('qualityGates') or [])>=8,f'incomplete virtual tool {tid}')
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
# 6.24.0: the terminal icon starts the bridge (Native Messaging), start.bat is gone,
# every engine ships a native toolkit, and Notion gets model/trial tooling.
for _rel in ['runtime/schema_repair.py','runtime/native_host.py','runtime/install_native_host.py','runtime/engine_toolkit.py','runtime/toolkit_blender.py','runtime/toolkit_roblox.py','runtime/toolkit_unity.py','runtime/toolkit_godot.py','runtime/launch_blender_mcp.py','Setup.bat','MacOS_Setup.command','extension/core/notion-usage.js','tests/test_engine_toolkit.py','tests/test_schema_repair_and_native_host.py','tests/test_notion_usage.js','tests/test_parser_repair.js','tests/test_studio_ui.js','tests/fixtures/roblox_mock.lua','tools/studio_harness.js']:
    need((ROOT/_rel).is_file(),f'missing 6.24.0 file {_rel}')
need(not (ROOT/'start.bat').exists(),'start.bat must stay removed - the terminal icon starts the bridge')
need('nativeMessaging' in manifest.get('permissions',[]),'manifest lost the nativeMessaging permission')
need(any('core/notion-usage.js' in sec.get('js',[]) for sec in manifest.get('content_scripts',[])),'notion-usage.js is not loaded on Notion')
_pm=load('runtime/product-manifest.json')
need(_pm.get('engineToolkit',{}).get('total',0)>=100,'product manifest lost the engine toolkit counts')
if errors:
    print('RELEASE CHECK FAILED')
    for e in errors: print(' -',e)
    sys.exit(1)
print(f'PASS Multi-Script {VERSION} release check: {len(skills)} skills, {len(js)} JavaScript files, manifest references and public docs verified')
