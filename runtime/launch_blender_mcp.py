# SPDX-License-Identifier: GPL-3.0-or-later
"""Launch the Blender MCP server (blender-mcp) over stdio for the Multi-Script bridge.

The server talks to the "BlenderMCP" add-on running inside Blender (Blender ->
Edit -> Preferences -> Add-ons -> install addon.py from the blender-mcp project,
then N-panel -> BlenderMCP -> Connect to MCP server, default 127.0.0.1:9876).

stdout belongs to the MCP protocol: this launcher never prints to it.
Environment: ZS_UVX_PATH (uvx binary), ZS_BLENDER_MCP_VERSION (pin a release),
BLENDER_HOST / BLENDER_PORT (passed through to the server).
"""
from __future__ import annotations
import os, shutil, subprocess, sys
from pathlib import Path


def find_uvx():
    override = os.environ.get("ZS_UVX_PATH")
    if override and Path(override).expanduser().is_file():
        return str(Path(override).expanduser())
    found = shutil.which("uvx")
    if found:
        return found
    home = Path.home()
    cands = [home / ".local" / "bin" / ("uvx.exe" if sys.platform == "win32" else "uvx")]
    if sys.platform == "win32":
        for key in ("LOCALAPPDATA", "APPDATA"):
            if os.environ.get(key):
                cands.append(Path(os.environ[key]) / "uv" / "bin" / "uvx.exe")
    for c in cands:
        if c.is_file():
            return str(c)
    return None


def build_command(uvx, version=""):
    spec = "blender-mcp" + (("==" + version) if version else "")
    cmd = [uvx, spec]
    if sys.platform == "win32" and uvx.lower().endswith((".cmd", ".bat")):
        cmd = ["cmd.exe", "/c"] + cmd
    return cmd


def main():
    uvx = find_uvx()
    if not uvx:
        sys.stderr.write("launch_blender_mcp: uvx was not found. Install uv from https://docs.astral.sh/uv/getting-started/installation/, restart the bridge, or set ZS_UVX_PATH.\n")
        return 1
    version = os.environ.get("ZS_BLENDER_MCP_VERSION", "").strip()
    cmd = build_command(uvx, version)
    sys.stderr.write("launch_blender_mcp: starting blender-mcp%s via %s\n" % (("==" + version) if version else "", uvx))
    sys.stderr.flush()
    proc = subprocess.Popen(cmd + sys.argv[1:])
    try:
        return proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        return 130


if __name__ == "__main__":
    sys.exit(main())
