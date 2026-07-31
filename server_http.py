"""Open Brain remote MCP — minimal HTTP surface for public/claude.ai connectors.

Lockdown:
- bind 0.0.0.0:$PORT (Cloud Run) / 127.0.0.1:8090 (VM+Cloudflare Tunnel fallback)
- MCP only under /{OPEN_BRAIN_REMOTE_SECRET}/mcp  (URL path = credential)
- simple per-IP rate limit
- capture_thought + search_thoughts + run_command (allowlisted, second secret)
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import time
from collections import defaultdict, deque

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server as brain

from mcp.server.mcpserver import MCPServer
from starlette.requests import Request
from starlette.responses import JSONResponse

SECRET = os.environ["OPEN_BRAIN_REMOTE_SECRET"]
if len(SECRET) < 24:
    raise SystemExit("OPEN_BRAIN_REMOTE_SECRET too short (need >= 24 chars)")

# second, independent secret gating run_command specifically — a leaked URL
# path alone must not be enough to run commands against the GCP project.
COMMAND_SECRET = os.environ.get("OPEN_BRAIN_COMMAND_SECRET")
if COMMAND_SECRET and len(COMMAND_SECRET) < 24:
    raise SystemExit("OPEN_BRAIN_COMMAND_SECRET too short (need >= 24 chars)")

# requests per window per client IP (CF sets CF-Connecting-IP)
RATE_LIMIT = int(os.environ.get("OPEN_BRAIN_RATE_LIMIT", "60"))
RATE_WINDOW = int(os.environ.get("OPEN_BRAIN_RATE_WINDOW_SEC", "60"))

# Cloud Run injects $PORT; the VM/Cloudflare-Tunnel deployment keeps 8090.
PORT = int(os.environ.get("PORT", "8090"))
HOST = os.environ.get("OPEN_BRAIN_HTTP_HOST", "0.0.0.0" if "PORT" in os.environ else "127.0.0.1")

mcp = MCPServer("open-brain")
STREAMABLE_HTTP_PATH = f"/{SECRET}/mcp"

mcp.tool()(brain.capture_thought)
mcp.tool()(brain.search_thoughts)

# ---------------------------------------------------------------------------
# run_command: allowlisted CLI execution, NOT a raw shell.
#
# Every entry is a fixed subcommand *prefix* (as separate argv tokens). Args
# supplied beyond the prefix are validated one at a time against a strict
# no-shell-metacharacter pattern and passed straight into subprocess.run's
# argv list (shell=False) — nothing here is ever concatenated into a shell
# string, so there is no injection surface via quoting/metacharacters.
# Anything not matching an allowlist prefix is rejected before any process
# is spawned.
# ---------------------------------------------------------------------------
COMMAND_ALLOWLIST: dict[str, list[list[str]]] = {
    "gcloud": [
        ["run", "services", "describe"],
        ["run", "services", "list"],
        ["run", "revisions", "list"],
        ["sql", "instances", "describe"],
        ["logging", "read"],
    ],
    "git": [
        ["status"],
        ["log"],
        ["pull"],
    ],
}

_ARG_RE = re.compile(r"^[A-Za-z0-9._/=:@\-]+$")


def _allowlist_match(tool: str, args: list[str]) -> list[str] | None:
    for prefix in COMMAND_ALLOWLIST.get(tool, []):
        if args[: len(prefix)] == prefix:
            return prefix
    return None


def _audit(tool: str, args: list[str], allowed: bool, result: str) -> None:
    try:
        brain.capture_thought(
            content=f"run_command audit: tool={tool} args={json.dumps(args)} allowed={allowed}",
            metadata_json=json.dumps(
                {
                    "type": "command_audit",
                    "tool": tool,
                    "args": args,
                    "allowed": allowed,
                    "result": result[:2000],
                }
            ),
        )
    except Exception:
        pass  # audit logging must never block/crash the response path


@mcp.tool()
def run_command(tool: str, args: list[str], command_secret: str) -> dict:
    """
    Run an allowlisted CLI command (gcloud/git) against the deployed project.
    Requires OPEN_BRAIN_COMMAND_SECRET (separate from the connector URL secret).
    Only fixed, pre-approved subcommand prefixes are runnable — this is not a
    general shell. Every call (accepted or rejected) is audit-logged as a
    thought with metadata.type == "command_audit".
    """
    if not COMMAND_SECRET:
        return {"success": False, "error": "run_command disabled (no OPEN_BRAIN_COMMAND_SECRET set)"}
    if command_secret != COMMAND_SECRET:
        _audit(tool, args, False, "bad command_secret")
        return {"success": False, "error": "forbidden"}

    if tool not in COMMAND_ALLOWLIST:
        _audit(tool, args, False, "tool not allowlisted")
        return {"success": False, "error": f"tool '{tool}' not allowlisted"}

    if not all(isinstance(a, str) and _ARG_RE.match(a) for a in args):
        _audit(tool, args, False, "arg failed pattern check")
        return {"success": False, "error": "args must match ^[A-Za-z0-9._/=:@-]+$"}

    prefix = _allowlist_match(tool, args)
    if prefix is None:
        _audit(tool, args, False, "no matching allowlist prefix")
        return {
            "success": False,
            "error": f"'{' '.join(args)}' does not match any allowlisted {tool} subcommand",
        }

    argv = [tool] + args
    try:
        proc = subprocess.run(
            argv, shell=False, capture_output=True, text=True, timeout=30
        )
        out = proc.stdout + proc.stderr
        _audit(tool, args, True, out)
        return {
            "success": proc.returncode == 0,
            "returncode": proc.returncode,
            "output": out[:8000],
        }
    except subprocess.TimeoutExpired:
        _audit(tool, args, True, "timeout")
        return {"success": False, "error": "command timed out after 30s"}
    except Exception as e:
        _audit(tool, args, True, f"exception: {e}")
        return {"success": False, "error": str(e)}


class LockdownMiddleware:
    """Path lockdown + per-IP rate limiting, as *pure ASGI* middleware.

    Deliberately NOT starlette.middleware.base.BaseHTTPMiddleware.

    BaseHTTPMiddleware wraps the downstream app in a task whose response is
    consumed through an anyio stream, which means it does not forward the
    response start until the body begins flowing. For a long-lived streaming
    response that is correct-but-fatal: MCP's Streamable-HTTP transport opens
    a server->client SSE stream on GET /<secret>/mcp that stays open and sends
    nothing until there is a message to push. Under BaseHTTPMiddleware the
    client therefore receives *no response headers at all* and simply hangs
    (observed: curl reporting http=000 after a 15s timeout, and every MCP
    client stalling ~5.5s on connect before giving up on the GET stream).

    Reproduced against the Cloud Run URL directly, so it was never the
    Cloudflare Tunnel. POST was unaffected because in stateless_http mode it
    returns a single complete JSON body.

    Pure ASGI middleware passes `send` straight through, so response start is
    forwarded the moment the app emits it and SSE streams immediately.
    """

    def __init__(self, app):
        self.app = app
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._prefix = f"/{SECRET}"

    def _client_ip(self, request: Request) -> str:
        return (
            request.headers.get("cf-connecting-ip")
            or request.headers.get("x-real-ip")
            or (request.client.host if request.client else "unknown")
        )

    def _rate_ok(self, ip: str) -> bool:
        now = time.time()
        q = self._hits[ip]
        while q and now - q[0] > RATE_WINDOW:
            q.popleft()
        if len(q) >= RATE_LIMIT:
            return False
        q.append(now)
        return True

    async def __call__(self, scope, receive, send):
        # Let non-HTTP scopes (lifespan, websocket) through untouched.
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path") or "/"

        # only secret tree is reachable; everything else 404
        if path != "/" and not path.startswith(self._prefix):
            await JSONResponse({"error": "not_found"}, status_code=404)(
                scope, receive, send
            )
            return

        request = Request(scope, receive=receive)
        ip = self._client_ip(request)
        if not self._rate_ok(ip):
            await JSONResponse({"error": "rate_limited"}, status_code=429)(
                scope, receive, send
            )
            return

        # block obvious probes on /
        if path == "/":
            await JSONResponse({"error": "not_found"}, status_code=404)(
                scope, receive, send
            )
            return

        await self.app(scope, receive, send)


if __name__ == "__main__":
    # Fail loudly rather than silently falling back to a path that ignores
    # the secret (the previous try/except here was the root cause of the
    # /<secret>/mcp connector 404ing while /sse kept working).
    app = mcp.streamable_http_app(
        streamable_http_path=STREAMABLE_HTTP_PATH, stateless_http=True, host=HOST
    )
    app.add_middleware(LockdownMiddleware)
    import uvicorn

    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
