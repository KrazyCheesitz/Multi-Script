#!/usr/bin/env python3
from pathlib import Path
import argparse,json,subprocess,sys,time
R=Path(__file__).resolve().parents[1];OUT=R/'runtime/critic-loop-report.json'
def cmd(a):
 t=time.time();p=subprocess.run(a,cwd=R,capture_output=True,text=True);return {'command':' '.join(map(str,a)),'passed':p.returncode==0,'durationSeconds':round(time.time()-t,3),'tail':(p.stdout+p.stderr)[-1200:]}
def audit():
 m=json.load(open(R/'runtime/product-manifest.json'));v=m['version'];e=[]
 def need(x,sev,code,detail):
  if not x:e.append({'severity':sev,'code':code,'detail':detail})
 ext=json.load(open(R/'extension/manifest.json'));b=(R/'runtime/bridge.py').read_text();p=(R/'roblox-plugin/MultiScriptCompanion.server.lua').read_text();sk=json.load(open(R/'runtime/skills.json'));vt=json.load(open(R/'runtime/virtual-tools.json'))['tools']
 need(ext['version']==v,'critical','extension-version','extension version drift');need(f'BRIDGE_VERSION = "{v}"' in b,'critical','bridge-version','bridge version drift');need(f'PLUGIN_VERSION="{v}"' in p,'high','plugin-version','plugin version drift')
 need(len(sk)==m['catalogue']['skills'],'critical','skill-count','skill count drift');need(len(vt)==m['catalogue']['virtualSpecialists'],'critical','virtual-count','virtual count drift');need(all(x.get('engine') and x.get('engineExecutionContract') for x in sk.values()),'high','skill-contract','skill engine contract missing');need(all(x.get('engine') and x.get('engineExecutionContract') for x in vt.values()),'high','virtual-contract','virtual engine contract missing')
 for x in m['robloxCompanion']['forbiddenExecutionSurfaces']:need(x not in p,'critical','plugin-boundary','forbidden plugin execution surface '+x)
 for x in ['/status','/catalog','/plugin/heartbeat']:need(x in p,'high','plugin-route','missing diagnostic route '+x)
 ar=(R/'extension/providers/arena.js').read_text();need('return "opus55"' in ar and 'MODEL-TARGETED TURN SIGNAL' in ar,'high','arena-targeting','Arena exact-model per-turn default missing')
 sa=json.load(open(R/'runtime/engine-compatibility-audit.json')).get('schemaAudit',{});need(sa.get('highRisk',0)==0,'high','schema-risk','high-risk schema remains')
 return e
def run(skip=False,write=True):
 t=time.time();f1=audit();rounds=[{'round':1,'name':'architecture and drift','verdict':'pass' if not f1 else 'revise','findings':f1}];checks=[] if skip else [cmd([sys.executable,str(x)]) for x in sorted((R/'tests').glob('test_*.py'))]+[cmd(['node',str(x)]) for x in sorted((R/'tests').glob('test_*.js'))]+[cmd([sys.executable,str(R/'tools/release_check.py')])];bad=[x for x in checks if not x['passed']];rounds.append({'round':2,'name':'complete regressions','verdict':'skipped' if skip else ('pass' if not bad else 'revise'),'findings':[{'severity':'critical','code':'test-failure','detail':x['command'],'tail':x['tail']} for x in bad]});f3=audit();blocking=[x for x in f3 if x['severity'] in ('critical','high')];recommendations=[{'priority':'live-validation','item':'Run attached editor smoke tests on Roblox, Unity, Godot and Blender before store publication.'},{'priority':'research','item':'Verify Mirelo official API/MCP, licensing, authentication and exports before integration.'},{'priority':'deferred','item':'Consider a VS Code extension later, reusing this bridge and schema validator.'}];verdict='pass' if not bad and not blocking else 'revise';rounds.append({'round':3,'name':'final critic and opportunities','verdict':verdict,'findings':f3,'recommendations':recommendations});r={'product':'Multi-Script','version':json.load(open(R/'runtime/product-manifest.json'))['version'],'generatedAtUnix':round(time.time()),'durationSeconds':round(time.time()-t,3),'verdict':verdict,'rounds':rounds,'testChecks':checks,'summary':{'critical':sum(x['severity']=='critical' for z in rounds for x in z.get('findings',[])),'high':sum(x['severity']=='high' for z in rounds for x in z.get('findings',[])),'recommendations':3}};
 if write:OUT.write_text(json.dumps(r,indent=2)+'\n')
 return r
if __name__=='__main__':
 a=argparse.ArgumentParser();a.add_argument('--skip-tests',action='store_true');q=a.parse_args();r=run(q.skip_tests);print(json.dumps({'version':r['version'],'verdict':r['verdict'],'summary':r['summary'],'rounds':[{'round':x['round'],'verdict':x['verdict']} for x in r['rounds']]},indent=2));raise SystemExit(0 if r['verdict']=='pass' else 1)
