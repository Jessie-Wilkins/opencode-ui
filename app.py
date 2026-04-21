#!/usr/bin/env python3

from __future__ import annotations

import asyncio
import base64
import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

app = FastAPI(
    title="OpenCode Lens",
    version="1.0.0",
    description=(
        "Mobile-friendly web wrapper for the OpenCode HTTP server. "
        "It proxies the upstream OpenCode API and adds a dashboard for sessions, "
        "events, files, and Ollama model selection."
    ),
)

UPSTREAM_URL = os.getenv("OPENCODE_SERVER_URL", "http://127.0.0.1:4096").rstrip("/")
UPSTREAM_USERNAME = os.getenv("OPENCODE_SERVER_USERNAME", "opencode")
UPSTREAM_PASSWORD = os.getenv("OPENCODE_SERVER_PASSWORD", "")
OLLAMA_URL = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
DEFAULT_MODEL = os.getenv("OPENCODE_MODEL", "")
REQUEST_TIMEOUT = float(os.getenv("OPENCODE_PROXY_TIMEOUT", "30"))
SERVER_START_TIMEOUT = float(os.getenv("OPENCODE_SERVER_START_TIMEOUT", "20"))
OPENCODE_INSTALL_URL = "https://raw.githubusercontent.com/opencode-ai/opencode/refs/heads/main/install"
OPENCODE_INSTALL_VERSION = os.getenv("OPENCODE_INSTALL_VERSION", "").strip()
OPENCODE_INSTALL_LOCK = asyncio.Lock()
OPENCODE_SERVER_LOCK = asyncio.Lock()

HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}

EVENT_TYPES = [
    "server.connected",
    "session.created",
    "session.compacted",
    "session.deleted",
    "session.diff",
    "session.error",
    "session.idle",
    "session.status",
    "session.updated",
    "message.part.removed",
    "message.part.updated",
    "message.removed",
    "message.updated",
    "permission.asked",
    "permission.replied",
    "file.edited",
    "file.watcher.updated",
    "tool.execute.before",
    "tool.execute.after",
    "command.executed",
    "lsp.client.diagnostics",
    "lsp.updated",
    "todo.updated",
    "shell.env",
    "installation.updated",
    "tui.prompt.append",
    "tui.command.execute",
    "tui.toast.show",
]


def upstream_auth_headers() -> dict[str, str]:
    if UPSTREAM_PASSWORD:
        token = base64.b64encode(f"{UPSTREAM_USERNAME}:{UPSTREAM_PASSWORD}".encode()).decode()
        return {"authorization": f"Basic {token}"}
    return {}


def filtered_response_headers(headers: httpx.Headers) -> dict[str, str]:
    allowed: dict[str, str] = {}
    for key, value in headers.items():
        if key.lower() in HOP_BY_HOP_HEADERS:
            continue
        if key.lower() in {"content-length", "content-encoding"}:
            continue
        allowed[key] = value
    return allowed


def opencode_binary_path() -> str | None:
    return shutil.which("opencode")


