#!/usr/bin/env python3
"""Secure local ElevenLabs sound-effects integration for Multi-Script."""
from pathlib import Path
import hashlib, json, os, re, time
from urllib import error, parse, request

HERE = Path(__file__).resolve().parent
ENV_FILE = HERE / ".env"
OUTPUT_DIR = HERE / "generated" / "audio"
ENDPOINT = "https://api.elevenlabs.io/v1/sound-generation"
MODEL_ID = "eleven_text_to_sound_v2"


def _api_key():
    value = os.getenv("ELEVENLABS_API_KEY", "").strip()
    if value:
        return value
    if ENV_FILE.is_file():
        for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip() == "ELEVENLABS_API_KEY":
                return value.strip().strip('"').strip("'")
    return ""


def configure_api_key(value):
    """Store one user's key only in the local bridge runtime/.env."""
    key = str(value or "").strip()
    if len(key) < 10 or any(c in key for c in "\r\n"):
        raise RuntimeError("Enter a valid ElevenLabs API key")
    lines = []
    if ENV_FILE.is_file():
        lines = [x for x in ENV_FILE.read_text(encoding="utf-8").splitlines()
                 if not x.strip().startswith("ELEVENLABS_API_KEY=")]
    lines.append(f"ELEVENLABS_API_KEY={key}")
    temp = ENV_FILE.with_suffix(".env.tmp")
    temp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try: os.chmod(temp, 0o600)
    except OSError: pass
    temp.replace(ENV_FILE)
    try: os.chmod(ENV_FILE, 0o600)
    except OSError: pass
    return status()


def clear_api_key():
    if ENV_FILE.is_file():
        lines = [x for x in ENV_FILE.read_text(encoding="utf-8").splitlines()
                 if not x.strip().startswith("ELEVENLABS_API_KEY=")]
        if lines:
            ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
            try: os.chmod(ENV_FILE, 0o600)
            except OSError: pass
        else:
            ENV_FILE.unlink(missing_ok=True)
    return status()


def status():
    return {
        "provider": "ElevenLabs",
        "configured": bool(_api_key()),
        "model": MODEL_ID,
        "endpoint": ENDPOINT,
        "outputDirectory": str(OUTPUT_DIR),
        "setupCommand": "python runtime/configure_elevenlabs.py",
        "secretStorage": "ELEVENLABS_API_KEY environment variable or runtime/.env (local bridge only)",
    }


def _safe_name(value):
    value = re.sub(r"[^a-zA-Z0-9._-]+", "-", str(value or "sound-effect")).strip("-.")
    return (value[:80] or "sound-effect")


def _http_error(exc):
    try:
        body = exc.read().decode("utf-8", "replace")
        parsed = json.loads(body)
        detail = parsed.get("detail", parsed)
        if isinstance(detail, dict):
            detail = detail.get("message") or detail.get("status") or json.dumps(detail)
        return f"ElevenLabs HTTP {exc.code}: {detail}"
    except Exception:
        return f"ElevenLabs HTTP {getattr(exc, 'code', 'error')}: {getattr(exc, 'reason', exc)}"


def generate_sound_effect(text, name="sound-effect", duration_seconds=None, loop=False,
                          prompt_influence=0.3, output_format="mp3_44100_128", opener=None):
    text = str(text or "").strip()
    if not text:
        raise RuntimeError("text is required")
    if len(text) > 2500:
        raise RuntimeError("text must be 2500 characters or fewer")
    key = _api_key()
    if not key:
        raise RuntimeError("ElevenLabs is not configured. Run: python runtime/configure_elevenlabs.py")
    if duration_seconds is not None:
        duration_seconds = float(duration_seconds)
        if duration_seconds < 0.5 or duration_seconds > 30:
            raise RuntimeError("duration_seconds must be between 0.5 and 30")
    influence = float(prompt_influence)
    if influence < 0 or influence > 1:
        raise RuntimeError("prompt_influence must be between 0 and 1")
    allowed_formats = {"mp3_22050_32", "mp3_44100_64", "mp3_44100_96", "mp3_44100_128", "mp3_44100_192", "pcm_16000", "pcm_22050", "pcm_24000", "pcm_44100"}
    if output_format not in allowed_formats:
        raise RuntimeError("unsupported output_format")
    payload = {"text": text, "loop": bool(loop), "prompt_influence": influence, "model_id": MODEL_ID}
    if duration_seconds is not None:
        payload["duration_seconds"] = duration_seconds
    url = ENDPOINT + "?" + parse.urlencode({"output_format": output_format})
    req = request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST", headers={
        "xi-api-key": key,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg, audio/wav, application/octet-stream",
        "User-Agent": "Multi-Script/5.3.1",
    })
    open_fn = opener or request.urlopen
    try:
        with open_fn(req, timeout=120) as response:
            audio = response.read()
            content_type = response.headers.get("Content-Type", "audio/mpeg").split(";", 1)[0].strip().lower()
            character_cost = response.headers.get("character-cost")
    except error.HTTPError as exc:
        raise RuntimeError(_http_error(exc)) from exc
    except error.URLError as exc:
        raise RuntimeError(f"Could not reach ElevenLabs: {exc.reason}") from exc
    if not audio:
        raise RuntimeError("ElevenLabs returned an empty audio response")
    extension = ".wav" if output_format.startswith("pcm_") or "wav" in content_type else ".mp3"
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    digest = hashlib.sha256(audio).hexdigest()[:10]
    path = OUTPUT_DIR / f"{_safe_name(name)}-{stamp}-{digest}{extension}"
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_bytes(audio)
    temporary.replace(path)
    metadata = {
        "provider": "ElevenLabs", "model": MODEL_ID, "created": str(path), "format": output_format,
        "contentType": content_type, "bytes": len(audio), "sha256": hashlib.sha256(audio).hexdigest(),
        "durationSeconds": duration_seconds, "loop": bool(loop), "promptInfluence": influence,
        "characterCost": character_cost, "prompt": text,
        "engineHandoff": "Import this local file through the connected engine MCP, configure loop/spatial/compression settings, then test it in context.",
    }
    path.with_suffix(path.suffix + ".json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    return metadata


def list_generated_audio(limit=50):
    limit = max(1, min(200, int(limit)))
    if not OUTPUT_DIR.is_dir():
        return {"count": 0, "assets": []}
    rows = []
    for path in sorted(OUTPUT_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
            if Path(item.get("created", "")).is_file():
                rows.append(item)
        except Exception:
            continue
        if len(rows) >= limit:
            break
    return {"count": len(rows), "assets": rows}
