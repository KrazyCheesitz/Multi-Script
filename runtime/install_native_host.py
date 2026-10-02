# SPDX-License-Identifier: GPL-3.0-or-later
"""install_native_host.py - one-time setup that lets the chat bar's terminal icon
start the Multi-Script bridge for you (this replaced the old start.bat launcher).

    python runtime/install_native_host.py              # auto-detect everything
    python runtime/install_native_host.py --extension-id <id> [--extension-id <id2>]
    python runtime/install_native_host.py --uninstall
    python runtime/install_native_host.py --status

What it does (all per-user, nothing needs administrator rights):
  1. checks Python and installs the one dependency (websockets)
  2. writes a tiny launcher next to native_host.py
  3. writes the native-messaging manifest listing ONLY your extension ID(s)
  4. registers that manifest for Chrome, Edge, Brave, Chromium and Vivaldi
Run it once. After that the terminal icon starts, stops and watches the bridge.
"""
import argparse
import glob
import json
import os
import re
import subprocess
import sys

HOST_NAME = "com.multiscript.bridge"
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
EXT_DIR = os.path.join(ROOT, "extension")
HOST_PY = os.path.join(HERE, "native_host.py")
IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"

# (label, windows registry subkey, mac app-support dir, linux config dir)
BROWSERS = [
    ("Chrome", r"Software\Google\Chrome", "Google/Chrome", "google-chrome"),
    ("Edge", r"Software\Microsoft\Edge", "Microsoft Edge", "microsoft-edge"),
    ("Brave", r"Software\BraveSoftware\Brave-Browser", "BraveSoftware/Brave-Browser", "BraveSoftware/Brave-Browser"),
    ("Chromium", r"Software\Chromium", "Chromium", "chromium"),
    ("Vivaldi", r"Software\Vivaldi", "Vivaldi", "vivaldi"),
]
# user-data roots used to auto-detect the unpacked extension's ID
def _user_data_roots():
    home = os.path.expanduser("~")
    out = []
    for label, _reg, mac, lin in BROWSERS:
        if IS_WIN:
            la = os.environ.get("LOCALAPPDATA", os.path.join(home, "AppData", "Local"))
            sub = {"Chrome": r"Google\Chrome", "Edge": r"Microsoft\Edge", "Brave": r"BraveSoftware\Brave-Browser",
                   "Chromium": "Chromium", "Vivaldi": "Vivaldi"}[label]
            out.append((label, os.path.join(la, sub, "User Data")))
        elif IS_MAC:
            out.append((label, os.path.join(home, "Library", "Application Support", mac)))
        else:
            out.append((label, os.path.join(home, ".config", lin)))
    return out


def say(msg=""):
    print(msg, flush=True)


def norm(p):
    return os.path.normcase(os.path.realpath(p)).rstrip("\\/")


def detect_extension_ids():
    """Find IDs of Chromium extensions loaded unpacked from OUR extension folder."""
    found = {}
    want = norm(EXT_DIR)
    for label, root in _user_data_roots():
        if not os.path.isdir(root):
            continue
        for pref in glob.glob(os.path.join(root, "*", "Preferences")) + glob.glob(os.path.join(root, "*", "Secure Preferences")):
            try:
                with open(pref, "r", encoding="utf-8", errors="replace") as fh:
                    data = json.load(fh)
                settings = ((data.get("extensions") or {}).get("settings")) or {}
            except Exception:
                continue
            for eid, info in settings.items():
                path = (info or {}).get("path")
                if isinstance(path, str) and path and norm(path) == want and re.fullmatch(r"[a-p]{32}", eid):
                    found.setdefault(eid, label)
    return found


def check_python():
    if sys.version_info < (3, 9):
        say(f"  x Python {sys.version.split()[0]} is too old - the bridge needs 3.9 or newer.")
        say("    Install the current Python from https://www.python.org/downloads/ (tick 'Add to PATH').")
        return False
    say(f"  ok Python {sys.version.split()[0]}  ({sys.executable})")
    return True


def ensure_websockets():
    try:
        __import__("websockets")
        say("  ok websockets library present")
        return True
    except ImportError:
        pass
    say("  .. installing the websockets library (first time only)...")
    r = subprocess.run([sys.executable, "-m", "pip", "install", "--user", "websockets"])
    if r.returncode != 0:
        say("  x pip could not install websockets (no internet, firewall, or no pip).")
        say("    Try:  python -m pip install websockets      then run this setup again.")
        return False
    say("  ok websockets installed")
    return True


def write_launcher():
    if IS_WIN:
        path = os.path.join(HERE, "native_host.bat")
        with open(path, "w", encoding="utf-8", newline="\r\n") as fh:
            fh.write('@echo off\r\n"%s" "%s" %%*\r\n' % (sys.executable, HOST_PY))
        return path
    path = os.path.join(HERE, "native_host.sh")
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write('#!/bin/sh\nexec "%s" "%s" "$@"\n' % (sys.executable, HOST_PY))
    os.chmod(path, 0o755)
    return path


def manifest_for(launcher, ids):
    return {
        "name": HOST_NAME,
        "description": "Multi-Script bridge launcher (starts runtime/bridge.py for the extension's terminal icon)",
        "path": launcher,
        "type": "stdio",
        "allowed_origins": [f"chrome-extension://{i}/" for i in ids],
    }


