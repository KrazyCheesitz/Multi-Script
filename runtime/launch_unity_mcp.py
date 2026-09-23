# SPDX-License-Identifier: GPL-3.0-or-later
"""Launch CoplayDev MCP for Unity v10.2.0 over stdio.

The Unity package must be installed in the open Unity project. The Python server
is resolved by uvx and pinned so a future upstream release cannot silently break
an existing Multi-Script bundle. Override uvx with ZS_UVX_PATH and the package
version with ZS_UNITY_MCP_VERSION when deliberately testing another release.
"""
from __future__ import annotations
import os, shutil, subprocess, sys
from pathlib import Path

DEFAULT_VERSION = "10.2.0"

def find_uvx():
    override=os.environ.get("ZS_UVX_PATH")
    if override and Path(override).expanduser().is_file(): return str(Path(override).expanduser())
    found=shutil.which("uvx")
    if found: return found
    home=Path.home()
    candidates=[home/".local"/"bin"/("uvx.exe" if sys.platform=="win32" else "uvx")]
    if sys.platform=="win32":
        for key in ("LOCALAPPDATA","APPDATA"):
            if os.environ.get(key): candidates.append(Path(os.environ[key])/"uv"/"bin"/"uvx.exe")
    for item in candidates:
        if item.is_file(): return str(item)
    return None

def main():
    uvx=find_uvx()
    if not uvx:
        sys.stderr.write("launch_unity_mcp: uvx was not found. Install uv from https://docs.astral.sh/uv/getting-started/installation/, restart this bridge, and keep Unity open with the MCP for Unity package installed.\n")
        return 1
    version=os.environ.get("ZS_UNITY_MCP_VERSION",DEFAULT_VERSION).strip() or DEFAULT_VERSION
    cmd=[uvx,"--from",f"mcpforunityserver=={version}","mcp-for-unity","--transport","stdio"]+sys.argv[1:]
    sys.stderr.write(f"launch_unity_mcp: starting CoplayDev MCP for Unity {version} via {uvx}\n"); sys.stderr.flush()
    if sys.platform=="win32" and uvx.lower().endswith((".cmd",".bat")):
        cmd=["cmd.exe","/c"]+cmd
    proc=subprocess.Popen(cmd)
    try: return proc.wait()
    except KeyboardInterrupt:
        proc.terminate(); return proc.wait()

if __name__=="__main__": raise SystemExit(main())
