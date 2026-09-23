#!/usr/bin/env python3
"""Build deterministic full and Chrome/Edge extension release archives."""
from pathlib import Path
import hashlib,json,subprocess,sys,zipfile
ROOT=Path(__file__).resolve().parents[1]; VERSION='5.3.1'; OUT=ROOT/'release'; OUT.mkdir(exist_ok=True)
r=subprocess.run([sys.executable,str(ROOT/'tools/release_check.py')]);
if r.returncode: raise SystemExit(r.returncode)
for old in OUT.glob('*'): old.unlink() if old.is_file() else None
SKIP_PARTS={'.git','__pycache__','logs','generated','release'}
def include(p):
    rel=p.relative_to(ROOT)
    return p.is_file() and not any(x in SKIP_PARTS for x in rel.parts) and p.suffix not in {'.pyc','.zip'}
def write_zip(path,files,prefix=''):
    with zipfile.ZipFile(path,'w',zipfile.ZIP_DEFLATED,compresslevel=9) as z:
        for p,arc in sorted(files,key=lambda x:x[1]):
            info=zipfile.ZipInfo(prefix+arc,(2026,9,23,12,0,0)); info.compress_type=zipfile.ZIP_DEFLATED; info.external_attr=(0o755 if p.suffix in {'.command','.py'} else 0o644)<<16
            z.writestr(info,p.read_bytes(),compress_type=zipfile.ZIP_DEFLATED,compresslevel=9)
full=[(p,p.relative_to(ROOT).as_posix()) for p in ROOT.rglob('*') if include(p)]
extroot=ROOT/'extension'; ext=[(p,p.relative_to(extroot).as_posix()) for p in extroot.rglob('*') if p.is_file() and '__pycache__' not in p.parts]
full_zip=OUT/f'Multi-Script-{VERSION}-Full.zip'; ext_zip=OUT/f'Multi-Script-Extension-{VERSION}.zip'
write_zip(full_zip,full,'Multi-Script/'); write_zip(ext_zip,ext)
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
manifest={'product':'Multi-Script','version':VERSION,'artifacts':[{'file':p.name,'bytes':p.stat().st_size,'sha256':sha(p)} for p in [full_zip,ext_zip]],'installGuide':'docs/INSTALL.md','privacy':'docs/PRIVACY.md','security':'docs/SECURITY.md'}
manifest_path=OUT/'release-manifest.json';manifest_path.write_text(json.dumps(manifest,indent=2)+'\n')
(OUT/'SHA256SUMS.txt').write_text('\n'.join(f"{x['sha256']}  {x['file']}" for x in manifest['artifacts'])+'\n')
print(json.dumps(manifest,indent=2))
