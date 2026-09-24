import asyncio
import importlib.util
import json
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
w = types.ModuleType("websockets")
w.ConnectionClosed = Exception
sys.modules.setdefault("websockets", w)
spec = importlib.util.spec_from_file_location("plugin_bridge", ROOT / "runtime" / "bridge.py")
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)

plugin_source = (ROOT / "roblox-plugin" / "MultiScriptCompanion.server.lua").read_text(encoding="utf-8")
for required in ['BRIDGE_URL="http://127.0.0.1:17614"','"/plugin/heartbeat"','"/status"',"Studio as MCP Server","MAX_ISSUES=100","MultiScriptManaged",'PLUGIN_VERSION="6.13.0"',"DIAGNOSTIC_VERSION=2","Run non-destructive readiness scan","Print sanitized diagnostic report","readinessScore"]: assert required in plugin_source, required
for forbidden in ["/plugin/next","/plugin/result","msrb_","roblox-companion-tools.json"]: assert forbidden not in plugin_source
assert "loadstring" in plugin_source and "SetAsync" in plugin_source

class FakeClient:
    tools_cache = [{"name": "get_studio_state"}, {"name": "script_read"}]
    def is_alive(self): return True

bridge.mgr.clients = {"roblox": FakeClient()}
bridge.probe_studio = lambda: {"app": True, "place": True}

async def request(port, method, path, body=b""):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(
        f"{method} {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode("ascii") + body
    )
    await writer.drain()
    raw = await reader.read()
    writer.close()
    await writer.wait_closed()
    head, payload = raw.split(b"\r\n\r\n", 1)
    code = int(head.split()[1])
    return code, json.loads(payload)

async def main():
    server = await asyncio.start_server(bridge.plugin_http_handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        heartbeat = json.dumps({
            "pluginVersion": "6.13.0",
            "placeId": 42,
            "placeName": "T" * 300,
            "diagnosticVersion": 2,
            "readinessScore": 140,
            "issueCount": 500,
            "highIssueCount": 7,
            "scanDurationMs": -5,
            "scriptCount": 8,
            "moduleCount": 3,
            "ignoredSecret": "must not persist",
        }).encode()
        code, result = await request(port, "POST", "/plugin/heartbeat", heartbeat)
        assert code == 200 and result["ok"] is True
        assert "ignoredSecret" not in bridge.plugin_state
        assert len(bridge.plugin_state["placeName"]) == 120
        assert bridge.plugin_state["readinessScore"] == 100 and bridge.plugin_state["issueCount"] == 100
        assert bridge.plugin_state["scanDurationMs"] == 0
        code, catalog = await request(port, "GET", "/catalog")
        assert code == 200 and catalog["nativeToolCount"] == 2
        code, status = await request(port, "GET", "/status")
        assert code == 200
        assert status["bridgeVersion"] == "6.13.0"
        assert status["roblox"]["connected"] is True
        assert status["roblox"]["nativeTools"] == 2
        assert status["companionPlugin"]["connected"] is True
        code, _ = await request(port, "GET", "/missing")
        assert code == 404

asyncio.run(main())
print("PASS Roblox companion v6.13 diagnostics, bounded typed heartbeat and security boundary")