async def probe_opencode() -> dict[str, Any]:
    binary = opencode_binary_path()
    status: dict[str, Any] = {
        "installed": bool(binary),
        "binary": binary,
    }
    if not binary:
        for candidate in (
            Path.home() / ".opencode" / "bin" / "opencode",
            Path.home() / ".local" / "bin" / "opencode",
            Path.home() / "bin" / "opencode",
        ):
            if candidate.exists():
                status["binary_hint"] = str(candidate)
                break
        return status

    try:
        proc = await asyncio.create_subprocess_exec(
            binary,
            "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        version_output = stdout.decode(errors="replace").strip() or stderr.decode(errors="replace").strip()
        status["version"] = version_output or None
    except Exception as exc:  # pragma: no cover - local environment specific
        status["version_error"] = str(exc)
    return status


def normalize_install_version(version: str | None) -> str:
    version = (version or "").strip()
    return version or OPENCODE_INSTALL_VERSION


def upstream_target() -> tuple[str, int, str]:
    parsed = urlparse(UPSTREAM_URL)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 4096
    scheme = parsed.scheme or "http"
    return host, port, scheme


def upstream_is_local() -> bool:
    host, _, scheme = upstream_target()
    return scheme in {"http", "https"} and host in {"127.0.0.1", "localhost", "::1"}


def upstream_is_default_local_server() -> bool:
    host, port, _ = upstream_target()
    return host in {"127.0.0.1", "localhost", "::1"} and port == 4096


async def probe_url(url: str) -> bool:
    try:
        auth = (UPSTREAM_USERNAME, UPSTREAM_PASSWORD) if UPSTREAM_PASSWORD else None
        async with httpx.AsyncClient(timeout=2.0, auth=auth) as client:
            response = await client.get(url)
        return response.status_code < 500
    except Exception:
        return False


async def install_opencode(version: str | None = None) -> dict[str, Any]:
    requested_version = normalize_install_version(version)
    if opencode_binary_path():
        return {
            "ok": True,
            "already_installed": True,
            "opencode": await probe_opencode(),
        }

    async with OPENCODE_INSTALL_LOCK:
        existing = opencode_binary_path()
        if existing:
            return {
                "ok": True,
                "already_installed": True,
                "opencode": await probe_opencode(),
            }

        env = os.environ.copy()
        if requested_version:
            env["VERSION"] = requested_version

        command = f"curl -fsSL {shlex.quote(OPENCODE_INSTALL_URL)} | bash"
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await proc.communicate()
        output = {
            "stdout": stdout.decode(errors="replace").strip(),
            "stderr": stderr.decode(errors="replace").strip(),
            "returncode": proc.returncode,
        }
        if proc.returncode != 0:
            raise HTTPException(
                status_code=500,
                detail={
                    "error": "OpenCode installation failed.",
                    "command": command,
                    "version": requested_version or None,
                    "output": output,
                },
            )

        binary = opencode_binary_path()
        if not binary:
            for candidate in (
                Path.home() / ".opencode" / "bin",
                Path.home() / ".local" / "bin",
                Path.home() / "bin",
            ):
                candidate_binary = candidate / "opencode"
                if candidate_binary.exists():
                    os.environ["PATH"] = f"{candidate}:{os.environ.get('PATH', '')}"
                    binary = str(candidate_binary)
                    break

        if not binary:
            raise HTTPException(
                status_code=500,
                detail={
                    "error": "OpenCode installed, but the binary is still not visible on PATH.",
                    "command": command,
                    "version": requested_version or None,
                    "output": output,
                },
            )

        return {
            "ok": True,
            "already_installed": False,
            "command": command,
            "version": requested_version or None,
            "output": output,
            "opencode": await probe_opencode(),
        }


async def start_opencode_server() -> dict[str, Any]:
    if not upstream_is_local():
        raise HTTPException(
            status_code=400,
            detail={
                "error": "OpenCode server auto-start is only enabled for local upstream URLs.",
                "upstream_url": UPSTREAM_URL,
            },
        )

    if not upstream_is_default_local_server():
        raise HTTPException(
            status_code=400,
            detail={
                "error": "Auto-start only supports the default local OpenCode port 4096.",
                "upstream_url": UPSTREAM_URL,
            },
        )

    host, port, _ = upstream_target()
    status_url = f"{UPSTREAM_URL}/doc"

    if await probe_url(status_url):
        return {
            "ok": True,
            "already_running": True,
            "upstream_url": UPSTREAM_URL,
            "status_url": status_url,
            "opencode": await probe_opencode(),
        }

    async with OPENCODE_SERVER_LOCK:
        if await probe_url(status_url):
            return {
                "ok": True,
                "already_running": True,
                "upstream_url": UPSTREAM_URL,
                "status_url": status_url,
                "opencode": await probe_opencode(),
            }

        install_result = await install_opencode()
        binary = install_result.get("opencode", {}).get("binary") or opencode_binary_path()
        if not binary:
            raise HTTPException(
                status_code=500,
                detail={
                    "error": "OpenCode binary is not available after installation.",
                    "install": install_result,
                },
            )

        log_dir = Path.home() / ".cache" / "opencode-web"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "opencode-server.log"
        log_handle = log_file.open("ab")
        try:
            proc = subprocess.Popen(
                [binary],
                cwd=str(Path.cwd()),
                env={
                    **os.environ,
                    "OPENCODE_CONFIG": str(Path.cwd() / "opencode.json"),
                    "OPENCODE_CONFIG_DIR": str(Path.cwd() / ".opencode"),
                },
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except Exception:
            log_handle.close()
            raise

        log_handle.close()

        deadline = asyncio.get_running_loop().time() + SERVER_START_TIMEOUT
        while asyncio.get_running_loop().time() < deadline:
            if await probe_url(status_url):
                return {
                    "ok": True,
                    "already_running": False,
                    "started": True,
                    "pid": proc.pid,
                    "upstream_url": UPSTREAM_URL,
                    "status_url": status_url,
                    "host": host,
                    "port": port,
                    "log_file": str(log_file),
                    "install": install_result,
                    "opencode": await probe_opencode(),
                }
            if proc.poll() is not None:
                break
            await asyncio.sleep(0.5)

        if proc.poll() is not None:
            raise HTTPException(
                status_code=500,
                detail={
                    "error": "OpenCode server exited before becoming ready.",
                    "pid": proc.pid,
                    "returncode": proc.returncode,
                    "host": host,
                    "port": port,
                    "log_file": str(log_file),
                    "install": install_result,
                },
            )

        raise HTTPException(
            status_code=500,
            detail={
                "error": "OpenCode server did not become ready in time.",
                "pid": proc.pid,
                "host": host,
                "port": port,
                "log_file": str(log_file),
                "install": install_result,
            },
        )


async def upstream_request(
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    body: Any = None,
    stream: bool = False,
) -> Response:
    url = f"{UPSTREAM_URL}/{path.lstrip('/')}"
    headers = upstream_auth_headers()

    if stream:
        client = httpx.AsyncClient(timeout=None)
        upstream = await client.stream(method, url, params=params, json=body, headers=headers).__aenter__()
        if upstream.status_code >= 400:
            raw = await upstream.aread()
            await upstream.aclose()
            await client.aclose()
            return JSONResponse(
                status_code=upstream.status_code,
                content={
                    "error": "Upstream OpenCode event stream failed.",
                    "status_code": upstream.status_code,
                    "body": raw.decode(errors="replace"),
                    "upstream_url": url,
                },
            )

        async def iterator():
            try:
                async for chunk in upstream.aiter_raw():
                    yield chunk
            finally:
                await upstream.aclose()
                await client.aclose()

        return StreamingResponse(
            iterator(),
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type", "text/event-stream"),
            headers=filtered_response_headers(upstream.headers),
        )

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        upstream = await client.request(method, url, params=params, json=body, headers=headers)
    if upstream.status_code >= 400:
        try:
            detail = upstream.json()
        except Exception:
            detail = {"error": upstream.text}
        return JSONResponse(
            status_code=upstream.status_code,
            content={
                "error": "Upstream OpenCode request failed.",
                "detail": detail,
                "status_code": upstream.status_code,
                "upstream_url": url,
            },
        )
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type", "application/json"),
        headers=filtered_response_headers(upstream.headers),
    )


async def upstream_json(path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
    response = await upstream_request("GET", path, params=params)
    if isinstance(response, JSONResponse):
        raise HTTPException(status_code=response.status_code, detail=response.body.decode())
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=response.body.decode())
    try:
        return json.loads(response.body.decode())
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Unable to decode upstream JSON from {path}") from exc


async def safe_json(path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        data = await upstream_json(path, params=params)
        return {"ok": True, "data": data}
    except HTTPException as exc:
        return {"ok": False, "error": exc.detail, "status_code": exc.status_code}
    except Exception as exc:  # pragma: no cover - defensive for UI only
        return {"ok": False, "error": str(exc), "status_code": 500}


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "upstream_url": UPSTREAM_URL,
        "ollama_url": OLLAMA_URL,
        "model": DEFAULT_MODEL,
        "opencode_server": {
            "configured": UPSTREAM_URL,
            "local": upstream_is_local(),
            "reachable": await probe_url(f"{UPSTREAM_URL}/doc"),
        },
        "opencode": {
            "installed": bool(opencode_binary_path()),
            "binary": opencode_binary_path(),
        },
    }


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return HTML_TEMPLATE


@app.get("/api/bootstrap")
async def bootstrap() -> dict[str, Any]:
    paths = {
        "app": "/app",
        "config": "/config",
        "providers": "/config/providers",
        "sessions": "/session",
        "agents": "/agent",
        "commands": "/command",
        "files": "/file/status",
        "lsp": "/lsp",
        "formatters": "/formatter",
        "mcp": "/mcp",
        "tool_ids": "/experimental/tool/ids",
        "doc": "/doc",
    }
    results = await asyncio.gather(*(safe_json(path) for path in paths.values()))
    return {
        "upstream": {
            "url": UPSTREAM_URL,
            "doc_url": f"{UPSTREAM_URL}/doc",
            "auth_enabled": bool(UPSTREAM_PASSWORD),
        },
        "ollama": {
            "url": OLLAMA_URL,
            "model": DEFAULT_MODEL,
        },
        "opencode": await probe_opencode(),
        "server": {
            "configured_url": UPSTREAM_URL,
            "local": upstream_is_local(),
            "reachable": await probe_url(f"{UPSTREAM_URL}/doc"),
        },
        "endpoints": dict(zip(paths.keys(), results, strict=True)),
        "event_types": EVENT_TYPES,
    }


@app.get("/api/opencode/status")
async def opencode_status() -> dict[str, Any]:
    return await probe_opencode()


@app.post("/api/opencode/install")
async def opencode_install(version: str | None = Query(default=None, min_length=1)) -> dict[str, Any]:
    return await install_opencode(version)


@app.post("/api/opencode/server/start")
async def opencode_server_start() -> dict[str, Any]:
    return await start_opencode_server()


@app.get("/api/session-pack/{session_id}")
async def session_pack(session_id: str) -> dict[str, Any]:
    paths = {
        "session": f"/session/{session_id}",
        "messages": f"/session/{session_id}/message",
        "children": f"/session/{session_id}/children",
    }
    results = await asyncio.gather(*(safe_json(path) for path in paths.values()))
    payload = dict(zip(paths.keys(), results, strict=True))
    if payload["messages"].get("ok"):
        first_message = payload["messages"]["data"][0]["info"]["id"] if payload["messages"]["data"] else None
        if first_message:
            payload["message_detail"] = await safe_json(f"/session/{session_id}/message/{first_message}")
        else:
            payload["message_detail"] = {"ok": True, "data": None}
    else:
        payload["message_detail"] = {"ok": False, "error": "Unable to load messages."}
    return payload


@app.get("/api/ollama/models")
async def ollama_models() -> dict[str, Any]:
    urls = [f"{OLLAMA_URL}/api/tags", f"{OLLAMA_URL}/v1/models"]
    results: list[dict[str, Any]] = []
    for url in urls:
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.get(url)
            if response.status_code < 400:
                results.append({"ok": True, "url": url, "data": response.json()})
            else:
                results.append({"ok": False, "url": url, "error": response.text, "status_code": response.status_code})
        except Exception as exc:  # pragma: no cover - local network variability
            results.append({"ok": False, "url": url, "error": str(exc), "status_code": 500})

    models: list[dict[str, Any]] = []
    if results and results[0].get("ok"):
        for entry in results[0]["data"].get("models", []):
            name = entry.get("name") or entry.get("model")
            models.append(
                {
                    "id": name,
                    "name": name,
                    "size": entry.get("size"),
                    "modified_at": entry.get("modified_at"),
                    "provider_ref": f"ollama/{name}",
                }
            )
    elif len(results) > 1 and results[1].get("ok"):
        for entry in results[1]["data"].get("data", []):
            model_id = entry.get("id")
            models.append(
                {
                    "id": model_id,
                    "name": model_id,
                    "provider_ref": f"ollama/{model_id}",
                }
            )

    return {
        "ollama_url": OLLAMA_URL,
        "models": models,
        "sources": results,
        "default_model": DEFAULT_MODEL,
    }


@app.get("/api/ollama/snippet")
async def ollama_snippet(model: str = Query(..., min_length=1)) -> dict[str, Any]:
    provider_model = f"ollama/{model}"
    snippet = {
        "$schema": "https://opencode.ai/config.json",
        "model": provider_model,
        "provider": {
            "ollama": {
                "baseURL": f"{OLLAMA_URL}/v1",
                "models": {
                    model: {
                        "name": model,
                    }
                },
            }
        },
    }
    return {
        "provider_model": provider_model,
        "snippet": snippet,
        "markdown": f"Use `model: \"{provider_model}\"` with Ollama at `{OLLAMA_URL}/v1`.",
    }


@app.api_route("/api/opencode/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def opencode_proxy(path: str, request: Request) -> Response:
    body: Any = None
    if request.method not in {"GET", "HEAD"}:
        raw = await request.body()
        if raw:
            try:
                body = json.loads(raw)
            except json.JSONDecodeError:
                body = raw.decode(errors="replace")

    if request.method == "GET" and path == "event":
        return await upstream_request("GET", path, params=dict(request.query_params), stream=True)

    response = await upstream_request(
        request.method,
        path,
        params=dict(request.query_params),
        body=body,
        stream=False,
    )
    return response


@app.get("/api/events")
async def events() -> Response:
    return await upstream_request("GET", "event", stream=True)


HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>OpenCode Lens</title>
  <style>
    :root {
      --bg: #08111f;
      --bg2: #0d1830;
      --panel: rgba(11, 21, 40, 0.86);
      --panel-border: rgba(148, 163, 184, 0.16);
      --text: #e5eefb;
      --muted: #94a3b8;
      --accent: #68e0cf;
      --accent2: #f5b971;
      --danger: #ff7b7b;
      --good: #63e6be;
      --shadow: 0 28px 80px rgba(0, 0, 0, 0.35);
      --radius: 20px;
      --radius-sm: 14px;
      --space: 16px;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: var(--text);
      background:
        radial-gradient(circle at top left, rgba(104, 224, 207, 0.12), transparent 34%),
        radial-gradient(circle at top right, rgba(245, 185, 113, 0.12), transparent 24%),
        linear-gradient(180deg, var(--bg), #050b14 80%);
      min-height: 100vh;
    }
    a { color: var(--accent); text-decoration: none; }
    button, input, textarea, select {
      font: inherit;
      color: inherit;
    }
    .shell {
      max-width: 1600px;
      margin: 0 auto;
      padding: 14px;
    }
    .header {
      position: sticky;
      top: 0;
      z-index: 10;
      backdrop-filter: blur(18px);
      background: linear-gradient(180deg, rgba(5, 11, 20, 0.92), rgba(5, 11, 20, 0.72));
      border: 1px solid rgba(255,255,255,0.06);
      border-radius: 22px;
      padding: 14px 16px;
      box-shadow: var(--shadow);
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 10px;
      justify-content: space-between;
    }
    .brand { display: flex; align-items: center; gap: 12px; min-width: 0; }
    .mark {
      width: 42px; height: 42px; border-radius: 14px;
      background: linear-gradient(135deg, var(--accent), var(--accent2));
      box-shadow: 0 10px 30px rgba(104,224,207,0.2);
      flex: 0 0 auto;
    }
    .title-wrap h1 { margin: 0; font-size: 1.03rem; letter-spacing: 0.02em; }
    .title-wrap p { margin: 0; color: var(--muted); font-size: 0.85rem; }
    .chips { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
    .chip {
      border: 1px solid var(--panel-border);
      background: rgba(255,255,255,0.04);
      border-radius: 999px;
      padding: 7px 11px;
      font-size: 0.82rem;
      color: var(--muted);
      white-space: nowrap;
    }
    .chip.good { color: var(--good); border-color: rgba(99, 230, 190, 0.28); }
    .chip.warn { color: var(--accent2); border-color: rgba(245, 185, 113, 0.28); }
    .grid {
      display: grid;
      grid-template-columns: 360px minmax(0, 1fr);
      gap: 14px;
      margin-top: 14px;
    }
    .stack { display: grid; gap: 14px; }
    .panel {
      background: var(--panel);
      border: 1px solid var(--panel-border);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
      overflow: hidden;
    }
    .panel .pad { padding: 16px; }
    .panel h2, .panel h3 {
      margin: 0 0 10px;
      font-size: 0.98rem;
    }
    .panel .sub {
      margin: -2px 0 12px;
      color: var(--muted);
      font-size: 0.86rem;
      line-height: 1.45;
    }
    .section-title {
      display: flex; align-items: center; justify-content: space-between; gap: 10px;
      margin-bottom: 12px;
    }
    .section-title small { color: var(--muted); }
    .list { display: grid; gap: 10px; }
    .session-card, .event-card, .model-card, .file-card, .tool-card, .command-card, .status-card {
      border: 1px solid rgba(255,255,255,0.08);
      background: rgba(255,255,255,0.03);
      border-radius: 16px;
      padding: 12px;
    }
    .session-card.active { border-color: rgba(104,224,207,0.5); box-shadow: 0 0 0 1px rgba(104,224,207,0.12) inset; }
    .session-card button, .model-card button, .event-card button {
      width: 100%; border: 0; background: transparent; text-align: left; padding: 0; cursor: pointer;
    }
    .session-card h4, .model-card h4, .event-card h4 { margin: 0 0 5px; font-size: 0.94rem; }
    .session-card p, .model-card p, .event-card p, .file-card p, .tool-card p, .command-card p {
      margin: 0; color: var(--muted); font-size: 0.82rem; line-height: 1.45;
      word-break: break-word;
    }
    .meta {
      display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px;
      color: var(--muted); font-size: 0.75rem;
    }
    .badge {
      border-radius: 999px;
      padding: 4px 8px;
      background: rgba(255,255,255,0.05);
      border: 1px solid rgba(255,255,255,0.08);
    }
    .badge.accent { color: var(--accent); border-color: rgba(104,224,207,0.2); }
    .badge.warn { color: var(--accent2); border-color: rgba(245,185,113,0.2); }
    .badge.good { color: var(--good); border-color: rgba(99,230,190,0.2); }
    .badge.danger { color: var(--danger); border-color: rgba(255,123,123,0.2); }
    .controls {
      display: grid; gap: 10px;
    }
    .row {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
    }
    .row.three { grid-template-columns: repeat(3, minmax(0, 1fr)); }
    label { display: grid; gap: 6px; font-size: 0.82rem; color: var(--muted); }
    input, textarea, select {
      width: 100%; border: 1px solid rgba(255,255,255,0.1); border-radius: 14px;
      background: rgba(0,0,0,0.22); padding: 11px 12px; outline: none;
    }
    textarea { min-height: 100px; resize: vertical; }
    input:focus, textarea:focus, select:focus {
      border-color: rgba(104,224,207,0.42);
      box-shadow: 0 0 0 3px rgba(104,224,207,0.12);
    }
    .btnrow { display: flex; flex-wrap: wrap; gap: 8px; }
    .btn {
      border: 1px solid rgba(255,255,255,0.1);
      border-radius: 999px;
      padding: 10px 13px;
      cursor: pointer;
      background: rgba(255,255,255,0.04);
      color: var(--text);
      transition: transform 0.12s ease, border-color 0.12s ease, background 0.12s ease;
    }
    .btn:hover { transform: translateY(-1px); border-color: rgba(104,224,207,0.35); }
    .btn.primary { background: linear-gradient(135deg, rgba(104,224,207,0.18), rgba(245,185,113,0.14)); }
    .btn.ghost { color: var(--muted); }
    .btn.danger { color: #ffd0d0; border-color: rgba(255,123,123,0.28); }
    .toolbar {
      display: flex; flex-wrap: wrap; gap: 10px; margin-top: 10px;
    }
    .toolbar .pill {
      padding: 8px 12px; border-radius: 999px; border: 1px solid rgba(255,255,255,0.08);
      background: rgba(255,255,255,0.04); color: var(--muted); font-size: 0.82rem;
    }
    .code {
      white-space: pre-wrap; word-break: break-word; overflow: auto;
      background: rgba(0,0,0,0.32); border: 1px solid rgba(255,255,255,0.08);
      border-radius: 16px; padding: 14px; font-size: 0.82rem; line-height: 1.55;
      color: #dce7f5;
    }
    .split {
      display: grid;
      grid-template-columns: 1.3fr 0.9fr;
      gap: 14px;
    }
    .muted { color: var(--muted); }
    .messages { display: grid; gap: 12px; }
    .message {
      border-radius: 18px;
      border: 1px solid rgba(255,255,255,0.08);
      background: rgba(255,255,255,0.03);
      padding: 14px;
    }
    .message .head {
      display: flex; justify-content: space-between; gap: 10px; flex-wrap: wrap;
      margin-bottom: 8px;
    }
    .message .role { font-weight: 600; font-size: 0.9rem; }
    .message .parts { display: grid; gap: 10px; }
    .part {
      border-left: 3px solid rgba(104,224,207,0.32);
      padding-left: 10px;
    }
    .part[data-kind="tool"] { border-left-color: rgba(245,185,113,0.5); }
    .part[data-kind="error"] { border-left-color: rgba(255,123,123,0.5); }
    .part .kind { text-transform: uppercase; font-size: 0.7rem; letter-spacing: 0.12em; color: var(--muted); margin-bottom: 4px; }
    .event-list { display: grid; gap: 10px; max-height: 420px; overflow: auto; }
    .events-live { min-height: 180px; }
    .footer-note { color: var(--muted); font-size: 0.82rem; line-height: 1.5; }
    .scroll-x { overflow-x: auto; }
    @media (max-width: 1080px) {
      .grid, .split { grid-template-columns: 1fr; }
    }
    @media (max-width: 720px) {
      .shell { padding: 10px; }
      .header { border-radius: 18px; }
      .panel .pad { padding: 14px; }
      .row, .row.three { grid-template-columns: 1fr; }
      .toolbar { gap: 8px; }
      .btn { width: 100%; }
    }
  </style>
</head>
<body>
  <div class="shell">
    <header class="header">
      <div class="brand">
        <div class="mark"></div>
        <div class="title-wrap">
          <h1>OpenCode Lens</h1>
          <p>Mobile-friendly dashboard for the OpenCode server, event stream, and local Ollama model selection.</p>
        </div>
      </div>
      <div class="chips">
        <span class="chip good" id="chip-connection">connecting</span>
        <span class="chip" id="chip-upstream">upstream</span>
        <span class="chip" id="chip-ollama">ollama</span>
        <span class="chip" id="chip-opencode">opencode</span>
        <span class="chip" id="chip-opencode-server">server</span>
        <button class="btn ghost" id="refresh-all">Refresh</button>
      </div>
    </header>

    <div class="grid">
      <aside class="stack">
        <section class="panel">
          <div class="pad">
            <div class="section-title">
              <h2>Connection</h2>
              <small>Bridge settings</small>
            </div>
            <div id="connection-summary" class="status-card code">Loading...</div>
            <div id="opencode-status-note" class="footer-note" style="margin-top:10px;">Checking whether OpenCode is installed...</div>
            <div id="server-status-note" class="footer-note" style="margin-top:6px;">Checking whether the OpenCode server is reachable...</div>
            <div class="toolbar" style="margin-top:12px;">
              <a class="btn" href="/api/bootstrap" target="_blank" rel="noreferrer">Bootstrap JSON</a>
              <a class="btn" href="/api/opencode/doc" target="_blank" rel="noreferrer">Upstream Doc</a>
              <button class="btn primary" id="install-opencode" type="button">Install OpenCode</button>
              <button class="btn primary" id="start-opencode-server" type="button">Start OpenCode Server</button>
            </div>
          </div>
        </section>

        <section class="panel">
          <div class="pad">
            <div class="section-title">
              <h2>Sessions</h2>
              <small id="session-count">0</small>
            </div>
            <div class="controls">
              <label>Filter sessions
                <input id="session-filter" type="search" placeholder="Filter by title or id" />
              </label>
              <div id="session-list" class="list"></div>
            </div>
          </div>
        </section>

        <section class="panel">
          <div class="pad">
            <div class="section-title">
              <h2>New Session</h2>
              <small>Start work fast</small>
            </div>
            <form id="new-session-form" class="controls">
              <div class="row">
                <label>Title
                  <input name="title" placeholder="Investigate payment bug" />
                </label>
                <label>Parent ID
                  <input name="parentID" placeholder="Optional parent session" />
                </label>
              </div>
              <button class="btn primary" type="submit">Create session</button>
            </form>
          </div>
        </section>

        <section class="panel">
          <div class="pad">
            <div class="section-title">
              <h2>Ollama</h2>
              <small>Easy model chooser</small>
            </div>
            <div class="controls">
              <label>Filter local models
                <input id="model-filter" type="search" placeholder="llama, qwen, coder..." />
              </label>
              <div id="model-list" class="list"></div>
              <label>Selected OpenCode model
                <input id="selected-model" placeholder="ollama/llama3.1" />
              </label>
              <div class="btnrow">
                <button class="btn primary" id="copy-model-snippet" type="button">Copy config snippet</button>
                <button class="btn" id="copy-model-ref" type="button">Copy model ref</button>
              </div>
              <div class="code" id="model-snippet">Choose a local Ollama model above.</div>
            </div>
          </div>
        </section>
      </aside>

      <main class="stack">
        <section class="panel">
          <div class="pad">
            <div class="section-title">
              <h2>Active Session</h2>
              <small id="active-session-label">none selected</small>
            </div>
            <div id="active-session-summary" class="code">Pick a session on the left.</div>
            <div class="toolbar">
              <button class="btn" id="btn-refresh-session" type="button">Refresh session</button>
              <button class="btn" id="btn-abort" type="button">Abort</button>
              <button class="btn" id="btn-share" type="button">Share</button>
              <button class="btn" id="btn-unshare" type="button">Unshare</button>
              <button class="btn" id="btn-summarize" type="button">Summarize</button>
              <button class="btn danger" id="btn-delete" type="button">Delete</button>
            </div>
          </div>
        </section>

        <section class="panel">
          <div class="pad">
            <div class="section-title">
              <h2>Send Prompt</h2>
              <small>To the selected session</small>
            </div>
            <form id="prompt-form" class="controls">
              <label>Prompt
                <textarea name="prompt" placeholder="Ask OpenCode to inspect, modify, or explain the codebase."></textarea>
              </label>
              <div class="row three">
                <label>Agent
                  <input name="agent" placeholder="Optional agent id" />
                </label>
                <label>Model
                  <input name="model" id="prompt-model" placeholder="ollama/llama3.1" />
                </label>
                <label>No reply
                  <select name="noReply">
                    <option value="false">false</option>
                    <option value="true">true</option>
                  </select>
                </label>
              </div>
              <div class="btnrow">
                <button class="btn primary" type="submit">Send message</button>
                <button class="btn" type="button" id="send-shell-sample">Insert shell sample</button>
              </div>
            </form>
          </div>
        </section>

        <div class="split">
          <section class="panel">
            <div class="pad">
              <div class="section-title">
                <h2>Timeline</h2>
                <small>Messages and parts</small>
              </div>
              <div id="message-list" class="messages">No session selected.</div>
            </div>
          </section>

          <section class="panel">
            <div class="pad">
              <div class="section-title">
                <h2>Live Events</h2>
                <small>Streaming bus feed</small>
              </div>
              <div class="event-list events-live" id="live-events"></div>
            </div>
          </section>
        </div>

        <div class="split">
          <section class="panel">
            <div class="pad">
              <div class="section-title">
                <h2>Files</h2>
                <small>Search and status</small>
              </div>
              <div class="controls">
                <div class="row">
                  <button class="btn" id="refresh-files" type="button">Refresh status</button>
                  <input id="file-query" placeholder="Search file or symbol" />
                </div>
                <div id="file-status" class="list"></div>
                <div class="btnrow">
                  <button class="btn" id="search-file" type="button">Find files</button>
                  <button class="btn" id="search-text" type="button">Find text</button>
                  <button class="btn" id="search-symbol" type="button">Find symbols</button>
                </div>
                <div id="file-search-results" class="code">No file search yet.</div>
              </div>
            </div>
          </section>

          <section class="panel">
            <div class="pad">
              <div class="section-title">
                <h2>Tools and Controls</h2>
                <small>OpenCode surface</small>
              </div>
              <div class="stack">
                <div class="status-card">
                  <h3>Agents / Commands / Tools</h3>
                  <div class="toolbar">
                    <button class="btn" id="load-agents" type="button">Reload agents</button>
                    <button class="btn" id="load-commands" type="button">Reload commands</button>
                    <button class="btn" id="load-tools" type="button">Reload tools</button>
                    <button class="btn" id="load-mcp" type="button">Reload MCP</button>
                  </div>
                  <div id="agents-list" class="list" style="margin-top:12px;"></div>
                  <div id="commands-list" class="list" style="margin-top:12px;"></div>
                  <div id="tools-list" class="list" style="margin-top:12px;"></div>
                  <div id="mcp-list" class="list" style="margin-top:12px;"></div>
                </div>
                <div class="status-card">
                  <h3>TUI actions</h3>
                  <div class="btnrow">
                    <button class="btn" data-tui="open-sessions" type="button">Open sessions</button>
                    <button class="btn" data-tui="open-models" type="button">Open models</button>
                    <button class="btn" data-tui="open-themes" type="button">Open themes</button>
                    <button class="btn" data-tui="open-help" type="button">Open help</button>
                    <button class="btn" data-tui="clear-prompt" type="button">Clear prompt</button>
                  </div>
                </div>
                <div class="status-card">
                  <h3>Raw API</h3>
                  <div class="footer-note">
                    The proxy exposes every upstream OpenCode endpoint under <code>/api/opencode/*</code>. The UI uses that proxy so you can inspect the entire agentic workflow without dealing with CORS or browser auth.
                  </div>
                </div>
              </div>
            </div>
          </section>
        </div>
      </main>
    </div>
  </div>

  <script>
    const state = {
      bootstrap: null,
      sessions: [],
      sessionPack: null,
      selectedSessionId: null,
      ollama: [],
      selectedModelRef: "",
      events: [],
      eventSource: null,
    };

    const $ = (id) => document.getElementById(id);

    function pretty(value) {
      if (value === null || value === undefined) return "";
      if (typeof value === "string") return value;
      try { return JSON.stringify(value, null, 2); } catch (e) { return String(value); }
    }

    function safeText(value) {
      return String(value ?? "");
    }

    async function api(path, options = {}) {
      const response = await fetch(path, {
        headers: { "Content-Type": "application/json", ...(options.headers || {}) },
        ...options,
      });
      const contentType = response.headers.get("content-type") || "";
      let data;
      if (contentType.includes("application/json")) {
        data = await response.json();
      } else {
        data = await response.text();
      }
      if (!response.ok) {
        throw new Error(typeof data === "string" ? data : JSON.stringify(data));
      }
      return data;
    }

    function setConnectionStatus(ok, text) {
      const el = $("chip-connection");
      el.textContent = text;
      el.className = ok ? "chip good" : "chip warn";
    }

    function renderConnection() {
      const bootstrap = state.bootstrap || {};
      const upstream = bootstrap.upstream || {};
      const ollama = bootstrap.ollama || {};
      const opencode = bootstrap.opencode || {};
      const server = bootstrap.server || {};
      $("chip-upstream").textContent = upstream.url || "upstream unknown";
      $("chip-ollama").textContent = ollama.url || "ollama unknown";
      $("chip-opencode").textContent = opencode.installed
        ? `opencode ${opencode.version || "installed"}`
        : "opencode missing";
      $("chip-opencode").className = opencode.installed ? "chip good" : "chip warn";
      $("chip-opencode-server").textContent = server.reachable
        ? "server live"
        : server.local
          ? "server down"
          : "server remote";
      $("chip-opencode-server").className = server.reachable ? "chip good" : server.local ? "chip warn" : "chip";
      $("opencode-status-note").textContent = opencode.installed
        ? `OpenCode is available${opencode.binary ? ` at ${opencode.binary}` : ""}.`
        : `OpenCode is not installed${opencode.binary_hint ? `; a likely location is ${opencode.binary_hint}` : ""}. Use Install OpenCode to fetch it.`;
      $("server-status-note").textContent = server.reachable
        ? `OpenCode server is reachable at ${server.configured_url || upstream.url || "the configured URL"}.`
        : server.local
          ? `OpenCode server is not reachable at ${server.configured_url || upstream.url || "the configured URL"}. Use Start OpenCode Server to launch it locally.`
          : `OpenCode server is configured remotely at ${server.configured_url || upstream.url || "the configured URL"}.`;
      $("connection-summary").textContent = pretty({
        upstream: upstream,
        ollama: ollama,
        opencode: opencode,
        server: server,
        last_model: state.selectedModelRef || ollama.model || "",
      });
    }

    function renderSessions() {
      const list = $("session-list");
      const filter = ($("session-filter").value || "").toLowerCase().trim();
      const sessions = (state.sessions || []).filter((session) => {
        const title = safeText(session.title || session.name || session.id).toLowerCase();
        const id = safeText(session.id).toLowerCase();
        return !filter || title.includes(filter) || id.includes(filter);
      });
      $("session-count").textContent = `${sessions.length}/${(state.sessions || []).length}`;
      if (!sessions.length) {
        list.innerHTML = '<div class="muted">No sessions found.</div>';
        return;
      }
      list.innerHTML = sessions.map((session) => {
        const active = session.id === state.selectedSessionId ? "active" : "";
        const title = session.title || session.name || session.id;
        const status = session.status || session.state || session.phase || "session";
        const updated = session.updatedAt || session.updated_at || session.modifiedAt || session.modified_at || "";
        return `
          <div class="session-card ${active}">
            <button data-session-id="${safeText(session.id)}">
              <h4>${safeText(title)}</h4>
              <p>${safeText(session.id)}</p>
              <div class="meta">
                <span class="badge accent">${safeText(status)}</span>
                ${updated ? `<span class="badge">${safeText(updated)}</span>` : ""}
              </div>
            </button>
          </div>
        `;
      }).join("");
      list.querySelectorAll("button[data-session-id]").forEach((button) => {
        button.addEventListener("click", () => selectSession(button.dataset.sessionId));
      });
    }

    function renderSessionPack() {
      const pack = state.sessionPack;
      const container = $("active-session-summary");
      const detail = $("message-list");
      if (!pack) {
        container.textContent = "Pick a session on the left.";
        detail.textContent = "No session selected.";
        $("active-session-label").textContent = "none selected";
        return;
      }
      const session = pack.session?.data || pack.session || {};
      $("active-session-label").textContent = session.title || session.id || state.selectedSessionId || "selected session";
      container.textContent = pretty({
        session: session,
        children: pack.children?.data || [],
        message_detail: pack.message_detail?.data || null,
      });

      const messages = pack.messages?.data || [];
      if (!messages.length) {
        detail.innerHTML = '<div class="muted">This session has no messages yet.</div>';
      } else {
        detail.innerHTML = messages.map((entry) => {
          const info = entry.info || {};
          const parts = entry.parts || [];
          const role = info.role || info.type || "message";
          const title = info.title || info.name || info.id || role;
          const created = info.createdAt || info.created_at || info.timestamp || "";
          return `
            <article class="message">
              <div class="head">
                <div>
                  <div class="role">${safeText(title)}</div>
                  <div class="muted">${safeText(info.id || "")} ${created ? "• " + safeText(created) : ""}</div>
                </div>
                <div class="badge accent">${safeText(role)}</div>
              </div>
              <div class="parts">
                ${parts.map((part) => {
                  const kind = part.type || part.kind || "text";
                  const body = part.text || part.content || part.message || pretty(part);
                  return `
                    <div class="part" data-kind="${safeText(kind)}">
                      <div class="kind">${safeText(kind)}</div>
                      <div class="code">${escapeHtml(body)}</div>
                    </div>
                  `;
                }).join("")}
              </div>
            </article>
          `;
        }).join("");
      }

      const files = pack.session?.data?.files || pack.session?.data?.fileChanges || [];
      $("file-status").innerHTML = files.length
        ? files.map((file) => `<div class="file-card"><h4>${safeText(file.path || file.name || "file")}</h4><p>${escapeHtml(pretty(file))}</p></div>`).join("")
        : '<div class="muted">No file metadata returned for this session.</div>';
    }

    function renderEvents() {
      const el = $("live-events");
      if (!state.events.length) {
        el.innerHTML = '<div class="muted">Waiting for OpenCode events...</div>';
        return;
      }
      el.innerHTML = state.events.slice(-100).map((event) => `
        <article class="event-card">
          <button>
            <h4>${safeText(event.type)}</h4>
            <p>${escapeHtml(pretty(event.data))}</p>
            <div class="meta">
              <span class="badge">${safeText(event.at)}</span>
            </div>
          </button>
        </article>
      `).join("");
    }

    function renderModels() {
      const list = $("model-list");
      const filter = ($("model-filter").value || "").toLowerCase().trim();
      const models = (state.ollama || []).filter((model) => {
        const text = `${model.id} ${model.name} ${model.provider_ref}`.toLowerCase();
        return !filter || text.includes(filter);
      });
      if (!models.length) {
        list.innerHTML = '<div class="muted">No local Ollama models detected. Check the Ollama base URL or start Ollama.</div>';
        return;
      }
      list.innerHTML = models.map((model) => `
        <div class="model-card">
          <button data-model-ref="${safeText(model.provider_ref)}" data-model-id="${safeText(model.id)}">
            <h4>${safeText(model.name)}</h4>
            <p>${safeText(model.provider_ref)}</p>
            <div class="meta">
              ${model.size ? `<span class="badge">${safeText(model.size)}</span>` : ""}
              ${model.modified_at ? `<span class="badge">${safeText(model.modified_at)}</span>` : ""}
            </div>
          </button>
        </div>
      `).join("");
      list.querySelectorAll("button[data-model-ref]").forEach((button) => {
        button.addEventListener("click", async () => {
          const ref = button.dataset.modelRef;
          const modelId = button.dataset.modelId;
          state.selectedModelRef = ref;
          $("selected-model").value = ref;
          $("prompt-model").value = ref;
          try {
            const snippet = await api(`/api/ollama/snippet?model=${encodeURIComponent(modelId)}`);
            $("model-snippet").textContent = pretty(snippet.snippet);
          } catch (error) {
            $("model-snippet").textContent = `Unable to generate config snippet: ${error.message}`;
          }
        });
      });
    }

    function renderMiscLists(data) {
      $("agents-list").innerHTML = renderList("Agents", data.agents, (item) => item.name || item.id || item.title);
      $("commands-list").innerHTML = renderList("Commands", data.commands, (item) => item.name || item.id || item.command);
      $("tools-list").innerHTML = renderList("Tools", data.tool_ids, (item) => item.name || item.id || item);
      $("mcp-list").innerHTML = renderList("MCP", data.mcp, (item) => item.name || item.id || item);
      const files = data.files?.data || [];
      $("file-status").innerHTML = files.length
        ? files.map((file) => `<div class="file-card"><h4>${safeText(file.path || file.name || "file")}</h4><p>${escapeHtml(pretty(file))}</p></div>`).join("")
        : '<div class="muted">No tracked files returned yet.</div>';
    }

    function renderList(title, payload, labelFn) {
      const data = payload?.data || [];
      if (!data.length) return `<div class="muted">No ${title.toLowerCase()} returned.</div>`;
      return data.slice(0, 10).map((item) => `
        <div class="tool-card">
          <h4>${safeText(labelFn(item))}</h4>
          <p>${escapeHtml(pretty(item))}</p>
        </div>
      `).join("");
    }

    function escapeHtml(value) {
      return safeText(value)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;");
    }

    function pushEvent(type, data) {
      state.events.push({ type, data, at: new Date().toLocaleTimeString() });
      if (state.events.length > 200) state.events.splice(0, state.events.length - 200);
      renderEvents();
      if (type.startsWith("session.") || type.startsWith("message.") || type.startsWith("file.") || type.startsWith("permission.") || type.startsWith("tool.")) {
        refreshSelectedSession();
        refreshLists();
      }
    }

    async function selectSession(sessionId) {
      state.selectedSessionId = sessionId;
      $("active-session-label").textContent = sessionId;
      renderSessions();
      await refreshSelectedSession();
    }

    async function refreshSelectedSession() {
      if (!state.selectedSessionId) return;
      const pack = await api(`/api/session-pack/${encodeURIComponent(state.selectedSessionId)}`);
      state.sessionPack = pack;
      renderSessionPack();
    }

    async function refreshLists() {
      const bootstrap = state.bootstrap || await api("/api/bootstrap");
      const data = bootstrap.endpoints || {};
      const [sessions, agents, commands, files, mcp, toolIds] = [
        data.sessions,
        data.agents,
        data.commands,
        data.files,
        data.mcp,
        data.tool_ids,
      ];
      state.sessions = sessions?.data || [];
      renderSessions();
      renderMiscLists({ agents, commands, files, mcp, tool_ids: toolIds });
      if (!state.selectedSessionId && state.sessions.length) {
        await selectSession(state.sessions[0].id);
      }
    }

    async function refreshBootstrap() {
      const bootstrap = await api("/api/bootstrap");
      state.bootstrap = bootstrap;
      renderConnection();
      state.ollama = [];
      try {
        const models = await api("/api/ollama/models");
        state.ollama = models.models || [];
      } catch (error) {
        state.ollama = [];
        $("model-list").innerHTML = `<div class="muted">${escapeHtml(error.message)}</div>`;
      }
      renderModels();
      const defaultModel = bootstrap.ollama?.model || bootstrap.ollama?.default_model || DEFAULT_MODEL_VALUE;
      if (defaultModel) {
        $("selected-model").value = defaultModel;
        $("prompt-model").value = defaultModel;
        state.selectedModelRef = defaultModel;
      }
      const data = bootstrap.endpoints || {};
      const appInfo = data.app?.data || {};
      const configInfo = data.config?.data || {};
      const providersInfo = data.providers?.data || {};
      $("connection-summary").textContent = pretty({
        upstream: bootstrap.upstream,
        ollama: bootstrap.ollama,
        app: appInfo,
        config: configInfo,
        providers: providersInfo,
      });
      await refreshLists();
    }

    async function sendFormJson(url, payload) {
      return api(url, { method: "POST", body: JSON.stringify(payload) });
    }

    async function createSession(event) {
      event.preventDefault();
      const form = event.currentTarget;
      const payload = Object.fromEntries(new FormData(form).entries());
      Object.keys(payload).forEach((key) => { if (!payload[key]) delete payload[key]; });
      const response = await sendFormJson("/api/opencode/session", payload);
      form.reset();
      await refreshBootstrap();
      if (response?.id) await selectSession(response.id);
    }

    async function sendPrompt(event) {
      event.preventDefault();
      if (!state.selectedSessionId) return alert("Select a session first.");
      const form = event.currentTarget;
      const values = Object.fromEntries(new FormData(form).entries());
      if (!values.prompt.trim()) return;
      const payload = {
        parts: [{ type: "text", text: values.prompt }],
      };
      if (values.agent) payload.agent = values.agent;
      if (values.model) payload.model = values.model;
      if (values.noReply === "true") payload.noReply = true;
      await sendFormJson(`/api/opencode/session/${encodeURIComponent(state.selectedSessionId)}/message`, payload);
      form.reset();
      await refreshSelectedSession();
    }

    async function runShellSample() {
      const prompt = $("prompt-form").elements.prompt;
      prompt.value = prompt.value || "Inspect the current workspace and summarize the likely next coding step.";
    }

    async function runSessionAction(action, body = {}) {
      if (!state.selectedSessionId) return alert("Select a session first.");
      const method = action === "delete" ? "DELETE" : "POST";
      const url = `/api/opencode/session/${encodeURIComponent(state.selectedSessionId)}${action === "delete" ? "" : "/" + action}`;
      await api(url, { method, body: method === "DELETE" ? undefined : JSON.stringify(body) });
      await refreshBootstrap();
      await refreshSelectedSession();
    }

    async function searchFiles(mode) {
      const q = $("file-query").value.trim();
      if (!q) return;
      const endpoint = mode === "text" ? `/api/opencode/find?pattern=${encodeURIComponent(q)}`
        : mode === "symbol" ? `/api/opencode/find/symbol?query=${encodeURIComponent(q)}`
        : `/api/opencode/find/file?query=${encodeURIComponent(q)}`;
      const result = await api(endpoint);
      $("file-search-results").textContent = pretty(result);
    }

    async function refreshFiles() {
      const files = await api("/api/opencode/file/status");
      $("file-status").innerHTML = Array.isArray(files)
        ? files.map((file) => `<div class="file-card"><h4>${safeText(file.path || file.name || "file")}</h4><p>${escapeHtml(pretty(file))}</p></div>`).join("")
        : pretty(files);
    }

    async function loadInfoList(path, targetId, label) {
      const result = await api(`/api/opencode/${path}`);
      const data = Array.isArray(result) ? result : Object.values(result || {});
      $(targetId).innerHTML = data.length
        ? data.map((item) => `<div class="tool-card"><h4>${safeText(item.name || item.id || label)}</h4><p>${escapeHtml(pretty(item))}</p></div>`).join("")
        : `<div class="muted">No ${label} returned.</div>`;
    }

    async function dispatchTui(action) {
      await api(`/api/opencode/tui/${action}`, { method: "POST", body: "{}" });
    }

    async function copyText(text) {
      await navigator.clipboard.writeText(text);
    }

    async function installOpenCode() {
      const note = $("opencode-status-note");
      note.textContent = "Installing OpenCode...";
      try {
        const result = await api("/api/opencode/install", { method: "POST" });
        note.textContent = result.already_installed
          ? "OpenCode was already installed."
          : "OpenCode installation completed.";
        await refreshBootstrap();
      } catch (error) {
        note.textContent = `OpenCode installation failed: ${error.message}`;
        throw error;
      }
    }

    async function startOpenCodeServer() {
      const note = $("server-status-note");
      note.textContent = "Starting OpenCode server...";
      try {
        const result = await api("/api/opencode/server/start", { method: "POST" });
        note.textContent = result.already_running
          ? "OpenCode server was already running."
          : `OpenCode server started on ${result.upstream_url}.`;
        await refreshBootstrap();
      } catch (error) {
        note.textContent = `OpenCode server start failed: ${error.message}`;
        throw error;
      }
    }

    async function copyModelSnippet() {
      const modelRef = $("selected-model").value.trim();
      if (!modelRef.startsWith("ollama/")) return alert("Pick an Ollama model first.");
      const modelId = modelRef.replace(/^ollama\//, "");
      const payload = await api(`/api/ollama/snippet?model=${encodeURIComponent(modelId)}`);
      await copyText(JSON.stringify(payload.snippet, null, 2));
      $("model-snippet").textContent = pretty(payload.snippet);
    }

    async function refreshModelSnippet() {
      const modelRef = $("selected-model").value.trim();
      if (!modelRef.startsWith("ollama/")) {
        $("model-snippet").textContent = "Choose a local Ollama model above.";
        return;
      }
      const modelId = modelRef.replace(/^ollama\//, "");
      const payload = await api(`/api/ollama/snippet?model=${encodeURIComponent(modelId)}`);
      $("model-snippet").textContent = pretty(payload.snippet);
    }

    function connectEvents() {
      if (state.eventSource) state.eventSource.close();
      const source = new EventSource("/api/events");
      state.eventSource = source;
      setConnectionStatus(true, "connected");
      source.onmessage = (event) => {
        pushEvent("message", event.data);
      };
      EVENT_TYPES.forEach((type) => {
        source.addEventListener(type, (event) => {
          let data = event.data;
          try { data = JSON.parse(event.data); } catch (e) {}
          pushEvent(type, data);
        });
      });
      source.onerror = () => setConnectionStatus(false, "event stream error");
      source.addEventListener("server.connected", () => setConnectionStatus(true, "live"));
    }

    let DEFAULT_MODEL_VALUE = "";

    async function init() {
      try {
        await refreshBootstrap();
        DEFAULT_MODEL_VALUE = state.bootstrap?.ollama?.model || "";
        connectEvents();
        renderEvents();
      } catch (error) {
        setConnectionStatus(false, "offline");
        $("connection-summary").textContent = error.message;
      }
    }

    $("refresh-all").addEventListener("click", refreshBootstrap);
    $("refresh-files").addEventListener("click", refreshFiles);
    $("btn-refresh-session").addEventListener("click", refreshSelectedSession);
    $("btn-abort").addEventListener("click", () => runSessionAction("abort"));
    $("btn-share").addEventListener("click", () => runSessionAction("share"));
    $("btn-unshare").addEventListener("click", () => runSessionAction("unshare"));
    $("btn-summarize").addEventListener("click", () => runSessionAction("summarize"));
    $("btn-delete").addEventListener("click", () => runSessionAction("delete"));
    $("new-session-form").addEventListener("submit", createSession);
    $("prompt-form").addEventListener("submit", sendPrompt);
    $("send-shell-sample").addEventListener("click", runShellSample);
    $("session-filter").addEventListener("input", renderSessions);
    $("model-filter").addEventListener("input", renderModels);
    $("selected-model").addEventListener("change", refreshModelSnippet);
    $("copy-model-snippet").addEventListener("click", copyModelSnippet);
    $("copy-model-ref").addEventListener("click", async () => copyText($("selected-model").value.trim()));
    $("install-opencode").addEventListener("click", installOpenCode);
    $("start-opencode-server").addEventListener("click", startOpenCodeServer);
    $("search-file").addEventListener("click", () => searchFiles("file"));
    $("search-text").addEventListener("click", () => searchFiles("text"));
    $("search-symbol").addEventListener("click", () => searchFiles("symbol"));
    $("load-agents").addEventListener("click", () => loadInfoList("agent", "agents-list", "agents"));
    $("load-commands").addEventListener("click", () => loadInfoList("command", "commands-list", "commands"));
    $("load-tools").addEventListener("click", () => api("/api/opencode/experimental/tool/ids").then((data) => {
      $("tools-list").innerHTML = Array.isArray(data) ? data.map((item) => `<div class="tool-card"><h4>${safeText(item.name || item.id || item)}</h4><p>${escapeHtml(pretty(item))}</p></div>`).join("") : pretty(data);
    }));
    $("load-mcp").addEventListener("click", () => api("/api/opencode/mcp").then((data) => {
      $("mcp-list").innerHTML = Array.isArray(data) ? data.map((item) => `<div class="tool-card"><h4>${safeText(item.name || item.id || item)}</h4><p>${escapeHtml(pretty(item))}</p></div>`).join("") : pretty(data);
    }));
    document.querySelectorAll("[data-tui]").forEach((button) => button.addEventListener("click", () => dispatchTui(button.dataset.tui)));

    init();
  </script>
</body>
</html>
"""
