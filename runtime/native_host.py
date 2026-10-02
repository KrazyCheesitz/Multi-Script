# SPDX-License-Identifier: GPL-3.0-or-later
"""native_host.py - the Chrome/Edge/Brave Native Messaging host for Multi-Script.

WHY
A web page and an extension cannot start a program. The one browser-sanctioned
way for an extension to run something on your machine is a *native messaging
host* that YOU registered once. This is that host. With it, the terminal icon in
the chat bar can start, stop and watch the local bridge without you ever opening
the old start.bat launcher (removed in 6.24.0).

SAFETY MODEL
* The browser only lets the extension IDs listed in the host manifest talk to it.
  No website can reach this process.
* It can do exactly one dangerous thing: run `runtime/bridge.py` from THIS folder
  with the interpreter that registered it. There is no "run this command" verb.
* It only ever kills the process it started (tracked in logs/bridge.pid) or a
  process whose command line is this folder's bridge.py.

PROTOCOL
Chrome framing: 4-byte little-endian length + UTF-8 JSON, both directions.
The extension uses sendNativeMessage (one request, one reply, host exits), which
is robust against MV3 service workers being suspended; a connectNative loop is
also supported. Every reply is {"ok": bool, ...} and never raises.
"""
import json
import os
import re
import socket
import struct
import subprocess
import sys
import time

HOST_VERSION = "1.0.0"
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
LOGS = os.path.join(ROOT, "logs")
PID_FILE = os.path.join(LOGS, "bridge.pid")
STDOUT_LOG = os.path.join(LOGS, "bridge_stdout.log")
BRIDGE = os.path.join(HERE, "bridge.py")
PORT = int(os.environ.get("ZS_BRIDGE_PORT", "17613"))
IS_WIN = sys.platform == "win32"

# Desktop apps worth showing next to the bridge ("everything that is running").
APP_PATTERNS = [
    ("Roblox Studio", ("robloxstudiobeta", "robloxstudio")),
    ("Unity Editor", ("unity.exe", "unity hub", "/unity", "unityhub")),
    ("Godot", ("godot",)),
    ("Blender", ("blender",)),
    ("Figma", ("figma",)),
]


# ---------------------------------------------------------------- framing
def _read_message():
    raw = sys.stdin.buffer.read(4)
    if len(raw) < 4:
        return None
    (length,) = struct.unpack("<I", raw)
    if length <= 0 or length > 1024 * 1024:
        return {"cmd": "__invalid__"}
    data = sys.stdin.buffer.read(length)
    try:
        return json.loads(data.decode("utf-8"))
    except Exception:
        return {"cmd": "__invalid__"}


def _send(obj):
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    sys.stdout.buffer.write(struct.pack("<I", len(data)))
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


# ---------------------------------------------------------------- helpers
def port_listening(port=PORT, timeout=0.4):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def _read_pid():
    try:
        with open(PID_FILE, "r", encoding="utf-8") as fh:
            pid = int(fh.read().strip().split()[0])
        return pid if pid > 0 else None
    except Exception:
        return None


def _pid_alive(pid):
    if not pid:
        return False
    if IS_WIN:
        try:
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                                 capture_output=True, text=True, timeout=8,
                                 creationflags=0x08000000).stdout
            return str(pid) in out
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def process_table():
    """[{pid, ppid, name, cmd}] for the whole machine, best effort."""
    rows = []
    try:
        if IS_WIN:
            ps = ("Get-CimInstance Win32_Process | "
                  "Select-Object ProcessId,ParentProcessId,Name,CommandLine | ConvertTo-Json -Compress")
            out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                                 capture_output=True, text=True, encoding="utf-8", errors="replace",
                                 timeout=20, creationflags=0x08000000).stdout
            data = json.loads(out) if out.strip() else []
            if isinstance(data, dict):
                data = [data]
            for p in data:
                rows.append({"pid": int(p.get("ProcessId") or 0), "ppid": int(p.get("ParentProcessId") or 0),
                             "name": p.get("Name") or "", "cmd": p.get("CommandLine") or ""})
        else:
            out = subprocess.run(["ps", "-eo", "pid=,ppid=,comm=,args="], capture_output=True, text=True,
                                 timeout=10).stdout
            for line in out.splitlines():
                parts = line.strip().split(None, 3)
                if len(parts) >= 3 and parts[0].isdigit():
                    rows.append({"pid": int(parts[0]), "ppid": int(parts[1]), "name": parts[2],
                                 "cmd": parts[3] if len(parts) > 3 else parts[2]})
    except Exception:
        pass
    return rows


