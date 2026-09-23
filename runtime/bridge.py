# # SPDX-License-Identifier: GPL-3.0-or-later
# bridge.py
# ──────────────────────────────────────────────────────────────────────────
#  Multi-Script Bridge
#  Local WebSocket <-> Roblox Studio MCP server.
#  The browser extension talks to this over ws://127.0.0.1:<PORT>.
#
#  What this bridge exposes to Kimi (aggregated into one tools/list):
#    - Every MCP server declared in config.json (by default: roblox), each
#      spawned as a stdio child and routed by tool name.
#
#  Design goals (robustness first):
#   - Each MCP stdio process is read by ONE dedicated thread; responses are
#     matched by JSON-RPC id (no "read the next line and hope" races).
#   - stderr is drained so a child never blocks on a full pipe.
#   - A dead server is auto-restarted and the failing call retried once.
#   - Tool calls are locked PER SERVER, so a slow server never blocks another.
#   - Every call ALWAYS produces a reply: a result OR a structured error.
#     Nothing ever hangs the agentic loop silently.
# ──────────────────────────────────────────────────────────────────────────
import asyncio
import concurrent.futures
import html
import json
import random
import os
import re
import queue
import subprocess
import sys
import threading
import time

try:
    # Sibling script (same folder as bridge.py, which Python puts on sys.path
    # automatically) - reused here purely to detect a Studio version bump
    # (see _current_studio_exe below), not to launch anything.
    import launch_studio_mcp as _studio_scan
except Exception:
    _studio_scan = None

try:
    import websockets
except ImportError:
    print("[bridge] Missing dependency. Run:  pip install websockets")
    sys.exit(1)

# Windows consoles often default to a legacy codepage (cp1252): printing
# non-ASCII text then raises UnicodeEncodeError INSIDE the WS handler, which
# kills the connection. Force UTF-8 (best effort). We also keep all console
# output strictly ASCII (no arrows / dots) so nothing garbles on a console that
# stayed on a legacy codepage anyway.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def _enable_ansi_colors():
    """On Windows, turn on ANSI escape processing so color codes render instead
    of printing as literal gibberish like "<ESC>[92m". Returns True on success."""
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        k = ctypes.windll.kernel32
        h = k.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if not k.GetConsoleMode(h, ctypes.byref(mode)):
            return False
        # ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        return bool(k.SetConsoleMode(h, mode.value | 0x0004))
    except Exception:
        return False


HOST = "127.0.0.1"
# Keep in sync with extension/manifest.json "version" - printed at
# startup so a user's terminal output alone tells us which build they're on.
BRIDGE_VERSION = "5.3.1"
PORT = int(os.environ.get("ZS_BRIDGE_PORT", "17613"))
PLUGIN_PORT = int(os.environ.get("ZS_PLUGIN_PORT", str(PORT + 1)))
HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")

# The primary server. It is always present, added by the installer, and can
# never be edited/removed through the extension (it is what Multi-Script is FOR).
PRIMARY_SERVER_ID = "roblox"

if _enable_ansi_colors():
    C = {
        "reset": "\033[0m", "dim": "\033[2m", "gr": "\033[92m",
        "yl": "\033[93m", "rd": "\033[91m", "cy": "\033[96m",
        # Bold white-on-red: for a non-technical user, an "ACTION NEEDED" step
        # must look nothing like the routine cyan/yellow status noise around
        # it, or it gets scrolled past unread (seen live 2026-07-13 - the
        # toggle instruction and the boot banner's own yellow re-explanation
        # of the SAME step were visually indistinguishable). Bright-yellow-bg
        # with black text was tried first but reads as low-contrast/washed
        # out on several real terminal color schemes (also seen live) - white
        # on red is the universal high-contrast "act now" pairing.
        "act": "\033[1m\033[97m\033[41m",
    }
else:
    C = {k: "" for k in ("reset", "dim", "gr", "yl", "rd", "cy", "act")}

# Every run appends here (never truncated), so a whole test session - across
# multiple restarts - stays in one file the user can just send us. Each
# process start writes a banner (see main()) so restarts are easy to spot.
LOGS_DIR = os.path.join(HERE, "logs")
os.makedirs(LOGS_DIR, exist_ok=True)
LOG_PATH = os.path.join(LOGS_DIR, "bridge_debug.log")
try:
    _log_file = open(LOG_PATH, "a", encoding="utf-8", errors="replace")
except Exception:
    _log_file = None


class _Spinner:
    """Terminal-only progress indicator for waits that can run several seconds
    (server launch/handshake, Studio attach grace period) so the console never
    just sits there looking dead - the #1 thing that makes a user assume the
    bridge hung and close the window. Purely cosmetic: writes over its own line
    with \\r, never touches bridge_debug.log, and is skipped entirely when
    stdout isn't a real console (redirected to a file, no ANSI)."""
    FRAMES = "|/-\\"
    # Only ONE spinner may animate at a time: server launches now run in
    # PARALLEL (see MCPManager.start_all), and several spinners fighting over
    # the same console line with \r produced interleaved garbage. Whoever
    # acquires this lock animates; the others silently skip (the log lines
    # around them still tell the story).
    _active = threading.Lock()

    def __init__(self, label):
        self.label = label
        self._stop = threading.Event()
        self._thread = None
        self._owns_lock = False

    def __enter__(self):
        if sys.stdout.isatty() and _Spinner._active.acquire(blocking=False):
            self._owns_lock = True
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.0)
            # Wipe the spinner line so the next log() line doesn't get glued
            # onto trailing spinner characters.
            print("\r" + " " * (len(self.label) + 4) + "\r", end="", flush=True)
        if self._owns_lock:
            _Spinner._active.release()

    def _run(self):
        i = 0
        while not self._stop.is_set():
            frame = self.FRAMES[i % len(self.FRAMES)]
            print(f"\r{C['dim']}{self.label} {frame}{C['reset']}", end="", flush=True)
            i += 1
            self._stop.wait(0.15)


def _clear_spinner_line():
    """Wipe whatever a live _Spinner (running on its own thread, mid-frame) left
    on the current console line via bare \\r writes, so the next print() below
    doesn't get glued onto its trailing characters - seen live 2026-07-14: an
    action_banner() fired while '[roblox] starting... -' was still mid-line and
    the red box rendered smashed onto it instead of starting on a fresh line.
    \\033[K (clear to end of line) doesn't depend on knowing the spinner's label
    length the way Spinner.__exit__'s own wipe does."""
    if sys.stdout.isatty():
        print("\r\033[K", end="", flush=True)


def log(msg, color="dim", terminal=True):
    """terminal=False: written to bridge_debug.log only, not the console. Use
    for noisy/technical detail (raw stderr from child MCP servers, per-call
    traces) that would bury the handful of lines a non-technical user actually
    needs to read. Nothing is ever lost - it all still lands in the file."""
    if terminal:
        _clear_spinner_line()
        ts = time.strftime("%H:%M:%S")
        print(f"{C['dim']}{ts}{C['reset']} {C.get(color,'')}{msg}{C['reset']}", flush=True)
    if _log_file:
        try:
            _log_file.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
            _log_file.flush()
        except Exception:
            pass


def action_banner(lines):
    """Print a step the USER must physically go do, styled so it cannot be
    mistaken for routine status/warning noise (see the 'act' color above).
    Framed with blank lines so it visually stands alone in a scrolling
    terminal - a non-technical user should be able to glance at the window
    and immediately spot this without reading everything above it.

    Every line (header, content, footer) is padded to the SAME width so the
    yellow block renders as one clean rectangle - an earlier version padded
    each line to a fixed guess independently, which produced a ragged block
    with mismatched edges on a real console (seen live 2026-07-13)."""
    header = "ACTION NEEDED"
    width = max([len(header) + 8] + [len(ln) for ln in lines]) + 2
    top = f">>> {header} " + ">" * max(0, width - len(header) - 5)
    _clear_spinner_line()
    print()
    print(f"{C['act']}  {top.ljust(width)}{C['reset']}")
    for ln in lines:
        print(f"{C['act']}  {ln.ljust(width)}{C['reset']}")
    print(f"{C['act']}  {'>' * width}{C['reset']}")
    print()
    if _log_file:
        try:
            _log_file.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} ACTION NEEDED: "
                             f"{' | '.join(lines)}\n")
            _log_file.flush()
        except Exception:
            pass


# Roblox Studio exposes its built-in MCP server on this loopback port. StudioMCP
# (and our bridge, via it) reaches Studio through it.
STUDIO_MCP_PORT = 13469


def _port_owner(port):
    """(pid, name, path) of the process LISTENING on `port`, or None. Win32 only."""
    if sys.platform != "win32":
        return None
    # BOTH stacks: "-p TCP" alone is IPv4-only, and a squatter listening on
    # [::1]:<port> (IPv6 loopback) was then completely invisible to this probe
    # even while Get-NetTCPConnection showed it plainly (the likely reason the
    # boot-time squatter check stayed silent on a machine where ropilot
    # provably held the port - see the 2026-07-13 live report).
    out = ""
    for proto in ("TCP", "TCPv6"):
        try:
            out += subprocess.run(
                ["netstat", "-ano", "-p", proto],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=8,
            ).stdout
        except Exception:
            pass
    if not out:
        return None
    pid = None
    # v4 lines end the local address in ":<port>", v6 in "]:<port>" - matching
    # on the ":<port> " suffix (with the column gap) covers both shapes.
    needle = f":{port} "
    for line in out.splitlines():
        if "LISTENING" in line and needle in line:
            parts = line.split()
            if parts and parts[-1].isdigit():
                pid = parts[-1]
                break
    if not pid:
        return None
    name, path = "?", ""
    try:
        ps = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"$p=Get-Process -Id {pid} -ErrorAction SilentlyContinue; "
             f"if($p){{$p.Name; $p.Path}}"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=8,
        ).stdout.splitlines()
        ps = [l.strip() for l in ps if l.strip()]
        if ps:
            name = ps[0]
            path = ps[1] if len(ps) > 1 else ""
    except Exception:
        pass
    return (pid, name, path)


def _roblox_studio_app_running():
    """True/False whether a real Roblox Studio window process exists, or None
    if this can't be determined (non-Windows, or the check itself failed)."""
    if sys.platform != "win32":
        return None
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq RobloxStudioBeta.exe"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=8,
        ).stdout
    except Exception:
        return None
    return "RobloxStudioBeta.exe" in out


def _kill_orphan_studio_mcp():
    """Kill leftover StudioMCP.exe processes from a PREVIOUS session/crash.

    StudioMCP.exe is Roblox's own MCP proxy; launch_studio_mcp.py spawns one
    as a direct child every time the bridge starts. If an earlier restart's
    tree-kill missed the grandchild (a reparenting race), or Studio itself
    crashed and left its own StudioMCP.exe running (seen live 2026-07-11:
    RobloxStudioBeta.exe zombied after two RobloxCrashHandler.exe events),
    the orphan keeps LISTENING on Studio's MCP port. Every StudioMCP.exe we
    launch afterward - even a freshly restarted one - just connects to that
    zombie instead of a real Studio, so the bridge reports "Studio connected"
    forever even with Studio fully closed. studio_watch's auto-restart cannot
    fix this on its own: restarting our proxy still lands on the same zombie.

    Only acts when NO real Studio app is running at all - in that state any
    existing StudioMCP.exe is unambiguously orphaned (a legitimate one only
    exists to serve a live Studio), so it is safe to auto-kill without asking.
    If Studio IS running (or this can't be determined), this is a no-op: a
    live StudioMCP.exe might be legitimately serving it, so nothing is
    touched - this must never risk killing a working connection.
    """
    if sys.platform != "win32":
        return
    if _roblox_studio_app_running() is not False:
        return
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq StudioMCP.exe"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=8,
        ).stdout
    except Exception:
        return
    if "StudioMCP.exe" not in out:
        return
    log("Found leftover StudioMCP.exe process(es) with no Roblox Studio running - "
        "cleaning them up (known cause of a phantom 'Studio connected' state).", "yl")
    try:
        subprocess.run(["taskkill", "/F", "/IM", "StudioMCP.exe"],
                       capture_output=True, text=True, timeout=8)
    except Exception as e:
        log(f"could not clean up orphaned StudioMCP.exe: {e}", "rd")


def _descendant_pids(root_pid):
    """Set of PIDs = root_pid + every descendant, or None if the process tree
    could not be read (in which case callers must NOT make kill decisions)."""
    if sys.platform != "win32":
        return None
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process | ForEach-Object "
             "{ \"$($_.ProcessId) $($_.ParentProcessId)\" }"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=10,
        ).stdout
    except Exception:
        return None
    children = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            children.setdefault(int(parts[1]), []).append(int(parts[0]))
    if not children:
        return None
    pids = {int(root_pid)}
    stack = [int(root_pid)]
    while stack:
        for c in children.get(stack.pop(), []):
            if c not in pids:
                pids.add(c)
                stack.append(c)
    return pids


def _reclaim_studio_port(client):
    """Kill a StudioMCP.exe that owns Studio's MCP port but is NOT our own child.

    The deadlock this breaks (reported live, survives every restart combo):
    a zombie StudioMCP.exe from a crashed session keeps LISTENING on 13469.
    The user reopens Studio -> its MCP plugin does its ONE-SHOT registration
    against the ZOMBIE (wasted). The user restarts the bridge -> Studio is now
    running, so _kill_orphan_studio_mcp's safety guard skips the cleanup, and
    check_studio_port waves the zombie through too (its path IS under Roblox).
    Our fresh StudioMCP can't own the port, Studio never re-registers on its
    own -> 0 tools forever, no restart order can fix it by hand.

    Ownership is decided by PID, not heuristics: we know the PID of the
    launcher we spawned (client.proc), so a StudioMCP.exe holding the port
    outside that process tree is a leftover by definition - Studio open or
    not. If the process tree can't be read, we do nothing (never risk killing
    our own healthy child on bad data). Returns True if a zombie was killed;
    the caller must then restart the roblox proxy (safe here even with Studio
    open: the plugin's single registration already went to the zombie, so
    there is no attempt left for a restart to collide with) AND tell the user
    to open Assistant Settings > MCP Servers so the plugin re-registers.
    """
    owner = _port_owner(STUDIO_MCP_PORT)
    if not owner:
        return False
    pid, name, path = owner
    # Only ever kill a StudioMCP.exe. Studio itself holding the port is fine;
    # a non-Roblox squatter is check_studio_port's (interactive) job.
    if "studiomcp" not in (name or "").lower():
        return False
    try:
        pid_i = int(pid)
    except (TypeError, ValueError):
        return False
    if client is not None and client.proc is not None and client.is_alive():
        tree = _descendant_pids(client.proc.pid)
        if tree is None or pid_i in tree:
            return False  # ours, or unknowable - leave it alone
    log(f"port {STUDIO_MCP_PORT} is held by a StudioMCP.exe (pid {pid_i}) that this "
        "bridge did NOT launch - a leftover from a previous session. Studio "
        "registered to it, so our proxy sees 0 tools.", "yl")
    try:
        subprocess.run(["taskkill", "/F", "/PID", str(pid_i)],
                       capture_output=True, text=True, timeout=8)
    except Exception as e:
        log(f"could not kill the leftover StudioMCP.exe: {e}", "rd")
        return False
    log(f"killed the leftover StudioMCP.exe (pid {pid_i}) to free Studio's MCP port.", "cy")
    return True


def _process_cmdline(pid):
    """Full command line of `pid`, or "" if it can't be read. Win32 only.

    Used to tell OUR OWN kind of process (a python running bridge.py) apart
    from an unrelated app that merely happens to listen on the same port -
    the process NAME is just "python"/"py"/"pythonw", far too generic to kill
    on. The command line is what proves it is a leftover bridge."""
    if sys.platform != "win32":
        return ""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-CimInstance Win32_Process -Filter \"ProcessId={pid}\" "
             f"-ErrorAction SilentlyContinue).CommandLine"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=8,
        ).stdout
    except Exception:
        return ""
    return (out or "").strip()


def _reclaim_bridge_port():
    """Free OUR OWN listen port (17613) from a leftover bridge before we bind.

    The common failure (reported live, WinError 10048 on bind): the user
    relaunches start.bat while an earlier bridge.py is still running - window
    closed with the X instead of Ctrl+C, a previous crash that left a detached
    python, or a double double-click. The old process still holds the port, so
    websockets.serve() dies on bind with a cryptic (localised) OSError and the
    whole bridge exits code 1.

    We reuse _port_owner (already generic over the port) and only ever kill a
    process we can PROVE is another bridge.py - never a same-name innocent
    (some unrelated python listening on 17613): the guard is the command line
    containing "bridge.py", plus an explicit self-exclusion by PID. Anything
    else (a non-python app, or a python whose cmdline we can't read) is left
    alone and surfaced to the user by the caller's friendly bind-error path.
    Returns True if a leftover bridge was killed."""
    owner = _port_owner(PORT)
    if not owner:
        return False
    pid, name, path = owner
    try:
        pid_i = int(pid)
    except (TypeError, ValueError):
        return False
    if pid_i == os.getpid():
        return False  # never kill ourselves (defensive; we haven't bound yet)
    # Must look like a python interpreter AND be running bridge.py. Killing on
    # the port alone would murder whatever legitimately owns 17613.
    if "python" not in (name or "").lower() and "py" != (name or "").lower():
        return False
    cmdline = _process_cmdline(pid_i)
    if "bridge.py" not in cmdline.lower():
        log(f"port {PORT} is held by pid {pid_i} ('{name}') but it does not look "
            f"like a Multi-Script bridge - leaving it alone.", "yl")
        return False
    log(f"port {PORT} is held by a leftover Multi-Script bridge (pid {pid_i}) from a "
        "previous session - killing it so this one can start.", "yl")
    try:
        subprocess.run(["taskkill", "/F", "/PID", str(pid_i)],
                       capture_output=True, text=True, timeout=8)
    except Exception as e:
        log(f"could not kill the leftover bridge (pid {pid_i}): {e}", "rd")
        return False
    log(f"killed the leftover bridge (pid {pid_i}); the port is free now.", "cy")
    return True


def _kill_port_squatter():
    """Kill a NON-Roblox process holding Studio's MCP port, no questions asked.

    Called only when the child's stderr has PROVEN the port is hijacked (see
    MCPClient.saw_foreign_ws_host - StudioMCP connected to a foreign host and
    could not parse its protocol; the ropilot case). At that point there is no
    ambiguity left to justify check_studio_port's interactive prompt, and the
    prompt was itself a trap: many users never answer it, and the one-shot boot
    check often runs a beat before a background helper (ropilot) grabs the
    port. Here we have hard evidence, so kill the squatter outright. Returns
    (killed, name) so the caller can tell the user which app to uninstall /
    remove from startup, since it will otherwise reclaim the port on next boot.
    """
    owner = _port_owner(STUDIO_MCP_PORT)
    if owner:
        pid, name, path = owner
        if "roblox" in (path or "").lower() or "studiomcp" in (name or "").lower():
            return False, None  # legitimate Studio-side owner; not a squatter
        log(f"port {STUDIO_MCP_PORT} is hijacked by '{name}' (pid {pid}, {path}).", "yl")
        log("    StudioMCP connected to it instead of Roblox Studio - that is why "
            "there are 0 tools.", "yl")
        try:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True, text=True, timeout=8)
        except Exception as e:
            log(f"could not kill '{name}': {e}", "rd")
            return False, name
        log(f"killed '{name}' so Studio can use the port.", "cy")
        return True, name
    # We could NOT resolve who owns the port, yet StudioMCP's stderr proved the
    # port is hijacked (this function is only called under that proof). This is
    # the state that used to fail SILENTLY: _port_owner returning None (e.g. a
    # squatter listening on IPv6 loopback that an IPv4-only netstat missed, or
    # any netstat quirk) left the user staring at 0 tools with no explanation.
    # Never be silent here. Try a name-based fallback for the known offender
    # (ropilot ships a background helper that squats this port), then always
    # tell the user what we know.
    log(f"port {STUDIO_MCP_PORT} is hijacked (StudioMCP could not talk to Roblox "
        "Studio on it) but the owning process could not be identified by port.", "yl")
    # ropilot is a multi-process app (validated live 2026-07-13): the port is
    # held by ropilot-infra-helper.exe, supervised by ropilot-infra.exe. Kill
    # both so the supervisor can't just respawn the helper and re-grab the port.
    killed_name = None
    for img in ("ropilot-infra-helper.exe", "ropilot-infra.exe", "ropilot.exe"):
        try:
            res = subprocess.run(["taskkill", "/F", "/IM", img],
                                 capture_output=True, text=True, timeout=8)
        except Exception:
            continue
        if res.returncode == 0:
            killed_name = img
            log(f"killed '{img}' (known port squatter) so Studio can use the port.", "cy")
    if killed_name:
        return True, killed_name
    log("    Could not auto-kill it. Find it manually: run  netstat -ano | "
        f"findstr {STUDIO_MCP_PORT}  then end that PID in Task Manager.", "yl")
    return False, None


def _print_squatter_hint(name):
    """After killing a port squatter (e.g. ropilot), tell the user how to stop
    it coming back - it is a background helper that respawns on the next boot
    and re-grabs the port before Studio, which is why a PC reboot never fixed
    this class of 0-tools report."""
    app = name or "the other app"
    action_banner([
        f"'{app}' fights Roblox Studio for its connection - it will keep",
        "coming back after every restart until you remove it.",
        f"1. Uninstall '{app}' (or remove it from Windows startup).",
        "2. In Roblox Studio: Assistant Settings > MCP Servers,",
        "   turn OFF then back ON 'Enable Studio as MCP server'.",
    ])


def _print_reregister_hint():
    """The one user action that completes a zombie-kill recovery: Studio's MCP
    plugin registers only once per boot and that attempt went to the zombie,
    so after the kill + proxy restart the user must make it register again."""
    # Opening the panel alone is technically enough to re-register, but we tell
    # the user to toggle OFF/ON to be sure - a toggle strictly implies opening
    # the panel, so it can never do less, and it removes any ambiguity about
    # whether "just looking at it" counted. Same wording as the squatter/no-place
    # banners so all three read as one identical instruction, not three variants.
    action_banner([
        "Go to Roblox Studio now.",
        "Turn OFF then back ON: Assistant Settings > MCP Servers",
        "         > 'Enable Studio as MCP server'",
        "Wait about 10 seconds - this window will turn green.",
    ])


def check_studio_port():
    """Warn (and optionally kill) a NON-Roblox process squatting Studio's MCP port.

    A third-party tool (e.g. "ropilot") that binds 13469 before Studio does
    hijacks the MCP channel: StudioMCP connects to IT instead of Studio, the
    handshake succeeds but tools/list never answers -> the bridge sees 0 tools.
    This is silent and brutal to diagnose, so we surface it up front.
    """
    owner = _port_owner(STUDIO_MCP_PORT)
    if not owner:
        return False
    pid, name, path = owner
    # The legitimate holder is Studio itself / a Roblox helper: its path lives
    # under a "...\Roblox\..." folder. Anything else is an intruder.
    if "roblox" in (path or "").lower():
        return False
    where = path or name
    log(f"port {STUDIO_MCP_PORT} (Studio's MCP port) is held by a non-Roblox process:", "yl")
    log(f"    {name} (pid {pid})  {where}", "yl")
    log("    This will block Studio's tools (the bridge will see 0 tools).", "yl")
    try:
        ans = input("    Kill this process so Studio can use the port? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        ans = ""
    if ans in ("y", "yes", "o", "oui"):
        try:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True, text=True, timeout=8)
            log(f"killed {name} (pid {pid}). Studio can use the port now.", "cy")
            # Tell the user the finishing step IMMEDIATELY, here, instead of only
            # after the ~48s server-launch grace loop that follows: killing the
            # squatter frees the port, but Studio's MCP plugin registers only
            # once per boot and that attempt already went to the squatter, so it
            # will NOT re-attach on its own - a toggle is needed. Printing this
            # now (not 48s later, after start_all's grace loop) is what turns a
            # ~1-minute "why is nothing happening" wait into an act-right-away
            # instruction. Uses action_banner (not log) so a non-technical user
            # visually cannot miss it among the surrounding status lines - seen
            # live indistinguishable when both used the same plain color.
            action_banner([
                "Go to Roblox Studio now.",
                "Turn OFF then back ON: Assistant Settings > MCP Servers",
                "         > 'Enable Studio as MCP server'",
                "Wait about 10 seconds - this window will turn green.",
            ])
            return True  # a squatter WAS killed -> Studio must reclaim the port
        except Exception as e:
            log(f"could not kill it: {e}", "rd")
    else:
        log("left it running. Close it yourself, then restart the bridge.", "yl")
    return False


_TRANSIENT_STUDIO_MARKERS = (
    "no roblox studio instance", "no active studio", "studio instance is connect",
    "studio instance connected", "not connected to", "no studio instance",
)


def _looks_like_transient_studio_drop(text):
    low = (text or "").lower()
    return any(m in low for m in _TRANSIENT_STUDIO_MARKERS)


# ── config.json read / write (for extension-driven add/remove) ──────────────
def _read_config():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            if isinstance(cfg, dict):
                cfg.setdefault("mcpServers", {})
                return cfg
        except Exception as e:
            log(f"config.json unreadable ({e}) - starting from a fresh one", "yl")
    return {"mcpServers": {PRIMARY_SERVER_ID: {"command": "launch_studio_mcp.py", "args": []}}}


def _write_config(cfg):
    """Atomic write so a crash mid-write never leaves a truncated config.json."""
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG_PATH)


def config_add_server(server_id, command, args=None, env=None):
    """Add/replace an addon server in config.json. Refuses to touch the primary
    (roblox) server. Returns (ok, error)."""
    sid = (server_id or "").strip()
    if not sid:
        return False, "server id is required"
    if sid == PRIMARY_SERVER_ID:
        return False, f"'{PRIMARY_SERVER_ID}' is the primary server and cannot be edited"
    if not (command or "").strip():
        return False, "a command is required"
    cfg = _read_config()
    spec = {"command": command.strip(), "args": list(args or [])}
    if env:
        spec["env"] = dict(env)
    cfg["mcpServers"][sid] = spec
    try:
        _write_config(cfg)
    except Exception as e:
        return False, f"could not write config.json: {e}"
    return True, None


def config_remove_server(server_id):
    """Remove an addon server from config.json. Refuses the primary server."""
    sid = (server_id or "").strip()
    if sid == PRIMARY_SERVER_ID:
        return False, f"'{PRIMARY_SERVER_ID}' is the primary server and cannot be removed"
    cfg = _read_config()
    if sid not in cfg.get("mcpServers", {}):
        return False, f"server '{sid}' is not in the config"
    del cfg["mcpServers"][sid]
    try:
        _write_config(cfg)
    except Exception as e:
        return False, f"could not write config.json: {e}"
    return True, None


def restart_self():
    """Replace this process with a fresh one so config.json is reloaded from
    scratch. Children are killed first to free their stdio pipes / ports before
    the new instance claims them. Never returns on success (os.execv)."""
    log("restarting bridge to load new server config...", "yl")
    try:
        for c in mgr.clients.values():
            c.stop()
    except Exception:
        pass
    if _log_file:
        try:
            _log_file.flush()
        except Exception:
            pass
    # sys.argv[0] may be relative ('bridge.py'); make it absolute so the restart
    # works regardless of the current working directory.
    argv = list(sys.argv)
    script = os.path.abspath(argv[0]) if argv else os.path.abspath(__file__)
    argv = [script] + argv[1:]
    try:
        os.execv(sys.executable, [sys.executable] + argv)
    except Exception as e:
        # execv failed (rare) - fall back to spawning a detached copy and exiting
        # so the user still ends up with a running, up-to-date bridge.
        log(f"in-place restart failed ({e}); spawning a fresh bridge...", "rd")
        try:
            subprocess.Popen([sys.executable] + argv, cwd=HERE)
        except Exception as e2:
            log(f"could not spawn a fresh bridge: {e2} - please restart it manually", "rd")
        os._exit(0)


# ── Multi-Script built-in tools and reusable skills ────────────────────────────
SKILLS_PATH = os.path.join(HERE, "skills.json")


