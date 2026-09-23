from __future__ import annotations
import os,re,shutil,subprocess,sys
from pathlib import Path
DEFAULT_VERSION='0.1.1'
def find_npx():
 p=os.environ.get('ZS_NPX_PATH'); return str(Path(p).expanduser()) if p and Path(p).expanduser().is_file() else shutil.which('npx')
def main():
 node=shutil.which('node'); npx=find_npx()
 if not node or not npx:
  sys.stderr.write('Godot MCP requires Node.js 18+ with npm/npx. Install Node.js and restart.\n'); return 1
 try: major=int(re.search(r'\d+',subprocess.run([node,'--version'],capture_output=True,text=True,timeout=8).stdout).group())
 except Exception: major=0
 if major<18: sys.stderr.write('Godot MCP requires Node.js 18+.\n'); return 1
 v=os.environ.get('ZS_GODOT_MCP_VERSION',DEFAULT_VERSION)
 cmd=[npx,'-y',f'@coding-solo/godot-mcp@{v}']+sys.argv[1:]
 if sys.platform=='win32' and npx.lower().endswith(('.cmd','.bat')): cmd=['cmd.exe','/c']+cmd
 return subprocess.Popen(cmd).wait()
if __name__=='__main__': raise SystemExit(main())