def register(manifest_path):
    done = []
    if IS_WIN:
        import winreg
        for label, reg, _m, _l in BROWSERS:
            try:
                key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, reg + r"\NativeMessagingHosts" + "\\" + HOST_NAME)
                winreg.SetValueEx(key, "", 0, winreg.REG_SZ, manifest_path)
                winreg.CloseKey(key)
                done.append(label)
            except OSError:
                pass
        return done
    home = os.path.expanduser("~")
    for label, _r, mac, lin in BROWSERS:
        base = os.path.join(home, "Library", "Application Support", mac) if IS_MAC else os.path.join(home, ".config", lin)
        if not os.path.isdir(base):
            continue  # browser not installed - do not litter
        d = os.path.join(base, "NativeMessagingHosts")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, HOST_NAME + ".json"), "w", encoding="utf-8") as fh:
            fh.write(open(manifest_path, encoding="utf-8").read())
        done.append(label)
    return done


def unregister():
    removed = []
    if IS_WIN:
        import winreg
        for label, reg, _m, _l in BROWSERS:
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, reg + r"\NativeMessagingHosts" + "\\" + HOST_NAME)
                removed.append(label)
            except OSError:
                pass
    else:
        home = os.path.expanduser("~")
        for label, _r, mac, lin in BROWSERS:
            base = os.path.join(home, "Library", "Application Support", mac) if IS_MAC else os.path.join(home, ".config", lin)
            f = os.path.join(base, "NativeMessagingHosts", HOST_NAME + ".json")
            if os.path.isfile(f):
                os.remove(f)
                removed.append(label)
    return removed


def manifest_path():
    return os.path.join(HERE, HOST_NAME + ".json")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Register the Multi-Script terminal host with your browser.")
    ap.add_argument("--extension-id", action="append", default=[], help="extension ID (chrome://extensions, Developer mode)")
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--no-pause", action="store_true")
    ap.add_argument("--no-start", action="store_true", help="register only; do not start the bridge now")
    ap.add_argument("--skip-deps", action="store_true", help="do not pip-install websockets (offline/testing)")
    a = ap.parse_args(argv)

    say()
    say("  === Multi-Script - terminal setup ===")
    say()
    if a.uninstall:
        say("  removed from: " + (", ".join(unregister()) or "(nothing was registered)"))
        return 0
    if a.status:
        say("  manifest : " + (manifest_path() if os.path.isfile(manifest_path()) else "not written"))
        if os.path.isfile(manifest_path()):
            say("  allowed  : " + ", ".join(json.load(open(manifest_path())).get("allowed_origins", [])))
        return 0

    say("  [1/4] Python")
    if not check_python():
        return 1
    say("  [2/4] Dependency")
    if not a.skip_deps and not ensure_websockets():
        return 1

    say("  [3/4] Finding your Multi-Script extension")
    ids = [i.strip() for i in a.extension_id if i.strip()]
    for i in ids:
        if not re.fullmatch(r"[a-p]{32}", i):
            say(f"  x '{i}' is not a valid extension ID (32 letters a-p).")
            return 1
    auto = detect_extension_ids()
    for eid, browser in auto.items():
        say(f"  ok found in {browser}: {eid}")
        if eid not in ids:
            ids.append(eid)
    # keep IDs from a previous run so a second browser/profile is additive
    if os.path.isfile(manifest_path()):
        try:
            for o in json.load(open(manifest_path())).get("allowed_origins", []):
                m = re.fullmatch(r"chrome-extension://([a-p]{32})/", o)
                if m and m.group(1) not in ids:
                    ids.append(m.group(1))
        except Exception:
            pass
    if not ids:
        say("  ! Could not find the extension automatically.")
        say("    1) Open chrome://extensions (or edge://extensions), turn on Developer mode")
        say("    2) Load unpacked ->  " + EXT_DIR)
        say("    3) Copy the ID shown on the Multi-Script card, then run:")
        say("       python runtime/install_native_host.py --extension-id <that id>")
        say("    (the terminal panel inside the chat also shows this exact command)")
        return 2

    say("  [4/4] Registering the host")
    launcher = write_launcher()
    with open(manifest_path(), "w", encoding="utf-8") as fh:
        json.dump(manifest_for(launcher, ids), fh, indent=2)
    where = register(manifest_path())
    if not where:
        say("  x No supported browser found to register with.")
        return 1
    say("  ok registered for: " + ", ".join(where))
    if not a.no_start and not a.skip_deps:
        try:
            sys.path.insert(0, HERE)
            import native_host as _nh
            r = _nh.cmd_start({})
            if r.get("ok"):
                say("  ok bridge is running on port " + str(_nh.PORT))
            else:
                say("  ! bridge did not start: " + str(r.get("error") or r.get("code")))
        except Exception as exc:  # never fail the install over the optional start
            say("  ! could not start the bridge now: " + str(exc))
    say()
    say("  Done. Fully restart the browser once, open any supported AI chat, and")
    say("  click the terminal icon in the Multi-Script bar. It starts the bridge for")
    say("  you from then on - there is no launcher script to keep running.")
    say()
    return 0


if __name__ == "__main__":
    code = main()
    if IS_WIN and "--no-pause" not in sys.argv and sys.stdin and sys.stdin.isatty():
        try:
            input("  Press Enter to close...")
        except EOFError:
            pass
    sys.exit(code)