def _norm_path(p):
    return os.path.normcase(os.path.abspath(p)).replace("\\", "/")


def _is_our_bridge(cmd):
    """True only for `python ... <this folder>/bridge.py` - never another
    program that merely mentions a file called bridge.py. Substring match on the
    normalised absolute path, so folders with spaces ("John Smith") still work."""
    flat = (cmd or "").replace('"', "").replace("\\", "/").lower()
    return _norm_path(BRIDGE).lower() in flat


def find_bridge_pids(rows=None):
    rows = rows if rows is not None else process_table()
    pids = [r["pid"] for r in rows if _is_our_bridge(r["cmd"]) and r["pid"] != os.getpid()]
    pid = _read_pid()
    if pid and pid not in pids and _pid_alive(pid):
        pids.append(pid)
    return pids


def descendants(root, rows):
    kids = {}
    for r in rows:
        kids.setdefault(r["ppid"], []).append(r)
    out, stack = [], list(kids.get(root, []))
    while stack:
        r = stack.pop()
        out.append(r)
        stack.extend(kids.get(r["pid"], []))
    return out


def _short(cmd, n=140):
    cmd = " ".join((cmd or "").split())
    return cmd if len(cmd) <= n else cmd[: n - 1] + "..."


# ---------------------------------------------------------------- commands
def cmd_ping(_m):
    return {"ok": True, "hostVersion": HOST_VERSION, "python": sys.executable,
            "pythonVersion": sys.version.split()[0], "platform": sys.platform, "root": ROOT,
            "bridgeFile": os.path.isfile(BRIDGE)}


def _deps_ok():
    try:
        r = subprocess.run([sys.executable, "-c", "import websockets"], capture_output=True, timeout=30,
                           creationflags=0x08000000 if IS_WIN else 0)
        return r.returncode == 0
    except Exception:
        return False


def _ensure_deps():
    if _deps_ok():
        return True, ""
    try:
        r = subprocess.run([sys.executable, "-m", "pip", "install", "--user", "--quiet", "websockets"],
                           capture_output=True, text=True, timeout=180,
                           creationflags=0x08000000 if IS_WIN else 0)
        if r.returncode == 0 and _deps_ok():
            return True, "installed websockets"
        return False, (r.stderr or r.stdout or "pip failed").strip()[-400:]
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def cmd_start(msg):
    if not os.path.isfile(BRIDGE):
        return {"ok": False, "code": "no_bridge", "error": f"bridge.py not found at {BRIDGE}"}
    if port_listening() and not msg.get("force"):
        return {"ok": True, "already": True, "listening": True, "pids": find_bridge_pids()}
    ok, why = _ensure_deps()
    if not ok:
        return {"ok": False, "code": "deps", "error": "Could not install the 'websockets' library: " + why}
    os.makedirs(LOGS, exist_ok=True)
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["ZS_STARTED_BY"] = "native-host"
    out = open(STDOUT_LOG, "ab", buffering=0)
    out.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} start requested by the extension =====\n".encode())
    kwargs = {"cwd": HERE, "env": env, "stdin": subprocess.DEVNULL, "stdout": out, "stderr": subprocess.STDOUT}
    if IS_WIN:
        # DETACHED + NEW_PROCESS_GROUP + NO_WINDOW: survives this host exiting and
        # never flashes a console window. The terminal panel IS the console now.
        kwargs["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000
    else:
        kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen([sys.executable, "-u", BRIDGE], **kwargs)
    except Exception as exc:
        return {"ok": False, "code": "spawn", "error": f"{type(exc).__name__}: {exc}"}
    try:
        with open(PID_FILE, "w", encoding="utf-8") as fh:
            fh.write(str(proc.pid))
    except Exception:
        pass
    deadline = time.time() + float(msg.get("wait", 10))
    while time.time() < deadline:
        if port_listening():
            return {"ok": True, "started": True, "listening": True, "pid": proc.pid}
        if proc.poll() is not None:
            return {"ok": False, "code": "exited", "exitCode": proc.returncode,
                    "error": "The bridge exited right after starting.", "tail": _tail(40)}
        time.sleep(0.25)
    return {"ok": True, "started": True, "listening": False, "pid": proc.pid,
            "note": "Bridge process started; it is still booting its engines."}


def _kill_tree(pid):
    try:
        if IS_WIN:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, timeout=15,
                           creationflags=0x08000000)
        else:
            import signal
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except Exception:
                os.kill(pid, signal.SIGTERM)
    except Exception:
        pass