def _load_skills():
    try:
        with open(SKILLS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        log(f"skills.json unreadable: {e}", "yl")
        return {}

STUDIO_STANDARD_PATH = os.path.join(HERE, "studio-standard.json")

def _load_studio_standard():
    try:
        with open(STUDIO_STANDARD_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        log(f"studio-standard.json unreadable: {e}", "yl")
        return {}

QUALITY_PRESETS_PATH = os.path.join(HERE, "quality-presets.json")

def _load_quality_presets():
    try:
        with open(QUALITY_PRESETS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        presets = data.get("presets", {}) if isinstance(data, dict) else {}
        return presets if isinstance(presets, dict) else {}
    except Exception as e:
        log(f"quality-presets.json unreadable: {e}", "yl")
        return {}

VIRTUAL_TOOLS_PATH = os.path.join(HERE, "virtual-tools.json")

def _load_virtual_tools():
    try:
        with open(VIRTUAL_TOOLS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        tools = data.get("tools", {}) if isinstance(data, dict) else {}
        return tools if isinstance(tools, dict) else {}
    except Exception as e:
        log(f"virtual-tools.json unreadable: {e}", "yl")
        return {}

_AUDIO_MODULE = None

def _elevenlabs_audio():
    global _AUDIO_MODULE
    if _AUDIO_MODULE is None:
        import importlib.util
        path = os.path.join(HERE, "elevenlabs_audio.py")
        spec = importlib.util.spec_from_file_location("ms_elevenlabs_audio", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _AUDIO_MODULE = module
    return _AUDIO_MODULE


ADVANCED_DIRECT_CONTRACTS = {'ms_camera_system_design': {'description': 'Design and validate a production camera system for gameplay, cinematics, collision, comfort, and every target input.', 'stages': ['camera goals and player readability', 'camera state graph and ownership', 'follow/orbit/aim behavior', 'collision and occlusion recovery', 'smoothing, damping and shake layers', 'input-device sensitivity and inversion', 'accessibility and motion comfort', 'runtime implementation and edge-case capture'], 'qualityGates': ['no clipping in representative spaces', 'stable transitions between every state', 'target lock and aim remain readable', 'mouse, controller and touch feel consistent', 'reduced-motion option is respected', 'camera never steals gameplay authority', 'frame-time cost stays within budget', 'runtime evidence covers worst cases']}, 'ms_input_mapping_plan': {'description': 'Create a cross-platform input architecture with rebinding, action maps, prompts, focus, and accessibility.', 'stages': ['inventory all player actions', 'separate semantic actions from device inputs', 'define gameplay and UI contexts', 'map keyboard, mouse, controller and touch', 'design rebinding and conflict resolution', 'implement device-change prompts', 'support hold/toggle and assist alternatives', 'test hot-plug, focus and persistence'], 'qualityGates': ['every required action is reachable', 'contexts cannot leak conflicting actions', 'rebinding survives restart', 'prompts update with active device', 'controller navigation has no dead ends', 'touch targets meet size requirements', 'disconnect and reconnect recover cleanly', 'accessibility alternatives are verified']}, 'ms_character_controller_review': {'description': 'Review or design a responsive character controller covering movement, slopes, jumps, grounding, networking, and animation integration.', 'stages': ['define movement fantasy and metrics', 'inspect grounding and collision model', 'design acceleration and air control', 'handle slopes, steps and moving platforms', 'spec jump buffering and coyote time', 'integrate animation and root motion policy', 'define network ownership and reconciliation', 'instrument and test edge geometry'], 'qualityGates': ['no tunneling or slope jitter', 'ground state is deterministic', 'jump behavior matches metrics', 'moving platforms preserve relative motion', 'animation never fights controller authority', 'latency does not create obvious warps', 'controller works at low and high frame rates', 'edge-case test scene passes']}, 'ms_combat_system_design': {'description': 'Create a complete combat architecture with timing, hit validation, damage, status effects, feedback, networking, and balance hooks.', 'stages': ['define combat pillars and verbs', 'spec attacks, cancels and state ownership', 'design hitboxes, hurtboxes and targeting', 'define authoritative damage pipeline', 'add cooldown, resource and status rules', 'synchronize animation, VFX and audio', 'instrument balance and abuse telemetry', 'implement test encounters and failure cases'], 'qualityGates': ['server validates consequential hits', 'timing windows match animation evidence', 'duplicate damage is impossible', 'invulnerability and interruption are explicit', 'feedback communicates cause and result', 'latency behavior is fair and bounded', 'balance variables are data-driven', 'automated and two-player tests pass']}, 'ms_ai_behavior_design': {'description': 'Design game AI with perception, decision making, navigation, combat tactics, recovery, debugging, and performance budgets.', 'stages': ['define behavior goals and difficulty envelope', 'design perception and memory', 'choose state tree, behavior tree or planner', 'spec navigation and spatial queries', 'add group tactics and reservations', 'handle interruption, failure and recovery', 'build debug visualization and telemetry', 'stress-test populations and edge cases'], 'qualityGates': ['AI decisions are explainable in debug view', 'perception respects occlusion and ranges', 'navigation recovers from blocked paths', 'states cannot deadlock', 'difficulty changes behavior not cheating', 'group agents avoid pathological crowding', 'CPU cost stays within population budget', 'seeded scenarios are repeatable']}, 'ms_quest_system_design': {'description': 'Design a data-driven quest, objective, dialogue, reward, persistence, and migration system.', 'stages': ['define quest grammar and lifecycle', 'model objectives and dependency graphs', 'separate definitions from player state', 'design triggers, dialogue and world reactions', 'validate rewards and idempotency', 'persist and migrate progress', 'build authoring and debug tools', 'test abandonment, replay and branching'], 'qualityGates': ['cycles and impossible objectives are detected', 'rewards cannot duplicate', 'progress survives save migration', 'branch conditions are deterministic', 'multiplayer ownership is explicit', 'localization text is externalized', 'debug tooling exposes current blockers', 'representative quest chains pass']}, 'ms_inventory_economy_design': {'description': 'Design inventory and economy systems with stacking, transactions, crafting, shops, persistence, anti-duplication, and tuning.', 'stages': ['define item schema and identifiers', 'spec slots, stacks, capacity and equipment', 'design atomic transaction boundaries', 'model currencies, sources and sinks', 'add crafting, loot and shop rules', 'persist with idempotent reconciliation', 'instrument economy and exploit telemetry', 'simulate progression and boundary cases'], 'qualityGates': ['transactions are atomic', 'duplicate and negative balances are impossible', 'server validates grants and purchases', 'unknown item versions migrate safely', 'UI state matches authoritative data', 'sources and sinks remain measurable', 'concurrency tests preserve invariants', 'rollback and repair paths exist']}, 'ms_procedural_generation_plan': {'description': 'Create a deterministic procedural-generation pipeline with seeds, constraints, validation, streaming, and authored overrides.', 'stages': ['define generated space and invariants', 'choose seed and random-stream ownership', 'separate topology, content and decoration passes', 'encode constraints and solvability checks', 'add authored anchors and overrides', 'design streaming and regeneration boundaries', 'build debug maps and reproduction tools', 'run statistical and adversarial seeds'], 'qualityGates': ['same seed reproduces exactly', 'all outputs satisfy hard constraints', 'critical paths remain solvable', 'generation has bounded retries', 'streaming preserves seams and state', 'authored content composes cleanly', 'worst-case generation meets time budget', 'failed seeds are logged and reproducible']}, 'ms_shader_production': {'description': 'Create an engine-ready shader production contract covering visual intent, lighting, variants, parameters, optimization, and fallback tiers.', 'stages': ['define visual target and reference lighting', 'choose surface, post, compute or screen-space path', 'design parameter and texture interface', 'implement lighting and transparency behavior', 'add animation and gameplay controls', 'build quality tiers and fallbacks', 'profile variants, overdraw and instruction cost', 'validate across cameras and target hardware'], 'qualityGates': ['shader matches target under representative lighting', 'parameters have safe documented ranges', 'transparent sorting artifacts are controlled', 'variants do not explode build size', 'mobile or low-tier fallback exists', 'no NaN, flicker or precision failure', 'GPU cost meets budget', 'runtime captures prove behavior']}, 'ms_lighting_production': {'description': 'Design and implement cohesive game lighting with readability, mood, probes, shadows, post-processing, and performance tiers.', 'stages': ['set mood, value structure and focal hierarchy', 'choose baked, mixed and dynamic ownership', 'place key, fill and practical sources', 'configure shadows and contact grounding', 'build probes, reflection and GI coverage', 'tune exposure, fog and post effects', 'create time or state transitions', 'profile and capture every quality tier'], 'qualityGates': ['gameplay targets remain readable', 'exposure is stable across transitions', 'characters ground correctly', 'probe seams are not visible', 'shadow cost meets budget', 'post effects preserve UI and accessibility', 'low tier retains art direction', 'target-device captures are approved']}, 'ms_environment_production': {'description': 'Create a complete environment-production contract from blockout through modular assets, dressing, lighting, gameplay readability, and optimization.', 'stages': ['define player journey and landmarks', 'block out scale, metrics and sightlines', 'design modular kit and reuse rules', 'create hero assets and material language', 'dress with hierarchy and storytelling', 'integrate collision, navigation and gameplay', 'light and add atmosphere', 'optimize streaming, LOD and occlusion'], 'qualityGates': ['scale supports movement metrics', 'critical routes and landmarks read clearly', 'modular seams are controlled', 'collision and navigation match visuals', 'asset density follows focal hierarchy', 'memory and draw-call budgets pass', 'lighting supports gameplay states', 'runtime walkthrough has clean logs']}, 'ms_character_model_production': {'description': 'Create a character-model pipeline covering concept translation, topology, UVs, materials, facial needs, LODs, rig handoff, and engine validation.', 'stages': ['lock silhouette, proportions and technical target', 'sculpt or model primary forms', 'build deformation-aware topology', 'create UV and texel-density plan', 'author materials and texture sets', 'prepare facial and customization requirements', 'generate LOD and optimization variants', 'handoff to rig and validate in engine'], 'qualityGates': ['silhouette reads at gameplay distance', 'topology supports required deformation', 'normals and UVs are clean', 'materials match art direction', 'LOD transitions are acceptable', 'scale, axes and pivots import correctly', 'memory and triangle budgets pass', 'test poses reveal no blocking defects']}, 'ms_rigging_production': {'description': 'Create a robust rigging contract for skeletons, skinning, controls, constraints, retargeting, physics, export, and engine tests.', 'stages': ['define skeleton and deformation requirements', 'build hierarchy, orientation and naming', 'skin and tune weight distribution', 'create animator controls and constraints', 'add IK, twist, facial and secondary systems', 'prepare retarget and avatar mapping', 'build export bake and validation preset', 'test extreme poses and engine playback'], 'qualityGates': ['hierarchy exports without helper leakage', 'joint orientation is consistent', 'weights meet influence limits', 'extreme poses avoid major collapse', 'IK and constraints bake deterministically', 'retargeting preserves proportions', 'physics layers do not fight animation', 'engine import and clips pass']}, 'ms_localization_audit': {'description': 'Audit localization readiness across text, fonts, layouts, input, audio, pluralization, cultural context, and QA.', 'stages': ['inventory all player-facing content', 'externalize strings and stable keys', 'define locale, plural and grammar rules', 'verify fonts and shaping coverage', 'design expansion and bidirectional layouts', 'localize assets, audio and controls', 'build pseudo-localization workflow', 'test saves, networking and fallback locale'], 'qualityGates': ['no hard-coded player text remains', 'missing keys fall back safely', '200-percent expansion does not break UI', 'RTL and complex scripts render correctly', 'fonts cover supported glyphs', 'variables preserve grammar and order', 'localized assets respect memory budgets', 'locale switching and persistence pass']}, 'ms_telemetry_plan': {'description': 'Create a privacy-aware telemetry specification with events, schemas, funnels, diagnostics, sampling, dashboards, and validation.', 'stages': ['define product questions and decisions', 'map player journey and failure points', 'spec event names and ownership', 'define typed properties and versioning', 'set privacy, consent and retention rules', 'add sampling and offline buffering', 'build dashboards and alert thresholds', 'validate events against real sessions'], 'qualityGates': ['every event answers a named question', 'schemas reject malformed payloads', 'PII is excluded or explicitly governed', 'duplicate events are controlled', 'offline and retry behavior is bounded', 'version changes remain queryable', 'dashboards expose actionable thresholds', 'test sessions reconcile end to end']}, 'ms_liveops_plan': {'description': 'Plan safe live operations covering configuration, events, segmentation, rollout, economy protection, moderation, observability, and rollback.', 'stages': ['define cadence and player value', 'separate remote config from code', 'design event and reward schemas', 'create segmentation and eligibility rules', 'add staged rollout and kill switches', 'protect economy and entitlement flows', 'instrument health and participation', 'prepare moderation, support and rollback'], 'qualityGates': ['events use server-authoritative time', 'rewards are idempotent', 'config is validated before activation', 'segments cannot expose restricted content', 'kill switch works without client update', 'economy impact has guardrails', 'support can diagnose player state', 'rollback rehearsal succeeds']}, 'ms_store_compliance_check': {'description': 'Generate a platform-store compliance and submission gate for privacy, monetization, content ratings, permissions, packaging, and review evidence.', 'stages': ['identify target storefront requirements', 'audit privacy disclosures and data flows', 'review purchases, ads and subscriptions', 'verify age rating and content declarations', 'minimize permissions and capabilities', 'validate package identity and signing', 'prepare screenshots, metadata and support links', 'run clean-install submission build'], 'qualityGates': ['privacy labels match actual behavior', 'restore purchases works where required', 'parental and regional rules are covered', 'permissions are justified', 'rating answers match shipped content', 'package and signing are reproducible', 'review account and instructions are valid', 'submission artifact passes smoke test']}, 'ms_security_abuse_review': {'description': 'Review a game feature for trust boundaries, secrets, permissions, injection, cheating, moderation, privacy, and incident response.', 'stages': ['map assets, actors and trust boundaries', 'identify sensitive data and secrets', 'audit input validation and authorization', 'review network and persistence abuse cases', 'add rate limits and replay protection', 'design moderation and privacy controls', 'instrument detection and incident evidence', 'test adversarial and recovery scenarios'], 'qualityGates': ['secrets never ship to clients', 'server validates consequential actions', 'least privilege is enforced', 'untrusted content is escaped or sandboxed', 'abuse limits are measurable', 'privacy controls match policy', 'security logs avoid sensitive leakage', 'repair and incident paths are tested']}, 'ms_bug_triage': {'description': 'Turn a bug report into a reproducible, prioritized engineering investigation with hypotheses, instrumentation, fix scope, and verification.', 'stages': ['normalize symptoms and expected behavior', 'capture build, platform and environment', 'minimize deterministic reproduction steps', 'collect logs, state and timing evidence', 'rank hypotheses by evidence', 'isolate regression range and ownership', 'define smallest safe fix', 'write verification and non-regression cases'], 'qualityGates': ['reproduction is specific and repeatable', 'severity and player impact are justified', 'evidence separates cause from correlation', 'fix does not hide symptoms only', 'related systems are inspected', 'regression test fails before the fix', 'target platforms pass after fix', 'release and rollback notes exist']}, 'ms_regression_plan': {'description': 'Create a risk-based regression plan covering changed surfaces, dependencies, automated checks, manual journeys, platforms, performance, and rollback.', 'stages': ['enumerate changes and dependency graph', 'rank regression risks', 'select automated unit and integration checks', 'define critical manual player journeys', 'cover save, network and compatibility paths', 'test device and quality tiers', 'compare performance baselines', 'record release decision and rollback trigger'], 'qualityGates': ['changed behavior has direct tests', 'dependent systems have targeted coverage', 'critical journey passes from clean state', 'old saves and content remain compatible', 'multiplayer version behavior is defined', 'performance has no material regression', 'failures produce actionable evidence', 'rollback threshold is explicit']}}


ADVANCED_DIRECT_CONTRACTS.update({'ms_dialogue_production': {'description': 'Dialogue production: branching dialogue, conditions, localization, VO hooks and runtime validation.', 'stages': ['Define dialogue production goals and measurable acceptance criteria', 'Inspect current project architecture, content and constraints', 'Design the data model, ownership and lifecycle for branching dialogue, conditions, localization, VO hooks and runtime validation', 'Implement the smallest complete vertical path in the target engine', 'Integrate UI, input, audio, VFX and persistence where relevant', 'Handle failure, interruption, migration and multiplayer edge cases', 'Profile target-platform performance and add observability', 'Run focused tests, read back state and capture verification evidence'], 'qualityGates': ['The requested result exists in the real target project', 'Authority and ownership are explicit and secure', 'Happy, edge, failure and interruption paths are covered', 'Configuration is data-driven and documented', 'Accessibility and all target inputs are considered', 'Performance is measured against an explicit budget', 'Automated or repeatable tests pass with clean logs', 'Final state is read back and supported by concrete evidence']}, 'ms_cinematic_production': {'description': 'Cinematic production: storyboards, cameras, animation, timing, audio, skip/replay and runtime capture.', 'stages': ['Define cinematic production goals and measurable acceptance criteria', 'Inspect current project architecture, content and constraints', 'Design the data model, ownership and lifecycle for storyboards, cameras, animation, timing, audio, skip/replay and runtime capture', 'Implement the smallest complete vertical path in the target engine', 'Integrate UI, input, audio, VFX and persistence where relevant', 'Handle failure, interruption, migration and multiplayer edge cases', 'Profile target-platform performance and add observability', 'Run focused tests, read back state and capture verification evidence'], 'qualityGates': ['The requested result exists in the real target project', 'Authority and ownership are explicit and secure', 'Happy, edge, failure and interruption paths are covered', 'Configuration is data-driven and documented', 'Accessibility and all target inputs are considered', 'Performance is measured against an explicit budget', 'Automated or repeatable tests pass with clean logs', 'Final state is read back and supported by concrete evidence']}, 'ms_tutorial_design': {'description': 'Tutorial design: onboarding, progressive disclosure, practice, recovery, telemetry and accessibility.', 'stages': ['Define tutorial design goals and measurable acceptance criteria', 'Inspect current project architecture, content and constraints', 'Design the data model, ownership and lifecycle for onboarding, progressive disclosure, practice, recovery, telemetry and accessibility', 'Implement the smallest complete vertical path in the target engine', 'Integrate UI, input, audio, VFX and persistence where relevant', 'Handle failure, interruption, migration and multiplayer edge cases', 'Profile target-platform performance and add observability', 'Run focused tests, read back state and capture verification evidence'], 'qualityGates': ['The requested result exists in the real target project', 'Authority and ownership are explicit and secure', 'Happy, edge, failure and interruption paths are covered', 'Configuration is data-driven and documented', 'Accessibility and all target inputs are considered', 'Performance is measured against an explicit budget', 'Automated or repeatable tests pass with clean logs', 'Final state is read back and supported by concrete evidence']}, 'ms_navigation_system_design': {'description': 'Navigation system design: navigation meshes, agents, links, avoidance, streaming and recovery.', 'stages': ['Define navigation system design goals and measurable acceptance criteria', 'Inspect current project architecture, content and constraints', 'Design the data model, ownership and lifecycle for navigation meshes, agents, links, avoidance, streaming and recovery', 'Implement the smallest complete vertical path in the target engine', 'Integrate UI, input, audio, VFX and persistence where relevant', 'Handle failure, interruption, migration and multiplayer edge cases', 'Profile target-platform performance and add observability', 'Run focused tests, read back state and capture verification evidence'], 'qualityGates': ['The requested result exists in the real target project', 'Authority and ownership are explicit and secure', 'Happy, edge, failure and interruption paths are covered', 'Configuration is data-driven and documented', 'Accessibility and all target inputs are considered', 'Performance is measured against an explicit budget', 'Automated or repeatable tests pass with clean logs', 'Final state is read back and supported by concrete evidence']}, 'ms_physics_system_audit': {'description': 'Physics system audit: collision layers, fixed-step behavior, ownership, determinism and profiling.', 'stages': ['Define physics system audit goals and measurable acceptance criteria', 'Inspect current project architecture, content and constraints', 'Design the data model, ownership and lifecycle for collision layers, fixed-step behavior, ownership, determinism and profiling', 'Implement the smallest complete vertical path in the target engine', 'Integrate UI, input, audio, VFX and persistence where relevant', 'Handle failure, interruption, migration and multiplayer edge cases', 'Profile target-platform performance and add observability', 'Run focused tests, read back state and capture verification evidence'], 'qualityGates': ['The requested result exists in the real target project', 'Authority and ownership are explicit and secure', 'Happy, edge, failure and interruption paths are covered', 'Configuration is data-driven and documented', 'Accessibility and all target inputs are considered', 'Performance is measured against an explicit budget', 'Automated or repeatable tests pass with clean logs', 'Final state is read back and supported by concrete evidence']}, 'ms_vehicle_system_design': {'description': 'Vehicle system design: handling, suspension, controls, camera, networking, damage and tuning.', 'stages': ['Define vehicle system design goals and measurable acceptance criteria', 'Inspect current project architecture, content and constraints', 'Design the data model, ownership and lifecycle for handling, suspension, controls, camera, networking, damage and tuning', 'Implement the smallest complete vertical path in the target engine', 'Integrate UI, input, audio, VFX and persistence where relevant', 'Handle failure, interruption, migration and multiplayer edge cases', 'Profile target-platform performance and add observability', 'Run focused tests, read back state and capture verification evidence'], 'qualityGates': ['The requested result exists in the real target project', 'Authority and ownership are explicit and secure', 'Happy, edge, failure and interruption paths are covered', 'Configuration is data-driven and documented', 'Accessibility and all target inputs are considered', 'Performance is measured against an explicit budget', 'Automated or repeatable tests pass with clean logs', 'Final state is read back and supported by concrete evidence']}, 'ms_world_streaming_plan': {'description': 'World streaming plan: partitioning, loading, persistence, seams, budgets and failure recovery.', 'stages': ['Define world streaming plan goals and measurable acceptance criteria', 'Inspect current project architecture, content and constraints', 'Design the data model, ownership and lifecycle for partitioning, loading, persistence, seams, budgets and failure recovery', 'Implement the smallest complete vertical path in the target engine', 'Integrate UI, input, audio, VFX and persistence where relevant', 'Handle failure, interruption, migration and multiplayer edge cases', 'Profile target-platform performance and add observability', 'Run focused tests, read back state and capture verification evidence'], 'qualityGates': ['The requested result exists in the real target project', 'Authority and ownership are explicit and secure', 'Happy, edge, failure and interruption paths are covered', 'Configuration is data-driven and documented', 'Accessibility and all target inputs are considered', 'Performance is measured against an explicit budget', 'Automated or repeatable tests pass with clean logs', 'Final state is read back and supported by concrete evidence']}, 'ms_level_design_review': {'description': 'Level design review: metrics, flow, pacing, landmarks, encounters, accessibility and playtests.', 'stages': ['Define level design review goals and measurable acceptance criteria', 'Inspect current project architecture, content and constraints', 'Design the data model, ownership and lifecycle for metrics, flow, pacing, landmarks, encounters, accessibility and playtests', 'Implement the smallest complete vertical path in the target engine', 'Integrate UI, input, audio, VFX and persistence where relevant', 'Handle failure, interruption, migration and multiplayer edge cases', 'Profile target-platform performance and add observability', 'Run focused tests, read back state and capture verification evidence'], 'qualityGates': ['The requested result exists in the real target project', 'Authority and ownership are explicit and secure', 'Happy, edge, failure and interruption paths are covered', 'Configuration is data-driven and documented', 'Accessibility and all target inputs are considered', 'Performance is measured against an explicit budget', 'Automated or repeatable tests pass with clean logs', 'Final state is read back and supported by concrete evidence']}, 'ms_puzzle_design': {'description': 'Puzzle design: rules, teaching, feedback, hinting, reset, exploits and difficulty progression.', 'stages': ['Define puzzle design goals and measurable acceptance criteria', 'Inspect current project architecture, content and constraints', 'Design the data model, ownership and lifecycle for rules, teaching, feedback, hinting, reset, exploits and difficulty progression', 'Implement the smallest complete vertical path in the target engine', 'Integrate UI, input, audio, VFX and persistence where relevant', 'Handle failure, interruption, migration and multiplayer edge cases', 'Profile target-platform performance and add observability', 'Run focused tests, read back state and capture verification evidence'], 'qualityGates': ['The requested result exists in the real target project', 'Authority and ownership are explicit and secure', 'Happy, edge, failure and interruption paths are covered', 'Configuration is data-driven and documented', 'Accessibility and all target inputs are considered', 'Performance is measured against an explicit budget', 'Automated or repeatable tests pass with clean logs', 'Final state is read back and supported by concrete evidence']}, 'ms_social_system_design': {'description': 'Social system design: parties, friends, presence, invites, privacy, moderation and safety.', 'stages': ['Define social system design goals and measurable acceptance criteria', 'Inspect current project architecture, content and constraints', 'Design the data model, ownership and lifecycle for parties, friends, presence, invites, privacy, moderation and safety', 'Implement the smallest complete vertical path in the target engine', 'Integrate UI, input, audio, VFX and persistence where relevant', 'Handle failure, interruption, migration and multiplayer edge cases', 'Profile target-platform performance and add observability', 'Run focused tests, read back state and capture verification evidence'], 'qualityGates': ['The requested result exists in the real target project', 'Authority and ownership are explicit and secure', 'Happy, edge, failure and interruption paths are covered', 'Configuration is data-driven and documented', 'Accessibility and all target inputs are considered', 'Performance is measured against an explicit budget', 'Automated or repeatable tests pass with clean logs', 'Final state is read back and supported by concrete evidence']}, 'ms_matchmaking_design': {'description': 'Matchmaking design: queues, skill, latency, parties, backfill, abuse prevention and telemetry.', 'stages': ['Define matchmaking design goals and measurable acceptance criteria', 'Inspect current project architecture, content and constraints', 'Design the data model, ownership and lifecycle for queues, skill, latency, parties, backfill, abuse prevention and telemetry', 'Implement the smallest complete vertical path in the target engine', 'Integrate UI, input, audio, VFX and persistence where relevant', 'Handle failure, interruption, migration and multiplayer edge cases', 'Profile target-platform performance and add observability', 'Run focused tests, read back state and capture verification evidence'], 'qualityGates': ['The requested result exists in the real target project', 'Authority and ownership are explicit and secure', 'Happy, edge, failure and interruption paths are covered', 'Configuration is data-driven and documented', 'Accessibility and all target inputs are considered', 'Performance is measured against an explicit budget', 'Automated or repeatable tests pass with clean logs', 'Final state is read back and supported by concrete evidence']}, 'ms_modding_pipeline': {'description': 'Modding pipeline: sandboxing, schemas, packaging, validation, compatibility and moderation.', 'stages': ['Define modding pipeline goals and measurable acceptance criteria', 'Inspect current project architecture, content and constraints', 'Design the data model, ownership and lifecycle for sandboxing, schemas, packaging, validation, compatibility and moderation', 'Implement the smallest complete vertical path in the target engine', 'Integrate UI, input, audio, VFX and persistence where relevant', 'Handle failure, interruption, migration and multiplayer edge cases', 'Profile target-platform performance and add observability', 'Run focused tests, read back state and capture verification evidence'], 'qualityGates': ['The requested result exists in the real target project', 'Authority and ownership are explicit and secure', 'Happy, edge, failure and interruption paths are covered', 'Configuration is data-driven and documented', 'Accessibility and all target inputs are considered', 'Performance is measured against an explicit budget', 'Automated or repeatable tests pass with clean logs', 'Final state is read back and supported by concrete evidence']}, 'ms_build_automation': {'description': 'Build automation: reproducible builds, versioning, tests, signing, artifacts and rollback.', 'stages': ['Define build automation goals and measurable acceptance criteria', 'Inspect current project architecture, content and constraints', 'Design the data model, ownership and lifecycle for reproducible builds, versioning, tests, signing, artifacts and rollback', 'Implement the smallest complete vertical path in the target engine', 'Integrate UI, input, audio, VFX and persistence where relevant', 'Handle failure, interruption, migration and multiplayer edge cases', 'Profile target-platform performance and add observability', 'Run focused tests, read back state and capture verification evidence'], 'qualityGates': ['The requested result exists in the real target project', 'Authority and ownership are explicit and secure', 'Happy, edge, failure and interruption paths are covered', 'Configuration is data-driven and documented', 'Accessibility and all target inputs are considered', 'Performance is measured against an explicit budget', 'Automated or repeatable tests pass with clean logs', 'Final state is read back and supported by concrete evidence']}, 'ms_crash_diagnostics': {'description': 'Crash diagnostics: symbols, dumps, breadcrumbs, reproduction, grouping and fix verification.', 'stages': ['Define crash diagnostics goals and measurable acceptance criteria', 'Inspect current project architecture, content and constraints', 'Design the data model, ownership and lifecycle for symbols, dumps, breadcrumbs, reproduction, grouping and fix verification', 'Implement the smallest complete vertical path in the target engine', 'Integrate UI, input, audio, VFX and persistence where relevant', 'Handle failure, interruption, migration and multiplayer edge cases', 'Profile target-platform performance and add observability', 'Run focused tests, read back state and capture verification evidence'], 'qualityGates': ['The requested result exists in the real target project', 'Authority and ownership are explicit and secure', 'Happy, edge, failure and interruption paths are covered', 'Configuration is data-driven and documented', 'Accessibility and all target inputs are considered', 'Performance is measured against an explicit budget', 'Automated or repeatable tests pass with clean logs', 'Final state is read back and supported by concrete evidence']}, 'ms_memory_leak_audit': {'description': 'Memory leak audit: allocation baselines, lifecycle ownership, retention paths and soak testing.', 'stages': ['Define memory leak audit goals and measurable acceptance criteria', 'Inspect current project architecture, content and constraints', 'Design the data model, ownership and lifecycle for allocation baselines, lifecycle ownership, retention paths and soak testing', 'Implement the smallest complete vertical path in the target engine', 'Integrate UI, input, audio, VFX and persistence where relevant', 'Handle failure, interruption, migration and multiplayer edge cases', 'Profile target-platform performance and add observability', 'Run focused tests, read back state and capture verification evidence'], 'qualityGates': ['The requested result exists in the real target project', 'Authority and ownership are explicit and secure', 'Happy, edge, failure and interruption paths are covered', 'Configuration is data-driven and documented', 'Accessibility and all target inputs are considered', 'Performance is measured against an explicit budget', 'Automated or repeatable tests pass with clean logs', 'Final state is read back and supported by concrete evidence']}, 'ms_network_bandwidth_audit': {'description': 'Network bandwidth audit: message inventory, payloads, rates, compression, interest and loss tests.', 'stages': ['Define network bandwidth audit goals and measurable acceptance criteria', 'Inspect current project architecture, content and constraints', 'Design the data model, ownership and lifecycle for message inventory, payloads, rates, compression, interest and loss tests', 'Implement the smallest complete vertical path in the target engine', 'Integrate UI, input, audio, VFX and persistence where relevant', 'Handle failure, interruption, migration and multiplayer edge cases', 'Profile target-platform performance and add observability', 'Run focused tests, read back state and capture verification evidence'], 'qualityGates': ['The requested result exists in the real target project', 'Authority and ownership are explicit and secure', 'Happy, edge, failure and interruption paths are covered', 'Configuration is data-driven and documented', 'Accessibility and all target inputs are considered', 'Performance is measured against an explicit budget', 'Automated or repeatable tests pass with clean logs', 'Final state is read back and supported by concrete evidence']}, 'ms_mobile_optimization': {'description': 'Mobile optimization: thermal, memory, GPU, input, UI, battery and device-tier validation.', 'stages': ['Define mobile optimization goals and measurable acceptance criteria', 'Inspect current project architecture, content and constraints', 'Design the data model, ownership and lifecycle for thermal, memory, GPU, input, UI, battery and device-tier validation', 'Implement the smallest complete vertical path in the target engine', 'Integrate UI, input, audio, VFX and persistence where relevant', 'Handle failure, interruption, migration and multiplayer edge cases', 'Profile target-platform performance and add observability', 'Run focused tests, read back state and capture verification evidence'], 'qualityGates': ['The requested result exists in the real target project', 'Authority and ownership are explicit and secure', 'Happy, edge, failure and interruption paths are covered', 'Configuration is data-driven and documented', 'Accessibility and all target inputs are considered', 'Performance is measured against an explicit budget', 'Automated or repeatable tests pass with clean logs', 'Final state is read back and supported by concrete evidence']}, 'ms_vr_xr_production': {'description': 'VR/XR production: comfort, interaction, locomotion, scale, performance and accessibility.', 'stages': ['Define vr/xr production goals and measurable acceptance criteria', 'Inspect current project architecture, content and constraints', 'Design the data model, ownership and lifecycle for comfort, interaction, locomotion, scale, performance and accessibility', 'Implement the smallest complete vertical path in the target engine', 'Integrate UI, input, audio, VFX and persistence where relevant', 'Handle failure, interruption, migration and multiplayer edge cases', 'Profile target-platform performance and add observability', 'Run focused tests, read back state and capture verification evidence'], 'qualityGates': ['The requested result exists in the real target project', 'Authority and ownership are explicit and secure', 'Happy, edge, failure and interruption paths are covered', 'Configuration is data-driven and documented', 'Accessibility and all target inputs are considered', 'Performance is measured against an explicit budget', 'Automated or repeatable tests pass with clean logs', 'Final state is read back and supported by concrete evidence']}, 'ms_web_build_optimization': {'description': 'Web build optimization: download size, startup, memory, browser compatibility and caching.', 'stages': ['Define web build optimization goals and measurable acceptance criteria', 'Inspect current project architecture, content and constraints', 'Design the data model, ownership and lifecycle for download size, startup, memory, browser compatibility and caching', 'Implement the smallest complete vertical path in the target engine', 'Integrate UI, input, audio, VFX and persistence where relevant', 'Handle failure, interruption, migration and multiplayer edge cases', 'Profile target-platform performance and add observability', 'Run focused tests, read back state and capture verification evidence'], 'qualityGates': ['The requested result exists in the real target project', 'Authority and ownership are explicit and secure', 'Happy, edge, failure and interruption paths are covered', 'Configuration is data-driven and documented', 'Accessibility and all target inputs are considered', 'Performance is measured against an explicit budget', 'Automated or repeatable tests pass with clean logs', 'Final state is read back and supported by concrete evidence']}, 'ms_console_readiness': {'description': 'Console readiness: platform input, suspend/resume, users, saves, certification and performance.', 'stages': ['Define console readiness goals and measurable acceptance criteria', 'Inspect current project architecture, content and constraints', 'Design the data model, ownership and lifecycle for platform input, suspend/resume, users, saves, certification and performance', 'Implement the smallest complete vertical path in the target engine', 'Integrate UI, input, audio, VFX and persistence where relevant', 'Handle failure, interruption, migration and multiplayer edge cases', 'Profile target-platform performance and add observability', 'Run focused tests, read back state and capture verification evidence'], 'qualityGates': ['The requested result exists in the real target project', 'Authority and ownership are explicit and secure', 'Happy, edge, failure and interruption paths are covered', 'Configuration is data-driven and documented', 'Accessibility and all target inputs are considered', 'Performance is measured against an explicit budget', 'Automated or repeatable tests pass with clean logs', 'Final state is read back and supported by concrete evidence']}, 'ms_controller_haptics': {'description': 'Controller haptics: event taxonomy, envelopes, device capabilities, comfort and testing.', 'stages': ['Define controller haptics goals and measurable acceptance criteria', 'Inspect current project architecture, content and constraints', 'Design the data model, ownership and lifecycle for event taxonomy, envelopes, device capabilities, comfort and testing', 'Implement the smallest complete vertical path in the target engine', 'Integrate UI, input, audio, VFX and persistence where relevant', 'Handle failure, interruption, migration and multiplayer edge cases', 'Profile target-platform performance and add observability', 'Run focused tests, read back state and capture verification evidence'], 'qualityGates': ['The requested result exists in the real target project', 'Authority and ownership are explicit and secure', 'Happy, edge, failure and interruption paths are covered', 'Configuration is data-driven and documented', 'Accessibility and all target inputs are considered', 'Performance is measured against an explicit budget', 'Automated or repeatable tests pass with clean logs', 'Final state is read back and supported by concrete evidence']}, 'ms_iconography_production': {'description': 'Iconography production: visual language, grids, states, readability, localization and export.', 'stages': ['Define iconography production goals and measurable acceptance criteria', 'Inspect current project architecture, content and constraints', 'Design the data model, ownership and lifecycle for visual language, grids, states, readability, localization and export', 'Implement the smallest complete vertical path in the target engine', 'Integrate UI, input, audio, VFX and persistence where relevant', 'Handle failure, interruption, migration and multiplayer edge cases', 'Profile target-platform performance and add observability', 'Run focused tests, read back state and capture verification evidence'], 'qualityGates': ['The requested result exists in the real target project', 'Authority and ownership are explicit and secure', 'Happy, edge, failure and interruption paths are covered', 'Configuration is data-driven and documented', 'Accessibility and all target inputs are considered', 'Performance is measured against an explicit budget', 'Automated or repeatable tests pass with clean logs', 'Final state is read back and supported by concrete evidence']}, 'ms_marketing_capture_plan': {'description': 'Marketing capture plan: shot list, builds, camera paths, clean UI, formats and approvals.', 'stages': ['Define marketing capture plan goals and measurable acceptance criteria', 'Inspect current project architecture, content and constraints', 'Design the data model, ownership and lifecycle for shot list, builds, camera paths, clean UI, formats and approvals', 'Implement the smallest complete vertical path in the target engine', 'Integrate UI, input, audio, VFX and persistence where relevant', 'Handle failure, interruption, migration and multiplayer edge cases', 'Profile target-platform performance and add observability', 'Run focused tests, read back state and capture verification evidence'], 'qualityGates': ['The requested result exists in the real target project', 'Authority and ownership are explicit and secure', 'Happy, edge, failure and interruption paths are covered', 'Configuration is data-driven and documented', 'Accessibility and all target inputs are considered', 'Performance is measured against an explicit budget', 'Automated or repeatable tests pass with clean logs', 'Final state is read back and supported by concrete evidence']}, 'ms_npc_population_system': {'description': 'NPC population system: spawning, schedules, simulation LOD, persistence and performance.', 'stages': ['Define npc population system goals and measurable acceptance criteria', 'Inspect current project architecture, content and constraints', 'Design the data model, ownership and lifecycle for spawning, schedules, simulation LOD, persistence and performance', 'Implement the smallest complete vertical path in the target engine', 'Integrate UI, input, audio, VFX and persistence where relevant', 'Handle failure, interruption, migration and multiplayer edge cases', 'Profile target-platform performance and add observability', 'Run focused tests, read back state and capture verification evidence'], 'qualityGates': ['The requested result exists in the real target project', 'Authority and ownership are explicit and secure', 'Happy, edge, failure and interruption paths are covered', 'Configuration is data-driven and documented', 'Accessibility and all target inputs are considered', 'Performance is measured against an explicit budget', 'Automated or repeatable tests pass with clean logs', 'Final state is read back and supported by concrete evidence']}, 'ms_weather_system': {'description': 'Weather system: state model, visuals, gameplay, audio, transitions, replication and budgets.', 'stages': ['Define weather system goals and measurable acceptance criteria', 'Inspect current project architecture, content and constraints', 'Design the data model, ownership and lifecycle for state model, visuals, gameplay, audio, transitions, replication and budgets', 'Implement the smallest complete vertical path in the target engine', 'Integrate UI, input, audio, VFX and persistence where relevant', 'Handle failure, interruption, migration and multiplayer edge cases', 'Profile target-platform performance and add observability', 'Run focused tests, read back state and capture verification evidence'], 'qualityGates': ['The requested result exists in the real target project', 'Authority and ownership are explicit and secure', 'Happy, edge, failure and interruption paths are covered', 'Configuration is data-driven and documented', 'Accessibility and all target inputs are considered', 'Performance is measured against an explicit budget', 'Automated or repeatable tests pass with clean logs', 'Final state is read back and supported by concrete evidence']}, 'ms_day_night_system': {'description': 'Day/night system: time authority, lighting, schedules, saves, networking and transitions.', 'stages': ['Define day/night system goals and measurable acceptance criteria', 'Inspect current project architecture, content and constraints', 'Design the data model, ownership and lifecycle for time authority, lighting, schedules, saves, networking and transitions', 'Implement the smallest complete vertical path in the target engine', 'Integrate UI, input, audio, VFX and persistence where relevant', 'Handle failure, interruption, migration and multiplayer edge cases', 'Profile target-platform performance and add observability', 'Run focused tests, read back state and capture verification evidence'], 'qualityGates': ['The requested result exists in the real target project', 'Authority and ownership are explicit and secure', 'Happy, edge, failure and interruption paths are covered', 'Configuration is data-driven and documented', 'Accessibility and all target inputs are considered', 'Performance is measured against an explicit budget', 'Automated or repeatable tests pass with clean logs', 'Final state is read back and supported by concrete evidence']}, 'ms_destructible_system': {'description': 'Destructible system: fracture states, authority, damage, debris, persistence and optimization.', 'stages': ['Define destructible system goals and measurable acceptance criteria', 'Inspect current project architecture, content and constraints', 'Design the data model, ownership and lifecycle for fracture states, authority, damage, debris, persistence and optimization', 'Implement the smallest complete vertical path in the target engine', 'Integrate UI, input, audio, VFX and persistence where relevant', 'Handle failure, interruption, migration and multiplayer edge cases', 'Profile target-platform performance and add observability', 'Run focused tests, read back state and capture verification evidence'], 'qualityGates': ['The requested result exists in the real target project', 'Authority and ownership are explicit and secure', 'Happy, edge, failure and interruption paths are covered', 'Configuration is data-driven and documented', 'Accessibility and all target inputs are considered', 'Performance is measured against an explicit budget', 'Automated or repeatable tests pass with clean logs', 'Final state is read back and supported by concrete evidence']}, 'ms_replay_system': {'description': 'Replay system: capture schema, determinism, seek, versioning, storage and playback validation.', 'stages': ['Define replay system goals and measurable acceptance criteria', 'Inspect current project architecture, content and constraints', 'Design the data model, ownership and lifecycle for capture schema, determinism, seek, versioning, storage and playback validation', 'Implement the smallest complete vertical path in the target engine', 'Integrate UI, input, audio, VFX and persistence where relevant', 'Handle failure, interruption, migration and multiplayer edge cases', 'Profile target-platform performance and add observability', 'Run focused tests, read back state and capture verification evidence'], 'qualityGates': ['The requested result exists in the real target project', 'Authority and ownership are explicit and secure', 'Happy, edge, failure and interruption paths are covered', 'Configuration is data-driven and documented', 'Accessibility and all target inputs are considered', 'Performance is measured against an explicit budget', 'Automated or repeatable tests pass with clean logs', 'Final state is read back and supported by concrete evidence']}, 'ms_photo_mode': {'description': 'Photo mode: camera controls, pause policy, filters, UI, platform saves and privacy.', 'stages': ['Define photo mode goals and measurable acceptance criteria', 'Inspect current project architecture, content and constraints', 'Design the data model, ownership and lifecycle for camera controls, pause policy, filters, UI, platform saves and privacy', 'Implement the smallest complete vertical path in the target engine', 'Integrate UI, input, audio, VFX and persistence where relevant', 'Handle failure, interruption, migration and multiplayer edge cases', 'Profile target-platform performance and add observability', 'Run focused tests, read back state and capture verification evidence'], 'qualityGates': ['The requested result exists in the real target project', 'Authority and ownership are explicit and secure', 'Happy, edge, failure and interruption paths are covered', 'Configuration is data-driven and documented', 'Accessibility and all target inputs are considered', 'Performance is measured against an explicit budget', 'Automated or repeatable tests pass with clean logs', 'Final state is read back and supported by concrete evidence']}})


STUDIO_DIRECT_CONTRACTS = {'ms_roblox_animation_studio': {'description': 'Roblox animation studio with a senior ten-year-studio craft bar.', 'category': 'animation', 'stages': ['Establish reference, emotional intent and gameplay readability', 'Inspect rig hierarchy, scale, constraints and existing clips', 'Block unmistakable key poses, silhouettes and contact points', 'Refine timing, spacing, arcs, weight, overlap and anticipation', 'Build loops, transitions, layers, masks, IK and interruption rules', 'Synchronize gameplay events, hit windows, VFX and audio cues', 'Integrate compression, retargeting, networking and runtime budgets', 'Review in gameplay camera at multiple speeds and fix every visible defect'], 'qualityGates': ['Strong poses read without context', 'Weight, balance and contacts remain believable', 'Timing supports gameplay rather than delaying it', 'Transitions have no pops, foot slides or dead frames', 'Loops preserve phase and root-motion policy', 'Events align with visible action frames', 'Retargeted/runtime output matches source intent', 'Engine capture and state-machine tests pass']}, 'ms_blender_animation_studio': {'description': 'Blender animation studio with a senior ten-year-studio craft bar.', 'category': 'animation', 'stages': ['Establish reference, emotional intent and gameplay readability', 'Inspect rig hierarchy, scale, constraints and existing clips', 'Block unmistakable key poses, silhouettes and contact points', 'Refine timing, spacing, arcs, weight, overlap and anticipation', 'Build loops, transitions, layers, masks, IK and interruption rules', 'Synchronize gameplay events, hit windows, VFX and audio cues', 'Integrate compression, retargeting, networking and runtime budgets', 'Review in gameplay camera at multiple speeds and fix every visible defect'], 'qualityGates': ['Strong poses read without context', 'Weight, balance and contacts remain believable', 'Timing supports gameplay rather than delaying it', 'Transitions have no pops, foot slides or dead frames', 'Loops preserve phase and root-motion policy', 'Events align with visible action frames', 'Retargeted/runtime output matches source intent', 'Engine capture and state-machine tests pass']}, 'ms_unity_animation_studio': {'description': 'Unity animation studio with a senior ten-year-studio craft bar.', 'category': 'animation', 'stages': ['Establish reference, emotional intent and gameplay readability', 'Inspect rig hierarchy, scale, constraints and existing clips', 'Block unmistakable key poses, silhouettes and contact points', 'Refine timing, spacing, arcs, weight, overlap and anticipation', 'Build loops, transitions, layers, masks, IK and interruption rules', 'Synchronize gameplay events, hit windows, VFX and audio cues', 'Integrate compression, retargeting, networking and runtime budgets', 'Review in gameplay camera at multiple speeds and fix every visible defect'], 'qualityGates': ['Strong poses read without context', 'Weight, balance and contacts remain believable', 'Timing supports gameplay rather than delaying it', 'Transitions have no pops, foot slides or dead frames', 'Loops preserve phase and root-motion policy', 'Events align with visible action frames', 'Retargeted/runtime output matches source intent', 'Engine capture and state-machine tests pass']}, 'ms_godot_animation_studio': {'description': 'Godot animation studio with a senior ten-year-studio craft bar.', 'category': 'animation', 'stages': ['Establish reference, emotional intent and gameplay readability', 'Inspect rig hierarchy, scale, constraints and existing clips', 'Block unmistakable key poses, silhouettes and contact points', 'Refine timing, spacing, arcs, weight, overlap and anticipation', 'Build loops, transitions, layers, masks, IK and interruption rules', 'Synchronize gameplay events, hit windows, VFX and audio cues', 'Integrate compression, retargeting, networking and runtime budgets', 'Review in gameplay camera at multiple speeds and fix every visible defect'], 'qualityGates': ['Strong poses read without context', 'Weight, balance and contacts remain believable', 'Timing supports gameplay rather than delaying it', 'Transitions have no pops, foot slides or dead frames', 'Loops preserve phase and root-motion policy', 'Events align with visible action frames', 'Retargeted/runtime output matches source intent', 'Engine capture and state-machine tests pass']}, 'ms_combat_animation_polish': {'description': 'Combat animation polish with a senior ten-year-studio craft bar.', 'category': 'animation', 'stages': ['Establish reference, emotional intent and gameplay readability', 'Inspect rig hierarchy, scale, constraints and existing clips', 'Block unmistakable key poses, silhouettes and contact points', 'Refine timing, spacing, arcs, weight, overlap and anticipation', 'Build loops, transitions, layers, masks, IK and interruption rules', 'Synchronize gameplay events, hit windows, VFX and audio cues', 'Integrate compression, retargeting, networking and runtime budgets', 'Review in gameplay camera at multiple speeds and fix every visible defect'], 'qualityGates': ['Strong poses read without context', 'Weight, balance and contacts remain believable', 'Timing supports gameplay rather than delaying it', 'Transitions have no pops, foot slides or dead frames', 'Loops preserve phase and root-motion policy', 'Events align with visible action frames', 'Retargeted/runtime output matches source intent', 'Engine capture and state-machine tests pass']}, 'ms_locomotion_animation_polish': {'description': 'Locomotion animation polish with a senior ten-year-studio craft bar.', 'category': 'animation', 'stages': ['Establish reference, emotional intent and gameplay readability', 'Inspect rig hierarchy, scale, constraints and existing clips', 'Block unmistakable key poses, silhouettes and contact points', 'Refine timing, spacing, arcs, weight, overlap and anticipation', 'Build loops, transitions, layers, masks, IK and interruption rules', 'Synchronize gameplay events, hit windows, VFX and audio cues', 'Integrate compression, retargeting, networking and runtime budgets', 'Review in gameplay camera at multiple speeds and fix every visible defect'], 'qualityGates': ['Strong poses read without context', 'Weight, balance and contacts remain believable', 'Timing supports gameplay rather than delaying it', 'Transitions have no pops, foot slides or dead frames', 'Loops preserve phase and root-motion policy', 'Events align with visible action frames', 'Retargeted/runtime output matches source intent', 'Engine capture and state-machine tests pass']}, 'ms_cinematic_animation_polish': {'description': 'Cinematic animation polish with a senior ten-year-studio craft bar.', 'category': 'animation', 'stages': ['Establish reference, emotional intent and gameplay readability', 'Inspect rig hierarchy, scale, constraints and existing clips', 'Block unmistakable key poses, silhouettes and contact points', 'Refine timing, spacing, arcs, weight, overlap and anticipation', 'Build loops, transitions, layers, masks, IK and interruption rules', 'Synchronize gameplay events, hit windows, VFX and audio cues', 'Integrate compression, retargeting, networking and runtime budgets', 'Review in gameplay camera at multiple speeds and fix every visible defect'], 'qualityGates': ['Strong poses read without context', 'Weight, balance and contacts remain believable', 'Timing supports gameplay rather than delaying it', 'Transitions have no pops, foot slides or dead frames', 'Loops preserve phase and root-motion policy', 'Events align with visible action frames', 'Retargeted/runtime output matches source intent', 'Engine capture and state-machine tests pass']}, 'ms_creature_animation': {'description': 'Creature animation with a senior ten-year-studio craft bar.', 'category': 'animation', 'stages': ['Establish reference, emotional intent and gameplay readability', 'Inspect rig hierarchy, scale, constraints and existing clips', 'Block unmistakable key poses, silhouettes and contact points', 'Refine timing, spacing, arcs, weight, overlap and anticipation', 'Build loops, transitions, layers, masks, IK and interruption rules', 'Synchronize gameplay events, hit windows, VFX and audio cues', 'Integrate compression, retargeting, networking and runtime budgets', 'Review in gameplay camera at multiple speeds and fix every visible defect'], 'qualityGates': ['Strong poses read without context', 'Weight, balance and contacts remain believable', 'Timing supports gameplay rather than delaying it', 'Transitions have no pops, foot slides or dead frames', 'Loops preserve phase and root-motion policy', 'Events align with visible action frames', 'Retargeted/runtime output matches source intent', 'Engine capture and state-machine tests pass']}, 'ms_facial_animation': {'description': 'Facial animation with a senior ten-year-studio craft bar.', 'category': 'animation', 'stages': ['Establish reference, emotional intent and gameplay readability', 'Inspect rig hierarchy, scale, constraints and existing clips', 'Block unmistakable key poses, silhouettes and contact points', 'Refine timing, spacing, arcs, weight, overlap and anticipation', 'Build loops, transitions, layers, masks, IK and interruption rules', 'Synchronize gameplay events, hit windows, VFX and audio cues', 'Integrate compression, retargeting, networking and runtime budgets', 'Review in gameplay camera at multiple speeds and fix every visible defect'], 'qualityGates': ['Strong poses read without context', 'Weight, balance and contacts remain believable', 'Timing supports gameplay rather than delaying it', 'Transitions have no pops, foot slides or dead frames', 'Loops preserve phase and root-motion policy', 'Events align with visible action frames', 'Retargeted/runtime output matches source intent', 'Engine capture and state-machine tests pass']}, 'ms_animation_retargeting': {'description': 'Animation retargeting with a senior ten-year-studio craft bar.', 'category': 'animation', 'stages': ['Establish reference, emotional intent and gameplay readability', 'Inspect rig hierarchy, scale, constraints and existing clips', 'Block unmistakable key poses, silhouettes and contact points', 'Refine timing, spacing, arcs, weight, overlap and anticipation', 'Build loops, transitions, layers, masks, IK and interruption rules', 'Synchronize gameplay events, hit windows, VFX and audio cues', 'Integrate compression, retargeting, networking and runtime budgets', 'Review in gameplay camera at multiple speeds and fix every visible defect'], 'qualityGates': ['Strong poses read without context', 'Weight, balance and contacts remain believable', 'Timing supports gameplay rather than delaying it', 'Transitions have no pops, foot slides or dead frames', 'Loops preserve phase and root-motion policy', 'Events align with visible action frames', 'Retargeted/runtime output matches source intent', 'Engine capture and state-machine tests pass']}, 'ms_animation_state_machine': {'description': 'Animation state-machine direction with a senior ten-year-studio craft bar.', 'category': 'animation', 'stages': ['Establish reference, emotional intent and gameplay readability', 'Inspect rig hierarchy, scale, constraints and existing clips', 'Block unmistakable key poses, silhouettes and contact points', 'Refine timing, spacing, arcs, weight, overlap and anticipation', 'Build loops, transitions, layers, masks, IK and interruption rules', 'Synchronize gameplay events, hit windows, VFX and audio cues', 'Integrate compression, retargeting, networking and runtime budgets', 'Review in gameplay camera at multiple speeds and fix every visible defect'], 'qualityGates': ['Strong poses read without context', 'Weight, balance and contacts remain believable', 'Timing supports gameplay rather than delaying it', 'Transitions have no pops, foot slides or dead frames', 'Loops preserve phase and root-motion policy', 'Events align with visible action frames', 'Retargeted/runtime output matches source intent', 'Engine capture and state-machine tests pass']}, 'ms_animation_runtime_qa': {'description': 'Animation runtime QA with a senior ten-year-studio craft bar.', 'category': 'animation', 'stages': ['Establish reference, emotional intent and gameplay readability', 'Inspect rig hierarchy, scale, constraints and existing clips', 'Block unmistakable key poses, silhouettes and contact points', 'Refine timing, spacing, arcs, weight, overlap and anticipation', 'Build loops, transitions, layers, masks, IK and interruption rules', 'Synchronize gameplay events, hit windows, VFX and audio cues', 'Integrate compression, retargeting, networking and runtime budgets', 'Review in gameplay camera at multiple speeds and fix every visible defect'], 'qualityGates': ['Strong poses read without context', 'Weight, balance and contacts remain believable', 'Timing supports gameplay rather than delaying it', 'Transitions have no pops, foot slides or dead frames', 'Loops preserve phase and root-motion policy', 'Events align with visible action frames', 'Retargeted/runtime output matches source intent', 'Engine capture and state-machine tests pass']}, 'ms_gui_design_studio': {'description': 'Professional GUI design studio with a senior ten-year-studio craft bar.', 'category': 'uiux', 'stages': ['Clarify the user goal, primary path and decision hierarchy', 'Inspect current design language, constraints and target inputs', 'Define tokens for type, color, spacing, radius, depth and motion', 'Build reusable components with complete interaction states', 'Compose responsive layouts for aspect ratios and localization', 'Add controller, keyboard, touch and accessibility behavior', 'Implement tasteful motion, feedback, loading and error recovery', 'Test the real flow with screenshots, navigation and usability evidence'], 'qualityGates': ['Hierarchy makes the primary action obvious', 'Components are reusable and internally consistent', 'Every state is intentional and visually finished', 'Layouts survive small screens and long localized text', 'Focus order and controller navigation have no dead ends', 'Contrast, type scale and target sizes are accessible', 'Motion feels responsive and supports reduced-motion settings', 'Final in-engine flow is functional and beautiful']}, 'ms_roblox_gui_studio': {'description': 'Roblox GUI production studio with a senior ten-year-studio craft bar.', 'category': 'uiux', 'stages': ['Clarify the user goal, primary path and decision hierarchy', 'Inspect current design language, constraints and target inputs', 'Define tokens for type, color, spacing, radius, depth and motion', 'Build reusable components with complete interaction states', 'Compose responsive layouts for aspect ratios and localization', 'Add controller, keyboard, touch and accessibility behavior', 'Implement tasteful motion, feedback, loading and error recovery', 'Test the real flow with screenshots, navigation and usability evidence'], 'qualityGates': ['Hierarchy makes the primary action obvious', 'Components are reusable and internally consistent', 'Every state is intentional and visually finished', 'Layouts survive small screens and long localized text', 'Focus order and controller navigation have no dead ends', 'Contrast, type scale and target sizes are accessible', 'Motion feels responsive and supports reduced-motion settings', 'Final in-engine flow is functional and beautiful']}, 'ms_unity_ui_studio': {'description': 'Unity UI production studio with a senior ten-year-studio craft bar.', 'category': 'uiux', 'stages': ['Clarify the user goal, primary path and decision hierarchy', 'Inspect current design language, constraints and target inputs', 'Define tokens for type, color, spacing, radius, depth and motion', 'Build reusable components with complete interaction states', 'Compose responsive layouts for aspect ratios and localization', 'Add controller, keyboard, touch and accessibility behavior', 'Implement tasteful motion, feedback, loading and error recovery', 'Test the real flow with screenshots, navigation and usability evidence'], 'qualityGates': ['Hierarchy makes the primary action obvious', 'Components are reusable and internally consistent', 'Every state is intentional and visually finished', 'Layouts survive small screens and long localized text', 'Focus order and controller navigation have no dead ends', 'Contrast, type scale and target sizes are accessible', 'Motion feels responsive and supports reduced-motion settings', 'Final in-engine flow is functional and beautiful']}, 'ms_godot_ui_studio': {'description': 'Godot UI production studio with a senior ten-year-studio craft bar.', 'category': 'uiux', 'stages': ['Clarify the user goal, primary path and decision hierarchy', 'Inspect current design language, constraints and target inputs', 'Define tokens for type, color, spacing, radius, depth and motion', 'Build reusable components with complete interaction states', 'Compose responsive layouts for aspect ratios and localization', 'Add controller, keyboard, touch and accessibility behavior', 'Implement tasteful motion, feedback, loading and error recovery', 'Test the real flow with screenshots, navigation and usability evidence'], 'qualityGates': ['Hierarchy makes the primary action obvious', 'Components are reusable and internally consistent', 'Every state is intentional and visually finished', 'Layouts survive small screens and long localized text', 'Focus order and controller navigation have no dead ends', 'Contrast, type scale and target sizes are accessible', 'Motion feels responsive and supports reduced-motion settings', 'Final in-engine flow is functional and beautiful']}, 'ms_ui_motion_design': {'description': 'UI motion and micro-interaction design with a senior ten-year-studio craft bar.', 'category': 'uiux', 'stages': ['Clarify the user goal, primary path and decision hierarchy', 'Inspect current design language, constraints and target inputs', 'Define tokens for type, color, spacing, radius, depth and motion', 'Build reusable components with complete interaction states', 'Compose responsive layouts for aspect ratios and localization', 'Add controller, keyboard, touch and accessibility behavior', 'Implement tasteful motion, feedback, loading and error recovery', 'Test the real flow with screenshots, navigation and usability evidence'], 'qualityGates': ['Hierarchy makes the primary action obvious', 'Components are reusable and internally consistent', 'Every state is intentional and visually finished', 'Layouts survive small screens and long localized text', 'Focus order and controller navigation have no dead ends', 'Contrast, type scale and target sizes are accessible', 'Motion feels responsive and supports reduced-motion settings', 'Final in-engine flow is functional and beautiful']}, 'ms_design_system_production': {'description': 'Design-system production with a senior ten-year-studio craft bar.', 'category': 'uiux', 'stages': ['Clarify the user goal, primary path and decision hierarchy', 'Inspect current design language, constraints and target inputs', 'Define tokens for type, color, spacing, radius, depth and motion', 'Build reusable components with complete interaction states', 'Compose responsive layouts for aspect ratios and localization', 'Add controller, keyboard, touch and accessibility behavior', 'Implement tasteful motion, feedback, loading and error recovery', 'Test the real flow with screenshots, navigation and usability evidence'], 'qualityGates': ['Hierarchy makes the primary action obvious', 'Components are reusable and internally consistent', 'Every state is intentional and visually finished', 'Layouts survive small screens and long localized text', 'Focus order and controller navigation have no dead ends', 'Contrast, type scale and target sizes are accessible', 'Motion feels responsive and supports reduced-motion settings', 'Final in-engine flow is functional and beautiful']}, 'ms_responsive_interface_polish': {'description': 'Responsive interface polish with a senior ten-year-studio craft bar.', 'category': 'uiux', 'stages': ['Clarify the user goal, primary path and decision hierarchy', 'Inspect current design language, constraints and target inputs', 'Define tokens for type, color, spacing, radius, depth and motion', 'Build reusable components with complete interaction states', 'Compose responsive layouts for aspect ratios and localization', 'Add controller, keyboard, touch and accessibility behavior', 'Implement tasteful motion, feedback, loading and error recovery', 'Test the real flow with screenshots, navigation and usability evidence'], 'qualityGates': ['Hierarchy makes the primary action obvious', 'Components are reusable and internally consistent', 'Every state is intentional and visually finished', 'Layouts survive small screens and long localized text', 'Focus order and controller navigation have no dead ends', 'Contrast, type scale and target sizes are accessible', 'Motion feels responsive and supports reduced-motion settings', 'Final in-engine flow is functional and beautiful']}, 'ms_controller_navigation_ux': {'description': 'Controller-navigation UX with a senior ten-year-studio craft bar.', 'category': 'uiux', 'stages': ['Clarify the user goal, primary path and decision hierarchy', 'Inspect current design language, constraints and target inputs', 'Define tokens for type, color, spacing, radius, depth and motion', 'Build reusable components with complete interaction states', 'Compose responsive layouts for aspect ratios and localization', 'Add controller, keyboard, touch and accessibility behavior', 'Implement tasteful motion, feedback, loading and error recovery', 'Test the real flow with screenshots, navigation and usability evidence'], 'qualityGates': ['Hierarchy makes the primary action obvious', 'Components are reusable and internally consistent', 'Every state is intentional and visually finished', 'Layouts survive small screens and long localized text', 'Focus order and controller navigation have no dead ends', 'Contrast, type scale and target sizes are accessible', 'Motion feels responsive and supports reduced-motion settings', 'Final in-engine flow is functional and beautiful']}, 'ms_accessible_interface_polish': {'description': 'Accessible interface polish with a senior ten-year-studio craft bar.', 'category': 'uiux', 'stages': ['Clarify the user goal, primary path and decision hierarchy', 'Inspect current design language, constraints and target inputs', 'Define tokens for type, color, spacing, radius, depth and motion', 'Build reusable components with complete interaction states', 'Compose responsive layouts for aspect ratios and localization', 'Add controller, keyboard, touch and accessibility behavior', 'Implement tasteful motion, feedback, loading and error recovery', 'Test the real flow with screenshots, navigation and usability evidence'], 'qualityGates': ['Hierarchy makes the primary action obvious', 'Components are reusable and internally consistent', 'Every state is intentional and visually finished', 'Layouts survive small screens and long localized text', 'Focus order and controller navigation have no dead ends', 'Contrast, type scale and target sizes are accessible', 'Motion feels responsive and supports reduced-motion settings', 'Final in-engine flow is functional and beautiful']}, 'ms_usability_polish': {'description': 'Usability and flow polish with a senior ten-year-studio craft bar.', 'category': 'uiux', 'stages': ['Clarify the user goal, primary path and decision hierarchy', 'Inspect current design language, constraints and target inputs', 'Define tokens for type, color, spacing, radius, depth and motion', 'Build reusable components with complete interaction states', 'Compose responsive layouts for aspect ratios and localization', 'Add controller, keyboard, touch and accessibility behavior', 'Implement tasteful motion, feedback, loading and error recovery', 'Test the real flow with screenshots, navigation and usability evidence'], 'qualityGates': ['Hierarchy makes the primary action obvious', 'Components are reusable and internally consistent', 'Every state is intentional and visually finished', 'Layouts survive small screens and long localized text', 'Focus order and controller navigation have no dead ends', 'Contrast, type scale and target sizes are accessible', 'Motion feels responsive and supports reduced-motion settings', 'Final in-engine flow is functional and beautiful']}, 'ms_blender_model_studio': {'description': 'Blender model studio with a senior ten-year-studio craft bar.', 'category': 'art3d', 'stages': ['Define art direction, references, scale and gameplay silhouette', 'Inspect target engine metrics, camera distance and asset budget', 'Block primary forms, proportion, composition and value structure', 'Refine topology, deformation, UVs, pivots and modular boundaries', 'Author coherent materials, textures and controlled detail hierarchy', 'Create lighting and presentation that supports the intended mood', 'Export and integrate correct scale, axes, collisions, LODs and shaders', 'Validate from gameplay view and optimize without losing the art target'], 'qualityGates': ['Silhouette and focal hierarchy read at gameplay distance', 'Topology, normals and UVs are technically clean', 'Material response is coherent under representative lighting', 'Detail density supports—not fights—the focal point', 'Scale, pivot, collision and orientation import correctly', 'LODs and compression preserve quality within budget', 'Asset matches the surrounding project art language', 'Final engine capture meets the reference bar']}, 'ms_stylized_art_production': {'description': 'Stylized art production with a senior ten-year-studio craft bar.', 'category': 'art3d', 'stages': ['Define art direction, references, scale and gameplay silhouette', 'Inspect target engine metrics, camera distance and asset budget', 'Block primary forms, proportion, composition and value structure', 'Refine topology, deformation, UVs, pivots and modular boundaries', 'Author coherent materials, textures and controlled detail hierarchy', 'Create lighting and presentation that supports the intended mood', 'Export and integrate correct scale, axes, collisions, LODs and shaders', 'Validate from gameplay view and optimize without losing the art target'], 'qualityGates': ['Silhouette and focal hierarchy read at gameplay distance', 'Topology, normals and UVs are technically clean', 'Material response is coherent under representative lighting', 'Detail density supports—not fights—the focal point', 'Scale, pivot, collision and orientation import correctly', 'LODs and compression preserve quality within budget', 'Asset matches the surrounding project art language', 'Final engine capture meets the reference bar']}, 'ms_realistic_art_production': {'description': 'Realistic art production with a senior ten-year-studio craft bar.', 'category': 'art3d', 'stages': ['Define art direction, references, scale and gameplay silhouette', 'Inspect target engine metrics, camera distance and asset budget', 'Block primary forms, proportion, composition and value structure', 'Refine topology, deformation, UVs, pivots and modular boundaries', 'Author coherent materials, textures and controlled detail hierarchy', 'Create lighting and presentation that supports the intended mood', 'Export and integrate correct scale, axes, collisions, LODs and shaders', 'Validate from gameplay view and optimize without losing the art target'], 'qualityGates': ['Silhouette and focal hierarchy read at gameplay distance', 'Topology, normals and UVs are technically clean', 'Material response is coherent under representative lighting', 'Detail density supports—not fights—the focal point', 'Scale, pivot, collision and orientation import correctly', 'LODs and compression preserve quality within budget', 'Asset matches the surrounding project art language', 'Final engine capture meets the reference bar']}, 'ms_environment_art_studio': {'description': 'Environment art studio with a senior ten-year-studio craft bar.', 'category': 'art3d', 'stages': ['Define art direction, references, scale and gameplay silhouette', 'Inspect target engine metrics, camera distance and asset budget', 'Block primary forms, proportion, composition and value structure', 'Refine topology, deformation, UVs, pivots and modular boundaries', 'Author coherent materials, textures and controlled detail hierarchy', 'Create lighting and presentation that supports the intended mood', 'Export and integrate correct scale, axes, collisions, LODs and shaders', 'Validate from gameplay view and optimize without losing the art target'], 'qualityGates': ['Silhouette and focal hierarchy read at gameplay distance', 'Topology, normals and UVs are technically clean', 'Material response is coherent under representative lighting', 'Detail density supports—not fights—the focal point', 'Scale, pivot, collision and orientation import correctly', 'LODs and compression preserve quality within budget', 'Asset matches the surrounding project art language', 'Final engine capture meets the reference bar']}, 'ms_character_art_studio': {'description': 'Character art studio with a senior ten-year-studio craft bar.', 'category': 'art3d', 'stages': ['Define art direction, references, scale and gameplay silhouette', 'Inspect target engine metrics, camera distance and asset budget', 'Block primary forms, proportion, composition and value structure', 'Refine topology, deformation, UVs, pivots and modular boundaries', 'Author coherent materials, textures and controlled detail hierarchy', 'Create lighting and presentation that supports the intended mood', 'Export and integrate correct scale, axes, collisions, LODs and shaders', 'Validate from gameplay view and optimize without losing the art target'], 'qualityGates': ['Silhouette and focal hierarchy read at gameplay distance', 'Topology, normals and UVs are technically clean', 'Material response is coherent under representative lighting', 'Detail density supports—not fights—the focal point', 'Scale, pivot, collision and orientation import correctly', 'LODs and compression preserve quality within budget', 'Asset matches the surrounding project art language', 'Final engine capture meets the reference bar']}, 'ms_prop_art_studio': {'description': 'Prop art studio with a senior ten-year-studio craft bar.', 'category': 'art3d', 'stages': ['Define art direction, references, scale and gameplay silhouette', 'Inspect target engine metrics, camera distance and asset budget', 'Block primary forms, proportion, composition and value structure', 'Refine topology, deformation, UVs, pivots and modular boundaries', 'Author coherent materials, textures and controlled detail hierarchy', 'Create lighting and presentation that supports the intended mood', 'Export and integrate correct scale, axes, collisions, LODs and shaders', 'Validate from gameplay view and optimize without losing the art target'], 'qualityGates': ['Silhouette and focal hierarchy read at gameplay distance', 'Topology, normals and UVs are technically clean', 'Material response is coherent under representative lighting', 'Detail density supports—not fights—the focal point', 'Scale, pivot, collision and orientation import correctly', 'LODs and compression preserve quality within budget', 'Asset matches the surrounding project art language', 'Final engine capture meets the reference bar']}, 'ms_pbr_material_studio': {'description': 'PBR material studio with a senior ten-year-studio craft bar.', 'category': 'art3d', 'stages': ['Define art direction, references, scale and gameplay silhouette', 'Inspect target engine metrics, camera distance and asset budget', 'Block primary forms, proportion, composition and value structure', 'Refine topology, deformation, UVs, pivots and modular boundaries', 'Author coherent materials, textures and controlled detail hierarchy', 'Create lighting and presentation that supports the intended mood', 'Export and integrate correct scale, axes, collisions, LODs and shaders', 'Validate from gameplay view and optimize without losing the art target'], 'qualityGates': ['Silhouette and focal hierarchy read at gameplay distance', 'Topology, normals and UVs are technically clean', 'Material response is coherent under representative lighting', 'Detail density supports—not fights—the focal point', 'Scale, pivot, collision and orientation import correctly', 'LODs and compression preserve quality within budget', 'Asset matches the surrounding project art language', 'Final engine capture meets the reference bar']}, 'ms_texture_detail_polish': {'description': 'Texture detail polish with a senior ten-year-studio craft bar.', 'category': 'art3d', 'stages': ['Define art direction, references, scale and gameplay silhouette', 'Inspect target engine metrics, camera distance and asset budget', 'Block primary forms, proportion, composition and value structure', 'Refine topology, deformation, UVs, pivots and modular boundaries', 'Author coherent materials, textures and controlled detail hierarchy', 'Create lighting and presentation that supports the intended mood', 'Export and integrate correct scale, axes, collisions, LODs and shaders', 'Validate from gameplay view and optimize without losing the art target'], 'qualityGates': ['Silhouette and focal hierarchy read at gameplay distance', 'Topology, normals and UVs are technically clean', 'Material response is coherent under representative lighting', 'Detail density supports—not fights—the focal point', 'Scale, pivot, collision and orientation import correctly', 'LODs and compression preserve quality within budget', 'Asset matches the surrounding project art language', 'Final engine capture meets the reference bar']}, 'ms_lighting_art_studio': {'description': 'Lighting art studio with a senior ten-year-studio craft bar.', 'category': 'art3d', 'stages': ['Define art direction, references, scale and gameplay silhouette', 'Inspect target engine metrics, camera distance and asset budget', 'Block primary forms, proportion, composition and value structure', 'Refine topology, deformation, UVs, pivots and modular boundaries', 'Author coherent materials, textures and controlled detail hierarchy', 'Create lighting and presentation that supports the intended mood', 'Export and integrate correct scale, axes, collisions, LODs and shaders', 'Validate from gameplay view and optimize without losing the art target'], 'qualityGates': ['Silhouette and focal hierarchy read at gameplay distance', 'Topology, normals and UVs are technically clean', 'Material response is coherent under representative lighting', 'Detail density supports—not fights—the focal point', 'Scale, pivot, collision and orientation import correctly', 'LODs and compression preserve quality within budget', 'Asset matches the surrounding project art language', 'Final engine capture meets the reference bar']}, 'ms_composition_render_polish': {'description': 'Composition and render polish with a senior ten-year-studio craft bar.', 'category': 'art3d', 'stages': ['Define art direction, references, scale and gameplay silhouette', 'Inspect target engine metrics, camera distance and asset budget', 'Block primary forms, proportion, composition and value structure', 'Refine topology, deformation, UVs, pivots and modular boundaries', 'Author coherent materials, textures and controlled detail hierarchy', 'Create lighting and presentation that supports the intended mood', 'Export and integrate correct scale, axes, collisions, LODs and shaders', 'Validate from gameplay view and optimize without losing the art target'], 'qualityGates': ['Silhouette and focal hierarchy read at gameplay distance', 'Topology, normals and UVs are technically clean', 'Material response is coherent under representative lighting', 'Detail density supports—not fights—the focal point', 'Scale, pivot, collision and orientation import correctly', 'LODs and compression preserve quality within budget', 'Asset matches the surrounding project art language', 'Final engine capture meets the reference bar']}, 'ms_game_feel_director': {'description': 'Game-feel director with a senior ten-year-studio craft bar.', 'category': 'gameplay', 'stages': ['Define the player fantasy and the exact feeling to create', 'Inspect existing controls, camera, animation, feedback and metrics', 'Set measurable response, timing, acceleration and clarity targets', 'Implement the smallest complete interactive loop in the real engine', 'Layer animation, VFX, audio, haptics and camera feedback with restraint', 'Handle cancellation, failure, latency, accessibility and edge states', 'Tune with repeatable scenarios and player-facing telemetry', 'Play-test, compare against targets and iterate until the feel is cohesive'], 'qualityGates': ['Input-to-feedback latency feels immediate', 'Rules and outcomes are legible without explanation', 'Feedback is layered but never noisy', 'Difficulty and timing are fair across target inputs', 'Systems remain secure and deterministic where needed', 'Edge and interruption states recover gracefully', 'Performance remains stable under representative load', 'Play-test evidence supports the final tuning']}, 'ms_combat_feel_director': {'description': 'Combat-feel director with a senior ten-year-studio craft bar.', 'category': 'gameplay', 'stages': ['Define the player fantasy and the exact feeling to create', 'Inspect existing controls, camera, animation, feedback and metrics', 'Set measurable response, timing, acceleration and clarity targets', 'Implement the smallest complete interactive loop in the real engine', 'Layer animation, VFX, audio, haptics and camera feedback with restraint', 'Handle cancellation, failure, latency, accessibility and edge states', 'Tune with repeatable scenarios and player-facing telemetry', 'Play-test, compare against targets and iterate until the feel is cohesive'], 'qualityGates': ['Input-to-feedback latency feels immediate', 'Rules and outcomes are legible without explanation', 'Feedback is layered but never noisy', 'Difficulty and timing are fair across target inputs', 'Systems remain secure and deterministic where needed', 'Edge and interruption states recover gracefully', 'Performance remains stable under representative load', 'Play-test evidence supports the final tuning']}, 'ms_movement_feel_director': {'description': 'Movement-feel director with a senior ten-year-studio craft bar.', 'category': 'gameplay', 'stages': ['Define the player fantasy and the exact feeling to create', 'Inspect existing controls, camera, animation, feedback and metrics', 'Set measurable response, timing, acceleration and clarity targets', 'Implement the smallest complete interactive loop in the real engine', 'Layer animation, VFX, audio, haptics and camera feedback with restraint', 'Handle cancellation, failure, latency, accessibility and edge states', 'Tune with repeatable scenarios and player-facing telemetry', 'Play-test, compare against targets and iterate until the feel is cohesive'], 'qualityGates': ['Input-to-feedback latency feels immediate', 'Rules and outcomes are legible without explanation', 'Feedback is layered but never noisy', 'Difficulty and timing are fair across target inputs', 'Systems remain secure and deterministic where needed', 'Edge and interruption states recover gracefully', 'Performance remains stable under representative load', 'Play-test evidence supports the final tuning']}, 'ms_camera_feel_director': {'description': 'Camera-feel director with a senior ten-year-studio craft bar.', 'category': 'gameplay', 'stages': ['Define the player fantasy and the exact feeling to create', 'Inspect existing controls, camera, animation, feedback and metrics', 'Set measurable response, timing, acceleration and clarity targets', 'Implement the smallest complete interactive loop in the real engine', 'Layer animation, VFX, audio, haptics and camera feedback with restraint', 'Handle cancellation, failure, latency, accessibility and edge states', 'Tune with repeatable scenarios and player-facing telemetry', 'Play-test, compare against targets and iterate until the feel is cohesive'], 'qualityGates': ['Input-to-feedback latency feels immediate', 'Rules and outcomes are legible without explanation', 'Feedback is layered but never noisy', 'Difficulty and timing are fair across target inputs', 'Systems remain secure and deterministic where needed', 'Edge and interruption states recover gracefully', 'Performance remains stable under representative load', 'Play-test evidence supports the final tuning']}, 'ms_onboarding_experience_polish': {'description': 'Onboarding experience polish with a senior ten-year-studio craft bar.', 'category': 'gameplay', 'stages': ['Define the player fantasy and the exact feeling to create', 'Inspect existing controls, camera, animation, feedback and metrics', 'Set measurable response, timing, acceleration and clarity targets', 'Implement the smallest complete interactive loop in the real engine', 'Layer animation, VFX, audio, haptics and camera feedback with restraint', 'Handle cancellation, failure, latency, accessibility and edge states', 'Tune with repeatable scenarios and player-facing telemetry', 'Play-test, compare against targets and iterate until the feel is cohesive'], 'qualityGates': ['Input-to-feedback latency feels immediate', 'Rules and outcomes are legible without explanation', 'Feedback is layered but never noisy', 'Difficulty and timing are fair across target inputs', 'Systems remain secure and deterministic where needed', 'Edge and interruption states recover gracefully', 'Performance remains stable under representative load', 'Play-test evidence supports the final tuning']}, 'ms_economy_experience_polish': {'description': 'Economy experience polish with a senior ten-year-studio craft bar.', 'category': 'gameplay', 'stages': ['Define the player fantasy and the exact feeling to create', 'Inspect existing controls, camera, animation, feedback and metrics', 'Set measurable response, timing, acceleration and clarity targets', 'Implement the smallest complete interactive loop in the real engine', 'Layer animation, VFX, audio, haptics and camera feedback with restraint', 'Handle cancellation, failure, latency, accessibility and edge states', 'Tune with repeatable scenarios and player-facing telemetry', 'Play-test, compare against targets and iterate until the feel is cohesive'], 'qualityGates': ['Input-to-feedback latency feels immediate', 'Rules and outcomes are legible without explanation', 'Feedback is layered but never noisy', 'Difficulty and timing are fair across target inputs', 'Systems remain secure and deterministic where needed', 'Edge and interruption states recover gracefully', 'Performance remains stable under representative load', 'Play-test evidence supports the final tuning']}, 'ms_social_experience_polish': {'description': 'Social experience polish with a senior ten-year-studio craft bar.', 'category': 'gameplay', 'stages': ['Define the player fantasy and the exact feeling to create', 'Inspect existing controls, camera, animation, feedback and metrics', 'Set measurable response, timing, acceleration and clarity targets', 'Implement the smallest complete interactive loop in the real engine', 'Layer animation, VFX, audio, haptics and camera feedback with restraint', 'Handle cancellation, failure, latency, accessibility and edge states', 'Tune with repeatable scenarios and player-facing telemetry', 'Play-test, compare against targets and iterate until the feel is cohesive'], 'qualityGates': ['Input-to-feedback latency feels immediate', 'Rules and outcomes are legible without explanation', 'Feedback is layered but never noisy', 'Difficulty and timing are fair across target inputs', 'Systems remain secure and deterministic where needed', 'Edge and interruption states recover gracefully', 'Performance remains stable under representative load', 'Play-test evidence supports the final tuning']}, 'ms_progression_experience_polish': {'description': 'Progression experience polish with a senior ten-year-studio craft bar.', 'category': 'gameplay', 'stages': ['Define the player fantasy and the exact feeling to create', 'Inspect existing controls, camera, animation, feedback and metrics', 'Set measurable response, timing, acceleration and clarity targets', 'Implement the smallest complete interactive loop in the real engine', 'Layer animation, VFX, audio, haptics and camera feedback with restraint', 'Handle cancellation, failure, latency, accessibility and edge states', 'Tune with repeatable scenarios and player-facing telemetry', 'Play-test, compare against targets and iterate until the feel is cohesive'], 'qualityGates': ['Input-to-feedback latency feels immediate', 'Rules and outcomes are legible without explanation', 'Feedback is layered but never noisy', 'Difficulty and timing are fair across target inputs', 'Systems remain secure and deterministic where needed', 'Edge and interruption states recover gracefully', 'Performance remains stable under representative load', 'Play-test evidence supports the final tuning']}, 'ms_studio_critic_loop': {'description': 'Studio-grade critic loop with a senior ten-year-studio craft bar.', 'category': 'quality', 'stages': ['Restate the user-visible acceptance criteria and quality target', 'Inspect the real artifact, project state and current evidence', 'Evaluate craft, usability, correctness, cohesion and technical risk', 'Find the highest-impact defects instead of listing cosmetic trivia', 'Fix blocking and high-value issues in the actual target project', 'Retest happy, edge, failure, accessibility and performance paths', 'Compare before/after evidence against the requested outcome', 'Approve only when evidence is concrete; otherwise return precise revisions'], 'qualityGates': ['No plan or generated text is mistaken for the deliverable', 'The requested artifact exists and works end to end', 'Visual and interaction quality is coherent', 'Known edge states are handled honestly', 'Logs and focused tests are clean', 'Performance meets an explicit target-tier budget', 'Final state is read back from the engine', 'Remaining limitations are disclosed rather than hidden']}, 'ms_visual_quality_audit': {'description': 'Visual quality audit with a senior ten-year-studio craft bar.', 'category': 'quality', 'stages': ['Restate the user-visible acceptance criteria and quality target', 'Inspect the real artifact, project state and current evidence', 'Evaluate craft, usability, correctness, cohesion and technical risk', 'Find the highest-impact defects instead of listing cosmetic trivia', 'Fix blocking and high-value issues in the actual target project', 'Retest happy, edge, failure, accessibility and performance paths', 'Compare before/after evidence against the requested outcome', 'Approve only when evidence is concrete; otherwise return precise revisions'], 'qualityGates': ['No plan or generated text is mistaken for the deliverable', 'The requested artifact exists and works end to end', 'Visual and interaction quality is coherent', 'Known edge states are handled honestly', 'Logs and focused tests are clean', 'Performance meets an explicit target-tier budget', 'Final state is read back from the engine', 'Remaining limitations are disclosed rather than hidden']}, 'ms_ui_quality_audit': {'description': 'UI quality audit with a senior ten-year-studio craft bar.', 'category': 'quality', 'stages': ['Restate the user-visible acceptance criteria and quality target', 'Inspect the real artifact, project state and current evidence', 'Evaluate craft, usability, correctness, cohesion and technical risk', 'Find the highest-impact defects instead of listing cosmetic trivia', 'Fix blocking and high-value issues in the actual target project', 'Retest happy, edge, failure, accessibility and performance paths', 'Compare before/after evidence against the requested outcome', 'Approve only when evidence is concrete; otherwise return precise revisions'], 'qualityGates': ['No plan or generated text is mistaken for the deliverable', 'The requested artifact exists and works end to end', 'Visual and interaction quality is coherent', 'Known edge states are handled honestly', 'Logs and focused tests are clean', 'Performance meets an explicit target-tier budget', 'Final state is read back from the engine', 'Remaining limitations are disclosed rather than hidden']}, 'ms_gameplay_quality_audit': {'description': 'Gameplay quality audit with a senior ten-year-studio craft bar.', 'category': 'quality', 'stages': ['Restate the user-visible acceptance criteria and quality target', 'Inspect the real artifact, project state and current evidence', 'Evaluate craft, usability, correctness, cohesion and technical risk', 'Find the highest-impact defects instead of listing cosmetic trivia', 'Fix blocking and high-value issues in the actual target project', 'Retest happy, edge, failure, accessibility and performance paths', 'Compare before/after evidence against the requested outcome', 'Approve only when evidence is concrete; otherwise return precise revisions'], 'qualityGates': ['No plan or generated text is mistaken for the deliverable', 'The requested artifact exists and works end to end', 'Visual and interaction quality is coherent', 'Known edge states are handled honestly', 'Logs and focused tests are clean', 'Performance meets an explicit target-tier budget', 'Final state is read back from the engine', 'Remaining limitations are disclosed rather than hidden']}, 'ms_performance_quality_audit': {'description': 'Performance quality audit with a senior ten-year-studio craft bar.', 'category': 'quality', 'stages': ['Restate the user-visible acceptance criteria and quality target', 'Inspect the real artifact, project state and current evidence', 'Evaluate craft, usability, correctness, cohesion and technical risk', 'Find the highest-impact defects instead of listing cosmetic trivia', 'Fix blocking and high-value issues in the actual target project', 'Retest happy, edge, failure, accessibility and performance paths', 'Compare before/after evidence against the requested outcome', 'Approve only when evidence is concrete; otherwise return precise revisions'], 'qualityGates': ['No plan or generated text is mistaken for the deliverable', 'The requested artifact exists and works end to end', 'Visual and interaction quality is coherent', 'Known edge states are handled honestly', 'Logs and focused tests are clean', 'Performance meets an explicit target-tier budget', 'Final state is read back from the engine', 'Remaining limitations are disclosed rather than hidden']}, 'ms_cross_engine_integration_audit': {'description': 'Cross-engine integration audit with a senior ten-year-studio craft bar.', 'category': 'quality', 'stages': ['Restate the user-visible acceptance criteria and quality target', 'Inspect the real artifact, project state and current evidence', 'Evaluate craft, usability, correctness, cohesion and technical risk', 'Find the highest-impact defects instead of listing cosmetic trivia', 'Fix blocking and high-value issues in the actual target project', 'Retest happy, edge, failure, accessibility and performance paths', 'Compare before/after evidence against the requested outcome', 'Approve only when evidence is concrete; otherwise return precise revisions'], 'qualityGates': ['No plan or generated text is mistaken for the deliverable', 'The requested artifact exists and works end to end', 'Visual and interaction quality is coherent', 'Known edge states are handled honestly', 'Logs and focused tests are clean', 'Performance meets an explicit target-tier budget', 'Final state is read back from the engine', 'Remaining limitations are disclosed rather than hidden']}, 'ms_release_quality_bar': {'description': 'Release quality bar with a senior ten-year-studio craft bar.', 'category': 'quality', 'stages': ['Restate the user-visible acceptance criteria and quality target', 'Inspect the real artifact, project state and current evidence', 'Evaluate craft, usability, correctness, cohesion and technical risk', 'Find the highest-impact defects instead of listing cosmetic trivia', 'Fix blocking and high-value issues in the actual target project', 'Retest happy, edge, failure, accessibility and performance paths', 'Compare before/after evidence against the requested outcome', 'Approve only when evidence is concrete; otherwise return precise revisions'], 'qualityGates': ['No plan or generated text is mistaken for the deliverable', 'The requested artifact exists and works end to end', 'Visual and interaction quality is coherent', 'Known edge states are handled honestly', 'Logs and focused tests are clean', 'Performance meets an explicit target-tier budget', 'Final state is read back from the engine', 'Remaining limitations are disclosed rather than hidden']}}
ADVANCED_DIRECT_CONTRACTS.update(STUDIO_DIRECT_CONTRACTS)


BUILTIN_TOOLS = [
    {"name": "ms_bridge_status", "description": "Show configured MCP servers, health, tool counts, and bridge version.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "ms_list_resources", "description": "List MCP resources exposed by one connected server. Essential for Unity editor state, instances, project data, and scene inspection.",
     "inputSchema": {"type": "object", "properties": {"server": {"type": "string"}, "cursor": {"type": "string"}}, "required": ["server"]}},
    {"name": "ms_read_resource", "description": "Read an MCP resource URI from one connected server.",
     "inputSchema": {"type": "object", "properties": {"server": {"type": "string"}, "uri": {"type": "string"}}, "required": ["server", "uri"]}},
    {"name": "ms_list_skills", "description": "List reusable Multi-Script workflows, optionally filtered by engine or search text. Use filters to avoid loading the full catalog.",
     "inputSchema": {"type": "object", "properties": {"engine": {"type": "string", "enum": ["roblox", "unity", "godot", "blender", "figma", "general"]}, "query": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 100}}}},
    {"name": "ms_recommend_skills", "description": "Recommend the smallest relevant skill set for an objective and engine without dumping the full catalog.",
     "inputSchema": {"type": "object", "properties": {"objective": {"type": "string"}, "engine": {"type": "string", "enum": ["roblox", "unity", "godot", "blender", "figma", "general"]}, "limit": {"type": "integer", "minimum": 1, "maximum": 10}}, "required": ["objective"]}},
    {"name": "ms_get_skill", "description": "Load one reusable workflow by id before performing that kind of work.",
     "inputSchema": {"type": "object", "properties": {"skill_id": {"type": "string"}}, "required": ["skill_id"]}},
    {"name": "ms_critic_review", "description": "Review implementation evidence and return pass/revise plus missing verification. Use after meaningful changes.",
     "inputSchema": {"type": "object", "properties": {
         "objective": {"type": "string"}, "changed_assets": {"type": "array", "items": {"type": "string"}},
         "checks": {"type": "array", "items": {"type": "string"}}, "evidence": {"type": "array", "items": {"type": "string"}},
         "known_issues": {"type": "array", "items": {"type": "string"}}, "round": {"type": "integer", "minimum": 1, "maximum": 3}},
      "required": ["objective", "checks", "evidence"]}},
    {"name": "ms_workflow_plan", "description": "Create a domain-aware production plan for UI/UX, animation, VFX, modeling, coding, testing, and builds across connected engines.",
     "inputSchema": {"type": "object", "properties": {"objective": {"type": "string"}, "domains": {"type": "array", "items": {"type": "string"}}, "targets": {"type": "array", "items": {"type": "string"}}}, "required": ["objective", "domains"]}},
    {"name": "ms_parallel_tools", "description": "Run independent MCP tools on different servers concurrently, such as generating a model in Blender while inspecting or building in Unity. Use at most one call per server and only when operations do not depend on each other.",
     "inputSchema": {"type": "object", "properties": {"calls": {"type": "array", "maxItems": 6, "items": {"type": "object", "properties": {"server": {"type": "string"}, "tool": {"type": "string"}, "arguments": {"type": "object"}}, "required": ["server", "tool"]}}}, "required": ["calls"]}},
    {"name": "ms_enhance_brief", "description": "Expand a short creative request into a polished, cohesive, implementation-ready brief while preserving the user's intent and avoiding scope creep.",
     "inputSchema": {"type": "object", "properties": {"request": {"type": "string"}, "engine": {"type": "string"}, "quality": {"type": "string", "enum": ["polished", "ambitious"]}, "constraints": {"type": "array", "items": {"type": "string"}}}, "required": ["request"]}},
    {"name": "ms_quality_scorecard", "description": "Score a result across clarity, cohesion, originality, usability, responsiveness, accessibility, technical quality, performance, and evidence; return prioritized improvements.",
     "inputSchema": {"type": "object", "properties": {"objective": {"type": "string"}, "evidence": {"type": "array", "items": {"type": "string"}}, "scores": {"type": "object", "additionalProperties": {"type": "number"}}, "notes": {"type": "array", "items": {"type": "string"}}}, "required": ["objective"]}},
    {"name": "ms_quality_checklist", "description": "Return a specialist quality gate for UI/UX, animation, VFX, 3D models, gameplay, or builds.",
     "inputSchema": {"type": "object", "properties": {"domain": {"type": "string", "enum": ["ui-ux", "animation", "vfx", "model", "gameplay", "build"]}, "target": {"type": "string"}}, "required": ["domain"]}},
    {"name": "ms_test_matrix", "description": "Generate edge-case and platform test coverage for a feature.", "inputSchema": {"type": "object", "properties": {"feature": {"type": "string"}, "platforms": {"type": "array", "items": {"type": "string"}}}, "required": ["feature"]}},
    {"name": "ms_create_canvas_texture", "description": "Create an actual reusable SVG texture on the local computer using deterministic Canvas-style procedural patterns. Returns the file path and SVG source for engine, Blender, browser-canvas, or Figma import.",
     "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}, "pattern": {"type": "string", "enum": ["studs", "brick", "checker", "grid", "dots", "stripes", "hex", "noise"]}, "width": {"type": "integer", "minimum": 64, "maximum": 4096}, "height": {"type": "integer", "minimum": 64, "maximum": 4096}, "tile_size": {"type": "integer", "minimum": 4, "maximum": 512}, "background": {"type": "string"}, "foreground": {"type": "string"}, "accent": {"type": "string"}, "seed": {"type": "integer"}}, "required": ["name", "pattern"]}},
    {"name": "ms_create_canvas_ui", "description": "Draw an actual SVG UI/UX mockup locally from frames, text, buttons, cards, inputs, images, and badges. Use as the universal fallback when a live Figma MCP is unavailable; the SVG can also be imported into Figma.",
     "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}, "width": {"type": "integer", "minimum": 240, "maximum": 4096}, "height": {"type": "integer", "minimum": 240, "maximum": 4096}, "title": {"type": "string"}, "theme": {"type": "string", "enum": ["dark", "light", "game", "glass"]}, "accent": {"type": "string"}, "components": {"type": "array", "maxItems": 100, "items": {"type": "object", "properties": {"type": {"type": "string", "enum": ["frame", "text", "button", "card", "input", "image", "badge"]}, "label": {"type": "string"}, "x": {"type": "number"}, "y": {"type": "number"}, "width": {"type": "number"}, "height": {"type": "number"}}}}}, "required": ["name"]}},
    {"name": "ms_figma_handoff", "description": "Create a Figma-ready UI/UX execution plan with pages, frames, components, variants, Auto Layout, variables, accessibility, prototype flows, and import assets; reports whether a configured Figma MCP server is currently available.",
     "inputSchema": {"type": "object", "properties": {"objective": {"type": "string"}, "platform": {"type": "string"}, "screens": {"type": "array", "items": {"type": "string"}}, "style": {"type": "string"}}, "required": ["objective"]}},
    {"name": "ms_asset_budget", "description": "Create measurable geometry, texture, animation, audio and VFX budgets.", "inputSchema": {"type": "object", "properties": {"target": {"type": "string"}, "platform": {"type": "string"}}, "required": ["target"]}},
    {"name": "ms_release_checklist", "description": "Generate a build, smoke-test, rollback and release evidence checklist.", "inputSchema": {"type": "object", "properties": {"engine": {"type": "string"}, "platform": {"type": "string"}}, "required": ["engine"]}},
    {"name": "ms_risk_register", "description": "Identify technical, content, integration and delivery risks with mitigations.", "inputSchema": {"type": "object", "properties": {"objective": {"type": "string"}, "systems": {"type": "array", "items": {"type": "string"}}}, "required": ["objective"]}},
    {"name": "ms_engine_handoff", "description": "Create a deterministic Blender-to-engine asset handoff checklist.",
     "inputSchema": {"type": "object", "properties": {
         "source": {"type": "string", "enum": ["blender", "other"]},
         "target": {"type": "string", "enum": ["roblox", "unity", "godot", "unreal", "other"]},
         "asset_type": {"type": "string"}, "format": {"type": "string"}},
      "required": ["source", "target", "asset_type"]}},
    {"name": "ms_prompt_rescue", "description": "Turn a vague, contradictory, or low-quality game-development request into a strong production brief without changing its core intent.", "inputSchema": {"type":"object","properties":{"prompt":{"type":"string"},"engine":{"type":"string","enum":["roblox","unity","godot","other"]},"genre":{"type":"string"},"constraints":{"type":"array","items":{"type":"string"}}},"required":["prompt"]}},
    {"name": "ms_game_blueprint", "description": "Create an end-to-end game blueprint covering pillars, loop, systems, content, architecture, milestones, risks, testing, and release.", "inputSchema": {"type":"object","properties":{"concept":{"type":"string"},"engine":{"type":"string"},"platforms":{"type":"array","items":{"type":"string"}},"multiplayer":{"type":"boolean"}},"required":["concept","engine"]}},
    {"name": "ms_vertical_slice_plan", "description": "Reduce a large game idea to the smallest polished playable slice with explicit exit criteria.", "inputSchema": {"type":"object","properties":{"objective":{"type":"string"},"engine":{"type":"string"},"deadline":{"type":"string"}},"required":["objective","engine"]}},
    {"name": "ms_system_design_review", "description": "Review a game system design for ownership, lifecycle, coupling, failure modes, testability, migration, and performance.", "inputSchema": {"type":"object","properties":{"system":{"type":"string"},"engine":{"type":"string"},"design":{"type":"string"},"dependencies":{"type":"array","items":{"type":"string"}}},"required":["system","engine"]}},
    {"name": "ms_gameplay_balance_plan", "description": "Produce measurable gameplay/economy balance variables, simulations, guardrails, telemetry, and tuning experiments.", "inputSchema": {"type":"object","properties":{"system":{"type":"string"},"goals":{"type":"array","items":{"type":"string"}},"player_segments":{"type":"array","items":{"type":"string"}}},"required":["system"]}},
    {"name": "ms_multiplayer_authority_audit", "description": "Audit multiplayer trust boundaries, validation, ownership, rate limits, prediction, reconnects, and exploit cases.", "inputSchema": {"type":"object","properties":{"engine":{"type":"string"},"feature":{"type":"string"},"messages":{"type":"array","items":{"type":"string"}}},"required":["engine","feature"]}},
    {"name": "ms_save_migration_plan", "description": "Design a backwards-compatible save-schema migration with validation, backups, idempotency, rollout, and rollback.", "inputSchema": {"type":"object","properties":{"engine":{"type":"string"},"current_version":{"type":"string"},"target_version":{"type":"string"},"changes":{"type":"array","items":{"type":"string"}}},"required":["engine","current_version","target_version"]}},
    {"name": "ms_performance_budget_plan", "description": "Create measurable CPU, memory, GPU, network, loading, and asset budgets for target platforms.", "inputSchema": {"type":"object","properties":{"engine":{"type":"string"},"platforms":{"type":"array","items":{"type":"string"}},"scene":{"type":"string"}},"required":["engine"]}},
    {"name": "ms_accessibility_audit", "description": "Audit controls, readability, motion, audio, color, cognitive load, difficulty, and assist settings.", "inputSchema": {"type":"object","properties":{"feature":{"type":"string"},"platforms":{"type":"array","items":{"type":"string"}},"evidence":{"type":"array","items":{"type":"string"}}},"required":["feature"]}},
    {"name": "ms_content_pipeline_plan", "description": "Plan source files, naming, import settings, validation, optimization, versioning, and engine handoff for game content.", "inputSchema": {"type":"object","properties":{"engine":{"type":"string"},"content_types":{"type":"array","items":{"type":"string"}},"source_tools":{"type":"array","items":{"type":"string"}}},"required":["engine","content_types"]}},
    {"name": "ms_playtest_protocol", "description": "Create a repeatable playtest protocol with cohorts, tasks, observations, telemetry, severity, and decision rules.", "inputSchema": {"type":"object","properties":{"objective":{"type":"string"},"build":{"type":"string"},"participants":{"type":"integer","minimum":1},"platforms":{"type":"array","items":{"type":"string"}}},"required":["objective"]}},
    {"name": "ms_definition_of_done", "description": "Create a strict, evidence-based completion gate for a game feature or release.", "inputSchema": {"type":"object","properties":{"feature":{"type":"string"},"engine":{"type":"string"},"risk":{"type":"string","enum":["low","medium","high"]}},"required":["feature","engine"]}},
    {"name": "ms_orchestrate_request", "description": "Automatically select a complementary multi-skill stack for a player request: design, implementation, polish, optimization, and validation—not a single isolated skill.", "inputSchema": {"type":"object","properties":{"request":{"type":"string"},"engine":{"type":"string","enum":["roblox","unity","godot"]},"max_skills":{"type":"integer","minimum":3,"maximum":8}},"required":["request","engine"]}},
    {"name": "ms_activate_skill_stack", "description": "Load and combine a professional skill stack into an executable work contract. Selection is preparation; real MCP implementation and verification must follow.", "inputSchema": {"type":"object","properties":{"request":{"type":"string"},"engine":{"type":"string"},"skill_ids":{"type":"array","minItems":2,"maxItems":8,"items":{"type":"string"}}},"required":["request","engine"]}},
    {"name": "ms_animation_director", "description": "Create an engine-ready professional animation direction and implementation plan covering rig, clips, state logic, layering, IK, events, networking, polish, and validation.", "inputSchema": {"type":"object","properties":{"engine":{"type":"string"},"subject":{"type":"string"},"style":{"type":"string"},"actions":{"type":"array","items":{"type":"string"}}},"required":["engine","subject"]}},
    {"name": "ms_texture_art_pipeline", "description": "Create a complete texture/material art workflow from visual target through source creation, UV/tiling, channel packing, import, optimization, and in-engine validation.", "inputSchema": {"type":"object","properties":{"engine":{"type":"string"},"asset":{"type":"string"},"style":{"type":"string"},"platforms":{"type":"array","items":{"type":"string"}}},"required":["engine","asset"]}},
    {"name": "ms_art_direction", "description": "Turn a visual idea into a cohesive professional art direction with shape, color, material, lighting, composition, readability, asset rules, and review gates.", "inputSchema": {"type":"object","properties":{"engine":{"type":"string"},"concept":{"type":"string"},"mood":{"type":"string"},"references":{"type":"array","items":{"type":"string"}}},"required":["engine","concept"]}},
    {"name": "ms_uiux_production", "description": "Create a complete UI/UX production contract: flow, hierarchy, components, states, responsive behavior, input navigation, accessibility, implementation, and usability evidence.", "inputSchema": {"type":"object","properties":{"engine":{"type":"string"},"feature":{"type":"string"},"platforms":{"type":"array","items":{"type":"string"}},"style":{"type":"string"}},"required":["engine","feature"]}},
    {"name": "ms_vfx_production", "description": "Create a professional VFX execution contract covering story beat, timing, readability, particles, shaders, lighting, audio sync, pooling, budgets, variants, and runtime capture.", "inputSchema": {"type":"object","properties":{"engine":{"type":"string"},"effect":{"type":"string"},"platforms":{"type":"array","items":{"type":"string"}},"style":{"type":"string"}},"required":["engine","effect"]}},
    {"name": "ms_audio_production", "description": "Create a professional game-audio execution contract covering event taxonomy, assets, spatialization, mixing, concurrency, adaptive music, accessibility, memory, and runtime validation.", "inputSchema": {"type":"object","properties":{"engine":{"type":"string"},"feature":{"type":"string"},"style":{"type":"string"},"platforms":{"type":"array","items":{"type":"string"}}},"required":["engine","feature"]}},
    {"name": "ms_list_virtual_tools", "description": "List the 200 engine-specific virtual quality tools, filtered by engine/domain/query, without flooding model context.", "inputSchema": {"type":"object","properties":{"engine":{"type":"string","enum":["roblox","unity","godot","blender"]},"domain":{"type":"string"},"query":{"type":"string"},"limit":{"type":"integer","minimum":1,"maximum":50}}}},
    {"name": "ms_match_virtual_tools", "description": "Match a user prompt to the strongest engine-specific virtual tools for UI, animation, graphics, shaders, VFX, models, textures, audio, gameplay, systems, or production.", "inputSchema": {"type":"object","properties":{"request":{"type":"string"},"engine":{"type":"string","enum":["roblox","unity","godot","blender"]},"limit":{"type":"integer","minimum":1,"maximum":5}},"required":["request","engine"]}},
    {"name": "ms_virtual_tool_details", "description": "Load one virtual tool's exact engine-specific stages, quality gates, and output contract.", "inputSchema": {"type":"object","properties":{"tool_id":{"type":"string"}},"required":["tool_id"]}},
    {"name": "ms_run_virtual_tool", "description": "Apply a matched virtual tool as hidden augmentation, then create the real deliverable through MCP. Internal briefs must not replace the requested result.", "inputSchema": {"type":"object","properties":{"tool_id":{"type":"string"},"request":{"type":"string"},"project_context":{"type":"string"},"constraints":{"type":"array","items":{"type":"string"}}},"required":["tool_id","request"]}},
    {"name": "ms_elevenlabs_status", "description": "Check whether the local bridge has a securely configured ElevenLabs API key. Never returns or logs the key.", "inputSchema": {"type":"object","properties":{}}},
    {"name": "ms_generate_sound_effect", "description": "Generate a real sound effect, ambience, Foley, UI sound, musical element, or seamless loop through the ElevenLabs Sound Effects API and save it locally for engine import.", "inputSchema": {"type":"object","properties":{"text":{"type":"string"},"name":{"type":"string"},"duration_seconds":{"type":"number","minimum":0.5,"maximum":30},"loop":{"type":"boolean"},"prompt_influence":{"type":"number","minimum":0,"maximum":1},"output_format":{"type":"string","enum":["mp3_22050_32","mp3_44100_64","mp3_44100_96","mp3_44100_128","mp3_44100_192","pcm_16000","pcm_22050","pcm_24000","pcm_44100"]},"target_engine":{"type":"string","enum":["roblox","unity","godot","other"]}},"required":["text","name"]}},
    {"name": "ms_list_generated_audio", "description": "List locally generated ElevenLabs audio assets and their metadata for reuse or engine import.", "inputSchema": {"type":"object","properties":{"limit":{"type":"integer","minimum":1,"maximum":200}}}},
    {'name': 'ms_camera_system_design', 'description': 'Design and validate a production camera system for gameplay, cinematics, collision, comfort, and every target input.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_input_mapping_plan', 'description': 'Create a cross-platform input architecture with rebinding, action maps, prompts, focus, and accessibility.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_character_controller_review', 'description': 'Review or design a responsive character controller covering movement, slopes, jumps, grounding, networking, and animation integration.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_combat_system_design', 'description': 'Create a complete combat architecture with timing, hit validation, damage, status effects, feedback, networking, and balance hooks.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_ai_behavior_design', 'description': 'Design game AI with perception, decision making, navigation, combat tactics, recovery, debugging, and performance budgets.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_quest_system_design', 'description': 'Design a data-driven quest, objective, dialogue, reward, persistence, and migration system.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_inventory_economy_design', 'description': 'Design inventory and economy systems with stacking, transactions, crafting, shops, persistence, anti-duplication, and tuning.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_procedural_generation_plan', 'description': 'Create a deterministic procedural-generation pipeline with seeds, constraints, validation, streaming, and authored overrides.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_shader_production', 'description': 'Create an engine-ready shader production contract covering visual intent, lighting, variants, parameters, optimization, and fallback tiers.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_lighting_production', 'description': 'Design and implement cohesive game lighting with readability, mood, probes, shadows, post-processing, and performance tiers.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_environment_production', 'description': 'Create a complete environment-production contract from blockout through modular assets, dressing, lighting, gameplay readability, and optimization.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_character_model_production', 'description': 'Create a character-model pipeline covering concept translation, topology, UVs, materials, facial needs, LODs, rig handoff, and engine validation.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_rigging_production', 'description': 'Create a robust rigging contract for skeletons, skinning, controls, constraints, retargeting, physics, export, and engine tests.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_localization_audit', 'description': 'Audit localization readiness across text, fonts, layouts, input, audio, pluralization, cultural context, and QA.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_telemetry_plan', 'description': 'Create a privacy-aware telemetry specification with events, schemas, funnels, diagnostics, sampling, dashboards, and validation.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_liveops_plan', 'description': 'Plan safe live operations covering configuration, events, segmentation, rollout, economy protection, moderation, observability, and rollback.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_store_compliance_check', 'description': 'Generate a platform-store compliance and submission gate for privacy, monetization, content ratings, permissions, packaging, and review evidence.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_security_abuse_review', 'description': 'Review a game feature for trust boundaries, secrets, permissions, injection, cheating, moderation, privacy, and incident response.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_bug_triage', 'description': 'Turn a bug report into a reproducible, prioritized engineering investigation with hypotheses, instrumentation, fix scope, and verification.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_regression_plan', 'description': 'Create a risk-based regression plan covering changed surfaces, dependencies, automated checks, manual journeys, platforms, performance, and rollback.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_dialogue_production', 'description': 'Dialogue production: branching dialogue, conditions, localization, VO hooks and runtime validation. Returns a real-execution contract with verification gates.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_cinematic_production', 'description': 'Cinematic production: storyboards, cameras, animation, timing, audio, skip/replay and runtime capture. Returns a real-execution contract with verification gates.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_tutorial_design', 'description': 'Tutorial design: onboarding, progressive disclosure, practice, recovery, telemetry and accessibility. Returns a real-execution contract with verification gates.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_navigation_system_design', 'description': 'Navigation system design: navigation meshes, agents, links, avoidance, streaming and recovery. Returns a real-execution contract with verification gates.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_physics_system_audit', 'description': 'Physics system audit: collision layers, fixed-step behavior, ownership, determinism and profiling. Returns a real-execution contract with verification gates.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_vehicle_system_design', 'description': 'Vehicle system design: handling, suspension, controls, camera, networking, damage and tuning. Returns a real-execution contract with verification gates.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_world_streaming_plan', 'description': 'World streaming plan: partitioning, loading, persistence, seams, budgets and failure recovery. Returns a real-execution contract with verification gates.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_level_design_review', 'description': 'Level design review: metrics, flow, pacing, landmarks, encounters, accessibility and playtests. Returns a real-execution contract with verification gates.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_puzzle_design', 'description': 'Puzzle design: rules, teaching, feedback, hinting, reset, exploits and difficulty progression. Returns a real-execution contract with verification gates.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_social_system_design', 'description': 'Social system design: parties, friends, presence, invites, privacy, moderation and safety. Returns a real-execution contract with verification gates.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_matchmaking_design', 'description': 'Matchmaking design: queues, skill, latency, parties, backfill, abuse prevention and telemetry. Returns a real-execution contract with verification gates.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_modding_pipeline', 'description': 'Modding pipeline: sandboxing, schemas, packaging, validation, compatibility and moderation. Returns a real-execution contract with verification gates.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_build_automation', 'description': 'Build automation: reproducible builds, versioning, tests, signing, artifacts and rollback. Returns a real-execution contract with verification gates.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_crash_diagnostics', 'description': 'Crash diagnostics: symbols, dumps, breadcrumbs, reproduction, grouping and fix verification. Returns a real-execution contract with verification gates.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_memory_leak_audit', 'description': 'Memory leak audit: allocation baselines, lifecycle ownership, retention paths and soak testing. Returns a real-execution contract with verification gates.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_network_bandwidth_audit', 'description': 'Network bandwidth audit: message inventory, payloads, rates, compression, interest and loss tests. Returns a real-execution contract with verification gates.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_mobile_optimization', 'description': 'Mobile optimization: thermal, memory, GPU, input, UI, battery and device-tier validation. Returns a real-execution contract with verification gates.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_vr_xr_production', 'description': 'VR/XR production: comfort, interaction, locomotion, scale, performance and accessibility. Returns a real-execution contract with verification gates.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_web_build_optimization', 'description': 'Web build optimization: download size, startup, memory, browser compatibility and caching. Returns a real-execution contract with verification gates.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_console_readiness', 'description': 'Console readiness: platform input, suspend/resume, users, saves, certification and performance. Returns a real-execution contract with verification gates.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_controller_haptics', 'description': 'Controller haptics: event taxonomy, envelopes, device capabilities, comfort and testing. Returns a real-execution contract with verification gates.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_iconography_production', 'description': 'Iconography production: visual language, grids, states, readability, localization and export. Returns a real-execution contract with verification gates.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_marketing_capture_plan', 'description': 'Marketing capture plan: shot list, builds, camera paths, clean UI, formats and approvals. Returns a real-execution contract with verification gates.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_npc_population_system', 'description': 'NPC population system: spawning, schedules, simulation LOD, persistence and performance. Returns a real-execution contract with verification gates.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_weather_system', 'description': 'Weather system: state model, visuals, gameplay, audio, transitions, replication and budgets. Returns a real-execution contract with verification gates.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_day_night_system', 'description': 'Day/night system: time authority, lighting, schedules, saves, networking and transitions. Returns a real-execution contract with verification gates.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_destructible_system', 'description': 'Destructible system: fracture states, authority, damage, debris, persistence and optimization. Returns a real-execution contract with verification gates.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_replay_system', 'description': 'Replay system: capture schema, determinism, seek, versioning, storage and playback validation. Returns a real-execution contract with verification gates.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_photo_mode', 'description': 'Photo mode: camera controls, pause policy, filters, UI, platform saves and privacy. Returns a real-execution contract with verification gates.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_roblox_capability_audit', 'description': 'Audit the live Roblox Studio MCP connection, native tool catalogue, major capability groups, and companion-plugin readiness without inventing unavailable tools.', 'inputSchema': {'type': 'object', 'properties': {'refresh': {'type': 'boolean'}}}},
    {'name': 'ms_engine_catalog', 'description': 'Load the exact live tool catalogue for one engine server, including schemas and native/direct counts.', 'inputSchema': {'type': 'object', 'properties': {'server': {'type': 'string'}, 'refresh': {'type': 'boolean'}}, 'required': ['server']}},
    {'name': 'ms_engine_connection_test', 'description': 'Run a real read-only smoke test against a named engine server and return verified evidence.', 'inputSchema': {'type': 'object', 'properties': {'server': {'type': 'string'}, 'tool': {'type': 'string'}, 'arguments': {'type': 'object'}}, 'required': ['server']}},
    {'name': 'ms_call_engine_tool', 'description': 'Reliably execute one exact native MCP tool on one exact engine server, avoiding cross-engine name collisions.', 'inputSchema': {'type': 'object', 'properties': {'server': {'type': 'string'}, 'tool': {'type': 'string'}, 'arguments': {'type': 'object'}, 'timeout_seconds': {'type': 'integer', 'minimum': 1, 'maximum': 300}}, 'required': ['server', 'tool']}},
    {'name': 'ms_engine_capability_map', 'description': 'Show live engine connections, exact native tool counts, Multi-Script direct tools, skills and virtual capability coverage.', 'inputSchema': {'type': 'object', 'properties': {'refresh': {'type': 'boolean'}}}},
    {'name': 'ms_roblox_animation_studio', 'description': 'Roblox animation studio with a senior ten-year-studio craft bar.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'style': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_blender_animation_studio', 'description': 'Blender animation studio with a senior ten-year-studio craft bar.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'style': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_unity_animation_studio', 'description': 'Unity animation studio with a senior ten-year-studio craft bar.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'style': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_godot_animation_studio', 'description': 'Godot animation studio with a senior ten-year-studio craft bar.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'style': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_combat_animation_polish', 'description': 'Combat animation polish with a senior ten-year-studio craft bar.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'style': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_locomotion_animation_polish', 'description': 'Locomotion animation polish with a senior ten-year-studio craft bar.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'style': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_cinematic_animation_polish', 'description': 'Cinematic animation polish with a senior ten-year-studio craft bar.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'style': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_creature_animation', 'description': 'Creature animation with a senior ten-year-studio craft bar.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'style': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_facial_animation', 'description': 'Facial animation with a senior ten-year-studio craft bar.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'style': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_animation_retargeting', 'description': 'Animation retargeting with a senior ten-year-studio craft bar.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'style': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_animation_state_machine', 'description': 'Animation state-machine direction with a senior ten-year-studio craft bar.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'style': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_animation_runtime_qa', 'description': 'Animation runtime QA with a senior ten-year-studio craft bar.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'style': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_gui_design_studio', 'description': 'Professional GUI design studio with a senior ten-year-studio craft bar.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'style': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_roblox_gui_studio', 'description': 'Roblox GUI production studio with a senior ten-year-studio craft bar.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'style': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_unity_ui_studio', 'description': 'Unity UI production studio with a senior ten-year-studio craft bar.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'style': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_godot_ui_studio', 'description': 'Godot UI production studio with a senior ten-year-studio craft bar.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'style': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_ui_motion_design', 'description': 'UI motion and micro-interaction design with a senior ten-year-studio craft bar.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'style': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_design_system_production', 'description': 'Design-system production with a senior ten-year-studio craft bar.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'style': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_responsive_interface_polish', 'description': 'Responsive interface polish with a senior ten-year-studio craft bar.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'style': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_controller_navigation_ux', 'description': 'Controller-navigation UX with a senior ten-year-studio craft bar.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'style': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_accessible_interface_polish', 'description': 'Accessible interface polish with a senior ten-year-studio craft bar.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'style': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_usability_polish', 'description': 'Usability and flow polish with a senior ten-year-studio craft bar.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'style': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_blender_model_studio', 'description': 'Blender model studio with a senior ten-year-studio craft bar.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'style': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_stylized_art_production', 'description': 'Stylized art production with a senior ten-year-studio craft bar.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'style': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_realistic_art_production', 'description': 'Realistic art production with a senior ten-year-studio craft bar.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'style': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_environment_art_studio', 'description': 'Environment art studio with a senior ten-year-studio craft bar.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'style': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_character_art_studio', 'description': 'Character art studio with a senior ten-year-studio craft bar.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'style': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_prop_art_studio', 'description': 'Prop art studio with a senior ten-year-studio craft bar.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'style': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_pbr_material_studio', 'description': 'PBR material studio with a senior ten-year-studio craft bar.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'style': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_texture_detail_polish', 'description': 'Texture detail polish with a senior ten-year-studio craft bar.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'style': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_lighting_art_studio', 'description': 'Lighting art studio with a senior ten-year-studio craft bar.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'style': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_composition_render_polish', 'description': 'Composition and render polish with a senior ten-year-studio craft bar.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'style': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_game_feel_director', 'description': 'Game-feel director with a senior ten-year-studio craft bar.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'style': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_combat_feel_director', 'description': 'Combat-feel director with a senior ten-year-studio craft bar.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'style': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_movement_feel_director', 'description': 'Movement-feel director with a senior ten-year-studio craft bar.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'style': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_camera_feel_director', 'description': 'Camera-feel director with a senior ten-year-studio craft bar.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'style': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_onboarding_experience_polish', 'description': 'Onboarding experience polish with a senior ten-year-studio craft bar.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'style': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_economy_experience_polish', 'description': 'Economy experience polish with a senior ten-year-studio craft bar.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'style': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_social_experience_polish', 'description': 'Social experience polish with a senior ten-year-studio craft bar.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'style': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_progression_experience_polish', 'description': 'Progression experience polish with a senior ten-year-studio craft bar.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'style': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_studio_critic_loop', 'description': 'Studio-grade critic loop with a senior ten-year-studio craft bar.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'style': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_visual_quality_audit', 'description': 'Visual quality audit with a senior ten-year-studio craft bar.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'style': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_ui_quality_audit', 'description': 'UI quality audit with a senior ten-year-studio craft bar.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'style': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_gameplay_quality_audit', 'description': 'Gameplay quality audit with a senior ten-year-studio craft bar.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'style': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_performance_quality_audit', 'description': 'Performance quality audit with a senior ten-year-studio craft bar.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'style': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_cross_engine_integration_audit', 'description': 'Cross-engine integration audit with a senior ten-year-studio craft bar.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'style': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_release_quality_bar', 'description': 'Release quality bar with a senior ten-year-studio craft bar.', 'inputSchema': {'type': 'object', 'properties': {'engine': {'type': 'string'}, 'feature': {'type': 'string'}, 'style': {'type': 'string'}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'context': {'type': 'string'}}, 'required': ['feature']}},
    {'name': 'ms_studio_director', 'description': 'Silently match any meaningful user request to a senior-studio stack of direct tools, skills, virtual tools and evidence gates before real MCP execution.', 'inputSchema': {'type': 'object', 'properties': {'request': {'type': 'string'}, 'engine': {'type': 'string', 'enum': ['roblox', 'unity', 'godot', 'blender', 'figma', 'general']}, 'platforms': {'type': 'array', 'items': {'type': 'string'}}, 'style': {'type': 'string'}, 'constraints': {'type': 'array', 'items': {'type': 'string'}}, 'max_tools': {'type': 'integer', 'minimum': 1, 'maximum': 5}}, 'required': ['request', 'engine']}},
    {'name': 'ms_list_direct_tools', 'description': 'Discover Multi-Script direct tools by category or query without dumping the full catalogue into model context.', 'inputSchema': {'type': 'object', 'properties': {'category': {'type': 'string', 'enum': ['animation', 'uiux', 'art3d', 'gameplay', 'quality', 'all']}, 'query': {'type': 'string'}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100}}}},
    {'name': 'ms_direct_tool_details', 'description': 'Load one direct tool schema plus its exact execution stages and quality gates.', 'inputSchema': {'type': 'object', 'properties': {'tool': {'type': 'string'}}, 'required': ['tool']}},
]
BUILTIN_TOOL_NAMES = {t["name"] for t in BUILTIN_TOOLS}
BUILTIN_TOOL_BY_NAME = {t["name"]: t for t in BUILTIN_TOOLS}


def _normalize_tool_arguments(schema, arguments, tool_name="tool"):
    """Normalize provider quirks, then validate the JSON-Schema subset MCP uses."""
    if arguments is None:
        arguments = {}
    if isinstance(arguments, str):
        try: arguments = json.loads(arguments)
        except Exception: raise RuntimeError(f"{tool_name}: parameters must be a JSON object")
    if not isinstance(arguments, dict):
        raise RuntimeError(f"{tool_name}: parameters must be an object, got {type(arguments).__name__}")
    schema = schema if isinstance(schema, dict) else {"type": "object", "properties": {}}
    props = schema.get("properties") or {}
    out = dict(arguments)
    errors = []
    def convert(value, spec, path):
        typ = spec.get("type")
        if value is None: return None
        try:
            if typ == "string":
                if isinstance(value, (dict, list)): raise ValueError("must be a string")
                value = str(value)
            elif typ == "integer":
                if isinstance(value, bool): raise ValueError("must be an integer")
                value = int(value)
            elif typ == "number":
                if isinstance(value, bool): raise ValueError("must be a number")
                value = float(value)
            elif typ == "boolean":
                if isinstance(value, str):
                    low=value.strip().lower()
                    if low in ("true","1","yes","on"): value=True
                    elif low in ("false","0","no","off"): value=False
                    else: raise ValueError("must be true or false")
                elif isinstance(value, (int,float)) and value in (0,1): value=bool(value)
                elif not isinstance(value,bool): raise ValueError("must be a boolean")
            elif typ == "array":
                if isinstance(value,str):
                    try: parsed=json.loads(value)
                    except Exception: parsed=[x.strip() for x in value.split(",") if x.strip()]
                    value=parsed
                if not isinstance(value,list): value=[value]
                item_spec=spec.get("items") or {}
                value=[convert(x,item_spec,f"{path}[{i}]") for i,x in enumerate(value)]
                if spec.get("minItems") is not None and len(value)<spec["minItems"]: raise ValueError(f"needs at least {spec['minItems']} item(s)")
                if spec.get("maxItems") is not None and len(value)>spec["maxItems"]: raise ValueError(f"allows at most {spec['maxItems']} item(s)")
            elif typ == "object":
                if isinstance(value,str): value=json.loads(value)
                if not isinstance(value,dict): raise ValueError("must be an object")
            if "enum" in spec:
                exact=next((x for x in spec["enum"] if x==value),None)
                if exact is None and isinstance(value,str): exact=next((x for x in spec["enum"] if isinstance(x,str) and x.lower()==value.lower()),None)
                if exact is None: raise ValueError("must be one of: "+", ".join(map(str,spec["enum"])))
                value=exact
            if isinstance(value,(int,float)) and not isinstance(value,bool):
                if spec.get("minimum") is not None and value<spec["minimum"]: raise ValueError(f"must be >= {spec['minimum']}")
                if spec.get("maximum") is not None and value>spec["maximum"]: raise ValueError(f"must be <= {spec['maximum']}")
            return value
        except (ValueError,TypeError,json.JSONDecodeError) as e:
            errors.append(f"{path} {e}"); return value
    for key,spec in props.items():
        if key in out: out[key]=convert(out[key],spec,key)
    for key in schema.get("required") or []:
        if key not in out or out[key] is None or (isinstance(out[key],str) and not out[key].strip()): errors.append(f"{key} is required")
    if errors: raise RuntimeError(f"{tool_name}: invalid parameters: "+"; ".join(errors))
    return out


def _builtin_call(name, arguments, manager):
    args = arguments or {}
    # Load catalogues before early-return handlers such as Studio Director and
    # engine capability mapping. Later handlers reuse the same local snapshots.
    skills = _load_skills()
    virtual_tools = _load_virtual_tools()
    studio_standard = _load_studio_standard()
    quality_presets = _load_quality_presets()
    if name == "ms_list_direct_tools":
        cat=str(args.get("category") or "all").lower();q=str(args.get("query") or "").lower();limit=max(1,min(100,int(args.get("limit") or 30)))
        rows=[]
        for t in BUILTIN_TOOLS:
            n=t.get("name","");spec=STUDIO_DIRECT_CONTRACTS.get(n);tc=(spec or {}).get("category","core")
            hay=(n+" "+t.get("description","")).lower()
            if (cat=="all" or tc==cat) and (not q or all(x in hay for x in q.split())): rows.append({"name":n,"category":tc,"description":t.get("description","")})
        return {"text":json.dumps({"totalMatches":len(rows),"tools":rows[:limit]},indent=2),"images":[]}
    if name == "ms_direct_tool_details":
        target=str(args.get("tool") or "").strip();tool=next((t for t in BUILTIN_TOOLS if t.get("name")==target),None)
        if not tool: raise RuntimeError(f"unknown direct tool '{target}'")
        return {"text":json.dumps({"tool":tool,"contract":ADVANCED_DIRECT_CONTRACTS.get(target),"executionRequired":True},indent=2),"images":[]}
    if name == "ms_studio_director":
        request=str(args.get("request") or "").strip();engine=str(args.get("engine") or "").lower();limit=max(1,min(5,int(args.get("max_tools") or 3)))
        if not request or engine not in {"roblox","unity","godot","blender","figma","general"}: raise RuntimeError("request and valid engine are required")
        txt=request.lower();wanted=[]
        normalized = re.sub(r"[^a-z0-9]+", " ", txt)
        preset_rank=[]
        for pid,preset in quality_presets.items():
            if engine not in set(preset.get("engines") or []) and "general" not in set(preset.get("engines") or []): continue
            signals=[str(x).lower() for x in (preset.get("signals") or [])]
            score=sum(10 + len(sig.split()) for sig in signals if sig in normalized)
            req_words=set(re.findall(r"[a-z0-9]+",normalized));sig_words=set(re.findall(r"[a-z0-9]+"," ".join(signals)))
            score += len(req_words & sig_words)
            if score>0:preset_rank.append((score,pid,preset))
        preset_rank.sort(key=lambda x:(-x[0],x[1]));matched_presets=[{"id":pid,"intent":v.get("intent"),"enhancements":v.get("enhancements") or [],"evidence":v.get("evidence") or []} for _,pid,v in preset_rank[:3]]
        def add(*names):
            for n in names:
                if n in STUDIO_DIRECT_CONTRACTS and n not in wanted:wanted.append(n)
        if any(x in txt for x in ["animat","idle","walk","run","attack","rig","motion"]):
            add({"roblox":"ms_roblox_animation_studio","blender":"ms_blender_animation_studio","unity":"ms_unity_animation_studio","godot":"ms_godot_animation_studio"}.get(engine,"ms_animation_state_machine"),"ms_animation_state_machine","ms_animation_runtime_qa")
        if any(x in txt for x in ["ui","gui","menu","shop","hud","interface","screen","button","inventory"]):
            add({"roblox":"ms_roblox_gui_studio","unity":"ms_unity_ui_studio","godot":"ms_godot_ui_studio"}.get(engine,"ms_gui_design_studio"),"ms_ui_motion_design","ms_ui_quality_audit")
        if any(x in txt for x in ["model","mesh","character","prop","environment","texture","material","art","light"]):
            add("ms_blender_model_studio" if engine=="blender" or "model" in txt or "mesh" in txt else "ms_environment_art_studio","ms_pbr_material_studio","ms_visual_quality_audit")
        if any(x in txt for x in ["combat","movement","camera","feel","gameplay","progression","economy"]):
            add("ms_combat_feel_director" if "combat" in txt else "ms_game_feel_director","ms_gameplay_quality_audit")
        if not wanted:add("ms_studio_critic_loop","ms_visual_quality_audit","ms_performance_quality_audit")
        if "ms_studio_critic_loop" not in wanted:wanted.append("ms_studio_critic_loop")
        words={w for w in re.findall(r"[a-z0-9]+",txt) if len(w)>2};ranked=[]
        for sid,v in skills.items():
            if v.get("engine") not in {engine,"general",None}:continue
            hay=(sid+" "+v.get("name","")+" "+v.get("description","")+" "+" ".join(v.get("tags") or [])).lower();score=sum(1 for w in words if w in hay)
            ranked.append((score,sid,v))
        ranked.sort(key=lambda x:(-x[0],x[1]));skill_ids=[sid for _,sid,_ in ranked[:5]]
        vtools=[]
        for tid,v in virtual_tools.items():
            if v.get("engine")!=engine:continue
            hay=(tid+" "+v.get("name","")+" "+v.get("description","")).lower();score=sum(1 for w in words if w in hay)
            vtools.append((score,tid))
        vtools=[x[1] for x in sorted(vtools,key=lambda x:(-x[0],x[1]))[:2]]
        return {"text":json.dumps({"request":request,"engine":engine,"hiddenAugmentation":True,"selectedDirectTools":wanted[:limit],"selectedSkills":skill_ids,"selectedVirtualTools":vtools,"matchedQualityPresets":matched_presets,"qualityIntent":"senior ten-year multidisciplinary studio bar","automaticEnhancement":{"simplePromptIsEnough":True,"preserveUserIntent":True,"applySilently":True,"scopeRule":"Add only compatible craft, states, accessibility, security, performance and evidence; do not replace the idea or stop at planning."},"mandatoryExecution":["inspect the real project","apply these contracts internally without narrating bureaucracy","create or edit the actual requested artifact through exact MCP calls","verify in the final engine and iterate on defects","report only the beautiful working result and concrete evidence"],"neverSubstitutePlanForDeliverable":True},indent=2),"images":[]}
    if name == "ms_roblox_capability_audit":
        tools = manager.list_server_tools("roblox", refresh=bool(args.get("refresh")))
        names = sorted(str(t.get("name") or "") for t in tools if t.get("name"))
        groups = {
            "inspect": ("get", "list", "search", "read", "state", "tree"),
            "scripts": ("script", "source", "luau", "code"),
            "instances": ("instance", "object", "property", "attribute", "tag"),
            "playtest": ("play", "test", "run", "stop", "output", "log"),
            "assets": ("asset", "image", "mesh", "material", "audio", "animation"),
            "world": ("terrain", "workspace", "camera", "lighting", "selection"),
        }
        coverage = {group: [n for n in names if any(k in n.lower() for k in keys)] for group, keys in groups.items()}
        with plugin_state_lock:
            companion = dict(plugin_state)
        companion["connected"] = bool(companion.get("lastSeen") and time.time() - float(companion["lastSeen"]) < 20)
        return {"text": json.dumps({
            "server": "roblox", "nativeToolCount": len(names), "nativeTools": names,
            "coverage": coverage, "companionPlugin": companion,
            "ready": bool(names),
            "note": "Native tools are advertised by Roblox Studio's official MCP server. The companion plugin improves diagnostics and project readiness; it does not fabricate native tools.",
        }, indent=2), "images": []}
    if name == "ms_engine_catalog":
        server=str(args.get("server") or "").strip()
        if not server: raise RuntimeError("server is required")
        tools=manager.list_server_tools(server, refresh=bool(args.get("refresh")))
        return {"text":json.dumps({"server":server,"nativeToolCount":len(tools),"multiScriptDirectToolCount":len(BUILTIN_TOOLS),"totalCallableForServer":len(tools)+len(BUILTIN_TOOLS),"tools":tools},indent=2),"images":[]}
    if name == "ms_call_engine_tool":
        server=str(args.get("server") or "").strip();tool=str(args.get("tool") or "").strip()
        if not server or not tool: raise RuntimeError("server and tool are required")
        timeout=max(1,min(300,int(args.get("timeout_seconds") or 120)))
        return manager.call_on_server(server,tool,args.get("arguments") or {},timeout)
    if name == "ms_engine_connection_test":
        server=str(args.get("server") or "").strip()
        if not server: raise RuntimeError("server is required")
        return {"text":json.dumps(manager.smoke_test(server,args.get("tool"),args.get("arguments") or {}),indent=2),"images":[]}
    if name == "ms_engine_capability_map":
        if args.get("refresh"): manager.list_tools(refresh=True)
        rows=[]
        for h in manager.health():
            rows.append({"server":h["id"],"alive":h["alive"],"nativeTools":h["tools"],"callableWithDirectTools":h["tools"]+len(BUILTIN_TOOLS)})
        return {"text":json.dumps({"servers":rows,"multiScriptDirectTools":len(BUILTIN_TOOLS),"skills":len(skills),"virtualTools":len(virtual_tools)},indent=2),"images":[]}
    if name in ADVANCED_DIRECT_CONTRACTS:
        feature = str(args.get("feature") or "").strip()
        if not feature: raise RuntimeError("feature is required")
        spec = ADVANCED_DIRECT_CONTRACTS[name]
        result = {
            "tool": name,
            "engine": str(args.get("engine") or "project engine"),
            "feature": feature,
            "platforms": [str(x) for x in (args.get("platforms") or [])],
            "constraints": [str(x) for x in (args.get("constraints") or [])],
            "context": str(args.get("context") or ""),
            "stages": spec["stages"],
            "qualityGates": spec["qualityGates"],
            "realImplementationRequired": True,
            "execution": "Inspect the current project, use exact connected engine/content MCP commands to create or change the real deliverable, run the relevant quality gates, read back final state, and report only verified evidence.",
        }
        return {"text": json.dumps(result, indent=2), "images": []}
    if name == "ms_elevenlabs_status":
        return {"text":json.dumps(_elevenlabs_audio().status(),indent=2),"images":[]}
    if name == "ms_generate_sound_effect":
        audio=_elevenlabs_audio(); meta=audio.generate_sound_effect(text=args.get("text"),name=args.get("name") or "sound-effect",duration_seconds=args.get("duration_seconds"),loop=bool(args.get("loop",False)),prompt_influence=args.get("prompt_influence",0.3),output_format=args.get("output_format") or "mp3_44100_128")
        meta["targetEngine"]=args.get("target_engine")
        meta["next"]="Import the generated file through the target engine MCP, configure 2D/3D, looping, attenuation, compression and bus settings, then play-test it in context."
        return {"text":json.dumps(meta,indent=2),"images":[]}
    if name == "ms_list_generated_audio":
        return {"text":json.dumps(_elevenlabs_audio().list_generated_audio(args.get("limit") or 50),indent=2),"images":[]}
    if name == "ms_bridge_status":
        return {"text": json.dumps({"bridgeVersion": BRIDGE_VERSION, "servers": manager.health()}, indent=2), "images": []}
    if name == "ms_list_resources":
        server = str(args.get("server", "")).strip()
        if not server:
            raise RuntimeError("server is required")
        return {"text": json.dumps(manager.list_resources(server, args.get("cursor")), indent=2), "images": []}
    if name == "ms_read_resource":
        server, uri = str(args.get("server", "")).strip(), str(args.get("uri", "")).strip()
        if not server or not uri:
            raise RuntimeError("server and uri are required")
        return manager.read_resource(server, uri)
    if name == "ms_list_virtual_tools":
        engine=str(args.get("engine","")).strip().lower(); domain=str(args.get("domain","")).strip().lower(); query=str(args.get("query","")).strip().lower(); limit=max(1,min(50,int(args.get("limit") or 20)))
        rows=[]
        for tid,v in virtual_tools.items():
            if engine and v.get("engine")!=engine: continue
            if domain and v.get("domain")!=domain: continue
            hay=f"{tid} {v.get('name','')} {v.get('description','')} {' '.join(v.get('signals') or [])}".lower()
            if query and query not in hay: continue
            rows.append({"id":tid,"name":v.get("name"),"engine":v.get("engine"),"domain":v.get("domain"),"description":v.get("description")})
        return {"text":json.dumps({"count":min(limit,len(rows)),"totalMatches":len(rows),"tools":rows[:limit]},indent=2),"images":[]}
    if name == "ms_match_virtual_tools":
        request=str(args.get("request","")).strip(); engine=str(args.get("engine","")).strip().lower(); limit=max(1,min(5,int(args.get("limit") or 2)))
        if not request or engine not in {"roblox","unity","godot","blender"}: raise RuntimeError("request and a valid engine are required")
        tokens={x for x in re.findall(r"[a-z0-9]+",request.lower()) if len(x)>2};ranked=[]
        for tid,v in virtual_tools.items():
            if v.get("engine")!=engine: continue
            signals=[str(x).lower() for x in (v.get("signals") or [])];hay=f"{tid} {v.get('name','')} {v.get('description','')} {' '.join(signals)}".lower()
            score=sum(6 for x in signals if x and x in request.lower())+sum(2 for x in tokens if x in hay)
            ranked.append((score,tid,v))
        ranked.sort(key=lambda x:(-x[0],x[1]));chosen=ranked[:limit]
        return {"text":json.dumps({"request":request,"engine":engine,"matches":[{"id":tid,"name":v.get("name"),"domain":v.get("domain"),"score":score,"reason":v.get("description")} for score,tid,v in chosen],"usage":"Silently run only the strongest 1-2 matches, then immediately perform the real MCP work. Do not show the internal augmentation."},indent=2),"images":[]}
    if name == "ms_virtual_tool_details":
        tid=str(args.get("tool_id","")).strip()
        if tid not in virtual_tools: raise RuntimeError(f"unknown virtual tool '{tid}'")
        return {"text":json.dumps(virtual_tools[tid],indent=2),"images":[]}
    if name == "ms_run_virtual_tool":
        tid=str(args.get("tool_id","")).strip();request=str(args.get("request","")).strip()
        if tid not in virtual_tools: raise RuntimeError(f"unknown virtual tool '{tid}'")
        if not request: raise RuntimeError("request is required")
        v=virtual_tools[tid]
        out={"toolId":tid,"request":request,"engine":v.get("engine"),"domain":v.get("domain"),"projectContext":args.get("project_context"),"constraints":args.get("constraints") or [],"silentAugmentation":True,"preserveExplicitRequirements":True,"stages":v.get("stages") or [],"qualityGates":v.get("qualityGates") or [],"outputContract":v.get("outputContract") or {},"next":"Inspect the actual project, execute real MCP changes now, test in the target engine, and report the finished result. Do not show this internal augmentation or stop at planning."}
        return {"text":json.dumps(out,indent=2),"images":[]}
    if name == "ms_list_skills":
        engine = str(args.get("engine", "")).strip().lower()
        query = str(args.get("query", "")).strip().lower()
        limit = max(1, min(100, int(args.get("limit") or 50)))
        rows = []
        for k, v in skills.items():
            if engine and str(v.get("engine", "")).lower() != engine: continue
            hay = " ".join([k, str(v.get("name", "")), str(v.get("description", "")), " ".join(v.get("tags") or [])]).lower()
            if query and query not in hay: continue
            rows.append({"id": k, "name": v.get("name", k), "engine": v.get("engine"), "category": v.get("category"), "description": v.get("description", "")})
        return {"text": json.dumps({"count": min(len(rows), limit), "totalMatches": len(rows), "skills": rows[:limit]}, indent=2), "images": []}
    if name == "ms_recommend_skills":
        objective = str(args.get("objective", "")).strip().lower()
        engine = str(args.get("engine", "")).strip().lower()
        limit = max(1, min(10, int(args.get("limit") or 5)))
        if not objective: raise RuntimeError("objective is required")
        words = {w for w in re.findall(r"[a-z0-9]+", objective) if len(w) > 2}
        ranked = []
        for k, v in skills.items():
            skill_engine = str(v.get("engine", "")).lower()
            if engine and skill_engine and skill_engine != engine: continue
            name_text = f"{k} {v.get('name','')}".lower()
            body_text = f"{v.get('description','')} {' '.join(v.get('tags') or [])}".lower()
            score = sum(4 for w in words if w in name_text) + sum(1 for w in words if w in body_text)
            if engine and skill_engine == engine: score += 2
            if score: ranked.append((score, k, v))
        # Prefer the established concise skill id when a new specialist pack overlaps it.
        ranked.sort(key=lambda x: (-x[0], len(x[1]), x[1]))
        rows = [{"id": k, "name": v.get("name", k), "engine": v.get("engine"), "score": score, "description": v.get("description", "")} for score, k, v in ranked[:limit]]
        return {"text": json.dumps({"objective": objective, "engine": engine or None, "recommendations": rows}, indent=2), "images": []}
    if name == "ms_get_skill":
        sid = str(args.get("skill_id", "")).strip()
        if sid not in skills:
            raise RuntimeError(f"unknown skill '{sid}'. Call ms_list_skills first.")
        skill = dict(skills[sid]); engine = skill.get("engine") or "general"
        standard = {"principles": studio_standard.get("principles", []), "definitionOfDone": studio_standard.get("definitionOfDone", []), "evidence": studio_standard.get("evidence", []), "recovery": studio_standard.get("recovery", []), "enginePractice": (studio_standard.get("engines") or {}).get(engine, (studio_standard.get("engines") or {}).get("general", []))}
        return {"text": json.dumps({"id": sid, **skill, "studioStandard": standard}, indent=2), "images": []}
    if name == "ms_critic_review":
        objective = str(args.get("objective", "")).strip()
        checks = [str(x).strip() for x in (args.get("checks") or []) if str(x).strip()]
        evidence = [str(x).strip() for x in (args.get("evidence") or []) if str(x).strip()]
        changed = [str(x).strip() for x in (args.get("changed_assets") or []) if str(x).strip()]
        issues = [str(x).strip() for x in (args.get("known_issues") or []) if str(x).strip()]
        round_no = max(1, min(3, int(args.get("round") or 1)))
        missing = []
        if not objective: missing.append("state a concrete objective")
        if not changed: missing.append("identify the files/scenes/assets changed")
        if not checks: missing.append("run at least one relevant check")
        if not evidence: missing.append("provide observable test/build/read-back evidence")
        weak = [x for x in evidence if len(x) < 12 or x.lower() in {"ok", "works", "done", "passed"}]
        if weak: missing.append("replace vague evidence with command output or observed behavior")
        if issues: missing.append("resolve or explicitly accept known issues: " + "; ".join(issues[:5]))
        verdict = "pass" if not missing else "revise"
        return {"text": json.dumps({"verdict": verdict, "round": round_no, "objective": objective,
            "missing": missing, "next": "finish and report" if verdict == "pass" else "fix the missing items, re-run checks, then call ms_critic_review again",
            "maxRoundsReached": round_no >= 3 and verdict != "pass"}, indent=2), "images": []}
    if name == "ms_enhance_brief":
        request = str(args.get("request", "")).strip()
        if not request: raise RuntimeError("request is required")
        low = request.lower(); quality = str(args.get("quality", "polished")).lower()
        constraints = [str(x).strip() for x in (args.get("constraints") or []) if str(x).strip()]
        goals = ["Preserve the original concept and make every added detail support it.", "Use a distinctive, cohesive visual language rather than generic template styling."]
        design, states, verification = [], [], []
        if any(x in low for x in ("ui", "panel", "menu", "hud", "shop", "store")):
            design += ["Establish a strong focal hierarchy, consistent spacing grid, readable type scale, semantic color tokens, and reusable components.", "Build responsively for mouse, keyboard/controller focus, and touch with safe areas and localization expansion."]
            states += ["default, hover, focus, pressed, selected, disabled", "loading, empty, affordable/unaffordable, success, warning, and error"]
            verification += ["review at phone, desktop, and ultrawide aspect ratios", "test keyboard/controller/touch navigation and contrast"]
        if any(x in low for x in ("shop", "store", "purchase", "item")):
            design += ["Include category navigation, product cards, price/currency clarity, owned/equipped badges, rarity treatment, details preview, and a deliberate purchase confirmation flow.", "Keep the primary purchase action obvious while preventing accidental buys and clearly explaining insufficient funds or unavailable items."]
        if "classic" in low and str(args.get("engine", "")).lower() == "roblox":
            design += ["Interpret classic as a Roblox-inspired visual language: strong rectangular hierarchy, crisp outlines, restrained bevel/inset depth, blocky iconography, readable legacy-style typography, and a limited nostalgic palette; avoid generic glassmorphism and excessive rounding.", "Use authentic Roblox UI primitives and responsive constraints, preserving the existing game's era and visual language instead of copying a modern template."]
            states += ["classic tactile hover/press feedback, clear selection outline, controller focus, and touch-sized targets"]
            verification += ["compare the final ScreenGui at phone, desktop, and console-safe resolutions", "confirm the style reads as intentionally classic while text and controls remain accessible"]
        if any(x in low for x in ("stud", "studded", "brick", "lego")):
            design += ["Use a seamless studded surface at believable scale with consistent spacing, softly beveled stud edges, controlled highlights, and subtle roughness/value variation to avoid flat repetition.", "Reserve the stud texture for structural surfaces and accents; keep text and critical controls on calmer inset plates for readability."]
        if any(x in low for x in ("cool", "stylish", "polished", "premium")):
            design += ["Choose one memorable signature motif and support it with restrained depth, shadows, highlights, icon treatment, and material-aware motion instead of adding unrelated decoration."]
        if any(x in low for x in ("3d", "model", "prop", "environment")):
            design += ["Define silhouette, real-world scale, pivot/origin, material breakup, topology/UV budget, collision, LODs, and camera-distance readability."]
            verification += ["inspect scale, normals, materials, collision, and LOD transitions after engine import"]
        if not design: design += ["Define hierarchy, composition, material/color language, interaction feedback, edge states, accessibility, performance budget, and final polish appropriate to the deliverable."]
        motion = ["Use short purposeful transitions with consistent easing, clear feedback, and a reduced-motion fallback."]
        if quality == "ambitious": goals.append("Add a portfolio-quality signature moment without expanding beyond the requested feature.")
        verification += ["inspect the finished result in context", "run a focused functional test with clean logs", "score the result with ms_quality_scorecard and fix the weakest dimensions"]
        brief = {"originalRequest": request, "engine": args.get("engine"), "quality": quality, "constraints": constraints, "preserveExplicitRequirements": True, "augmentationMode": "silent", "goals": goals, "designDirection": design, "requiredStates": states, "motionAndFeedback": motion, "verification": verification, "executionDirective": "Use this internally, inspect the real project, then create the requested deliverable directly. Do not present this rewritten brief unless the user asks."}
        enhanced = request + "\n\nEnhance it with:\n- " + "\n- ".join(goals + design + motion) + ("\nRequired states: " + ", ".join(states) if states else "") + "\nVerify by: " + "; ".join(verification) + "."
        return {"text": json.dumps({"enhancedPrompt": enhanced, "brief": brief, "implementImmediately": True, "showBriefToUser": False}, indent=2), "images": []}
    if name == "ms_quality_scorecard":
        dims = ["clarity", "cohesion", "originality", "usability", "responsiveness", "accessibility", "technicalQuality", "performance", "evidence"]
        supplied = args.get("scores") or {}; evidence = [str(x) for x in (args.get("evidence") or [])]
        scores = {d: max(0, min(10, float(supplied[d]))) for d in dims if d in supplied}
        explicit_weak = sorted([d for d,v in scores.items() if v < 7], key=lambda d: scores[d])
        unscored = [d for d in dims if d not in scores]
        priorities = (explicit_weak + unscored)[:3]
        if not evidence: priorities = (["evidence"] + [x for x in priorities if x != "evidence"])[:3]
        verdict = "pass" if len(scores) == len(dims) and all(v >= 7 for v in scores.values()) and evidence else "improve"
        return {"text": json.dumps({"objective": args.get("objective"), "verdict": verdict, "scores": scores, "unscored": unscored, "priorityImprovements": priorities, "evidenceCount": len(evidence), "notes": args.get("notes") or []}, indent=2), "images": []}
    if name == "ms_workflow_plan":
        objective = str(args.get("objective", "")).strip()
        domains = [str(x).strip().lower() for x in (args.get("domains") or []) if str(x).strip()]
        targets = [str(x).strip().lower() for x in (args.get("targets") or []) if str(x).strip()]
        phases = ["Discover current project state and define measurable acceptance criteria."]
        mapping = {
            "ui": "Design information hierarchy and states; implement responsive layout; verify navigation, readability, focus, and input.",
            "ui-ux": "Design information hierarchy and states; implement responsive layout; verify navigation, readability, focus, and input.",
            "animation": "Define clips, rig/avatar constraints, transitions, root motion, events, and interruption cases; preview and test in runtime.",
            "vfx": "Define timing, readability, color/contrast, spawn bounds, pooling, and performance budget; capture visual evidence.",
            "model": "Set scale/poly/UV/material/collision budgets; create in Blender; export, import, and validate in the target engine.",
            "gameplay": "Implement the smallest vertical slice; add automated checks and a focused play test.",
            "build": "Compile, run tests, create a development build, inspect logs, and smoke-test the artifact.",
        }
        phases += [mapping.get(d, f"Implement and verify the {d} workstream.") for d in domains]
        if "blender" in targets and any(t in targets for t in ("unity", "roblox", "godot", "unreal")):
            phases.append("Use ms_parallel_tools only for independent Blender and engine operations; use ms_engine_handoff before import.")
        phases += ["Integrate all workstreams and test cross-system behavior.", "Run ms_quality_checklist for each domain, then ms_critic_review with concrete evidence."]
        return {"text": json.dumps({"objective": objective, "domains": domains, "targets": targets, "phases": phases}, indent=2), "images": []}
    if name == "ms_parallel_tools":
        calls = list(args.get("calls") or [])
        if not calls or len(calls) > 6:
            raise RuntimeError("calls must contain 1 to 6 entries")
        servers = [str(c.get("server", "")).strip() for c in calls]
        if any(not x for x in servers) or len(set(servers)) != len(servers):
            raise RuntimeError("each parallel call must target a different non-empty server")
        def run_one(item):
            sid, tool = str(item.get("server", "")).strip(), str(item.get("tool", "")).strip()
            if not tool: raise RuntimeError("tool is required")
            result = manager.call_on_server(sid, tool, item.get("arguments") or {}, 120)
            return sid, tool, result
        rows, images = [], []
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(calls)) as pool:
            future_map = {pool.submit(run_one, item): item for item in calls}
            for fut in concurrent.futures.as_completed(future_map):
                item = future_map[fut]
                try:
                    sid, tool, result = fut.result()
                    rows.append({"server": sid, "tool": tool, "ok": True, "text": result.get("text", "")})
                    images.extend(result.get("images") or [])
                except Exception as exc:
                    rows.append({"server": item.get("server"), "tool": item.get("tool"), "ok": False, "error": str(exc)})
        return {"text": json.dumps({"results": rows}, indent=2), "images": images}
    if name == "ms_quality_checklist":
        domain = str(args.get("domain", "")).strip().lower()
        gates = {
            "ui-ux": ["all states represented", "responsive anchors/layout", "keyboard/controller/touch path", "readable contrast and type", "empty/error/loading feedback", "no console errors"],
            "animation": ["rig/avatar compatibility", "clip loop/timing", "transition and interruption behavior", "root motion policy", "events aligned", "runtime preview without warnings"],
            "vfx": ["silhouette/readability", "timing and lifecycle", "pooling/no leaks", "bounds and culling", "target-platform particle/GPU budget", "multi-angle capture"],
            "model": ["correct scale/orientation/pivot", "clean normals/topology", "UV/material integrity", "poly/texture budget", "collision/LOD", "export-import round trip"],
            "gameplay": ["acceptance criteria", "edge cases", "save/network/input interactions", "focused automated test", "play-mode reproduction", "clean logs"],
            "build": ["clean compile", "tests pass", "development build produced", "startup smoke test", "runtime logs clean", "artifact/version recorded"],
        }
        if domain not in gates: raise RuntimeError(f"unknown quality domain '{domain}'")
        return {"text": json.dumps({"domain": domain, "target": args.get("target"), "requiredEvidence": gates[domain]}, indent=2), "images": []}
    if name == "ms_test_matrix":
        f=str(args.get("feature", "feature")); plats=args.get("platforms") or ["target platform"]
        checks=["happy path", "invalid/missing data", "rapid repeat and interruption", "save/load or reconnect", "input/device changes", "performance under expected load", "clean logs"]
        return {"text": json.dumps({"feature": f, "platforms": plats, "checks": checks}, indent=2), "images": []}
    if name == "ms_create_canvas_texture":
        raw_name = str(args.get("name", "texture")).strip() or "texture"
        safe = re.sub(r"[^a-zA-Z0-9._-]+", "-", raw_name).strip("-.")[:80] or "texture"
        pattern = str(args.get("pattern", "checker")).lower()
        w = max(64, min(4096, int(args.get("width") or 1024))); h = max(64, min(4096, int(args.get("height") or 1024)))
        tile = max(4, min(512, int(args.get("tile_size") or 64)))
        bg, fg, ac = str(args.get("background") or "#24242b"), str(args.get("foreground") or "#8b82f6"), str(args.get("accent") or "#c7c3ff")
        seed = int(args.get("seed") or 1); rnd = random.Random(seed)
        defs, shapes = [], [f'<rect width="{w}" height="{h}" fill="{html.escape(bg)}"/>']
        if pattern == "checker":
            for y in range(0,h,tile):
                for x in range(0,w,tile):
                    if (x//tile+y//tile)%2==0: shapes.append(f'<rect x="{x}" y="{y}" width="{tile}" height="{tile}" fill="{html.escape(fg)}"/>')
        elif pattern in ("grid","brick"):
            for y in range(0,h,tile):
                offset = tile//2 if pattern=="brick" and (y//tile)%2 else 0
                shapes.append(f'<path d="M0 {y}H{w}" stroke="{html.escape(fg)}" stroke-width="2"/>')
                for x in range(-offset,w,tile): shapes.append(f'<path d="M{x} {y}v{tile}" stroke="{html.escape(fg)}" stroke-width="2"/>')
        elif pattern in ("dots","studs"):
            r=max(2,tile*.28)
            for y in range(tile//2,h,tile):
                for x in range(tile//2,w,tile):
                    shapes.append(f'<circle cx="{x}" cy="{y}" r="{r:.1f}" fill="{html.escape(fg)}" stroke="{html.escape(ac)}" stroke-width="{max(1,tile*.04):.1f}"/>')
        elif pattern == "stripes":
            for x in range(-h,w,tile*2): shapes.append(f'<path d="M{x} {h}L{x+h} 0" stroke="{html.escape(fg)}" stroke-width="{tile}" opacity=".8"/>')
        elif pattern == "hex":
            rr=tile/2; dy=rr*1.732
            for row,y in enumerate([i*dy for i in range(int(h/dy)+2)]):
                for x in range(-tile,w+tile,int(tile*1.5)):
                    cx=x+(tile*.75 if row%2 else 0); pts=[]
                    for i in range(6):
                        import math; a=math.pi/3*i; pts.append(f"{cx+rr*math.cos(a):.1f},{y+rr*math.sin(a):.1f}")
                    shapes.append(f'<polygon points="{" ".join(pts)}" fill="none" stroke="{html.escape(fg)}" stroke-width="2"/>')
        elif pattern == "noise":
            for _ in range(min(5000,max(200,w*h//800))):
                x,y=rnd.randrange(w),rnd.randrange(h); r=rnd.uniform(1,max(2,tile*.12)); op=rnd.uniform(.08,.45)
                shapes.append(f'<circle cx="{x}" cy="{y}" r="{r:.1f}" fill="{html.escape(fg)}" opacity="{op:.2f}"/>')
        else: raise RuntimeError(f"unsupported pattern '{pattern}'")
        svg=f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">{"".join(defs+shapes)}</svg>'
        out_dir=os.path.join(HERE,"generated","canvas"); os.makedirs(out_dir,exist_ok=True); path=os.path.join(out_dir,safe+".svg")
        with open(path,"w",encoding="utf-8") as f: f.write(svg)
        return {"text": json.dumps({"created": path, "format":"SVG", "size":[w,h], "pattern":pattern, "seed":seed, "figmaImport":"Drag the SVG into Figma or pass this path/source to a connected Figma MCP.", "svg":svg}, indent=2), "images": []}
    if name == "ms_create_canvas_ui":
        raw_name=str(args.get("name","ui-mockup")); safe=re.sub(r"[^a-zA-Z0-9._-]+","-",raw_name).strip("-.")[:80] or "ui-mockup"
        w=max(240,min(4096,int(args.get("width") or 1440))); h=max(240,min(4096,int(args.get("height") or 900)))
        theme=str(args.get("theme") or "dark"); accent=str(args.get("accent") or "#7c6cf2")
        pal={"light":("#f5f6fa","#ffffff","#1c1d24","#666978"),"game":("#10131c","#1b2231","#f6f0d8","#aab2c5"),"glass":("#141522","#26283aaa","#ffffff","#b7b9c8"),"dark":("#111116","#1c1c24","#f4f3fa","#9a98a8")}.get(theme,("#111116","#1c1c24","#f4f3fa","#9a98a8"))
        bg,panel,text,muted=pal; title=html.escape(str(args.get("title") or raw_name))
        comps=args.get("components") or [{"type":"frame","x":80,"y":90,"width":w-160,"height":h-180,"label":""},{"type":"text","x":120,"y":145,"width":500,"height":48,"label":title},{"type":"card","x":120,"y":220,"width":320,"height":260,"label":"Featured item"},{"type":"button","x":120,"y":520,"width":220,"height":56,"label":"Continue"}]
        els=[f'<rect width="{w}" height="{h}" fill="{bg}"/>']
        for c in comps[:100]:
            typ=str(c.get("type","frame")); x=float(c.get("x",0)); y=float(c.get("y",0)); cw=max(1,float(c.get("width",200))); ch=max(1,float(c.get("height",50))); label=html.escape(str(c.get("label",typ.title())))
            if typ=="text": els.append(f'<text x="{x}" y="{y+ch*.72}" fill="{text}" font-family="Arial,sans-serif" font-size="{max(12,min(42,ch*.65)):.0f}" font-weight="700">{label}</text>')
            else:
                fill=accent if typ=="button" else panel; stroke=accent if typ in ("input","badge") else "#ffffff18"; radius=min(18,ch*.28)
                els.append(f'<rect x="{x}" y="{y}" width="{cw}" height="{ch}" rx="{radius}" fill="{fill}" stroke="{stroke}"/>')
                if label: els.append(f'<text x="{x+16}" y="{y+ch/2+5}" fill="{text}" font-family="Arial,sans-serif" font-size="14" font-weight="600">{label}</text>')
        svg=f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">{"".join(els)}</svg>'
        out_dir=os.path.join(HERE,"generated","canvas"); os.makedirs(out_dir,exist_ok=True); path=os.path.join(out_dir,safe+".svg")
        with open(path,"w",encoding="utf-8") as f:f.write(svg)
        return {"text":json.dumps({"created":path,"format":"SVG","size":[w,h],"theme":theme,"components":len(comps),"figmaImport":"Import the SVG into Figma, then convert repeated elements to components/variants and apply Auto Layout.","svg":svg},indent=2),"images":[]}
    if name == "ms_figma_handoff":
        objective=str(args.get("objective","")).strip(); screens=args.get("screens") or ["Primary screen"]
        health=manager.health(); figma=next((x for x in health if "figma" in str(x.get("id","")).lower()),None)
        plan={"objective":objective,"platform":args.get("platform") or "responsive","style":args.get("style") or "project design system","figmaMcpAvailable":bool(figma and figma.get("alive")),"pages":["00 Cover & notes","01 Foundations","02 Components","03 Screens","04 Prototype"],"foundations":["color variables with semantic names","type scale and text styles","spacing/radius/elevation variables","grid and breakpoints"],"screens":screens,"componentRequirements":["Auto Layout","constraints/resizing","variants for default/hover/focus/pressed/disabled/loading/error","component properties","accessible naming"],"execution":"If figmaMcpAvailable is true, call list_commands for server figma and create the file content now; otherwise call ms_create_canvas_ui and import its SVG into Figma."}
        return {"text":json.dumps(plan,indent=2),"images":[]}
    if name == "ms_asset_budget":
        return {"text": json.dumps({"target": args.get("target"), "platform": args.get("platform"), "budgets": ["triangle/vertex and draw-call limits", "texture dimensions/compression/memory", "material and shader variants", "bones/weights/animation clips", "audio format/streaming/voices", "VFX particles/overdraw/bounds", "LOD and collision complexity"]}, indent=2), "images": []}
    if name == "ms_release_checklist":
        return {"text": json.dumps({"engine": args.get("engine"), "platform": args.get("platform"), "checks": ["clean compile", "automated tests", "version and content manifest", "development build", "startup and changed-feature smoke test", "logs/crash reporting", "performance sanity", "rollback artifact"]}, indent=2), "images": []}
    if name == "ms_risk_register":
        systems=args.get("systems") or []
        risks=[{"area": x, "risk": "unknown integration or edge-case failure", "mitigation": "inspect, test in isolation, then integration-test with evidence"} for x in systems]
        risks += [{"area": "delivery", "risk": "unverified success", "mitigation": "quality checklist plus critic review and runnable build"}]
        return {"text": json.dumps({"objective": args.get("objective"), "risks": risks}, indent=2), "images": []}
    if name == "ms_prompt_rescue":
        prompt=str(args.get("prompt","")).strip()
        if not prompt: raise RuntimeError("prompt is required")
        engine=str(args.get("engine") or "unspecified"); constraints=[str(x) for x in (args.get("constraints") or [])]
        brief={"intent":prompt,"engine":engine,"genre":args.get("genre") or "infer from project","inferredDefaults":["preserve existing project conventions","build the smallest polished playable version first","support target inputs and aspect ratios","include loading/empty/error/interruption states","use secure authority and validated persistence where relevant","measure performance instead of assuming it"],"acceptanceCriteria":["real deliverable exists in the target project","primary user flow works end to end","edge and failure states are handled","focused tests and clean logs are recorded","visual/usability quality is coherent","performance is acceptable on the weakest target"],"constraints":constraints,"ambiguitiesToAskOnlyIfConsequential":["irreversible deletion or migration","paid/external service commitment","conflicting art/product direction","missing credentials or unavailable engine"]}
        return {"text":json.dumps(brief,indent=2),"images":[]}
    if name == "ms_game_blueprint":
        return {"text":json.dumps({"concept":args.get("concept"),"engine":args.get("engine"),"platforms":args.get("platforms") or ["desktop","mobile"],"multiplayer":bool(args.get("multiplayer")),"sections":{"pillars":["player fantasy","differentiator","session promise"],"coreLoop":["enter","decide","act","feedback","reward","progress"],"systems":["controls/camera","gameplay","progression/economy","content","UI/accessibility","save/network","analytics/liveops"],"architecture":["ownership","data flow","module boundaries","failure recovery","configuration"],"production":["prototype","vertical slice","content production","alpha","beta","release"],"quality":["automated checks","playtests","performance budgets","security","release/rollback"]}},indent=2),"images":[]}
    if name == "ms_vertical_slice_plan":
        return {"text":json.dumps({"objective":args.get("objective"),"engine":args.get("engine"),"deadline":args.get("deadline"),"include":["one representative player journey","production architecture for the riskiest systems","final-quality sample art/audio/UI","save/network path if relevant","instrumentation","focused tests and target-device profile"],"exclude":["content breadth","secondary modes","premature tooling","unproven polish variants"],"exitCriteria":["playable end to end","no blocking errors","quality bar demonstrated","risk retired with evidence","next milestone estimated from measured work"]},indent=2),"images":[]}
    if name == "ms_system_design_review":
        return {"text":json.dumps({"system":args.get("system"),"engine":args.get("engine"),"dependencies":args.get("dependencies") or [],"review":["single source of truth and authority","lifecycle/initialization/teardown","public interfaces and coupling","data validation and persistence","concurrency/reentrancy/idempotency","failure/retry/reconnect/rollback","test seams and observability","CPU/memory/network/content budgets","migration and compatibility"],"requiredEvidence":["current-state inspection","dependency map","focused test","runtime read-back","clean logs","profile where material"]},indent=2),"images":[]}
    if name == "ms_gameplay_balance_plan":
        return {"text":json.dumps({"system":args.get("system"),"goals":args.get("goals") or ["fair choices","clear progression","stable economy"],"segments":args.get("player_segments") or ["new","returning","expert"],"variables":["inputs/costs","outputs/rewards","time-to-goal","success/failure rate","power curves","sources/sinks","caps/cooldowns"],"method":["baseline equations","boundary simulation","seeded scenarios","telemetry events","A/B-safe configuration","review exploit loops","tune one variable family at a time"],"guardrails":["no pay-to-win coercion","no infinite source loop","no unrecoverable negative state","server validates awards"]},indent=2),"images":[]}
    if name == "ms_multiplayer_authority_audit":
        return {"text":json.dumps({"engine":args.get("engine"),"feature":args.get("feature"),"messages":args.get("messages") or [],"audit":["client trust boundary","server ownership","type/range/state validation","permission checks","rate/burst limits","replay/duplicate protection","ordering/idempotency","prediction/reconciliation","disconnect/rejoin","bandwidth and serialization","abuse telemetry"],"passEvidence":["invalid-message tests","spam test","two-client conflict test","latency/loss test","server-state read-back","clean logs"]},indent=2),"images":[]}
    if name == "ms_save_migration_plan":
        return {"text":json.dumps({"engine":args.get("engine"),"from":args.get("current_version"),"to":args.get("target_version"),"changes":args.get("changes") or [],"plan":["version every record","validate before mutate","backup or retain previous representation","migrate stepwise and idempotently","default missing fields conservatively","preserve unknown fields when possible","write only after verification","stage rollout with telemetry","support rollback/repair"],"tests":["oldest supported fixture","partially migrated fixture","corrupt/missing fields","repeat migration","concurrent save","rollback restore"]},indent=2),"images":[]}
    if name == "ms_performance_budget_plan":
        return {"text":json.dumps({"engine":args.get("engine"),"scene":args.get("scene"),"platforms":args.get("platforms") or ["weakest supported device"],"budgets":{"frame":"target FPS, main/render thread ms and spikes","memory":"steady/peak memory and leak-free transitions","gpu":"draw calls, triangles, overdraw, shader cost","network":"bytes/sec, messages/sec and burst caps","loading":"cold start, scene transition and streaming stalls","content":"texture, mesh, animation, audio and VFX limits"},"process":["capture baseline","profile representative worst case","fix largest measured bottleneck","repeat identical capture","record budget in CI/release gate"]},indent=2),"images":[]}
    if name == "ms_accessibility_audit":
        return {"text":json.dumps({"feature":args.get("feature"),"platforms":args.get("platforms") or [],"audit":["remappable controls and non-hold alternatives","keyboard/controller/touch focus path","text scale and contrast","color-independent meaning","captions and audio cues","reduced motion/flashing controls","timing/difficulty assists","plain language and cognitive load","screen-reader/platform semantics where supported"],"evidence":args.get("evidence") or [],"completion":"test with assist settings and at least keyboard/controller/touch paths relevant to targets"},indent=2),"images":[]}
    if name == "ms_content_pipeline_plan":
        return {"text":json.dumps({"engine":args.get("engine"),"contentTypes":args.get("content_types"),"sourceTools":args.get("source_tools") or [],"pipeline":["source-of-truth folder and ownership","naming/version/unit/axis conventions","export preset","automated import preset","validation of scale, pivots, UVs, materials, rigs and clips","optimization/LOD/collision/compression","copyright/license metadata","integration test scene","change propagation and rollback"],"doneWhen":["round trip is repeatable","warnings are resolved","asset meets budget","visual/runtime evidence captured"]},indent=2),"images":[]}
    if name == "ms_playtest_protocol":
        return {"text":json.dumps({"objective":args.get("objective"),"build":args.get("build"),"participants":max(1,int(args.get("participants") or 5)),"platforms":args.get("platforms") or [],"protocol":["freeze build and hypothesis","define participant segments","assign realistic tasks without leading","record behavior before opinions","capture completion/time/errors/confusion","debrief with open questions","classify severity and confidence","separate bugs from preference","make decision and retest"],"report":["observations","metrics","quotes","severity","root hypothesis","decision","owner","retest condition"]},indent=2),"images":[]}
    if name == "ms_definition_of_done":
        risk=str(args.get("risk") or "medium")
        return {"text":json.dumps({"feature":args.get("feature"),"engine":args.get("engine"),"risk":risk,"gates":["acceptance criteria demonstrated","code/content follows project architecture","happy path plus edge/failure/interruption states","automated or repeatable focused tests","clean compile/import/runtime logs","security/authority/save compatibility reviewed","accessibility/input/platform behavior checked","performance measured against budget","documentation/config/migration updated","critic review passed with concrete evidence","rollback path exists"],"extraForHighRisk":["two-person review","staged rollout","backup/repair tooling","load and abuse testing"] if risk=="high" else []},indent=2),"images":[]}
    if name == "ms_orchestrate_request":
        request=str(args.get("request","")).strip();engine=str(args.get("engine","")).lower();limit=max(3,min(8,int(args.get("max_skills") or 5)))
        if not request or engine not in {"roblox","unity","godot"}: raise RuntimeError("request and a valid engine are required")
        words={w for w in re.findall(r"[a-z0-9]+",request.lower()) if len(w)>2};ranked=[]
        for sid,v in skills.items():
            if v.get("engine")!=engine: continue
            hay=f"{sid} {v.get('name','')} {v.get('description','')} {' '.join(v.get('tags') or [])}".lower();score=sum(3 for w in words if w in hay)
            ranked.append((score,sid,v))
        ranked.sort(key=lambda x:(-x[0],x[1]));chosen=[];roles=set()
        for score,sid,v in ranked:
            r=v.get("capabilityRole","implementation")
            if r not in roles or score>3:
                chosen.append(sid);roles.add(r)
            if len(chosen)>=limit: break
        for _,sid,v in ranked:
            if len(chosen)>=limit: break
            if sid not in chosen: chosen.append(sid)
        return {"text":json.dumps({"request":request,"engine":engine,"skillStack":[{"id":sid,"name":skills[sid].get("name"),"role":skills[sid].get("capabilityRole"),"domain":skills[sid].get("capabilityDomain")} for sid in chosen],"executionRequired":True,"next":"Use this stack silently as guidance, inspect the project, and implement immediately through exact MCP commands. Call ms_activate_skill_stack only for genuinely complex work."},indent=2),"images":[]}
    if name == "ms_activate_skill_stack":
        request=str(args.get("request","")).strip();engine=str(args.get("engine","")).lower();ids=[str(x) for x in (args.get("skill_ids") or [])]
        if not request or engine not in {"roblox","unity","godot"}: raise RuntimeError("request and a valid engine are required")
        if ids and (len(ids)<2 or len(ids)>8): raise RuntimeError("skill_ids must contain 2 to 8 entries")
        if not ids:
            candidates=[sid for sid,v in skills.items() if v.get("engine")==engine];ids=sorted(candidates,key=lambda x:(skills[x].get("capabilityRole")!='implementation',x))[:4]
        missing=[x for x in ids if x not in skills or skills[x].get("engine")!=engine]
        if missing: raise RuntimeError("unknown or wrong-engine skills: "+", ".join(missing))
        stages=[];gates=[]
        for sid in ids:
            v=skills[sid];stages.append({"skill":sid,"role":v.get("capabilityRole"),"steps":v.get("steps") or [],"deliverables":v.get("deliverables") or []})
            for g in v.get("qualityGates") or []:
                if g not in gates:gates.append(g)
        return {"text":json.dumps({"request":request,"engine":engine,"planOnly":False,"executionRequired":True,"stages":stages,"completionGates":list(dict.fromkeys(gates + studio_standard.get("definitionOfDone", []))),"studioStandard":{"principles":studio_standard.get("principles",[]),"evidence":studio_standard.get("evidence",[]),"recovery":studio_standard.get("recovery",[]),"enginePractice":(studio_standard.get("engines") or {}).get(engine,[])},"mandatorySequence":["inspect current project","execute real MCP edits/artifact creation","run engine-native tests and profiling","read back final state","critic-review evidence","report only verified result"]},indent=2),"images":[]}
    if name == "ms_animation_director":
        return {"text":json.dumps({"engine":args.get("engine"),"subject":args.get("subject"),"style":args.get("style") or "match project art direction","actions":args.get("actions") or [],"production":["reference and motion intent","rig/avatar compatibility and scale","key poses, arcs, weight, spacing and timing","clip list and naming","state machine, layers, masks and transitions","root motion policy and movement ownership","IK/constraints/procedural layers","events, hit windows and audio/VFX sync","retargeting, looping, interruption and blending","compression, LOD and networking","in-engine multi-angle review and gameplay validation"],"requiredSkillDomains":["animation","input-camera-character","performance-platform","testing-release"]},indent=2),"images":[]}
    if name == "ms_texture_art_pipeline":
        return {"text":json.dumps({"engine":args.get("engine"),"asset":args.get("asset"),"style":args.get("style") or "match project","platforms":args.get("platforms") or [],"production":["visual target and material breakup","real-world scale and texel density","UV strategy, seams, trim/atlas/tile decision","albedo/value hierarchy without baked lighting","normal/roughness/metal/AO/emissive channel rules","seamless or unique texture creation using Canvas/Blender as appropriate","color-space and compression settings","engine material and shader setup","mipmaps, filtering and distance readability","memory/batching/variant budget","final lighting and target-device validation"],"realArtifactRequired":True},indent=2),"images":[]}
    if name == "ms_art_direction":
        return {"text":json.dumps({"engine":args.get("engine"),"concept":args.get("concept"),"mood":args.get("mood"),"references":args.get("references") or [],"direction":["player fantasy and emotional target","shape and silhouette language","composition and focal hierarchy","palette and value structure","material/texture language","environment and prop rules","character readability","lighting, atmosphere and post-processing","UI/icon alignment","animation/VFX/audio cohesion","content consistency checklist and review board"],"execution":"Create representative final assets in the connected tools, import them, and validate in gameplay camera—not only a moodboard."},indent=2),"images":[]}
    if name == "ms_uiux_production":
        return {"text":json.dumps({"engine":args.get("engine"),"feature":args.get("feature"),"platforms":args.get("platforms") or [],"style":args.get("style"),"production":["user goal and flow","information hierarchy","wireframe and content states","tokens and reusable components","default/hover/focus/pressed/selected/disabled/loading/empty/error/success states","safe areas, aspect ratios and localization expansion","keyboard/controller/touch navigation","contrast, type scale, motion and reduced-motion","engine-native implementation and data binding","usability test and target-device screenshots"],"realImplementationRequired":True},indent=2),"images":[]}
    if name == "ms_vfx_production":
        return {"text":json.dumps({"engine":args.get("engine"),"effect":args.get("effect"),"style":args.get("style"),"platforms":args.get("platforms") or [],"production":["gameplay purpose and readability envelope","anticipation/impact/sustain/dissipation timing","shape, color and value progression","particles, meshes, trails, decals and shaders","lighting and camera interaction","animation/audio/gameplay event synchronization","pooling, bounds, lifecycle and cancellation","quality tiers and accessibility alternatives","overdraw, particle, shader and memory budgets","multi-angle runtime capture on target hardware"],"realEffectRequired":True},indent=2),"images":[]}
    if name == "ms_audio_production":
        return {"text":json.dumps({"engine":args.get("engine"),"feature":args.get("feature"),"style":args.get("style"),"platforms":args.get("platforms") or [],"production":["event taxonomy and priorities","source asset/recording/synthesis plan","variation and anti-repetition","spatialization, attenuation and occlusion","bus routing, loudness and dynamic range","concurrency, voice stealing and pooling","adaptive music states and transition timing","animation/VFX/gameplay sync","caption/visual cue/accessibility support","compression, streaming and memory budget","in-context mix review across devices"],"realAudioIntegrationRequired":True},indent=2),"images":[]}
    if name == "ms_engine_handoff":
        source, target = args.get("source", "blender"), args.get("target", "other")
        asset_type, fmt = args.get("asset_type", "asset"), args.get("format") or ("FBX" if target in ("unity", "unreal") else "glTF/GLB")
        steps = [f"Freeze naming and scale for the {asset_type} in {source}.", "Apply transforms; validate normals, UVs, materials, and origin/pivot.",
                 f"Export as {fmt} with textures in a portable relative folder.", f"Import through the {target} MCP server and capture importer warnings.",
                 "Place the asset in a test scene, verify scale/orientation/materials/collisions.", "Run ms_critic_review with import logs and visual/runtime evidence."]
        return {"text": json.dumps({"source": source, "target": target, "format": fmt, "checklist": steps}, indent=2), "images": []}
    raise RuntimeError(f"unknown built-in tool '{name}'")


# ══════════════════════════════════════════════════════════════════════════
#  HARDENED MCP CLIENT  (one per server in config.json)
# ══════════════════════════════════════════════════════════════════════════
class MCPClient:
    def __init__(self, server_id, command, args, env=None):
        self.id = server_id
        self.command = command
        self.args = list(args or [])
        self.env = env or {}
        self.proc = None
        self.req_id = 1
        self.write_lock = threading.Lock()
        self.call_lock = threading.Lock()   # serialize tool calls (single stdio pipe)
        self.pending = {}                    # id -> queue.Queue (one slot)
        self.pend_lock = threading.Lock()
        self.tools_cache = []
        self.start_lock = threading.Lock()
        self._reader_thread = None
        # Crash-loop forensics (read by server_watch). The auto-restart used to
        # hide a server that something else kills over and over: the terminal
        # showed an endless quiet restart cycle with no explanation at all. We
        # keep just enough state to NAME the problem in the terminal instead:
        #  - last_exit: exit code from the final _reader EOF (crash vs kill hint)
        #  - stderr_tail: the last few stderr lines (usually the actual reason -
        #    port bind failure, missing dependency, crash trace)
        #  - restart_times: recent auto-restart timestamps (loop detector input)
        #  - loop_warned_at: throttle so the big red banner prints once per
        #    cooldown, not every 5s poll
        self.last_exit = None
        self.stderr_tail = []
        self.restart_times = []
        self.loop_warned_at = 0.0
        # Set when the configured command itself couldn't be launched at all
        # (e.g. 'uvx' not installed / not on PATH). This is NOT a crash - the
        # process never existed, so last_exit/stderr_tail stay empty and the
        # generic crash-loop banner used to print "the server printed no error
        # output before dying", which is misleading for a config problem the
        # user can fix in seconds. Kept across restarts so the banner can name
        # the real cause instead.
        self.start_error = None
        # Set when StudioMCP's stderr shows it connected to a FOREIGN WS host on
        # Studio's MCP port (not Studio). The unmistakable signature is a parse
        # error on the host's messages ("missing field `type`") - Studio speaks
        # the expected protocol, a squatter like ropilot speaks its own. This is
        # a timing-independent proof that the port is hijacked, unlike the
        # one-shot check_studio_port() boot probe which can miss a squatter that
        # grabs the port a moment after boot (seen live 2026-07-13: ropilot took
        # the port ~1s after the boot check ran, so nothing was flagged).
        self.saw_foreign_ws_host = False

    # ── lifecycle ─────────────────────────────────────────────────────────
    def _resolve(self, s):
        return os.path.expandvars(os.path.expanduser(str(s)))

    def start(self):
        with self.start_lock:
            if self.is_alive():
                return
            cmd = [self._resolve(self.command)] + [self._resolve(a) for a in self.args]
            # A bare .py command (relative paths resolve against the bridge dir)
            # is run with the SAME interpreter the bridge itself uses, so it works
            # even on installs where only the `py` launcher exists (no `python`
            # on PATH). This is how the Studio MCP launcher is wired by default.
            if cmd[0].lower().endswith(".py"):
                script = cmd[0]
                if not os.path.isabs(script):
                    script = os.path.join(HERE, script)
                cmd = [sys.executable, script] + cmd[1:]
            # On Windows, npx/npm/yarn/pnpm/bunx are .cmd shims that Popen can't
            # launch directly (WinError 2). Run them through cmd.exe so any
            # node-based MCP server "just works" from config.json.
            if sys.platform == "win32":
                base = os.path.basename(cmd[0]).lower()
                if base in ("npx", "npm", "yarn", "pnpm", "bunx"):
                    cmd = ["cmd.exe", "/c"] + cmd
            env = dict(os.environ)
            for k, v in self.env.items():
                env[k] = self._resolve(v)
            # A restarted server must never retain a catalogue from its previous
            # process. Stale schemas made the UI look healthy and routed calls to
            # tools the new process hadn't advertised yet.
            self.tools_cache = []
            with self.pend_lock:
                self.pending.clear()
            log(f"[{self.id}] launching  ({' '.join(cmd)})", "cy")
            with _Spinner(f"    [{self.id}] starting..."):
                try:
                    self.proc = subprocess.Popen(
                        cmd,
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        bufsize=1,
                        encoding="utf-8",
                        errors="replace",
                        cwd=HERE,
                        env=env,
                    )
                except FileNotFoundError:
                    # The OS couldn't find cmd[0] at all - this is a config
                    # problem (missing dependency, typo, not on PATH), not a
                    # transient crash. Auto-restart will keep retrying (the
                    # user may install it later), but name the real cause so
                    # it doesn't just look like an endless silent restart loop.
                    self.start_error = (
                        f"command not found: '{cmd[0]}' - is it installed and on PATH? "
                        f"(configured for server '{self.id}' in config.json)"
                    )
                    log(f"[{self.id}] {self.start_error}", "rd")
                    raise
                except OSError as e:
                    self.start_error = f"could not launch '{cmd[0]}': {e}"
                    log(f"[{self.id}] {self.start_error}", "rd")
                    raise
                else:
                    self.start_error = None
                self.saw_foreign_ws_host = False  # fresh process, fresh verdict
                self._reader_thread = threading.Thread(target=self._reader, args=(self.proc,), daemon=True)
                self._reader_thread.start()
                threading.Thread(target=self._stderr_drain, args=(self.proc,), daemon=True).start()

                # MCP handshake.
                self._request("initialize", {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "zeroscript-bridge", "version": "1.0"},
                }, timeout=30)
                self._notify("notifications/initialized")
                # Some MCP servers (notably Roblox's StudioMCP) advertise 0 tools at
                # the instant initialize returns, because they connect to their
                # backend (the running Studio) a moment AFTER the stdio handshake.
                # A single tools/list then caches an empty list forever. So if we
                # get nothing, retry for a few seconds to let the backend attach.
                # Short per-attempt timeout so the bridge never looks frozen if the
                # server stays silent (e.g. Studio not open yet); ~12s total budget.
                for _ in range(12):
                    if self.refresh_tools(timeout=3):
                        break
                    if not self.is_alive():
                        break
                    time.sleep(1.0)
            log(f"[{self.id}] MCP server up  ({len(self.tools_cache)} tools advertised)", "cy")

    def is_alive(self):
        return self.proc is not None and self.proc.poll() is None

    def restart(self):
        log(f"[{self.id}] restarting...", "yl")
        self.stop()
        time.sleep(0.4)
        self.start()

    def stop(self):
        with self.pend_lock:
            for q in self.pending.values():
                try:
                    q.put_nowait(None)
                except Exception:
                    pass
            self.pending.clear()
        if self.proc:
            # proc.terminate() (TerminateProcess on Windows) only kills THIS
            # pid. Our command is often a wrapper (e.g. launch_studio_mcp.py)
            # that Popen()s a real child (StudioMCP.exe) to own the stdio
            # pipes - terminate() would leave that child orphaned, still bound
            # to Studio's MCP port, fighting the next restart's fresh instance.
            # taskkill /T kills the whole tree.
            try:
                if sys.platform == "win32":
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(self.proc.pid)],
                        capture_output=True, timeout=8,
                    )
                else:
                    self.proc.terminate()
            except Exception:
                pass
        self.proc = None

    # ── io threads ────────────────────────────────────────────────────────
    def _reader(self, proc):
        stream = proc.stdout
        while True:
            try:
                line = stream.readline()
            except Exception:
                break
            if line == "":  # EOF -> process exited
                break
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except Exception:
                continue  # stray non-JSON log on stdout
            mid = msg.get("id")
            if mid is None:
                continue  # server notification, nothing waits on it
            with self.pend_lock:
                q = self.pending.get(mid)
            if q is not None:
                try:
                    q.put_nowait(msg)
                except Exception:
                    pass
        code = proc.poll()
        self.last_exit = code  # kept for the crash-loop banner in server_watch
        log(f"[{self.id}] stdout closed (process ended, exit code {code})", "rd")
        with self.pend_lock:
            for q in self.pending.values():
                try:
                    q.put_nowait(None)
                except Exception:
                    pass

    def _stderr_drain(self, proc):
        # Surface the child's stderr instead of silently discarding it - this
        # is often the ONLY clue why a server died (crash trace, port bind
        # failure, missing Studio, etc).
        try:
            for line in iter(proc.stderr.readline, ""):
                line = line.rstrip()
                if line:
                    # Ring buffer of the last stderr lines: when the server
                    # enters a crash loop, these are printed in the terminal
                    # banner - they are usually the only real explanation
                    # (port already in use, module not found, crash trace).
                    self.stderr_tail.append(line)
                    if len(self.stderr_tail) > 8:
                        self.stderr_tail.pop(0)
                    # Squatter signature: StudioMCP connected to a non-Studio host
                    # on the MCP port and can't parse its protocol. This is the
                    # ropilot hijack, timing-independent (see saw_foreign_ws_host).
                    low = line.lower()
                    if "failed to parse message from ws host" in low or "missing field `type`" in low:
                        self.saw_foreign_ws_host = True
                    log(f"[{self.id}] stderr: {line}", "yl", terminal=False)
        except Exception:
            pass

    # ── jsonrpc ───────────────────────────────────────────────────────────
    def _next_id(self):
        with self.write_lock:
            rid = self.req_id
            self.req_id += 1
            return rid

    def _notify(self, method, params=None):
        payload = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        with self.write_lock:
            self.proc.stdin.write(json.dumps(payload) + "\n")
            self.proc.stdin.flush()

    def _request(self, method, params, timeout):
        if not self.is_alive():
            raise RuntimeError(f"server '{self.id}' is not running")
        rid = self._next_id()
        q = queue.Queue(maxsize=1)
        with self.pend_lock:
            self.pending[rid] = q
        try:
            payload = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}
            with self.write_lock:
                self.proc.stdin.write(json.dumps(payload) + "\n")
                self.proc.stdin.flush()
            try:
                return q.get(timeout=timeout)
            except queue.Empty:
                return None
        finally:
            with self.pend_lock:
                self.pending.pop(rid, None)

    # ── high-level ────────────────────────────────────────────────────────
    def refresh_tools(self, timeout=20):
        msg = self._request("tools/list", {}, timeout=timeout)
        if msg and "result" in msg:
            self.tools_cache = msg["result"].get("tools", [])
        return self.tools_cache

    def list_resources(self, cursor=None, timeout=20):
        """Return this server's MCP resources without guessing their URIs."""
        with self.call_lock:
            if not self.is_alive():
                self.restart()
            params = {"cursor": cursor} if cursor else {}
            msg = self._request("resources/list", params, timeout=timeout)
            if msg is None:
                raise TimeoutError(f"No resource list from server '{self.id}' after {timeout}s.")
            if msg.get("error"):
                raise RuntimeError(msg["error"].get("message", json.dumps(msg["error"])))
            result = msg.get("result", {})
            return {"server": self.id, "resources": result.get("resources", []), "nextCursor": result.get("nextCursor")}

    def read_resource(self, uri, timeout=30):
        """Read text/blob MCP resource contents and preserve returned images."""
        with self.call_lock:
            if not self.is_alive():
                self.restart()
            msg = self._request("resources/read", {"uri": uri}, timeout=timeout)
            if msg is None:
                raise TimeoutError(f"No resource response from server '{self.id}' after {timeout}s.")
            if msg.get("error"):
                raise RuntimeError(msg["error"].get("message", json.dumps(msg["error"])))
            contents = msg.get("result", {}).get("contents", [])
            texts, images, metadata = [], [], []
            for item in contents:
                mime = item.get("mimeType", "")
                metadata.append({k: item.get(k) for k in ("uri", "mimeType") if item.get(k) is not None})
                if item.get("text") is not None:
                    texts.append(str(item.get("text")))
                elif item.get("blob") is not None and mime.startswith("image/"):
                    images.append({"data": item["blob"], "mimeType": mime})
                elif item.get("blob") is not None:
                    texts.append(json.dumps({"uri": item.get("uri", uri), "mimeType": mime, "blob": item["blob"]}))
            if metadata:
                texts.insert(0, json.dumps({"server": self.id, "resource": uri, "contents": metadata}, indent=2))
            return {"text": "\n".join(texts), "images": images}

    def call_tool(self, name, arguments, timeout):
        """Returns {"text":..., "images":[...]}. Raises on error/timeout."""
        with self.call_lock:
            for attempt in (1, 2):
                if not self.is_alive():
                    self.restart()
                msg = self._request("tools/call",
                                    {"name": name, "arguments": arguments}, timeout)
                if msg is None:
                    if not self.is_alive():
                        self.restart()
                        msg = self._request("tools/call",
                                            {"name": name, "arguments": arguments}, timeout)
                    if msg is None:
                        raise TimeoutError(
                            f"No response from server '{self.id}' after {timeout}s.")
                if msg.get("error"):
                    err = msg["error"]
                    err_text = err.get("message", json.dumps(err))
                    if attempt == 1 and _looks_like_transient_studio_drop(err_text):
                        log(f"[{self.id}] {name}: transient Studio drop, retrying once...", "yl")
                        time.sleep(1.5)
                        continue
                    raise RuntimeError(err_text)
                content = msg.get("result", {}).get("content", [])
                text = "\n".join(it.get("text", "") for it in content if it.get("type") == "text")
                images = [{"data": it["data"], "mimeType": it.get("mimeType", "image/jpeg")}
                          for it in content if it.get("type") == "image" and it.get("data")]
                if not text and not images and content:
                    text = json.dumps(content)[:4000]
                # Studio's own MCP proxy briefly loses its binding to the Studio
                # app every few seconds on some machines (seen live: repeated
                # "Bound studio ... disconnected for proxy ..." stderr, self-
                # healing within ~1-4s). A tool call landing in that window
                # fails with a "no Studio instance connected" style message
                # even though Studio is genuinely open - confirmed live via
                # start_stop_play. One short retry rides through it instead of
                # surfacing a spurious error to the user.
                if attempt == 1 and _looks_like_transient_studio_drop(text):
                    log(f"[{self.id}] {name}: transient Studio drop, retrying once...", "yl")
                    time.sleep(1.5)
                    continue
                return {"text": text, "images": images}


# ══════════════════════════════════════════════════════════════════════════
#  MANAGER  - aggregates every MCP server, routes by tool name.
# ══════════════════════════════════════════════════════════════════════════
class MCPManager:
    def __init__(self):
        self.clients = {}          # server_id -> MCPClient
        self.index = {}            # advertised_name -> (holder, real_name)
        self.index_lock = threading.Lock()

    def load_config(self):
        servers = _read_config().get("mcpServers", {}) or {}
        for sid, spec in servers.items():
            self.clients[sid] = MCPClient(
                sid, spec.get("command"), spec.get("args"), spec.get("env"))
        log(f"configured {len(self.clients)} MCP server(s): {', '.join(self.clients) or '(none)'}", "cy")

    def start_all(self):
        # Launch every configured server IN PARALLEL, not one after another.
        # client.start() can block for up to ~12s (its own "wait for Studio's
        # tools to appear" grace loop) - with a sequential for-loop, Roblox
        # being first in config.json meant every OTHER server (Blender, any
        # addon) didn't even begin launching until Roblox's grace loop gave
        # up, even though that addon has nothing to do with Roblox and could
        # have been ready in 1-2s. A thread per client removes that
        # dependency entirely: a slow/absent Roblox Studio no longer holds up
        # an addon server the user actually wants right now.
        threads = []
        for sid, client in self.clients.items():
            def _run(sid=sid, client=client):
                try:
                    client.start()
                except Exception as e:
                    log(f"[{sid}] failed to start: {e}  (other servers continue)", "rd")
            t = threading.Thread(target=_run, daemon=True)
            t.start()
            threads.append(t)
        for t in threads:
            t.join()
        self.rebuild_index()

    def rebuild_index(self):
        """Aggregate server tools. Collisions get a 'server/' prefix."""
        with self.index_lock:
            self.index = {}
            for sid, client in self.clients.items():
                for t in (client.tools_cache or []):
                    name = t.get("name")
                    if not name:
                        continue
                    advertised = name if name not in self.index else f"{sid}/{name}"
                    self.index[advertised] = (client, name)

    def list_tools(self, refresh=False):
        if refresh:
            for sid, client in self.clients.items():
                try:
                    if not client.is_alive():
                        client.start()
                    else:
                        client.refresh_tools()
                except Exception as e:
                    log(f"[{sid}] refresh failed: {e}", "yl")
            self.rebuild_index()
        out = [dict(t, server="zeroscript") for t in BUILTIN_TOOLS]
        for sid, client in self.clients.items():
            for t in (client.tools_cache or []):
                name = t.get("name")
                advertised = name
                with self.index_lock:
                    # find the advertised key that maps to this (client, name)
                    for k, (holder, real) in self.index.items():
                        if holder is client and real == name:
                            advertised = k
                            break
                tt = dict(t)
                tt["name"] = advertised
                tt["server"] = sid
                out.append(tt)
        return out

    def call(self, name, arguments, timeout):
        if name in BUILTIN_TOOL_NAMES:
            tool = BUILTIN_TOOL_BY_NAME[name]
            clean = _normalize_tool_arguments(tool.get("inputSchema"), arguments, name)
            return _builtin_call(name, clean, self)
        with self.index_lock:
            entry = self.index.get(name)
        if entry is None:
            # Maybe a freshly added tool - rebuild once and retry.
            self.rebuild_index()
            with self.index_lock:
                entry = self.index.get(name)
        if entry is None:
            raise RuntimeError(f"unknown tool '{name}'")
        holder, real_name = entry
        schema = next((t.get("inputSchema") for t in (holder.tools_cache or []) if t.get("name") == real_name), None)
        clean = _normalize_tool_arguments(schema, arguments, name)
        return holder.call_tool(real_name, clean, timeout)

    def list_server_tools(self, server_id, refresh=False):
        client = self._client(server_id)
        if refresh:
            if not client.is_alive(): client.start()
            else: client.refresh_tools()
            self.rebuild_index()
        return [dict(t) for t in (client.tools_cache or [])]

    def smoke_test(self, server_id, tool_name=None, arguments=None):
        client=self._client(server_id)
        if not client.is_alive(): raise RuntimeError(f"server '{server_id}' is offline")
        names={t.get("name") for t in (client.tools_cache or [])}
        defaults={
            "roblox":[("get_studio_state",{}), ("search_game_tree",{"query":"Workspace"})],
            "unity":[("manage_editor",{"action":"get_state"}), ("read_console",{"action":"get"})],
            "godot":[("get_godot_version",{}), ("get_project_info",{})],
            "blender":[("get_scene_info",{}), ("get_current_scene",{})],
        }
        if tool_name:
            candidates=[(str(tool_name),arguments or {})]
        else:
            candidates=defaults.get(server_id,[])
            if not candidates and names: candidates=[(sorted(n for n in names if n)[0],arguments or {})]
        chosen=next(((n,a) for n,a in candidates if n in names),None)
        if not chosen: raise RuntimeError(f"server '{server_id}' has no supported read-only smoke-test tool; choose one with the tool parameter")
        n,a=chosen;result=client.call_tool(n,a,30)
        return {"server":server_id,"connected":True,"nativeToolCount":len(names),"testedTool":n,"result":result.get("text","")[:4000],"images":len(result.get("images") or []),"verified":True}

    def call_on_server(self, server_id, tool_name, arguments, timeout=120):
        client = self._client(server_id)
        advertised = {t.get("name") for t in (client.tools_cache or [])}
        if tool_name not in advertised:
            raise RuntimeError(f"server '{server_id}' does not advertise tool '{tool_name}'")
        schema = next((t.get("inputSchema") for t in (client.tools_cache or []) if t.get("name") == tool_name), None)
        clean = _normalize_tool_arguments(schema, arguments, f"{server_id}/{tool_name}")
        return client.call_tool(tool_name, clean, timeout)

    def _client(self, server_id):
        client = self.clients.get(server_id)
        if client is None:
            raise RuntimeError(f"unknown MCP server '{server_id}'")
        return client

    def list_resources(self, server_id, cursor=None):
        return self._client(server_id).list_resources(cursor=cursor)

    def read_resource(self, server_id, uri):
        return self._client(server_id).read_resource(uri)

    def restart(self, server_id=None):
        targets = [self.clients[server_id]] if server_id and server_id in self.clients else list(self.clients.values())
        for client in targets:
            try:
                client.restart()
            except Exception as e:
                log(f"[{client.id}] restart failed: {e}", "rd")
        self.rebuild_index()

    def health(self):
        # Built-ins are always available but are not an external MCP connection;
        # excluding them keeps the UI from claiming an engine is online when only
        # Multi-Script's local helper tools are available.
        return [{"id": sid, "alive": c.is_alive(), "tools": len(c.tools_cache)}
                for sid, c in self.clients.items()]

    def any_alive(self):
        return any(c.is_alive() for c in self.clients.values())


# ══════════════════════════════════════════════════════════════════════════
#  WEBSOCKET SERVER
# ══════════════════════════════════════════════════════════════════════════
mgr = MCPManager()
clients = set()
plugin_state_lock = threading.Lock()
plugin_state = {"lastSeen": None, "pluginVersion": None}


def _plugin_status_payload(studio=None):
    studio = studio or {"app": None, "place": None}
    roblox = mgr.clients.get(PRIMARY_SERVER_ID)
    with plugin_state_lock:
        companion = dict(plugin_state)
    companion["connected"] = bool(companion.get("lastSeen") and time.time() - float(companion["lastSeen"]) < 20)
    return {
        "product": "Multi-Script",
        "bridgeVersion": BRIDGE_VERSION,
        "roblox": {
            "alive": bool(roblox and roblox.is_alive()),
            "connected": studio.get("place"),
            "studioApp": studio.get("app"),
            "nativeTools": len(roblox.tools_cache) if roblox else 0,
        },
        "directTools": len(BUILTIN_TOOLS),
        "skills": len(_load_skills()),
        "virtualTools": len(_load_virtual_tools()),
        "companionPlugin": companion,
    }


async def plugin_http_handler(reader, writer):
    """Tiny loopback-only HTTP surface for the Roblox Studio companion plugin.

    It intentionally exposes status and a bounded heartbeat only. Tool execution
    remains on the authenticated-by-locality WebSocket path used by the extension.
    """
    status, body = 200, b""
    try:
        raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=3)
        if len(raw) > 32768:
            raise ValueError("headers too large")
        header_text = raw.decode("iso-8859-1", "replace")
        lines = header_text.split("\r\n")
        parts = lines[0].split()
        if len(parts) != 3:
            raise ValueError("invalid request line")
        method, path, _ = parts
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        length = int(headers.get("content-length", "0") or 0)
        if length < 0 or length > 65536:
            status, body = 413, b'{"error":"payload too large"}'
        else:
            request_body = await asyncio.wait_for(reader.readexactly(length), timeout=3) if length else b""
            if method == "GET" and path.rstrip("/") == "/status":
                studio = await asyncio.to_thread(probe_studio)
                body = json.dumps(_plugin_status_payload(studio)).encode("utf-8")
            elif method == "GET" and path.rstrip("/") == "/catalog":
                roblox = mgr.clients.get(PRIMARY_SERVER_ID)
                tools = [dict(t) for t in (roblox.tools_cache or [])] if roblox else []
                body = json.dumps({"server":"roblox","nativeToolCount":len(tools),"tools":tools}).encode("utf-8")
            elif method == "POST" and path.rstrip("/") == "/plugin/heartbeat":
                data = json.loads(request_body.decode("utf-8") or "{}")
                if not isinstance(data, dict):
                    raise ValueError("heartbeat must be an object")
                allowed = {k: data.get(k) for k in ("pluginVersion", "placeId", "placeName", "isRunning", "selectionCount", "scriptCount", "moduleCount")}
                allowed["lastSeen"] = time.time()
                with plugin_state_lock:
                    plugin_state.clear()
                    plugin_state.update(allowed)
                body = b'{"ok":true}'
            else:
                status, body = 404, b'{"error":"not found"}'
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError, ValueError, json.JSONDecodeError) as e:
        status, body = 400, json.dumps({"error": str(e)}).encode("utf-8")
    except Exception as e:
        status, body = 500, json.dumps({"error": type(e).__name__}).encode("utf-8")
    reason = {200: "OK", 400: "Bad Request", 404: "Not Found", 413: "Payload Too Large", 500: "Internal Server Error"}.get(status, "Error")
    response = (f"HTTP/1.1 {status} {reason}\r\nContent-Type: application/json; charset=utf-8\r\n"
                f"Content-Length: {len(body)}\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n").encode("ascii") + body
    writer.write(response)
    try:
        await writer.drain()
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass

# ── Studio connectivity probe ──────────────────────────────────────────────
# The MCP server process stays alive even when Roblox Studio is closed or its
# MCP option is disabled - tool calls then return instantly with an "Unable to
# find an active Studio instance" text. So "mcp_alive" alone is misleading.
#
# TWO LEVELS (validated live 2026-06):
#  - list_roblox_studios: instant, side-effect-free. studios == [] means NO Studio
#    is connected to the MCP (app closed, OR its "Studio as MCP Server" option is
#    disabled - the two are indistinguishable at this layer). A non-empty list
#    means a Studio app IS connected, BUT note its entry stays present (active:true)
#    even when no place is open - only its "name" goes null. So presence != usable.
#  - get_studio_state: tells whether a PLACE is actually loaded. With a place open
#    it returns "Available DataModels: ..."; with the Studio on the home screen (or
#    the active place closed) it returns "...doesn't have a place opened / previously
#    active Studio has disconnected". That is the authoritative "place loaded" signal
#    (same phrase the call path already recognises in core/main.js).
STUDIO_PROBE_TOOL = "list_roblox_studios"
STUDIO_STATE_TOOL = "get_studio_state"
# Substrings get_studio_state emits when a Studio is connected but no place is open.
NO_PLACE_MARKERS = ("doesn't have a place", "no place opened", "place opened",
                    "has disconnected", "no active studio")


def _probe_tool_text(tool):
    """Call a side-effect-free probe tool with no args; return its text, or None if
    the tool is unavailable / the server is busy / it errored (best-effort)."""
    with mgr.index_lock:
        entry = mgr.index.get(tool)
    if entry is None:
        return None
    holder, real_name = entry
    # Never queue behind a long-running tool call (the probe is best-effort).
    if not holder.call_lock.acquire(blocking=False):
        return None
    try:
        if not holder.is_alive():
            return None
        msg = holder._request("tools/call", {"name": real_name, "arguments": {}}, timeout=8)
        if not msg or msg.get("error"):
            return None
        content = msg.get("result", {}).get("content", [])
        return "\n".join(it.get("text", "") for it in content if it.get("type") == "text")
    except Exception:
        return None
    finally:
        holder.call_lock.release()


def probe_studio():
    """Two-level Studio connectivity. Returns {"app": x, "place": y} where each is
    True / False / None (None = unknown: probe tool missing or server busy).
      app   - a Roblox Studio instance is connected to the MCP server. False = Studio
              closed OR its MCP-server option disabled (indistinguishable here).
      place - a place/datamodel is actually loaded and usable. False = Studio open on
              the home screen, or the active place was closed. Only meaningful when
              app is True (when app is False/None, place mirrors it)."""
    roblox = mgr.clients.get(PRIMARY_SERVER_ID)
    if roblox is not None and roblox.is_alive() and not roblox.tools_cache:
        # StudioMCP advertises ZERO tools - including list_roblox_studios itself -
        # until Studio actually attaches. That makes _probe_tool_text() below
        # return None (tool missing) the same way it would for a genuinely
        # transient "probe busy" blip, even though "Studio is simply closed" is
        # the common, SUSTAINED case here, not a blip. Left unhandled, the
        # extension's "unknown = don't degrade" rule (by design, for real
        # transient blips) then leaves the status dot stuck GREEN forever with
        # Studio fully closed (seen live 2026-07-11: dot stayed "on", tooltip
        # showing only an addon server's tool count). An alive client with an
        # empty catalogue is an unambiguous "not connected", so short-circuit
        # straight to that verdict instead of falling through to "unknown".
        return {"app": False, "place": False}
    text = _probe_tool_text(STUDIO_PROBE_TOOL)
    if text is None:
        return {"app": None, "place": None}
    try:
        studios = json.loads(text).get("studios") or []
    except Exception:
        return {"app": None, "place": None}
    if not studios:
        return {"app": False, "place": False}
    # A Studio app is connected - now check whether a place is actually open.
    state = _probe_tool_text(STUDIO_STATE_TOOL)
    if state is None:
        return {"app": True, "place": None}
    low = state.lower()
    place = not any(m in low for m in NO_PLACE_MARKERS)
    return {"app": True, "place": place}


def probe_unity():
    """Best-effort check for a live Unity Editor via the official instances resource."""
    client = mgr.clients.get("unity")
    if client is None or not client.is_alive() or not client.tools_cache:
        return False
    if not client.call_lock.acquire(blocking=False):
        return None
    try:
        msg = client._request("resources/read", {"uri": "mcpforunity://instances"}, timeout=6)
        if not msg or msg.get("error"):
            return None
        texts = [str(x.get("text", "")) for x in msg.get("result", {}).get("contents", []) if x.get("text") is not None]
        payload = "\n".join(texts)
        try:
            data = json.loads(payload)
            return bool(data.get("instance_count", len(data.get("instances", []))))
        except Exception:
            low = payload.lower()
            if '"instance_count": 0' in low or '"instances": []' in low:
                return False
            return True if "unity" in low and ("instance" in low or "project" in low) else None
    except Exception:
        return None
    finally:
        client.call_lock.release()


def probe_godot():
    """Side-effect-free Godot readiness check via Coding-Solo get_godot_version."""
    client = mgr.clients.get("godot")
    if client is None or not client.is_alive() or not client.tools_cache: return False
    if not client.call_lock.acquire(blocking=False): return None
    try:
        msg = client._request("tools/call", {"name": "get_godot_version", "arguments": {}}, timeout=8)
        if not msg or msg.get("error"): return False
        text = "\n".join(x.get("text", "") for x in msg.get("result", {}).get("content", []) if x.get("type") == "text")
        return False if any(x in text.lower() for x in ("could not find", "not found", "error")) else bool(text.strip())
    except Exception: return None
    finally: client.call_lock.release()


def engine_snapshot(roblox_state=None, unity_state=None, godot_state=None):
    """Connection-aware engine status used by the extension for auto-routing."""
    if roblox_state is None:
        roblox_state = probe_studio()
    if unity_state is None:
        unity_state = probe_unity()
    if godot_state is None:
        godot_state = probe_godot()
    states = []
    for sid, client in mgr.clients.items():
        connected = unity_state if sid == "unity" else (godot_state if sid == "godot" else (roblox_state.get("place") if sid == "roblox" else None))
        states.append({"id": sid, "alive": client.is_alive(), "connected": connected, "tools": len(client.tools_cache)})
    return states


def safe_call(name, arguments, timeout):
    """Never raises. Always returns a dict the extension can feed back to DeepSeek."""
    try:
        result = mgr.call(name, arguments, timeout)
        return {"ok": True, "text": result["text"], "images": result["images"]}
    except TimeoutError as e:
        return {"ok": False, "error": str(e), "kind": "timeout"}
    except Exception as e:
        return {"ok": False, "error": str(e), "kind": type(e).__name__}


async def run_tool_task(ws, name, args, timeout, rid):
    """Execute one tool off the socket read loop and send its result back.

    Kept as a standalone task (not awaited inline in handler) so a long tool
    never starves the connection's ability to answer app-level pings - see the
    call_tool branch in handler() for the full rationale."""
    t0 = time.monotonic()
    res = await asyncio.to_thread(safe_call, name, args, timeout)
    elapsed = time.monotonic() - t0
    tag = "gr" if res.get("ok") else "rd"
    summary = (res.get("text") or res.get("error") or "")[:80].replace("\n", " ")
    slow = "  [SLOW]" if elapsed > 5 else ""
    # Routine per-call traces are technical noise for a non-dev user watching
    # the console; they still land in bridge_debug.log. A failed/slow call
    # DOES surface on the terminal - that's the signal a user should notice.
    log(f"<- {name} ({elapsed:.1f}s){slow}: {summary}", tag, terminal=not res.get("ok") or elapsed > 5)
    try:
        await ws.send(json.dumps({"type": "tool_result", "id": rid, **res}))
    except websockets.ConnectionClosed:
        pass


async def broadcast_status():
    """Push a fresh status snapshot to every currently-connected extension tab.

    Needed because the socket now starts listening (see _boot_and_diagnose in
    main()) before every MCP server has necessarily finished launching in the
    background - an extension that connects in that window gets an early,
    incomplete "connected" snapshot (e.g. an addon server not started yet).
    The extension's own periodic poll only reads a passively cached copy of
    the LAST message it received (background.js never re-probes on its own),
    so without a follow-up push that stale snapshot can persist forever (seen
    live 2026-07-11: Blender not yet alive at connect-time froze the "Start
    Roblox agent" button in its fully-disabled, non-degraded state even long
    after Blender was actually up). background.js already handles a second
    "connected" message arriving at any time (updates its cache and re-renders
    the bar), so re-sending this exact shape once startup truly settles is
    enough to self-correct with zero extension-side changes needed.
    """
    if not clients:
        return
    try:
        _st, _unity, _godot = await asyncio.gather(
            asyncio.to_thread(probe_studio), asyncio.to_thread(probe_unity), asyncio.to_thread(probe_godot))
        _proc = await asyncio.to_thread(_roblox_studio_app_running)
        payload = json.dumps({
            "type": "connected",
            "mcp_alive": mgr.any_alive(),
            "studio": _st["place"], "studio_app": _st["app"],
            # Whether a Roblox Studio WINDOW process exists at all - lets the
            # extension word the corrective step correctly ("open the MCP
            # panel in your already-open Studio" vs "launch Studio").
            "studio_proc": _proc,
            "servers": mgr.health(),
            "engines": engine_snapshot(_st, _unity, _godot),
            "tools": mgr.list_tools(),
            "port": PORT,
        })
    except Exception:
        return
    for ws in list(clients):
        try:
            await ws.send(payload)
        except Exception:
            pass


async def handler(ws):
    peer = getattr(ws, "remote_address", ("?",))[0]
    clients.add(ws)
    log(f"extension connected  ({peer})  [{len(clients)} client(s)]", "gr")
    try:
        _st, _unity, _godot = await asyncio.gather(
            asyncio.to_thread(probe_studio), asyncio.to_thread(probe_unity), asyncio.to_thread(probe_godot))
        await ws.send(json.dumps({
            "type": "connected",
            "mcp_alive": mgr.any_alive(),
            "studio": _st["place"], "studio_app": _st["app"],
            "studio_proc": await asyncio.to_thread(_roblox_studio_app_running),
            "servers": mgr.health(),
            "engines": engine_snapshot(_st, _unity, _godot),
            "tools": mgr.list_tools(),
            "port": PORT,
        }))
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            mtype = msg.get("type")
            rid = msg.get("id")

            if mtype == "ping":
                await ws.send(json.dumps({"type": "pong", "id": rid}))

            elif mtype == "studio_status":
                studio, unity, godot = await asyncio.gather(
                    asyncio.to_thread(probe_studio), asyncio.to_thread(probe_unity), asyncio.to_thread(probe_godot))
                await ws.send(json.dumps({
                    "type": "studio_status", "id": rid,
                    "studio": studio["place"], "studio_app": studio["app"],
                    "studio_proc": await asyncio.to_thread(_roblox_studio_app_running),
                    "mcp_alive": mgr.any_alive(), "engines": engine_snapshot(studio, unity, godot),
                }))

            elif mtype == "list_tools":
                try:
                    tools = await asyncio.to_thread(mgr.list_tools, True)
                except Exception as e:
                    tools = mgr.list_tools()
                    log(f"list_tools error: {e}", "yl")
                _st, _unity = await asyncio.gather(
                    asyncio.to_thread(probe_studio), asyncio.to_thread(probe_unity))
                await ws.send(json.dumps({
                    "type": "tools", "id": rid,
                    "tools": tools, "mcp_alive": mgr.any_alive(),
                    "studio": _st["place"], "studio_app": _st["app"],
                    "studio_proc": await asyncio.to_thread(_roblox_studio_app_running),
                    "servers": mgr.health(), "engines": engine_snapshot(_st, _unity, _godot),
                }))

            elif mtype in ("elevenlabs_status", "elevenlabs_configure", "elevenlabs_clear"):
                try:
                    audio = _elevenlabs_audio()
                    if mtype == "elevenlabs_configure":
                        state = await asyncio.to_thread(audio.configure_api_key, msg.get("api_key"))
                    elif mtype == "elevenlabs_clear":
                        state = await asyncio.to_thread(audio.clear_api_key)
                    else:
                        state = await asyncio.to_thread(audio.status)
                    await ws.send(json.dumps({"type":"elevenlabs_settings","id":rid,"ok":True,"status":state}))
                except Exception as e:
                    await ws.send(json.dumps({"type":"elevenlabs_settings","id":rid,"ok":False,"error":str(e)}))

            elif mtype == "call_tool":
                name = msg.get("name", "")
                args = msg.get("arguments") or {}
                timeout = float(msg.get("timeout", 120000)) / 1000.0
                log(f"-> tool  {name}({', '.join(args.keys())})", "cy", terminal=False)
                # Run the tool as a BACKGROUND task instead of awaiting it here.
                # Awaiting inline parks this read loop for the WHOLE tool call, so
                # a long tool (e.g. wait_job_finished > 25s) means the client's
                # app-level pings are never read/answered - its half-open-socket
                # watchdog then force-closes the connection and the in-flight call
                # is dropped as "bridge unreachable" (reported live). As a task,
                # the loop stays free to answer pings/status while the tool runs.
                # The extension only ever has ONE call_tool in flight (its agent
                # loop awaits each result before sending the next), so this never
                # overlaps tool executions.
                asyncio.create_task(run_tool_task(ws, name, args, timeout, rid))

            elif mtype in ("add_server", "remove_server"):
                # Adding/removing an addon MCP server rewrites config.json, which
                # the bridge only reads at launch - so we ack, then restart the
                # whole process to pick it up cleanly. The primary Roblox server
                # is protected inside config_add/remove_server.
                if mtype == "add_server":
                    ok, err = await asyncio.to_thread(
                        config_add_server,
                        msg.get("server_id"), msg.get("command"),
                        msg.get("args"), msg.get("env"))
                else:
                    ok, err = await asyncio.to_thread(
                        config_remove_server, msg.get("server_id"))
                await ws.send(json.dumps({
                    "type": "server_changed", "id": rid,
                    "ok": ok, "error": err, "restarting": ok,
                }))
                if ok:
                    # Give the ack a beat to flush over the socket, then restart.
                    async def _do_restart():
                        await asyncio.sleep(0.4)
                        restart_self()
                    asyncio.create_task(_do_restart())

            elif mtype == "restart_mcp":
                sid = msg.get("server")
                try:
                    await asyncio.to_thread(mgr.restart, sid)
                    ok, err = True, None
                except Exception as e:
                    ok, err = False, str(e)
                _rst, _runity, _rgodot = await asyncio.gather(
                    asyncio.to_thread(probe_studio), asyncio.to_thread(probe_unity), asyncio.to_thread(probe_godot))
                await ws.send(json.dumps({
                    "type": "mcp_status", "id": rid,
                    "alive": mgr.any_alive(), "ok": ok, "error": err,
                    "servers": mgr.health(), "engines": engine_snapshot(_rst, _runity, _rgodot),
                    "tools": mgr.list_tools(),
                }))

            else:
                await ws.send(json.dumps({
                    "type": "error", "id": rid,
                    "error": f"unknown message type: {mtype}",
                }))
    except websockets.ConnectionClosed:
        pass
    except Exception as e:
        log(f"handler error: {e}", "rd")
    finally:
        clients.discard(ws)
        log(f"extension disconnected  [{len(clients)} client(s)]", "yl")


async def server_watch():
    """Poll every MCP server and restart any that died unexpectedly (e.g. the
    StudioMCP proxy crashing on its own - see stop()'s taskkill /T fix and the
    stderr logging above for why this used to happen silently). Without this,
    a dead server only got noticed on the NEXT real tool call, which is what
    made "Studio looks connected but nothing responds" possible."""
    # Crash-LOOP detection thresholds: LOOP_N deaths within LOOP_WINDOW seconds
    # means something is killing (or instantly crashing) the server every time
    # we bring it back - the silent restart cycle the auto-restart otherwise
    # hides completely. We still keep restarting (the cause may be transient,
    # e.g. the user is about to start Blender), but the terminal now NAMES the
    # problem: exit code, the child's last stderr lines, and - for a port-bound
    # server - who is squatting the port. Banner re-prints at most every
    # LOOP_WARN_COOLDOWN so the terminal stays readable.
    LOOP_N = 3
    LOOP_WINDOW = 60
    LOOP_WARN_COOLDOWN = 120
    while True:
        await asyncio.sleep(5)
        for sid, client in list(mgr.clients.items()):
            try:
                if not client.is_alive():
                    now = time.time()
                    # restart_times holds RESTART ATTEMPTS (appended just before
                    # each start below), never per-poll sightings - appending on
                    # every 5s poll would keep the window full forever and the
                    # "slow down" branch would then block restarts permanently.
                    client.restart_times = [t for t in client.restart_times if now - t < LOOP_WINDOW]
                    looping = len(client.restart_times) >= LOOP_N
                    if looping and now - client.loop_warned_at > LOOP_WARN_COOLDOWN:
                        client.loop_warned_at = now
                        log(f"[{sid}] CRASH LOOP: died {len(client.restart_times)} times in the last "
                            f"{LOOP_WINDOW}s (last exit code: {client.last_exit}). Something is killing it "
                            f"or it cannot start.", "rd")
                        if client.start_error:
                            log(f"[{sid}] {client.start_error}", "rd")
                        elif client.stderr_tail:
                            log(f"[{sid}] last error output (usually the real reason):", "rd")
                            for ln in client.stderr_tail:
                                log(f"[{sid}]   {ln}", "yl")
                        else:
                            log(f"[{sid}] the server printed no error output before dying.", "yl")
                        # Port forensics: name the process squatting a port this
                        # server needs. For the primary Roblox proxy that is
                        # Studio's MCP port; a squatter there (seen live: a
                        # 'ropilot' app) makes the proxy die/misbehave forever.
                        if sid == "roblox":
                            owner = _port_owner(STUDIO_MCP_PORT)
                            if owner:
                                pid, name, path = owner
                                if "roblox" not in (name or "").lower() and "studio" not in (path or "").lower():
                                    log(f"[{sid}] port {STUDIO_MCP_PORT} is held by '{name}' (pid {pid}, {path}) - "
                                        f"close that program, it is squatting Studio's MCP port.", "rd")
                        log(f"[{sid}] common causes: its app is not running (e.g. Blender + addon), a port "
                            f"conflict, an antivirus killing it, or a bad command in config.json. "
                            f"Auto-restart continues in the background.", "yl")
                    if looping and client.restart_times and now - client.restart_times[-1] < 15:
                        # Clearly hopeless right now: drop to a ~15s cadence so a
                        # broken command isn't hammer-spawned every 5 seconds,
                        # while still retrying forever (the cause may clear, e.g.
                        # the user finally opens Blender).
                        continue
                    client.restart_times.append(now)
                    log(f"[{sid}] found dead - auto-restarting...", "yl")
                    await asyncio.to_thread(client.start)
                    mgr.rebuild_index()
                    await broadcast_status()  # tell any connected extension right away
            except Exception as e:
                log(f"[{sid}] auto-restart failed: {e}", "rd")


def _current_studio_exe():
    """The StudioMCP.exe our launcher would currently pick (newest version
    folder paired with a real RobloxStudioBeta.exe), or None. Reused here only
    to detect a Studio update happening mid-session: Roblox's own bug report
    ("Studio MCP turning off after update") says the toggle resets to OFF
    whenever Studio auto-updates - restarting our proxy can't fix that (Studio
    itself refuses the connection while its toggle is off), so the terminal
    should say "re-enable the toggle" instead of "wait for auto-recovery"
    when a version bump coincides with the disconnect."""
    if _studio_scan is None:
        return None
    try:
        return _studio_scan.find_studio_mcp()
    except Exception:
        return None


async def studio_watch(initial_app, initial_place=None):
    """Poll Studio attachment and log transitions, so the terminal confirms in
    GREEN the moment Studio attaches (e.g. after the user toggles its MCP server)
    and warns again if it later drops. Best-effort; never raises.

    Also auto-recovers from a real disconnect: two bugs reported on the Roblox
    devforum leave StudioMCP.exe alive (our client stays "alive" - the process
    never dies, so server_watch's dead-process restart never fires) but stuck
    talking to nothing - (1) StudioMCP keeps a stale named-pipe handle keyed by
    Studio's old PID after Studio is closed and reopened, and never rediscovers
    the new one; (2) MCP silently disconnects every 5-15 minutes on some
    machines. The documented user workaround for both is "toggle Studio's MCP
    server off/on" / "reopen the MCP panel" - which just forces StudioMCP to
    redo its handshake. Restarting OUR proxy process is the equivalent from
    this side (taskkill + fresh launch_studio_mcp.py), so do it automatically
    once a drop looks real (sustained, not a momentary blip) instead of leaving
    the user to notice and toggle it themselves."""
    prev_app = initial_app
    prev_place = initial_place
    disconnected_since = None
    last_auto_restart = 0.0
    empty_since = None       # when the roblox catalogue was first seen empty
    last_reclaim = 0.0       # cooldown for the zombie-StudioMCP port reclaim
    place_transitions = []
    known_studio_exe = await asyncio.to_thread(_current_studio_exe)
    update_suspected = False
    # Only auto-restart a disconnect that follows a real connection (matches
    # the two known bugs above) - never spam-restart while Studio simply isn't
    # open yet at all (prev_app starting False/None is the common cold-start
    # case and restarting there would just be noise every cooldown).
    ever_connected = initial_app is True
    while True:
        await asyncio.sleep(4)
        # If StudioMCP launched while Studio was closed, its catalogue is EMPTY
        # and stays that way: start()'s 12s retry loop has long given up, and
        # nothing else ever re-asks for tools/list (probe_studio can't - the
        # probe tools themselves are part of the missing catalogue, which is
        # why it short-circuits to "not connected" on an empty cache). So a
        # Studio opened AFTER that window was never detected until the user
        # restarted the whole bridge (seen live 2026-07-11). Re-ask here on
        # every poll while the catalogue is empty; the moment Studio attaches,
        # tools appear, the index rebuilds, and the normal probe below flips
        # the state to connected on this same iteration.
        rc0 = mgr.clients.get("roblox")
        if rc0 is not None and rc0.is_alive() and not rc0.tools_cache:
            got = False
            try:
                got = bool(await asyncio.to_thread(rc0.refresh_tools, 3))
            except Exception:
                got = False
            if got:
                mgr.rebuild_index()
                log(f"Roblox Studio's tools appeared ({len(rc0.tools_cache)}) - Studio attached.", "gr")
                empty_since = None
            else:
                now0 = time.time()
                if empty_since is None:
                    empty_since = now0
                # First: a PROVEN port hijack (stderr showed StudioMCP talking to
                # a foreign host, e.g. ropilot). Hard evidence, so recover fast
                # and unconditionally - no need to wait out the sustained-empty
                # window the ambiguous zombie case below uses.
                if (rc0.saw_foreign_ws_host and now0 - last_reclaim > 180):
                    last_reclaim = now0
                    killed, sname = await asyncio.to_thread(_kill_port_squatter)
                    if killed:
                        try:
                            await asyncio.to_thread(mgr.restart, "roblox")
                        except Exception as e:
                            log(f"roblox proxy restart after squatter kill failed: {e}", "rd")
                        _print_squatter_hint(sname)
                        await broadcast_status()
                # Otherwise: catalogue stuck empty WITH a Studio window open often
                # means a zombie StudioMCP.exe (not ours) still owns port 13469 and
                # swallowed Studio's one-shot registration - a state no manual
                # restart combination can escape (see _reclaim_studio_port).
                # Sustained-empty threshold + cooldown so a Studio that is
                # merely slow to boot never triggers a spurious kill.
                elif (now0 - empty_since > 20 and now0 - last_reclaim > 180
                        and await asyncio.to_thread(_roblox_studio_app_running) is True):
                    last_reclaim = now0
                    if await asyncio.to_thread(_reclaim_studio_port, rc0):
                        try:
                            await asyncio.to_thread(mgr.restart, "roblox")
                        except Exception as e:
                            log(f"roblox proxy restart after zombie kill failed: {e}", "rd")
                        _print_reregister_hint()
                        await broadcast_status()
        else:
            empty_since = None
        try:
            st = await asyncio.to_thread(probe_studio)
        except Exception:
            continue
        app, place = st["app"], st["place"]
        if app is not None and app != prev_app:
            if app is True:
                # Roblox-only count, not mgr.list_tools() (sums every server,
                # e.g. + Blender) - this message is specifically about Roblox
                # attaching, so it must not borrow addon tool counts (same
                # class of bug as the startup banner, see roblox_total above).
                rc = mgr.clients.get("roblox")
                roblox_now = len(rc.tools_cache) if rc else 0
                log(f"Roblox Studio connected - {roblox_now} tools ready.", "gr")
                ever_connected = True
                disconnected_since = None
                update_suspected = False
            else:
                cur_exe = await asyncio.to_thread(_current_studio_exe)
                if cur_exe and known_studio_exe and cur_exe != known_studio_exe:
                    # A newer Studio version folder appeared since we last saw
                    # one - restarting the proxy will NOT fix this (Studio
                    # itself refuses the MCP connection while its own toggle
                    # is off), so tell the user the actual fix instead of
                    # letting the generic auto-recovery below spin uselessly.
                    ver = os.path.basename(os.path.dirname(cur_exe))
                    log(f"Roblox Studio appears to have UPDATED (new version: {ver}). "
                        "Studio often turns its MCP toggle back OFF after an update - open "
                        "Roblox Studio > Assistant Settings > MCP Servers and re-enable "
                        "'Enable Studio as MCP server'.", "yl")
                    update_suspected = True
                else:
                    log("Roblox Studio disconnected - re-enable its MCP server (toggle off/on).", "yl")
                    update_suspected = False
                known_studio_exe = cur_exe or known_studio_exe
                if ever_connected and disconnected_since is None:
                    disconnected_since = time.time()
            prev_app = app
            # studio_watch only used to LOG transitions - an extension sitting
            # on the pre-start standby screen (no tool calls happening, so
            # nothing else round-trips to the bridge) never saw Studio connect
            # or disconnect mid-session until it happened to poll for an
            # unrelated reason. Push it immediately instead of leaving that
            # extension staring at a stale snapshot indefinitely.
            await broadcast_status()
        # Set once per iteration: BOTH the app-drop branch and the place-churn
        # block below use it. It used to be assigned only inside the app-drop
        # branch, so any iteration that skipped that branch crashed the whole
        # watcher with UnboundLocalError on the churn line (seen live: the task
        # died right after a successful reconnect, silently ending ALL Studio
        # monitoring and status broadcasts until the bridge was restarted).
        now = time.time()
        if app is False and ever_connected and disconnected_since is not None and not update_suspected:
            # ~20s sustained (5 polls) before treating it as a real drop, not a
            # momentary blip; 90s cooldown between recovery attempts so a
            # Studio that is genuinely closed for a while doesn't get hammered.
            # Skipped entirely when a version bump was the likely cause (see
            # above) - restarting our proxy cannot flip Studio's own toggle
            # back on, so retrying would just be noise every 90s.
            if now - disconnected_since > 20 and now - last_auto_restart > 90:
                last_auto_restart = now
                # Which recovery applies depends on whether a Studio WINDOW is
                # actually running (validated live 2026-07-11, both directions):
                #  - Studio RUNNING but not attached: Studio's MCP plugin only
                #    registers ONCE, at Studio boot or on a toggle flip. It
                #    never retries by itself, and restarting OUR proxy cannot
                #    reach into Studio to re-register it - worse, a restart
                #    that lands while Studio is booting kills the listener at
                #    the exact moment the plugin makes its single attempt,
                #    which is precisely how this state got created. So: do NOT
                #    touch the proxy; tell the user the one action that works.
                #  - No Studio running: a restart is safe (nothing to collide
                #    with) and clears genuinely stuck/stale proxy state.
                if await asyncio.to_thread(_roblox_studio_app_running) is True:
                    log("Roblox Studio is RUNNING but its MCP plugin has not registered with the "
                        "bridge yet.", "yl")
                    log("If Studio is still STARTING UP, give it a minute (its plugin registers "
                        "late in boot).", "yl")
                    log("If Studio is fully loaded and this stays yellow: in Roblox Studio, simply "
                        "OPEN Assistant Settings > MCP Servers - opening that panel makes the "
                        "plugin re-register (validated twice live). If that's not enough, toggle "
                        "'Enable Studio as MCP server' OFF then ON there.", "yl")
                else:
                    log("Roblox Studio proxy looks stuck (known StudioMCP disconnect bug) - "
                        "restarting it to recover.", "yl")
                    try:
                        await asyncio.to_thread(mgr.restart, "roblox")
                        await broadcast_status()
                    except Exception as e:
                        log(f"auto-restart of roblox proxy failed: {e}", "rd")
        # PLACE-level churn: `app` can stay stuck reporting True the whole time
        # (seen live 2026-07-11 - Studio fully closed, list_roblox_studios kept
        # answering with a leftover studio entry for 4+ minutes, so the app-drop
        # trigger above never fires) while `place` flip-flops "loaded"/"closed"
        # every ~10-20s forever. That is not a user opening/closing places that
        # fast - it is the same class of stuck-proxy bug, just visible at the
        # place layer instead of the app layer. A fresh StudioMCP.exe process
        # cannot carry over stale cached state, so the same restart applies.
        place_transitions[:] = [t for t in place_transitions if now - t < 90]
        if len(place_transitions) >= 4 and now - last_auto_restart > 90:
            # Same running-Studio guard as the app-drop recovery above: with a
            # real Studio window up, a proxy restart can only collide with the
            # plugin's one-shot registration; the churn is Studio-side state.
            if await asyncio.to_thread(_roblox_studio_app_running) is not True:
                last_auto_restart = now
                log(f"Roblox Studio's place status flipped {len(place_transitions)} times in the last "
                    "90s (known StudioMCP stuck-proxy bug) - restarting the proxy to recover.", "yl")
                try:
                    await asyncio.to_thread(mgr.restart, "roblox")
                    await broadcast_status()
                except Exception as e:
                    log(f"auto-restart of roblox proxy failed: {e}", "rd")
                place_transitions.clear()
        if place is not None and place != prev_place:
            # Debounce: StudioMCP's binding to Studio blips every few seconds
            # on some machines (self-healing in ~1-4s - seen live as "Bound
            # studio ... disconnected" stderr). A probe landing in that window
            # can misread EITHER direction - most confusingly, reporting
            # "place loaded" from a stale cached response while the place is
            # actually still closed (seen live). Recheck once before trusting
            # a transition, in either direction.
            await asyncio.sleep(1.2)
            try:
                confirm = (await asyncio.to_thread(probe_studio))["place"]
            except Exception:
                confirm = None
            if confirm is None or confirm != place:
                continue  # didn't hold up on recheck - treat as noise, not a real change
            if place is True:
                log("Place loaded in Studio.", "gr")
            else:
                log("Place closed (Studio app still connected).", "yl")
            prev_place = place
            place_transitions.append(time.time())
            await broadcast_status()


async def _supervised(name, coro_factory):
    """Run a watcher coroutine forever, restarting it if it ever raises.

    Both watchers are designed to never raise, but one line proved that wrong
    in practice (an UnboundLocalError killed studio_watch SILENTLY - asyncio
    only prints 'Task exception was never retrieved' at shutdown, so all
    Studio monitoring and status broadcasts just stopped until the user
    restarted the bridge). A crash in a watcher must never be silent or
    permanent: log it loudly, wait a beat, start a fresh instance.
    """
    while True:
        try:
            await coro_factory()
            return  # normal completion (doesn't happen today, but respect it)
        except Exception as e:
            log(f"{name} crashed: {type(e).__name__}: {e} - restarting it in 5s "
                f"(please report this).", "rd")
            await asyncio.sleep(5)


async def main():
    print(f"\n{C['cy']}  Multi-Script Bridge v{BRIDGE_VERSION}{C['reset']}  {C['dim']}- Multi-Engine MCP - ws://{HOST}:{PORT}{C['reset']}\n")
    log(f"===== BRIDGE START  v{BRIDGE_VERSION}  pid={os.getpid()}  log={LOG_PATH} =====", "cy")
    await asyncio.to_thread(_kill_orphan_studio_mcp)
    killed_squatter = await asyncio.to_thread(check_studio_port)
    mgr.load_config()

    # Shared "we already told the user the corrective step" flag. Two producers
    # can print the 'toggle Studio's MCP server' action banner: the early
    # _early_studio_guidance task (fast, doesn't wait for start_all's ~48s grace
    # loop) and the post-start_all diagnostic block in _boot_and_diagnose. This
    # flag lets whichever fires first suppress the other, so the user never sees
    # the same instruction twice. Mutable dict so both nested coroutines share it.
    _guidance_shown = {"v": False}

    async def _early_studio_guidance():
        """Print the corrective action banner WITHOUT waiting for start_all()'s
        full ~48s grace loop.

        RobloxClient.start() retries tools/list for up to ~48s to catch a Studio
        that attaches a little late - correct for a Studio that IS coming, but it
        also means a user whose Studio is simply closed or whose MCP toggle is off
        waits ~48s before the terminal tells them what to do (the extension, which
        reads status over the socket, already says it immediately). So after a
        short grace we check independently and, if still not connected, show the
        step now. If Studio then attaches, studio_watch prints the green
        'connected' line - so an early banner is at worst redundant, never wrong
        (the user confirmed a premature toggle hint during boot is harmless).

        Deliberately does NOT try to distinguish 'Studio closed' from 'MCP toggle
        off': probe_studio can't tell them apart (both read app=False, see its
        docstring), and the banner wording already covers both, so there is no
        finer detection to preserve here. A proven port-squatter case is left to
        _boot_and_diagnose / studio_watch, which print their own, more specific
        hint."""
        if PRIMARY_SERVER_ID not in mgr.clients:
            return
        await asyncio.sleep(12)  # give a fast, normal attach the chance to win
        if _guidance_shown["v"]:
            return
        rc = mgr.clients.get(PRIMARY_SERVER_ID)
        if rc is None or getattr(rc, "saw_foreign_ws_host", False):
            return
        if rc.tools_cache:
            # Tools present - either connected, or an attach is mid-flight; defer
            # to the authoritative probe / studio_watch rather than second-guess.
            st = await asyncio.to_thread(probe_studio)
            if st["app"] is not False:
                return
        _guidance_shown["v"] = True
        action_banner([
            "Open your place in Roblox Studio.",
            "Go to: Assistant Settings > MCP Servers",
            "       > 'Enable Studio as MCP server'",
            "It can take up to ~10s; this window will turn green.",
        ])

    async def _boot_and_diagnose():
        """Launch every configured MCP server and print the boot diagnostic
        banner. Runs as a background task AFTER the socket below is already
        listening, so a slow or absent Roblox Studio never delays the
        extension's ability to connect and use OTHER MCP servers (e.g.
        Blender) right away - only the terminal banner and Roblox's own
        auto-recovery loop wait on this. (mgr.start_all() itself also
        launches every server in parallel now, for the same reason.)"""
        try:
            await asyncio.to_thread(mgr.start_all)
        except Exception as e:
            log(f"server startup error: {e}", "rd")
            log("The bridge will keep running; it retries on the first tool call.", "yl")
        total = len(mgr.list_tools())
        # Roblox-only count for the corrective message below: list_tools() sums
        # every configured server (Roblox + addons like Blender), so printing
        # `total` there falsely blamed addon tools on "NO Roblox Studio connected"
        # (seen live: 49 = 27 Roblox + 22 Blender, message only about Roblox).
        roblox_client = mgr.clients.get("roblox")
        roblox_total = len(roblox_client.tools_cache) if roblox_client else 0

        # Port-hijack check (ropilot etc.), done at boot: the child's stderr has
        # by now had its ~12s grace loop to reveal it connected to a foreign host
        # on the MCP port. This is proof the port is squatted even when the
        # one-shot check_studio_port() at startup missed it (a background helper
        # grabbing the port a beat after that check ran - seen live 2026-07-13).
        if (roblox_client is not None and roblox_total == 0
                and roblox_client.saw_foreign_ws_host):
            killed, sname = await asyncio.to_thread(_kill_port_squatter)
            if killed:
                try:
                    await asyncio.to_thread(mgr.restart, "roblox")
                    roblox_total = len(roblox_client.tools_cache)
                    total = len(mgr.list_tools())
                except Exception as e:
                    log(f"roblox proxy restart after squatter kill failed: {e}", "rd")
                _print_squatter_hint(sname)

        # Set True if we kill a leftover StudioMCP zombie below and print the
        # re-register hint - so the diagnostic block further down doesn't ALSO
        # print its own near-identical action banner (the same de-duplication
        # killed_squatter already does for the ropilot-squatter path).
        reclaimed_zombie = False
        # Zombie-port deadlock check, done at boot too (not just studio_watch):
        # with Studio ALREADY open, _kill_orphan_studio_mcp was skipped by its
        # safety guard, so a leftover StudioMCP.exe may still own the port and
        # our fresh proxy just spent its 12s grace loop talking to nothing.
        if (roblox_client is not None and roblox_total == 0
                and await asyncio.to_thread(_roblox_studio_app_running) is True
                and await asyncio.to_thread(_reclaim_studio_port, roblox_client)):
            try:
                await asyncio.to_thread(mgr.restart, "roblox")
                roblox_total = len(roblox_client.tools_cache)
                total = len(mgr.list_tools())
            except Exception as e:
                log(f"roblox proxy restart after zombie kill failed: {e}", "rd")
            reclaimed_zombie = True
            _print_reregister_hint()

        # A tool count alone only proves StudioMCP (the proxy) is up - it advertises
        # its catalogue even with NO Studio attached. The authoritative "a Studio is
        # actually connected" signal is the list_roblox_studios probe. So we probe
        # FIRST and only show the green "ready" line when Studio is really attached;
        # otherwise we show just the corrective step (no misleading green success).
        # Probe even when total == 0: StudioMCP advertises an EMPTY catalogue when
        # Studio's MCP server toggle is off (or no place is open), so 0 tools is the
        # most common "needs a corrective step" state, not a success.
        _st = await asyncio.to_thread(probe_studio)
        # Even when Studio (and its place) were ALREADY open before the bridge
        # started, the freshly-launched StudioMCP proxy needs a moment to (re)bind
        # to Studio's own MCP port - so an instant probe right after launch often
        # reads app=False for a beat before flipping True a few seconds later
        # (studio_watch would catch it, but only after printing a scary yellow
        # "not connected" block first). Give it the same grace period the tools
        # probe already gets before deciding it is a real problem.
        if _st["app"] is False:
            with _Spinner("    waiting for Roblox Studio to attach..."):
                for _ in range(8):
                    await asyncio.sleep(1)
                    _st = await asyncio.to_thread(probe_studio)
                    if _st["app"] is not False:
                        break
        # A single app=True reading can be a STALE positive: StudioMCP.exe can
        # answer list_roblox_studios with a leftover studio entry from a PREVIOUS
        # session even though no Studio window is actually open right now (seen
        # live 2026-07-11: bridge booted with Studio fully closed, still printed
        # "Roblox Studio connected" from the very first probe). studio_watch
        # already distrusts a single reading for PLACE transitions the same way -
        # apply the identical confirm-before-trusting step here for APP, so the
        # boot banner can't announce a connection that isn't really there.
        if _st["app"] is True:
            await asyncio.sleep(1.5)
            confirm = await asyncio.to_thread(probe_studio)
            if confirm["app"] is not True:
                _st = confirm
        if roblox_client is not None and (roblox_total == 0 or _st["app"] is False):
            if killed_squatter or reclaimed_zombie or _guidance_shown["v"]:
                # The action banner was ALREADY shown - either right after a kill
                # (check_studio_port / _print_reregister_hint for a zombie) or by
                # the early _early_studio_guidance task. Repeating the full
                # explanation here in a different color, seconds later, reads as a
                # second unrelated problem to a non-technical user (seen live
                # 2026-07-13: the toggle instruction and this block blurred
                # together). Just confirm we're still waiting, no new instructions.
                log("    still waiting for you to toggle Studio's MCP server "
                    "(see the action box above)...", "yl")
            else:
                _guidance_shown["v"] = True
                # No squatter: Studio is simply closed, or its MCP option is off.
                # Match the exact steps the extension itself tells the user
                # (Assistant Settings > MCP Servers), not a paraphrase - a
                # differently-worded instruction here reads as a second,
                # unrelated problem instead of the same one step.
                if roblox_total > 0:
                    log(f"    {roblox_total} Roblox tools loaded, but NO Roblox Studio is connected yet.", "yl")
                    log("    (This can be a slow attach that clears itself within ~10-15s -", "yl")
                    log("    watch for a green 'Roblox Studio connected' line right after.)", "yl")
                action_banner([
                    "Open your place in Roblox Studio.",
                    "Go to: Assistant Settings > MCP Servers",
                    "       > 'Enable Studio as MCP server'",
                    "It can take up to ~10s; this window will turn green.",
                ])
        elif _st["app"] is True:
            log(f"ready {total} tools available - Roblox Studio connected", "gr")
        else:
            log(f"ready {total} tools available ({len(mgr.clients)} MCP server(s))", "gr")
        asyncio.create_task(_supervised(
            "studio_watch", lambda: studio_watch(_st["app"], _st["place"])))

    async def _early_status_pushes():
        """A few follow-up status broadcasts shortly after boot.

        mgr.start_all() still doesn't RETURN until every server's thread has
        joined - including Roblox's, which can take up to ~48s (StudioMCP's
        own internal "waiting for tools" retry loop, seen live). So a single
        broadcast placed after start_all() would be just as slow as the old
        blocking behavior for the exact case this is meant to fix: an addon
        server (e.g. Blender) that's ready in 1-13s while Roblox is still
        slowly timing out. Poll-and-broadcast a few times instead, cheaply,
        so any extension that connected during that window self-corrects
        quickly instead of staying stuck on its first, incomplete snapshot.
        """
        for interval in (2, 2, 4, 6, 6):  # cumulative: 2s, 4s, 8s, 14s, 20s after boot
            await asyncio.sleep(interval)
            await broadcast_status()

    # Free our own port from a leftover bridge (double-launch / X-closed window /
    # prior crash) BEFORE binding, so relaunching start.bat "just works" instead
    # of dying on WinError 10048. Only ever kills a proven bridge.py; anything
    # else falls through to the friendly bind-error below.
    if await asyncio.to_thread(_reclaim_bridge_port):
        await asyncio.sleep(0.6)  # let Windows release the socket before we bind

    try:
        server_ctx = await websockets.serve(
            handler, HOST, PORT, ping_interval=20, ping_timeout=20,
            max_size=16 * 1024 * 1024)
    except OSError as e:
        # errno 10048 (Win) / EADDRINUSE: something we could NOT auto-kill still
        # owns the port - another app, or a python whose cmdline we couldn't read.
        if getattr(e, "errno", None) in (98, 10048) or "10048" in str(e):
            owner = await asyncio.to_thread(_port_owner, PORT)
            who = f" by '{owner[1]}' (pid {owner[0]})" if owner else ""
            log(f"could not start: port {PORT} is already in use{who}.", "rd")
            log(f"    A previous bridge may still be running, or another app took "
                f"the port. Close it, then relaunch. To find it:", "yl")
            log(f"      netstat -ano | findstr {PORT}", "yl")
            log(f"      taskkill /F /PID <the pid from the last column>", "yl")
            log(f"    Or set a different port before start.bat:  set ZS_BRIDGE_PORT=17614", "yl")
            return
        raise

    try:
        plugin_server = await asyncio.start_server(plugin_http_handler, HOST, PLUGIN_PORT, limit=65536)
    except OSError as e:
        plugin_server = None
        log(f"companion-plugin status port {PLUGIN_PORT} unavailable: {e}", "yl")

    async with server_ctx:
        if plugin_server:
            await plugin_server.start_serving()
        log(f"listening on ws://{HOST}:{PORT}  - companion plugin http://{HOST}:{PLUGIN_PORT}", "cy")
        asyncio.create_task(_supervised("server_watch", server_watch))
        asyncio.create_task(_boot_and_diagnose())
        asyncio.create_task(_early_studio_guidance())
        asyncio.create_task(_early_status_pushes())
        await asyncio.Future()  # run forever


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("shutting down...", "yl")
        for c in mgr.clients.values():
            c.stop()
    finally:
        log("===== BRIDGE STOP =====", "cy")