def cmd_stop(_m):
    rows = process_table()
    pids = find_bridge_pids(rows)
    if not pids:
        return {"ok": True, "already": True, "stopped": []}
    for pid in pids:
        _kill_tree(pid)
    deadline = time.time() + 6
    while time.time() < deadline and port_listening():
        time.sleep(0.2)
    try:
        os.remove(PID_FILE)
    except OSError:
        pass
    return {"ok": True, "stopped": pids, "listening": port_listening()}


def cmd_restart(msg):
    cmd_stop(msg)
    time.sleep(0.6)
    out = cmd_start(dict(msg, force=True))
    out["restarted"] = True
    return out


def cmd_status(_m):
    rows = process_table()
    pids = find_bridge_pids(rows)
    return {"ok": True, "hostVersion": HOST_VERSION, "listening": port_listening(), "pids": pids,
            "pidFile": _read_pid(), "pythonVersion": sys.version.split()[0]}


def cmd_processes(_m):
    rows = process_table()
    pids = find_bridge_pids(rows)
    procs = []
    seen = set()
    for pid in pids:
        row = next((r for r in rows if r["pid"] == pid), None)
        procs.append({"pid": pid, "role": "bridge", "label": "Multi-Script bridge",
                      "detail": _short(row["cmd"] if row else "bridge.py")})
        seen.add(pid)
        for k in descendants(pid, rows):
            if k["pid"] in seen:
                continue
            seen.add(k["pid"])
            low = (k["cmd"] or k["name"]).lower()
            label = "MCP server"
            for key, nice in (("launch_studio_mcp", "Roblox MCP launcher"), ("studiomcp", "Roblox Studio MCP"),
                              ("launch_unity_mcp", "Unity MCP launcher"), ("launch_godot_mcp", "Godot MCP launcher"),
                              ("godot-mcp", "Godot MCP"), ("unity-mcp", "Unity MCP"), ("blender", "Blender MCP"),
                              ("uvx", "uvx tool server"), ("npx", "npx tool server"), ("node", "Node tool server")):
                if key in low:
                    label = nice
                    break
            procs.append({"pid": k["pid"], "role": "mcp", "label": label, "detail": _short(k["cmd"] or k["name"])})
    apps = []
    for r in rows:
        if r["pid"] in seen:
            continue   # a child of the bridge is an MCP server, not the app itself
        # Match the executable NAME, not the whole command line: "npm exec
        # godot-mcp" mentions Godot but is not the Godot editor.
        low = os.path.basename((r["name"] or "").replace("\\", "/")).lower()
        for nice, keys in APP_PATTERNS:
            if any(k in low for k in keys) and not any(a["label"] == nice for a in apps):
                apps.append({"pid": r["pid"], "role": "app", "label": nice, "detail": _short(r["name"])})
    return {"ok": True, "listening": port_listening(), "processes": procs, "apps": apps}


def _tail(n=60):
    try:
        with open(STDOUT_LOG, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 24000))
            text = fh.read().decode("utf-8", "replace")
        text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)
        return text.splitlines()[-n:]
    except Exception:
        return []


def cmd_tail(msg):
    n = max(1, min(200, int(msg.get("lines") or 60)))
    return {"ok": True, "lines": _tail(n), "listening": port_listening()}


COMMANDS = {"ping": cmd_ping, "start": cmd_start, "stop": cmd_stop, "restart": cmd_restart,
            "status": cmd_status, "processes": cmd_processes, "tail": cmd_tail}


def handle(msg):
    if not isinstance(msg, dict):
        return {"ok": False, "error": "message must be an object"}
    fn = COMMANDS.get(str(msg.get("cmd") or ""))
    if not fn:
        return {"ok": False, "error": f"unknown command '{msg.get('cmd')}'", "commands": sorted(COMMANDS)}
    try:
        return fn(msg)
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def main():
    if IS_WIN:
        import msvcrt
        msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
        msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)
    while True:
        msg = _read_message()
        if msg is None:
            break
        reply = handle(msg)
        if isinstance(msg, dict) and "id" in msg:
            reply["id"] = msg["id"]
        _send(reply)


if __name__ == "__main__":
    main()
