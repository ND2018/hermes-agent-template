"""
Hermes Agent Ã¢ Railway admin server.

Responsibilities:
        if tool_name == "gbrain_query":
            text = await _call_gbrain(["query", arguments.get("question", "")])
            return {"jsonrpc": "2.0", "id": msg_id, "result": {"content": [{"type": "text", "text": text}], "isError": False}}
        if tool_name == "gbrain_search":
            text = await _call_gbrain(["search", arguments.get("query", "")])
            return {"jsonrpc": "2.0", "id": msg_id, "result": {"content": [{"type": "text", "text": text}], "isError": False}}
        if tool_name == "gbrain_put_page":
            text = await _call_gbrain(["put", arguments.get("slug", "")], stdin_data=arguments.get("content", ""))
            return {"jsonrpc": "2.0", "id": msg_id, "result": {"content": [{"type": "text", "text": text}], "isError": False}}
        if tool_name == "gbrain_get_page":
            text = await _call_gbrain(["get", arguments.get("slug", "")])
            return {"jsonrpc": "2.0", "id": msg_id, "result": {"content": [{"type": "text", "text": text}], "isError": False}}
  - Admin UI / setup wizard at /setup (Starlette + Jinja, cookie-auth guarded)
  - Management API at /setup/api/* (config, status, logs, gateway, pairing)
  - Reverse proxy at / and /* Ã¢ native Hermes dashboard (hermes_cli/web_server, on 127.0.0.1:9119)
  - Managed subprocesses: `hermes gateway` (agent) and `hermes dashboard` (native UI)
  - Cookie-based session auth at /login (HMAC-signed, 7-day expiry, httponly)

Auth model: Basic Auth was dropped in favor of cookies because the Hermes React
SPA's plain fetch() calls do not reliably include basic-auth creds across browsers,
and basic-auth's per-directory protection space forced separate prompts for
/setup and /. Cookies auto-include on every same-origin request, so both the
setup UI and the proxied dashboard work with a single login. The cookie signing
secret is regenerated on every process start, so any ADMIN_PASSWORD change on
Railway (which triggers a redeploy) invalidates all existing sessions.

First-visit behavior: if no provider+model config exists, GET / redirects to /setup.
Once configured, / proxies to the Hermes dashboard. A small "Ã¢ Setup" widget is
injected into every proxied HTML response so users can always return to the wizard.
"""

import asyncio
import amazon_scheduler
import json
import os
import re
import secrets
import signal
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import websockets
import websockets.exceptions
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from starlette.routing import Route, WebSocketRoute
from starlette.templating import Jinja2Templates
from starlette.websockets import WebSocket, WebSocketDisconnect, WebSocketState

ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

HERMES_HOME = os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))
GBRAIN_HOME   = os.environ.get("GBRAIN_HOME", "/data/.gbrain")
GBRAIN2_URL   = os.environ.get("GBRAIN2_URL", "")
GBRAIN2_TOKEN = os.environ.get("GBRAIN2_TOKEN", "")
ENV_FILE = Path(HERMES_HOME) / ".env"
PAIRING_DIR = Path(HERMES_HOME) / "pairing"
PAIRING_TTL = 3600

# Native Hermes dashboard Ã¢ runs on loopback, fronted by our reverse proxy.
HERMES_DASHBOARD_HOST = "127.0.0.1"
HERMES_DASHBOARD_PORT = int(os.environ.get("HERMES_DASHBOARD_PORT", "9119"))
HERMES_DASHBOARD_URL = f"http://{HERMES_DASHBOARD_HOST}:{HERMES_DASHBOARD_PORT}"

# Mirror dashboard-ref-only/auth_proxy.py: strip only `host` (httpx sets it)
# and `transfer-encoding` (httpx recomputes it from the body). Keep everything
# else Ã¢ notably `authorization`, because the SPA uses Bearer tokens against
# hermes's own /api/env/reveal and OAuth endpoints, and keep `cookie` since
# some hermes endpoints read it. Aggressive stripping was masking requests in
# ways that produced spurious 401s.
HOP_BY_HOP = {"host", "transfer-encoding"}

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
if not ADMIN_PASSWORD:
    ADMIN_PASSWORD = secrets.token_urlsafe(16)
    print(f"[server] Admin credentials Ã¢ username: {ADMIN_USERNAME}  password: {ADMIN_PASSWORD}", flush=True)
else:
    print(f"[server] Admin username: {ADMIN_USERNAME}", flush=True)

# Ã¢Ã¢ Env var registry Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢
# (key, label, category, is_secret)
ENV_VARS = [
    ("LLM_MODEL",               "Model",                    "model",     False),
    ("OPENROUTER_API_KEY",       "OpenRouter",               "provider",  True),
    ("DEEPSEEK_API_KEY",         "DeepSeek",                 "provider",  True),
    ("DASHSCOPE_API_KEY",        "DashScope",                "provider",  True),
    ("GLM_API_KEY",              "GLM / Z.AI",               "provider",  True),
    ("KIMI_API_KEY",             "Kimi",                     "provider",  True),
    ("MINIMAX_API_KEY",          "MiniMax",                  "provider",  True),
    ("HF_TOKEN",                 "Hugging Face",             "provider",  True),
    # Added in v2026.4.23 (hermes v0.11.0). All plain API-key auth Ã¢ hermes
    # auto-routes by env-var presence, no extra config needed on our side.
    # OAuth-based providers (Gemini CLI, Qwen OAuth, Claude Code, Copilot)
    # are reachable via the dashboard's Keys tab and not exposed here.
    ("NVIDIA_API_KEY",           "NVIDIA NIM",               "provider",  True),
    ("ARCEE_API_KEY",            "Arcee AI",                 "provider",  True),
    ("STEPFUN_API_KEY",          "Step Plan",                "provider",  True),
    ("AI_GATEWAY_API_KEY",       "Vercel AI Gateway",        "provider",  True),
    ("GEMINI_API_KEY",           "Google AI Studio",         "provider",  True),
    ("ANTHROPIC_API_KEY",        "Anthropic (Claude)",        "provider",  True),
    ("PARALLEL_API_KEY",         "Parallel (search)",        "tool",      True),
    ("FIRECRAWL_API_KEY",        "Firecrawl (scrape)",       "tool",      True),
    ("TAVILY_API_KEY",           "Tavily (search)",          "tool",      True),
    ("FAL_KEY",                  "FAL (image gen)",          "tool",      True),
    ("BROWSERBASE_API_KEY",      "Browserbase key",          "tool",      True),
    ("BROWSERBASE_PROJECT_ID",   "Browserbase project",      "tool",      False),
    ("GITHUB_TOKEN",             "GitHub token",             "tool",      True),
    ("VOICE_TOOLS_OPENAI_KEY",   "OpenAI (voice/TTS)",       "tool",      True),
    ("HONCHO_API_KEY",           "Honcho (memory)",          "tool",      True),
    ("TELEGRAM_BOT_TOKEN",       "Bot Token",                "telegram",  True),
    ("TELEGRAM_ALLOWED_USERS",   "Allowed User IDs",         "telegram",  False),
    ("DISCORD_BOT_TOKEN",        "Bot Token",                "discord",   True),
    ("DISCORD_ALLOWED_USERS",    "Allowed User IDs",         "discord",   False),
    ("SLACK_BOT_TOKEN",          "Bot Token (xoxb-...)",     "slack",     True),
    ("SLACK_APP_TOKEN",          "App Token (xapp-...)",     "slack",     True),
    ("WHATSAPP_ENABLED",         "Enable WhatsApp",          "whatsapp",  False),
    ("EMAIL_ADDRESS",            "Email Address",            "email",     False),
    ("EMAIL_PASSWORD",           "Email Password",           "email",     True),
    ("EMAIL_IMAP_HOST",          "IMAP Host",                "email",     False),
    ("EMAIL_SMTP_HOST",          "SMTP Host",                "email",     False),
    ("MATTERMOST_URL",           "Server URL",               "mattermost",False),
    ("MATTERMOST_TOKEN",         "Bot Token",                "mattermost",True),
    ("MATRIX_HOMESERVER",        "Homeserver URL",           "matrix",    False),
    ("MATRIX_ACCESS_TOKEN",      "Access Token",             "matrix",    True),
    ("MATRIX_USER_ID",           "User ID",                  "matrix",    False),
    ("GATEWAY_ALLOW_ALL_USERS",  "Allow all users",          "gateway",   False),
    ("ADMIN_USERNAME",           "Admin username",           "admin",     False),
    ("ADMIN_PASSWORD",           "Admin password",           "admin",     True),
]

SECRET_KEYS  = {k for k, _, _, s in ENV_VARS if s}
PROVIDER_KEYS = [k for k, _, c, _ in ENV_VARS if c == "provider"]
CHANNEL_MAP  = {
    "Telegram":    "TELEGRAM_BOT_TOKEN",
    "Discord":     "DISCORD_BOT_TOKEN",
    "Slack":       "SLACK_BOT_TOKEN",
    "WhatsApp":    "WHATSAPP_ENABLED",
    "Email":       "EMAIL_ADDRESS",
    "Mattermost":  "MATTERMOST_TOKEN",
    "Matrix":      "MATRIX_ACCESS_TOKEN",
}


# Ã¢Ã¢ .env helpers Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢
def read_env(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    out = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
            v = v[1:-1]
        out[k.strip()] = v
    return out


def write_config_yaml(data: dict[str, str]) -> None:
    """Write config.yaml Ã¢ deep-merge template defaults with any existing user/cron-managed sections.

    Previously this overwrote ``$HERMES_HOME/config.yaml`` with a hardcoded template
    body on every boot, silently erasing user-managed top-level keys. The most
    common casualty is ``mcp_servers`` Ã¢ Hermes reads downstream MCP servers
    *only* from this file (see ``hermes_cli/mcp_config.py:_get_mcp_servers``), so
    the wipe broke ``hermes mcp add/test/list`` state across every container
    restart and required hand-restoration after each redeploy.

    The fix: load the existing file if any, apply the deployment-managed keys
    (``model.default``, ``model.provider``, ``terminal``, ``agent``, ``data_dir``)
    on top, and write the merged result. Unknown top-level keys (``mcp_servers``,
    custom skill config, etc.) are preserved verbatim.
    """
    import yaml  # hermes-agent already pulls pyyaml; deferred import keeps cold start light

    model = data.get("LLM_MODEL", "")
    config_path = Path(HERMES_HOME) / "config.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)

    existing: dict = {}
    if config_path.exists():
        try:
            with config_path.open() as f:
                loaded = yaml.safe_load(f)
            if isinstance(loaded, dict):
                existing = loaded
        except (yaml.YAMLError, OSError):
            # Treat unparseable as absent Ã¢ we'll overwrite with template defaults.
            existing = {}

    merged = dict(existing)

    # Deployment-managed (always authoritative Ã¢ these reflect the runtime env).
    merged_model = dict(merged.get("model") if isinstance(merged.get("model"), dict) else {})
    merged_model["default"] = model
    merged_model["provider"] = "auto"
    merged["model"] = merged_model

    merged_terminal = dict(merged.get("terminal") if isinstance(merged.get("terminal"), dict) else {})
    merged_terminal["backend"] = "local"
    merged_terminal["timeout"] = 60
    merged_terminal["cwd"] = "/tmp"
    merged["terminal"] = merged_terminal

    merged_agent = dict(merged.get("agent") if isinstance(merged.get("agent"), dict) else {})
    merged_agent.setdefault("max_iterations", 50)
    merged["agent"] = merged_agent

    merged["data_dir"] = HERMES_HOME

    with config_path.open("w") as f:
        yaml.safe_dump(merged, f, sort_keys=False, default_flow_style=False)


def write_env(path: Path, data: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    cat_order = ["model", "provider", "tool",
                 "telegram", "discord", "slack", "whatsapp",
                 "email", "mattermost", "matrix", "gateway"]
    cat_labels = {
        "model": "Model", "provider": "Providers", "tool": "Tools",
        "telegram": "Telegram", "discord": "Discord", "slack": "Slack",
        "whatsapp": "WhatsApp", "email": "Email",
        "mattermost": "Mattermost", "matrix": "Matrix", "gateway": "Gateway",
    }
    key_cat = {k: c for k, _, c, _ in ENV_VARS}
    grouped: dict[str, list[str]] = {c: [] for c in cat_order}
    grouped["other"] = []

    for k, v in data.items():
        if not v:
            continue
        cat = key_cat.get(k, "other")
        grouped.setdefault(cat, []).append(f"{k}={v}")

    lines: list[str] = []
    for cat in cat_order:
        entries = sorted(grouped.get(cat, []))
        if entries:
            lines.append(f"# {cat_labels.get(cat, cat)}")
            lines.extend(entries)
            lines.append("")
    if grouped["other"]:
        lines.append("# Other")
        lines.extend(sorted(grouped["other"]))
        lines.append("")

    path.write_text("\n".join(lines))


def is_config_complete(data: dict[str, str] | None = None) -> bool:
    """Single source of truth for 'ready to run the gateway'.

    Used by: GET / redirect, auto_start on boot, admin API status.
    """
    if data is None:
        data = read_env(ENV_FILE)
    has_model = bool(data.get("LLM_MODEL"))
    has_provider = any(data.get(k) for k in PROVIDER_KEYS)
    return has_model and has_provider


def mask(data: dict[str, str]) -> dict[str, str]:
    return {
        k: (v[:8] + "***" if len(v) > 8 else "***") if k in SECRET_KEYS and v else v
        for k, v in data.items()
    }


def unmask(new: dict[str, str], existing: dict[str, str]) -> dict[str, str]:
    return {
        k: (existing.get(k, "") if k in SECRET_KEYS and v.endswith("***") else v)
        for k, v in new.items()
    }


# Ã¢Ã¢ Auth (cookie-based) Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢
# We use HMAC-signed cookies instead of HTTP Basic Auth because:
#   1. Basic auth's per-directory protection space means browsers cache creds
#      for /setup/* separately from /*, forcing re-prompt on navigation.
#   2. Browser behavior for sending Basic auth on XHR/fetch is inconsistent;
#      the Hermes React SPA's plain fetch() calls don't reliably include it,
#      causing every proxied API call to 401.
# Cookies are auto-included on every same-origin request (navigation + XHR)
# so both the setup UI and the proxied Hermes dashboard work with one login.
#
# The SECRET is regenerated on every process start. That means any ADMIN_PASSWORD
# change via Railway Ã¢ redeploy Ã¢ all existing cookies invalidate Ã¢ users re-login.
import hashlib as _hashlib
import hmac as _hmac
from urllib.parse import quote as _url_quote, urlparse as _urlparse

COOKIE_NAME = "hermes_auth"
COOKIE_MAX_AGE = 7 * 86400  # 7 days
COOKIE_SECRET = secrets.token_bytes(32)
MCP_API_KEY = os.environ.get("MCP_API_KEY", "")  # Bearer token for mcp-remote clients

# Public paths Ã¢ no auth required. Everything else is behind the cookie gate.
PUBLIC_PATHS = {"/health", "/login", "/logout"}


def _make_auth_token() -> str:
    """Build a cookie value: `<expires>.<hmac-sha256>`."""
    expires = str(int(time.time()) + COOKIE_MAX_AGE)
    sig = _hmac.new(COOKIE_SECRET, expires.encode(), _hashlib.sha256).hexdigest()
    return f"{expires}.{sig}"


def _verify_auth_token(token: str) -> bool:
    try:
        expires_s, sig = token.rsplit(".", 1)
        if int(expires_s) < time.time():
            return False
        expected = _hmac.new(COOKIE_SECRET, expires_s.encode(), _hashlib.sha256).hexdigest()
        return _hmac.compare_digest(sig, expected)
    except Exception:
        return False


def _is_authenticated(request: Request) -> bool:
    if _verify_auth_token(request.cookies.get(COOKIE_NAME, "")):
        return True
    if MCP_API_KEY:
        auth_header = request.headers.get("authorization", "")
        if auth_header == f"Bearer {MCP_API_KEY}":
            return True
    return False


def _safe_return_to(value: str) -> str:
    """Reject open-redirect attempts Ã¢ only allow same-origin relative paths."""
    if not value or not value.startswith("/") or value.startswith("//"):
        return "/"
    # Strip any scheme/netloc that slipped through.
    p = _urlparse(value)
    if p.scheme or p.netloc:
        return "/"
    return value


def guard(request: Request) -> Response | None:
    """Enforce auth on protected routes.

    - HTML navigation: 302 to /login?returnTo=<path>
    - API / XHR: 401 JSON (so the SPA's fetch() can surface it cleanly)
    """
    if _is_authenticated(request):
        return None
    accept = request.headers.get("accept", "").lower()
    wants_html = "text/html" in accept
    if wants_html:
        rt = request.url.path
        if request.url.query:
            rt = f"{rt}?{request.url.query}"
        return RedirectResponse(f"/login?returnTo={_url_quote(rt)}", status_code=302)
    return JSONResponse({"error": "Unauthorized"}, status_code=401)


LOGIN_PAGE_HTML = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Hermes Agent Ã¢ Sign in</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0d0f14;color:#c9d1d9;font-family:'IBM Plex Sans',sans-serif;
  min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px}
.card{background:#14181f;border:1px solid #252d3d;border-radius:12px;padding:36px 32px;width:100%;max-width:380px;
  box-shadow:0 20px 40px rgba(0,0,0,0.4)}
.brand{text-align:center;margin-bottom:28px}
.brand-logo{display:inline-flex;align-items:center;gap:10px;font-family:'IBM Plex Mono',monospace;font-weight:600;font-size:18px;color:#6272ff}
.brand-logo span{color:#6b7688;font-weight:400}
.brand-sub{font-family:'IBM Plex Mono',monospace;font-size:11px;color:#6b7688;margin-top:8px;letter-spacing:1.5px;text-transform:uppercase}
label{display:block;font-family:'IBM Plex Mono',monospace;font-size:11px;color:#6b7688;
  letter-spacing:0.05em;text-transform:uppercase;margin-bottom:6px;margin-top:16px}
input{width:100%;background:#0d0f14;border:1px solid #252d3d;border-radius:6px;color:#c9d1d9;
  font-family:'IBM Plex Mono',monospace;font-size:13px;padding:9px 11px;outline:none;transition:border-color .15s}
input:focus{border-color:#6272ff}
button{width:100%;margin-top:24px;background:#6272ff;border:1px solid #6272ff;border-radius:6px;color:#fff;
  font-family:'IBM Plex Mono',monospace;font-size:13px;font-weight:500;padding:10px;cursor:pointer;
  transition:background .15s,border-color .15s}
button:hover{background:#7b8fff;border-color:#7b8fff}
.err{background:rgba(248,81,73,0.08);border:1px solid rgba(248,81,73,0.3);border-radius:6px;
  color:#f85149;font-family:'IBM Plex Mono',monospace;font-size:12px;padding:8px 12px;margin-bottom:14px;text-align:center}
.footnote{margin-top:18px;font-family:'IBM Plex Mono',monospace;font-size:10px;color:#6b7688;text-align:center;line-height:1.6}
</style></head>
<body>
<div class="card">
  <div class="brand">
    <div class="brand-logo">hermes<span>/admin</span></div>
    <div class="brand-sub">Sign in to continue</div>
  </div>
  __ERROR__
  <form method="POST" action="/login">
    <input type="hidden" name="returnTo" value="__RETURN_TO__">
    <label for="username">Username</label>
    <input id="username" name="username" type="text" autocomplete="username" autofocus required>
    <label for="password">Password</label>
    <input id="password" name="password" type="password" autocomplete="current-password" required>
    <button type="submit">Sign in</button>
  </form>
  <p class="footnote">Credentials are the <code>ADMIN_USERNAME</code> and <code>ADMIN_PASSWORD</code><br>Railway service variables.</p>
</div>
</body></html>"""


def _html_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;").replace("'", "&#39;"))


async def page_login(request: Request) -> Response:
    """GET /login Ã¢ render the sign-in form."""
    # Already signed in? Bounce to returnTo (or /).
    if _is_authenticated(request):
        return RedirectResponse(_safe_return_to(request.query_params.get("returnTo", "/")), status_code=302)
    rt = _safe_return_to(request.query_params.get("returnTo", "/"))
    error_html = ('<div class="err">Invalid username or password</div>'
                  if request.query_params.get("error") else "")
    html = (LOGIN_PAGE_HTML
            .replace("__ERROR__", error_html)
            .replace("__RETURN_TO__", _html_escape(rt)))
    return HTMLResponse(html)


async def login_post(request: Request) -> Response:
    """POST /login Ã¢ validate creds and set the auth cookie."""
    form = await request.form()
    username = str(form.get("username", ""))
    password = str(form.get("password", ""))
    return_to = _safe_return_to(str(form.get("returnTo", "/")))

    valid_user = _hmac.compare_digest(username, ADMIN_USERNAME)
    valid_pw = _hmac.compare_digest(password, ADMIN_PASSWORD)
    if valid_user and valid_pw:
        resp = RedirectResponse(return_to, status_code=302)
        resp.set_cookie(
            COOKIE_NAME,
            _make_auth_token(),
            max_age=COOKIE_MAX_AGE,
            httponly=True,
            samesite="lax",
            path="/",
        )
        return resp
    return RedirectResponse(f"/login?returnTo={_url_quote(return_to)}&error=1", status_code=302)


async def logout(request: Request) -> Response:
    """GET /logout Ã¢ clear cookie and bounce to login."""
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp


# Ã¢Ã¢ Gateway manager Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢
class Gateway:
    def __init__(self):
        self.proc: asyncio.subprocess.Process | None = None
        self.state = "stopped"
        self.logs: deque[str] = deque(maxlen=500)
        self.started_at: float | None = None
        self.restarts = 0

    async def start(self):
        if self.proc and self.proc.returncode is None:
            return
        self.state = "starting"
        try:
            # .env values take priority over Railway env vars.
            # We build the env this way so hermes's own dotenv loading
            # (which reads the same file) doesn't shadow our values.
            env = {**os.environ, "HERMES_HOME": HERMES_HOME}
            env.update(read_env(ENV_FILE))
            model = env.get("LLM_MODEL", "")
            provider_key = next((env.get(k, "") for k in PROVIDER_KEYS if env.get(k)), "")
            print(f"[gateway] model={model or 'Ã¢  NOT SET'} | provider_key={'set' if provider_key else 'Ã¢  NOT SET'}", flush=True)
            # Write config.yaml so hermes picks up the model (env vars alone aren't always enough)
            write_config_yaml(read_env(ENV_FILE))
            self.proc = await asyncio.create_subprocess_exec(
                "hermes", "gateway", "run", "--replace",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
            )
            self.state = "running"
            self.started_at = time.time()
            asyncio.create_task(self._drain())
        except Exception as e:
            self.state = "error"
            self.logs.append(f"[error] Failed to start: {e}")

    async def stop(self):
        if not self.proc or self.proc.returncode is not None:
            self.state = "stopped"
            return
        self.state = "stopping"
        self.proc.terminate()
        try:
            await asyncio.wait_for(self.proc.wait(), timeout=10)
        except asyncio.TimeoutError:
            self.proc.kill()
            await self.proc.wait()
        self.state = "stopped"
        self.started_at = None

    async def restart(self):
        await self.stop()
        self.restarts += 1
        await self.start()

    async def _drain(self):
        assert self.proc and self.proc.stdout
        async for raw in self.proc.stdout:
            line = ANSI_ESCAPE.sub("", raw.decode(errors="replace").rstrip())
            self.logs.append(line)
        if self.state == "running":
            self.state = "error"
            self.logs.append(f"[error] Gateway exited (code {self.proc.returncode})")

    def status(self) -> dict:
        uptime = int(time.time() - self.started_at) if self.started_at and self.state == "running" else None
        return {
            "state":    self.state,
            "pid":      self.proc.pid if self.proc and self.proc.returncode is None else None,
            "uptime":   uptime,
            "restarts": self.restarts,
        }


gw = Gateway()
cfg_lock = asyncio.Lock()


# Ã¢Ã¢ Hermes dashboard subprocess Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢
class Dashboard:
    """Manages the `hermes dashboard` subprocess (native Hermes web UI).

    Bound to loopback only Ã¢ we expose it to the public internet through our
    reverse proxy on $PORT, where edge basic auth guards every request.
    The dashboard is independent of the gateway: it reads config files
    directly and tolerates a stopped gateway.

    All subprocess output is streamed to our stdout (Ã¢ Railway logs) with a
    `[dashboard]` prefix AND retained in a ring buffer for diagnostics.
    Unexpected exits are explicitly logged with their return code.
    """

    def __init__(self):
        self.proc: asyncio.subprocess.Process | None = None
        self.logs: deque[str] = deque(maxlen=300)
        self._drain_task: asyncio.Task | None = None

    async def start(self):
        if self.proc and self.proc.returncode is None:
            return
        try:
            self.proc = await asyncio.create_subprocess_exec(
                "hermes", "dashboard",
                "--host", HERMES_DASHBOARD_HOST,
                "--port", str(HERMES_DASHBOARD_PORT),
                "--no-open",
                # --tui exposes /api/pty + /api/ws + /api/events so the
                # dashboard's embedded Chat tab works end-to-end. Requires
                # hermes >= v2026.4.23 Ã¢ older releases exit immediately
                # with "unrecognized arguments: --tui". The Dockerfile
                # pre-builds ui-tui/dist/ so PTY spawn is instant.
                "--tui",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            print(f"[dashboard] spawned pid={self.proc.pid} Ã¢ {HERMES_DASHBOARD_URL}", flush=True)
            self._drain_task = asyncio.create_task(self._drain())
        except Exception as e:
            print(f"[dashboard] FAILED to spawn: {e!r}", flush=True)

    async def _drain(self):
        """Stream subprocess output to Railway logs (prefixed) and a ring buffer."""
        assert self.proc and self.proc.stdout
        try:
            async for raw in self.proc.stdout:
                line = ANSI_ESCAPE.sub("", raw.decode(errors="replace").rstrip())
                self.logs.append(line)
                print(f"[dashboard] {line}", flush=True)
        except Exception as e:
            print(f"[dashboard] drain error: {e!r}", flush=True)
        finally:
            rc = self.proc.returncode if self.proc else None
            if rc is not None and rc != 0:
                print(f"[dashboard] EXITED with code {rc} Ã¢ reverse proxy will return 503 until restart", flush=True)
            elif rc == 0:
                print(f"[dashboard] exited cleanly (code 0)", flush=True)

    async def stop(self):
        if not self.proc or self.proc.returncode is not None:
            return
        self.proc.terminate()
        try:
            await asyncio.wait_for(self.proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            self.proc.kill()
            await self.proc.wait()


dash = Dashboard()

# Shared async HTTP client for the reverse proxy. Created lazily so we pick up
# the running event loop, torn down in lifespan.
_http_client: httpx.AsyncClient | None = None


def get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=5.0),
            follow_redirects=False,
        )
    return _http_client


# Ã¢Ã¢ Route handlers Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢
async def page_index(request: Request):
    if err := guard(request): return err
    return templates.TemplateResponse(request, "index.html")


async def route_health(request: Request):
    return JSONResponse({"status": "ok", "gateway": gw.state})


async def api_config_get(request: Request):
    if err := guard(request): return err
    async with cfg_lock:
        data = read_env(ENV_FILE)
    defs = [{"key": k, "label": l, "category": c, "secret": s} for k, l, c, s in ENV_VARS]
    return JSONResponse({"vars": mask(data), "defs": defs})


async def api_config_put(request: Request):
    if err := guard(request): return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    try:
        restart = body.pop("_restart", False)
        new_vars = body.get("vars", {})
        async with cfg_lock:
            existing = read_env(ENV_FILE)
            merged = unmask(new_vars, existing)
            for k, v in existing.items():
                if k not in merged:
                    merged[k] = v
            write_env(ENV_FILE, merged)
            write_config_yaml(merged)
        if restart:
            asyncio.create_task(gw.restart())
        return JSONResponse({"ok": True, "restarting": restart})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_status(request: Request):
    if err := guard(request): return err
    data = read_env(ENV_FILE)
    providers = {
        k.replace("_API_KEY","").replace("_TOKEN","").replace("HF_","HuggingFace ").replace("_"," ").title():
        {"configured": bool(data.get(k))}
        for k in PROVIDER_KEYS
    }
    channels = {
        name: {"configured": bool(v := data.get(key,"")) and v.lower() not in ("false","0","no")}
        for name, key in CHANNEL_MAP.items()
    }
    return JSONResponse({"gateway": gw.status(), "providers": providers, "channels": channels})


async def api_logs(request: Request):
    if err := guard(request): return err
    return JSONResponse({"lines": list(gw.logs)})


async def api_gw_start(request: Request):
    if err := guard(request): return err
    asyncio.create_task(gw.start())
    return JSONResponse({"ok": True})


async def api_gw_stop(request: Request):
    if err := guard(request): return err
    asyncio.create_task(gw.stop())
    return JSONResponse({"ok": True})


async def api_gw_restart(request: Request):
    if err := guard(request): return err
    asyncio.create_task(gw.restart())
    return JSONResponse({"ok": True})


async def api_config_reset(request: Request):
    if err := guard(request): return err
    asyncio.create_task(gw.stop())
    async with cfg_lock:
        if ENV_FILE.exists():
            ENV_FILE.unlink()
        write_config_yaml({})
    return JSONResponse({"ok": True})


# Ã¢Ã¢ Pairing Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢
def _pjson(path: Path) -> dict:
    try:
        return json.loads(path.read_text()) if path.exists() else {}
    except Exception:
        return {}


def _wjson(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    try: os.chmod(path, 0o600)
    except OSError: pass


def _platforms(suffix: str) -> list[str]:
    if not PAIRING_DIR.exists(): return []
    return [f.stem.rsplit(f"-{suffix}", 1)[0] for f in PAIRING_DIR.glob(f"*-{suffix}.json")]


async def api_pairing_pending(request: Request):
    if err := guard(request): return err
    now = time.time()
    out = []
    for p in _platforms("pending"):
        for code, info in _pjson(PAIRING_DIR / f"{p}-pending.json").items():
            if now - info.get("created_at", now) <= PAIRING_TTL:
                out.append({"platform": p, "code": code,
                            "user_id": info.get("user_id",""), "user_name": info.get("user_name",""),
                            "age_minutes": int((now - info.get("created_at", now)) / 60)})
    return JSONResponse({"pending": out})


async def api_pairing_approve(request: Request):
    if err := guard(request): return err
    try: body = await request.json()
    except Exception: return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    platform, code = body.get("platform",""), body.get("code","").upper().strip()
    if not platform or not code:
        return JSONResponse({"error": "platform and code required"}, status_code=400)
    pending_path = PAIRING_DIR / f"{platform}-pending.json"
    pending = _pjson(pending_path)
    if code not in pending:
        return JSONResponse({"error": "Code not found"}, status_code=404)
    entry = pending.pop(code)
    _wjson(pending_path, pending)
    approved = _pjson(PAIRING_DIR / f"{platform}-approved.json")
    approved[entry["user_id"]] = {"user_name": entry.get("user_name",""), "approved_at": time.time()}
    _wjson(PAIRING_DIR / f"{platform}-approved.json", approved)
    return JSONResponse({"ok": True})


async def api_pairing_deny(request: Request):
    if err := guard(request): return err
    try: body = await request.json()
    except Exception: return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    platform, code = body.get("platform",""), body.get("code","").upper().strip()
    p = PAIRING_DIR / f"{platform}-pending.json"
    pending = _pjson(p)
    if code in pending:
        del pending[code]
        _wjson(p, pending)
    return JSONResponse({"ok": True})


async def api_pairing_approved(request: Request):
    if err := guard(request): return err
    out = []
    for p in _platforms("approved"):
        for uid, info in _pjson(PAIRING_DIR / f"{p}-approved.json").items():
            out.append({"platform": p, "user_id": uid,
                        "user_name": info.get("user_name",""), "approved_at": info.get("approved_at",0)})
    return JSONResponse({"approved": out})


async def api_pairing_revoke(request: Request):
    if err := guard(request): return err
    try: body = await request.json()
    except Exception: return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    platform, uid = body.get("platform",""), body.get("user_id","")
    if not platform or not uid:
        return JSONResponse({"error": "platform and user_id required"}, status_code=400)
    p = PAIRING_DIR / f"{platform}-approved.json"
    approved = _pjson(p)
    if uid in approved:
        del approved[uid]
        _wjson(p, approved)
    return JSONResponse({"ok": True})


# Ã¢Ã¢ Reverse proxy Ã¢ Hermes dashboard Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢
_WIDGET_LINK_STYLE = (
    "background:rgba(20,24,31,0.92);backdrop-filter:blur(8px);"
    "border:1px solid #252d3d;border-radius:6px;padding:6px 12px;"
    "color:#c9d1d9;text-decoration:none;display:inline-flex;"
    "align-items:center;gap:6px;"
)
BACK_TO_SETUP_WIDGET = (
    '<div id="hermes-back-widget" style="position:fixed;bottom:14px;right:14px;'
    'z-index:99999;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;'
    'font-size:11px;display:flex;gap:8px;">'
    f'<a href="/setup" style="{_WIDGET_LINK_STYLE}">Ã¢ Setup</a>'
    f'<a href="/logout" style="{_WIDGET_LINK_STYLE}">Sign out</a>'
    '</div>'
)

DASHBOARD_UNAVAILABLE_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><title>Dashboard startingÃ¢Â¦</title>
<style>body{background:#0d0f14;color:#c9d1d9;font-family:ui-monospace,Menlo,monospace;
display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}
.card{max-width:480px;padding:32px;border:1px solid #252d3d;border-radius:12px;
background:#14181f;text-align:center}
h1{font-size:16px;color:#d29922;margin:0 0 12px;font-weight:600}
p{font-size:13px;color:#6b7688;line-height:1.6;margin:0 0 16px}
a{color:#6272ff;text-decoration:none;border:1px solid #252d3d;border-radius:6px;
padding:7px 14px;font-size:12px;display:inline-block}
a:hover{border-color:#6272ff}</style></head>
<body><div class="card">
<h1>Ã¢  Hermes dashboard unavailable</h1>
<p>The native Hermes dashboard is not responding on port %d.<br>
It may still be starting up, or it may have crashed.</p>
<p>Try refreshing in a few seconds, or head back to setup.</p>
<a href="/setup">Ã¢ Back to Setup</a>
</div>
<script>setTimeout(()=>location.reload(),4000);</script>
</body></html>""" % HERMES_DASHBOARD_PORT


async def _proxy_to_dashboard(request: Request) -> Response:
    """Forward an authenticated request to the Hermes dashboard subprocess.

    Assumes edge auth (basic auth middleware) has already validated the caller.
    HTTP-only: the native Hermes dashboard does not use WebSockets.
    """
    client = get_http_client()
    target = f"{HERMES_DASHBOARD_URL}{request.url.path}"
    if request.url.query:
        target = f"{target}?{request.url.query}"

    req_headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in HOP_BY_HOP
    }
    body = await request.body()

    try:
        upstream = await client.request(
            request.method,
            target,
            headers=req_headers,
            content=body,
        )
    except (httpx.ConnectError, httpx.ConnectTimeout):
        return HTMLResponse(DASHBOARD_UNAVAILABLE_HTML, status_code=503)
    except httpx.RequestError as e:
        print(f"[proxy] upstream error for {request.method} {request.url.path}: {e}", flush=True)
        return HTMLResponse(DASHBOARD_UNAVAILABLE_HTML, status_code=502)

    # Surface non-2xx responses from hermes into Railway logs so we can
    # diagnose 401/500s without needing browser DevTools access.
    if upstream.status_code >= 400:
        body_snip = upstream.content[:200].decode("utf-8", errors="replace")
        print(
            f"[proxy] {request.method} {request.url.path} -> {upstream.status_code} "
            f"body={body_snip!r}",
            flush=True,
        )

    # Strip hop-by-hop and length/encoding headers Ã¢ Starlette recomputes them.
    resp_headers = {
        k: v for k, v in upstream.headers.items()
        if k.lower() not in HOP_BY_HOP
        and k.lower() not in ("content-encoding", "content-length")
    }

    content = upstream.content
    content_type = upstream.headers.get("content-type", "").lower()

    # Inject the "Ã¢ Setup" widget into HTML pages so users can always return.
    if "text/html" in content_type and b"</body>" in content:
        try:
            text = content.decode("utf-8", errors="replace")
            text = text.replace("</body>", BACK_TO_SETUP_WIDGET + "</body>", 1)
            content = text.encode("utf-8")
        except Exception:
            pass  # on any error, fall back to raw upstream content

    return Response(
        content=content,
        status_code=upstream.status_code,
        headers=resp_headers,
    )


async def route_root(request: Request) -> Response:
    """GET /: first-visit smart redirect, otherwise proxy to the dashboard.

    - Unconfigured + bare GET `/` Ã¢ bounce to `/setup` so new users land on
      the wizard instead of a half-empty dashboard.
    - Sidebar / in-app links pass `?force=1` to opt out of that redirect Ã¢
      users who explicitly want the dashboard (e.g. to set providers via
      the Keys tab) can still reach it without saving config first.
    - Non-GET (SPA API calls, etc.) always proxy through.
    """
    if err := guard(request): return err
    if (request.method == "GET"
            and request.query_params.get("force") != "1"
            and not is_config_complete()):
        return RedirectResponse("/setup", status_code=302)
    return await _proxy_to_dashboard(request)


async def route_proxy(request: Request) -> Response:
    """Catch-all: forward any unmatched path to the Hermes dashboard."""
    if err := guard(request): return err
    return await _proxy_to_dashboard(request)


async def route_setup_404(request: Request) -> Response:
    """Typos under /setup/* should 404 here Ã¢ not fall through to the proxy."""
    if err := guard(request): return err
    return Response("Not Found", status_code=404, media_type="text/plain")


# Ã¢Ã¢ App lifecycle Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢
async def auto_start():
    if is_config_complete():
        asyncio.create_task(gw.start())
    else:
        print("[server] Config incomplete Ã¢ gateway not started. Configure provider + model in the admin UI.", flush=True)


@asynccontextmanager
async def lifespan(app):
    # Dashboard runs always Ã¢ it's the user-facing UI after setup is done,
    # and it's independent of gateway state.
    asyncio.create_task(dash.start())
    asyncio.create_task(amazon_scheduler.start())
    await auto_start()
    try:
        yield
    finally:
        await asyncio.gather(
            gw.stop(),
            dash.stop(),
            return_exceptions=True,
        )
        global _http_client
        if _http_client is not None:
            await _http_client.aclose()
            _http_client = None


# Ã¢Ã¢ WebSocket reverse proxy Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢Ã¢
# The hermes dashboard exposes 4 WebSocket endpoints when started with --tui.
# Three are opened by the browser SPA and need to flow through our reverse
# proxy; the fourth (/api/pub) is opened only by the PTY child against
# loopback and is intentionally NOT proxied Ã¢ exposing it would let an
# authed user spam events into channels.
#
#   /api/pty     binary stream Ã¢ embedded TUI keystrokes/output
#   /api/ws      JSON-RPC      Ã¢ gateway sidecar driving Chat metadata
#   /api/events  text frames   Ã¢ dashboard subscriber for /api/pub fan-out
#
# Auth model (matches the HTTP proxy):
#   * Edge: our HMAC cookie via _is_authenticated. WebSocket inherits .cookies
#     from starlette HTTPConnection so the same helper works unchanged.
#   * Upstream: hermes's own ?token=<_SESSION_TOKEN> query param. The SPA
#     fetches that token via /api/auth/session-token and includes it in the
#     WS URL, so we just forward path + query verbatim.
PROXIED_WS_PATHS = ("/api/pty", "/api/ws", "/api/events")


async def _ws_pump_client_to_upstream(
    client: WebSocket,
    upstream: websockets.WebSocketClientProtocol,
) -> None:
    """Forward client Ã¢ upstream until the client side disconnects.

    Handles both binary (PTY bytes) and text (JSON-RPC) frames.
    """
    try:
        while True:
            msg = await client.receive()
            if msg.get("type") == "websocket.disconnect":
                return
            data = msg.get("bytes")
            if data is not None:
                await upstream.send(data)
                continue
            text = msg.get("text")
            if text is not None:
                await upstream.send(text)
    except (WebSocketDisconnect, websockets.exceptions.ConnectionClosed):
        return
    except Exception as e:
        print(f"[ws-proxy] clientÃ¢upstream error on {client.url.path}: {e!r}", flush=True)
        return


async def _ws_pump_upstream_to_client(
    upstream: websockets.WebSocketClientProtocol,
    client: WebSocket,
) -> None:
    """Forward upstream Ã¢ client until upstream closes."""
    try:
        async for msg in upstream:
            if isinstance(msg, bytes):
                await client.send_bytes(msg)
            else:
                await client.send_text(msg)
    except (websockets.exceptions.ConnectionClosed, WebSocketDisconnect):
        return
    except Exception as e:
        print(f"[ws-proxy] upstreamÃ¢client error on {client.url.path}: {e!r}", flush=True)
        return


async def ws_proxy(websocket: WebSocket) -> None:
    """Reverse-proxy a single WebSocket from browser Ã¢ hermes dashboard.

    Order matters: connect upstream BEFORE accepting the client. If hermes
    is wedged or rejects the upgrade, we close the client with a meaningful
    code instead of accepting and then dropping silently.

    Connection lifecycle:
      1. Verify edge cookie auth Ã¢ 4401 close on failure
      2. Open upstream WS with bounded open_timeout Ã¢ 1011 on failure
      3. Accept client
      4. Spawn two pump tasks (bidirectional byte forwarding)
      5. When either direction ends (client navigates away, upstream PTY
         exits, etc.), cancel the other task and close both sockets
    """
    # 1. Edge auth.
    if not _is_authenticated(websocket):
        # Close before accept Ã¢ browser sees the handshake fail (expected
        # for unauthenticated calls).
        await websocket.close(code=4401)
        return

    # 2. Build upstream URL preserving the SPA's path + query (the query
    #    contains the hermes session token + channel id).
    path = websocket.url.path
    qs = websocket.url.query
    upstream_url = f"ws://{HERMES_DASHBOARD_HOST}:{HERMES_DASHBOARD_PORT}{path}"
    if qs:
        upstream_url = f"{upstream_url}?{qs}"

    try:
        upstream = await websockets.connect(
            upstream_url,
            open_timeout=5,
            # Don't forward client cookies/headers Ã¢ hermes WS auth is
            # purely token-based via the URL, and forwarding random
            # headers risks future upstream surprises.
        )
    except (asyncio.TimeoutError, OSError, websockets.exceptions.WebSocketException) as e:
        # Hermes dashboard down, restarting, or rejected the upgrade
        # (e.g. bad/missing session token).
        print(f"[ws-proxy] upstream connect failed for {path}: {e!r}", flush=True)
        # 1011 = internal error; client SPA will surface a generic close.
        await websocket.close(code=1011)
        return

    # 3. Both sides ready Ã¢ accept and start pumping.
    await websocket.accept()

    pump_in = asyncio.create_task(_ws_pump_client_to_upstream(websocket, upstream))
    pump_out = asyncio.create_task(_ws_pump_upstream_to_client(upstream, websocket))

    try:
        # First side to finish wins; cancel the other.
        done, pending = await asyncio.wait(
            (pump_in, pump_out),
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
    finally:
        # websockets.connect() outside `async with` doesn't auto-close;
        # do it explicitly. Same for the client side if still open.
        try:
            await upstream.close()
        except Exception:
            pass
        if websocket.client_state == WebSocketState.CONNECTED:
            try:
                await websocket.close()
            except Exception:
                pass


ANY_METHOD = ["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"]


# ââ MCP server (Bearer-token guarded) ââââââââââââââââââââââââââââââââââââââââ
# POST /mcp  â Streamable HTTP transport (mcp-remote http-first strategy).
# GET  /mcp  â SSE transport (mcp-remote sse-only fallback).
# POST /mcp/messages â message channel for SSE sessions.
# Auth: Authorization: Bearer <MCP_API_KEY>  (set in Railway env vars).
# Tool: hermes_chat(message) â runs `hermes -z <message>` (oneshot CLI mode).

_MCP_SERVER_INFO      = {"name": "hermes-agent", "version": "1.0.0"}
_MCP_PROTOCOL_VERSION = "2024-11-05"
_MCP_CAPABILITIES     = {"tools": {}}
_MCP_TOOLS = [
    {
        "name": "hermes_chat",
        "description": (
            "Send a message or task to the Hermes Agent and get a response. "
            "Use for complex research, planning, coding, or any task that "
            "benefits from Hermes autonomous reasoning."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "The message or task to send to Hermes.",
                }
            },
            "required": ["message"],
        },
    }
,
    {
        "name": "gbrain_query",
        "description": (
            "Hybrid search (vector + keyword + RRF) over the company brain. "
            "Use for business questions about sales, products, customers, or any company data. "
            "Returns ranked results with citations."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "The question or search query."}
            },
            "required": ["question"],
        },
    },
    {
        "name": "gbrain_search",
        "description": "Fast keyword search over the company brain.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Keywords to search for."}
            },
            "required": ["query"],
        },
    },
    {
        "name": "gbrain_put_page",
        "description": (
            "Write or update a brain page. Use to save business data, sales reports, "
            "or any structured knowledge to the company brain."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "slug": {"type": "string", "description": "Page ID, e.g. 'data/ventas/amazon-es-2025' or 'companies/naturdao'."},
                "content": {"type": "string", "description": "Full markdown content of the page."}
            },
            "required": ["slug", "content"],
        },
    },
    {
        "name": "gbrain_get_page",
        "description": "Read a brain page by its slug.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "slug": {"type": "string", "description": "Page slug to retrieve."}
            },
            "required": ["slug"],
        },
    }]

_mcp_sse_sessions: dict = {}  # session_id -> asyncio.Queue


def _mcp_auth_ok(request: Request) -> bool:
    if not MCP_API_KEY:
        return False
    return request.headers.get("authorization", "") == f"Bearer {MCP_API_KEY}"


async def _call_hermes(message: str) -> str:
    try:
        env = {**os.environ, "HERMES_HOME": HERMES_HOME, "HERMES_YOLO_MODE": "1"}
        proc = await asyncio.create_subprocess_exec(
            "hermes", "-z", message,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120.0)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return "[hermes-agent error] Request timed out after 120s"
        if proc.returncode != 0:
            err = stderr.decode("utf-8", errors="replace").strip()
            return f"[hermes-agent error] hermes -z exit {proc.returncode}: {err[:500]}"
        result = stdout.decode("utf-8", errors="replace").strip()
        return result or "[hermes-agent error] Empty response from hermes -z"
    except Exception as exc:
        return f"[hermes-agent error] {exc}"





async def _call_gbrain2_mcp(
    action: str, slug: str = "", question: str = "", content_data: str = ""
) -> str:
    """Call GBrain2 (Railway) via MCP HTTP/SSE. Uses only stdlib."""
    import re as _re, json as _json, urllib.request as _urlreq, asyncio as _asyncio

    _tool_map = {
        "query":  ("query",    {"query": question}),
        "search": ("query",    {"query": question}),
        "get":    ("get_page", {"slug": slug}),
        "put":    ("put_page", {
            "slug":    slug,
            "content": content_data,
            "title":   slug.replace("-", " ").title(),
        }),
    }
    if action not in _tool_map:
        return f"error: unknown gbrain action '{action}'"

    _tool_name, _tool_args = _tool_map[action]
    _payload = _json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params":  {"name": _tool_name, "arguments": _tool_args},
    }).encode()

    def _do_req():
        _req = _urlreq.Request(
            f"{GBRAIN2_URL}/mcp",
            data=_payload,
            headers={
                "Authorization": f"Bearer {GBRAIN2_TOKEN}",
                "Content-Type":  "application/json",
                "Accept":        "application/json, text/event-stream",
            },
            method="POST",
        )
        try:
            with _urlreq.urlopen(_req, timeout=30) as _resp:
                return _resp.read().decode()
        except Exception as _e:
            return f"error: {_e}"

    _text = await _asyncio.get_event_loop().run_in_executor(None, _do_req)
    _m = _re.search(r"^data: (.+)$", _text, _re.MULTILINE)
    if not _m:
        return f"error: unexpected GBrain2 response: {_text[:300]}"
    _data = _json.loads(_m.group(1))
    if _data.get("result", {}).get("isError"):
        return "error: " + _data["result"]["content"][0]["text"]
    return _data["result"]["content"][0]["text"]


async def _call_gbrain(args: list, stdin_data: str | None = None) -> str:
    """Run a gbrain CLI command. Routes to GBrain2 HTTP API when configured."""
    if GBRAIN2_URL and GBRAIN2_TOKEN:
        _action   = args[0] if args else "query"
        _slug_q   = args[1] if len(args) > 1 else ""
        return await _call_gbrain2_mcp(
            _action,
            slug=_slug_q,
            question=_slug_q,
            content_data=stdin_data or "",
        )
    try:
        env = {**os.environ, "GBRAIN_HOME": GBRAIN_HOME, "PATH": "/root/.bun/bin:" + os.environ.get("PATH", "")}
        proc = await asyncio.create_subprocess_exec(
            "/root/.bun/bin/gbrain", *args,
            stdin=asyncio.subprocess.PIPE if stdin_data is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            input_bytes = stdin_data.encode() if stdin_data is not None else None
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=input_bytes), timeout=60.0
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return "[gbrain error] Request timed out after 60s"
        if proc.returncode != 0:
            err = stderr.decode("utf-8", errors="replace").strip()
            return f"[gbrain error] exit {proc.returncode}: {err[:500]}"
        result = stdout.decode("utf-8", errors="replace").strip()
        return result or "[gbrain error] Empty response"
    except Exception as exc:
        return f"[gbrain error] {exc}"
def _mcp_respond(data: dict, status: int = 200) -> Response:
    import json as _json
    return Response(
        content=_json.dumps(data),
        status_code=status,
        media_type="application/json",
    )


async def _mcp_handle_jsonrpc(body: dict):
    method = body.get("method", "")
    msg_id = body.get("id")
    params = body.get("params", {})

    if method == "initialize":
        return {
            "jsonrpc": "2.0", "id": msg_id,
            "result": {
                "protocolVersion": _MCP_PROTOCOL_VERSION,
                "serverInfo":      _MCP_SERVER_INFO,
                "capabilities":    _MCP_CAPABILITIES,
            },
        }
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": _MCP_TOOLS}}

    if method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})
        if tool_name == "hermes_chat":
            text = await _call_hermes(arguments.get("message", ""))
            return {
                "jsonrpc": "2.0", "id": msg_id,
                "result": {"content": [{"type": "text", "text": text}], "isError": False},
            }
        elif tool_name == "gbrain_query":
            text = await _call_gbrain(["query", arguments.get("question", "")])
            return {
                "jsonrpc": "2.0", "id": msg_id,
                "result": {"content": [{"type": "text", "text": text}], "isError": False},
            }
        elif tool_name == "gbrain_search":
            text = await _call_gbrain(["search", arguments.get("query", "")])
            return {
                "jsonrpc": "2.0", "id": msg_id,
                "result": {"content": [{"type": "text", "text": text}], "isError": False},
            }
        elif tool_name == "gbrain_put_page":
            slug = arguments.get("slug", "")
            content = arguments.get("content", "")
            text = await _call_gbrain(["put", slug, "--content", content])
            return {
                "jsonrpc": "2.0", "id": msg_id,
                "result": {"content": [{"type": "text", "text": text}], "isError": False},
            }
        elif tool_name == "gbrain_get_page":
            text = await _call_gbrain(["get", arguments.get("slug", "")])
            return {
                "jsonrpc": "2.0", "id": msg_id,
                "result": {"content": [{"type": "text", "text": text}], "isError": False},
            }
        return {
            "jsonrpc": "2.0", "id": msg_id,
            "error": {"code": -32601, "message": f"Unknown tool: {tool_name}"},
        }

    if msg_id is not None:
        return {
            "jsonrpc": "2.0", "id": msg_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }
    return None  # Notification


async def route_mcp(request: Request) -> Response:
    if not _mcp_auth_ok(request):
        return Response("Unauthorized", status_code=401)

    if request.method == "POST":
        try:
            body = await request.json()
        except Exception:
            return _mcp_respond(
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
                status=400,
            )
        resp = await _mcp_handle_jsonrpc(body)
        if resp is None:
            return Response(status_code=204)
        return _mcp_respond(resp)

    # GET â SSE transport
    import json as _json
    session_id = secrets.token_hex(16)
    queue: asyncio.Queue = asyncio.Queue()
    _mcp_sse_sessions[session_id] = queue

    async def event_stream():
        try:
            yield f"event: endpoint\ndata: /mcp/messages?session_id={session_id}\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=30.0)
                    if event is None:
                        break
                    yield f"data: {_json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            _mcp_sse_sessions.pop(session_id, None)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def route_mcp_messages(request: Request) -> Response:
    if not _mcp_auth_ok(request):
        return Response("Unauthorized", status_code=401)

    session_id = request.query_params.get("session_id", "")
    queue = _mcp_sse_sessions.get(session_id)
    if queue is None:
        return Response("Session not found", status_code=404)

    try:
        body = await request.json()
    except Exception:
        return Response("Bad Request", status_code=400)

    resp = await _mcp_handle_jsonrpc(body)
    if resp is not None:
        await queue.put(resp)
    return Response(status_code=202)



# ââ Velocity data endpoint (privat, autenticat) âââââââââââââââââââââââââââââââ
VELOCITY_FILE = Path("/data/velocity-data.json")

def _check_mcp_auth(request: Request) -> bool:
    token = request.query_params.get("token", "")
    auth  = request.headers.get("authorization", "")
    return auth == f"Bearer {MCP_API_KEY}" or (MCP_API_KEY and token == MCP_API_KEY)

async def route_velocity_get(request: Request) -> Response:
    """GET /api/velocity  â retorna el JSON de velocitat de vendes."""
    _CORS = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, PUT, OPTIONS",
        "Access-Control-Allow-Headers": "Authorization, Content-Type",
    }
    # Handle CORS preflight
    if request.method == "OPTIONS":
        return Response("", headers=_CORS)
    if not _check_mcp_auth(request):
        return Response("Unauthorized", status_code=401)
    if not VELOCITY_FILE.exists():
        return JSONResponse({"error": "velocity data not found"}, status_code=404)
    return Response(VELOCITY_FILE.read_text(encoding="utf-8"),
                    media_type="application/json", headers=_CORS)

async def route_velocity_put(request: Request) -> Response:
    """PUT /api/velocity  â escriu el JSON de velocitat (Railway scripts)."""
    if not _check_mcp_auth(request):
        return Response("Unauthorized", status_code=401)
    try:
        body = await request.body()
        data = json.loads(body)
        VELOCITY_FILE.parent.mkdir(parents=True, exist_ok=True)
        VELOCITY_FILE.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        return JSONResponse({"ok": True, "updated": data.get("meta", {}).get("updated", "")})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


# ââ Velocity short-URL endpoint (artifact-friendly, PIN auth) ââââââââââââââââ
VELOCITY_SHORT_PIN = "nd2018"  # simple read-only PIN, URL stays short for web_fetch

async def route_velocity_vdata(request: Request) -> Response:
    """GET /api/vdata?pin=nd2018  â endpoint curt per a artifacts (URL <= 80 chars)."""
    _CORS = {"Access-Control-Allow-Origin": "*",
             "Access-Control-Allow-Methods": "GET, OPTIONS",
             "Access-Control-Allow-Headers": "Authorization, Content-Type"}
    if request.method == "OPTIONS":
        return Response("", headers=_CORS)
    pin = request.query_params.get("pin", "")
    if pin != VELOCITY_SHORT_PIN:
        return Response("Unauthorized", status_code=401)
    if not VELOCITY_FILE.exists():
        return JSONResponse({"error": "velocity data not found"}, status_code=404)
    return Response(VELOCITY_FILE.read_text(encoding="utf-8"),
                    media_type="application/json", headers=_CORS)

# ââ Velocity Dashboard (Chrome-compatible, token-auth) ââââââââââââââââââââââââ
VELOCITY_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="ca">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Velocitat Vendes â Naturdao</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.5.0/dist/chart.umd.js" crossorigin="anonymous"></script>
<style>
:root{color-scheme:light}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',system-ui,sans-serif;font-size:13px;background:#f0f4ff;color:#1e293b;padding:16px}
h1{font-size:16px;font-weight:700;margin-bottom:2px}
.sub{font-size:11px;color:#64748b;margin-bottom:14px}
.status{font-size:11px;padding:4px 10px;border-radius:12px;display:inline-flex;align-items:center;gap:5px;margin-bottom:14px}
.status.loading{background:#fef3c7;color:#92400e}
.status.ok{background:#dcfce7;color:#166534}
.status.error{background:#fee2e2;color:#991b1b}
.filters{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px;align-items:center}
.filters span{font-size:11px;color:#64748b}
.fbtn{font-size:11px;padding:3px 10px;border-radius:5px;border:1px solid #cbd5e1;background:#fff;color:#64748b;cursor:pointer}
.sep{width:1px;height:20px;background:#e2e8f0}
.kpi-row{display:grid;grid-template-columns:repeat(auto-fill,minmax(108px,1fr));gap:8px;margin-bottom:12px}
.kc{background:#fff;border:1px solid #e2e8f0;border-radius:8px;padding:9px 12px}
.kc .kl{font-size:9px;text-transform:uppercase;letter-spacing:.06em;color:#94a3b8;margin-bottom:2px}
.kc .kv{font-size:20px;font-weight:700;color:#1e293b;line-height:1.1}
.kc .ks{font-size:10px;color:#94a3b8;margin-top:1px}
.sku-row{display:grid;grid-template-columns:repeat(auto-fill,minmax(105px,1fr));gap:7px;margin-bottom:12px}
.sc{background:#fff;border:1px solid #e2e8f0;border-radius:6px;padding:7px 10px}
.sc .sl{font-size:9px;font-family:monospace;font-weight:700;margin-bottom:2px}
.sc .sv{font-size:16px;font-weight:700;font-family:monospace;color:#1e293b}
.sc .ss{font-size:10px;color:#94a3b8;margin-top:1px}
.mkt-row{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:12px}
.mc{background:#fff;border:1px solid #e2e8f0;border-radius:6px;padding:8px 12px}
.mc .ml{font-size:9px;font-weight:700;margin-bottom:2px}
.mc .mv{font-size:18px;font-weight:700;font-family:monospace;color:#1e293b}
.mc .ms{font-size:10px;color:#94a3b8}
.chart-grid{display:grid;grid-template-columns:2fr 1fr;gap:12px;margin-bottom:12px}
.chart-row{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.cc{background:#fff;border:1px solid #e2e8f0;border-radius:8px;padding:14px}
.cc h2{font-size:11px;font-weight:600;color:#64748b;margin-bottom:10px}
.cc canvas{max-height:220px}
.donut-wrap{display:flex;align-items:center;justify-content:center;min-height:220px}
.donut-wrap canvas{max-height:200px;max-width:200px}
.pbtn{font-size:11px;padding:4px 10px;border-radius:5px;border:1px solid #cbd5e1;background:#fff;color:#64748b;cursor:pointer}
.pbtn.active{border-color:#4f46e5;background:#eef2ff;color:#4f46e5;font-weight:700}
.period-lbl{font-size:12px;color:#4f46e5;font-weight:700}
.view-tabs{display:flex;align-items:center;gap:6px;margin-bottom:10px}
.vtab{font-size:11px;padding:4px 12px;border-radius:5px;border:1px solid #cbd5e1;background:#fff;color:#64748b;cursor:pointer}
.vtab.active{border-color:#4f46e5;background:#eef2ff;color:#4f46e5;font-weight:700}
.spinner{display:inline-block;width:14px;height:14px;border:2px solid #fbbf24;border-top-color:transparent;border-radius:50%;animation:spin .8s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
</style>
</head>
<body>
<h1>ð Velocitat Vendes per SKU â Naturdao</h1>
<p class="sub">Dades: Hermes privat Â· Actualitzat cada nit automÃ ticament</p>
<div id="status" class="status loading"><span class="spinner"></span> Carregant...</div>
<div id="app" style="display:none">
  <div class="filters">
    <span>Canal:</span><div id="canal-btns"></div>
    <div class="sep"></div>
    <span>Producte:</span><div id="sku-btns"></div>
    <div class="sep"></div>
    <div style="display:flex;gap:4px">
      <button class="pbtn" onclick="setPreset(30)">30d</button>
      <button class="pbtn" onclick="setPreset(60)">60d</button>
      <button class="pbtn" onclick="setPreset(90)">90d</button>
      <button class="pbtn" onclick="setPreset(182)">6m</button>
      <button class="pbtn" onclick="setPreset(365)">1a</button>
    </div>
    <span id="period-lbl" class="period-lbl"></span>
  </div>
  <div class="kpi-row" id="kpis"></div>
  <div class="sku-row" id="skus"></div>
  <div class="mkt-row" id="markets"></div>
  <div class="view-tabs">
    <span style="font-size:10px;color:#94a3b8">GrÃ fics per:</span>
    <button class="vtab active" onclick="setView('canal')">Canal</button>
    <button class="vtab" onclick="setView('sku')">Producte</button>
  </div>
  <div class="chart-grid">
    <div class="cc"><h2>Unitats per canal</h2><canvas id="cs"></canvas></div>
    <div class="cc"><h2>Mix</h2><div class="donut-wrap"><canvas id="cd"></canvas></div></div>
  </div>
  <div class="chart-row">
    <div class="cc"><h2>TendÃ¨ncia</h2><canvas id="cl"></canvas></div>
    <div class="cc"><h2>Ranking</h2><canvas id="cb"></canvas></div>
  </div>
</div>
<script>
const PIN=new URLSearchParams(location.search).get('pin')||'';
const TOKEN=new URLSearchParams(location.search).get('token')||'';
const API_URL=PIN?'/api/vdata?pin='+encodeURIComponent(PIN):'/api/velocity?token='+encodeURIComponent(TOKEN);
const CANAL_ORDER=["WEB_B2C","B2B","MAJORISTA_NATURITAS","AMZ_EU","AMZ_USA"];
const CANAL_LABELS={"WEB_B2C":"Web B2C","B2B":"B2B","MAJORISTA_NATURITAS":"Majorista","AMZ_EU":"Amazon EU","AMZ_USA":"Amazon USA"};
const CANAL_COLORS={"WEB_B2C":"#6366f1","B2B":"#f59e0b","MAJORISTA_NATURITAS":"#10b981","AMZ_EU":"#3b82f6","AMZ_USA":"#ef4444"};
const SKU_ORDER=["1#1M","1#3M","1#PLUS","1#MAX","US1#1M","US1#PLUS","US1#MAX"];
const SKU_COLORS={"1#1M":"#6366f1","1#3M":"#8b5cf6","1#PLUS":"#06b6d4","1#MAX":"#f59e0b","US1#1M":"#ef4444","US1#PLUS":"#f97316","US1#MAX":"#ec4899"};
const EU_SKUS=["1#1M","1#3M","1#PLUS","1#MAX"];
const USA_SKUS=["US1#1M","US1#PLUS","US1#MAX"];
const MNAMES=['gen','feb','mar','abr','mai','jun','jul','ago','set','oct','nov','des'];
let SKU_DATA={},MONTHS=[],MLBL=[],MAX_DATE=new Date(),activePreset=182;
let activeCanal=new Set(CANAL_ORDER),activeSku=new Set(SKU_ORDER),chartView='canal';
let CS,CD,CL,CB;
async function loadData(){
  try{
    const r=await fetch(API_URL);
    if(!r.ok)throw new Error('HTTP '+r.status);
    const d=await r.json();
    const dataset={updated:(d.meta&&d.meta.updated)||new Date().toISOString().slice(0,10),months:d.MONTHS||[],data:d.SKU_DATA||{}};
    if(!dataset.months.length)throw new Error('Sense dades');
    initDashboard(dataset);
    document.getElementById('status').className='status ok';
    document.getElementById('status').textContent='â Dades del '+dataset.updated;
  }catch(err){
    document.getElementById('status').className='status error';
    document.getElementById('status').textContent='â '+err.message;
  }
}
function initDashboard(ds){
  SKU_DATA=ds.data;MONTHS=ds.months;
  MLBL=MONTHS.map(m=>{const[y,mo]=m.split('-').map(Number);return MNAMES[mo-1]+' '+String(y).slice(2);});
  const metaUpdated=ds.updated;
  if(metaUpdated&&/^\d{4}-\d{2}-\d{2}$/.test(metaUpdated)){MAX_DATE=new Date(metaUpdated+'T12:00:00');}
  else{const lastM=MONTHS[MONTHS.length-1];if(lastM){const[y,mo]=lastM.split('-').map(Number);MAX_DATE=new Date(y,mo-1,28);}}
  const _today=new Date();_today.setHours(12,0,0,0);if(MAX_DATE>_today)MAX_DATE=_today;
  document.getElementById('app').style.display='block';
  buildFilters();setPreset(182);
}
function buildFilters(){
  const cb=document.getElementById('canal-btns');cb.innerHTML='';
  CANAL_ORDER.forEach(c=>{
    const b=document.createElement('button');b.className='fbtn active';b.textContent=CANAL_LABELS[c];
    b.style.borderColor=CANAL_COLORS[c];b.style.background=CANAL_COLORS[c];b.style.color='#fff';
    b.onclick=()=>{if(activeCanal.has(c)){if(activeCanal.size===1)return;activeCanal.delete(c);b.classList.remove('active');b.style.background='#fff';b.style.color='#64748b';}else{activeCanal.add(c);b.classList.add('active');b.style.background=CANAL_COLORS[c];b.style.color='#fff';}update();};
    cb.appendChild(b);
  });
  const sb=document.getElementById('sku-btns');sb.innerHTML='';
  SKU_ORDER.forEach(s=>{
    const b=document.createElement('button');b.className='fbtn active';b.textContent=s;b.style.fontFamily='monospace';
    b.style.borderColor=SKU_COLORS[s];b.style.background=SKU_COLORS[s];b.style.color='#fff';
    b.onclick=()=>{if(activeSku.has(s)){if(activeSku.size===1)return;activeSku.delete(s);b.classList.remove('active');b.style.background='#fff';b.style.color='#64748b';}else{activeSku.add(s);b.classList.add('active');b.style.background=SKU_COLORS[s];b.style.color='#fff';}update();};
    sb.appendChild(b);
  });
}
function getQty(s,c,m){return(SKU_DATA[s]&&SKU_DATA[s][c]&&SKU_DATA[s][c][m])||0;}
function dim(y,m0){return new Date(y,m0+1,0).getDate();}
function monthFrac(ms,sd){
  const[y,m]=ms.split('-').map(Number),m0=m-1;
  const mS=new Date(y,m0,1),rawE=new Date(y,m0,dim(y,m0));
  const effE=rawE<=MAX_DATE?rawE:new Date(MAX_DATE);const effS=mS>=sd?mS:new Date(sd);
  if(effS>effE)return 0;
  return(Math.round((effE-effS)/864e5)+1)/(Math.round((effE-mS)/864e5)+1);
}
function computePreset(days){
  const sd=new Date(MAX_DATE);sd.setDate(sd.getDate()-days+1);
  const skus=SKU_ORDER.filter(s=>activeSku.has(s));
  const canals=CANAL_ORDER.filter(c=>activeCanal.has(c));
  const months=MONTHS.filter(ms=>{const[y,mo]=ms.split('-').map(Number);return new Date(y,mo-1,1)<=MAX_DATE&&new Date(y,mo-1,dim(y,mo-1))>=sd;});
  const cm={};canals.forEach(c=>{cm[c]={};months.forEach(m=>{cm[c][m]=0;});});
  skus.forEach(s=>canals.forEach(c=>months.forEach(m=>{cm[c][m]+=Math.round(getQty(s,c,m)*monthFrac(m,sd));})));
  const sm={};skus.forEach(s=>{sm[s]={};months.forEach(m=>{sm[s][m]=canals.reduce((a,c)=>a+Math.round(getQty(s,c,m)*monthFrac(m,sd)),0);});});
  const ct={};canals.forEach(c=>{ct[c]=months.reduce((a,m)=>a+(cm[c][m]||0),0);});
  const st={};skus.forEach(s=>{st[s]=canals.reduce((a,c)=>a+months.reduce((b,m)=>b+Math.round(getQty(s,c,m)*monthFrac(m,sd)),0),0);});
  return{months,skus,canals,cm,sm,ct,st,grand:canals.reduce((a,c)=>a+ct[c],0),presetDays:days};
}
const PLBL={30:'30d',60:'60d',90:'90d',182:'6m',365:'1a'};
function setPreset(days){
  activePreset=days;
  document.querySelectorAll('.pbtn').forEach(b=>b.classList.toggle('active',b.textContent===PLBL[days]));
  const e=new Date(MAX_DATE),s=new Date(MAX_DATE);s.setDate(s.getDate()-days+1);
  const mn=d=>d.getDate()+' '+MNAMES[d.getMonth()]+(s.getFullYear()!==e.getFullYear()?' '+String(d.getFullYear()).slice(2):'');
  document.getElementById('period-lbl').textContent=mn(s)+' â '+mn(e);
  update();
}
function setView(v){chartView=v;document.querySelectorAll('.vtab').forEach(b=>b.classList.toggle('active',b.textContent===(v==='canal'?'Canal':'Producte')));const d=computePreset(activePreset);renderCharts(d);}
function update(){const d=computePreset(activePreset);renderKPIs(d);renderSKUs(d);renderMarkets(d);renderCharts(d);}
function renderKPIs(d){
  const{canals,ct,st,skus,grand,presetDays:pd,months:ms}=d,n=ms.length;
  const rate=v=>pd?Math.round(v/pd*30):Math.round(v/Math.max(n,1));
  const eu=EU_SKUS.filter(s=>skus.includes(s)).reduce((a,s)=>a+(st[s]||0),0);
  const usa=USA_SKUS.filter(s=>skus.includes(s)).reduce((a,s)=>a+(st[s]||0),0);
  const cards=[
    {l:'Total',v:grand.toLocaleString('ca'),s:pd+' dies',bc:''},
    {l:'Mitjana/mes',v:rate(grand).toLocaleString('ca'),s:'u/mes',bc:''},
    ...canals.map(c=>({l:CANAL_LABELS[c],v:ct[c].toLocaleString('ca'),s:rate(ct[c]).toLocaleString('ca')+' u/mes',bc:CANAL_COLORS[c]})),
    {l:'ðªðº EU',v:eu.toLocaleString('ca'),s:rate(eu).toLocaleString('ca')+' u/mes',bc:'#3b82f6'},
    {l:'ðºð¸ USA',v:usa.toLocaleString('ca'),s:rate(usa).toLocaleString('ca')+' u/mes',bc:'#ef4444'},
  ];
  document.getElementById('kpis').innerHTML=cards.map(c=>`<div class="kc" style="${c.bc?'border-left:3px solid '+c.bc:''}"><div class="kl" style="${c.bc?'color:'+c.bc:''}">${c.l}</div><div class="kv">${c.v}</div><div class="ks">${c.s}</div></div>`).join('');
}
function renderSKUs(d){
  const{skus,st,months:ms,presetDays:pd}=d,n=ms.length;
  document.getElementById('skus').innerHTML=skus.filter(s=>(st[s]||0)>0).map(s=>{
    const v=st[s]||0,r=pd?Math.round(v/pd*30):Math.round(v/Math.max(n,1)),col=SKU_COLORS[s];
    return`<div class="sc" style="border-left:3px solid ${col}"><div class="sl" style="color:${col}">${s}</div><div class="sv">${v.toLocaleString('ca')}</div><div class="ss">${r.toLocaleString('ca')} u/mes</div></div>`;
  }).join('');
}
function renderMarkets(d){
  const{skus,st,months:ms,presetDays:pd}=d,n=ms.length;
  const rate=v=>pd?Math.round(v/pd*30):Math.round(v/Math.max(n,1));
  const eu=EU_SKUS.filter(s=>skus.includes(s)).reduce((a,s)=>a+(st[s]||0),0);
  const usa=USA_SKUS.filter(s=>skus.includes(s)).reduce((a,s)=>a+(st[s]||0),0);
  document.getElementById('markets').innerHTML=`<div class="mc" style="border-left:3px solid #3b82f6"><div class="ml" style="color:#3b82f6">ðªðº Europa</div><div class="mv">${eu.toLocaleString('ca')}</div><div class="ms">${rate(eu).toLocaleString('ca')} u/mes</div></div><div class="mc" style="border-left:3px solid #ef4444"><div class="ml" style="color:#ef4444">ðºð¸ USA</div><div class="mv">${usa.toLocaleString('ca')}</div><div class="ms">${rate(usa).toLocaleString('ca')} u/mes</div></div>`;
}
const LEG={position:'bottom',labels:{color:'#64748b',font:{size:10},boxWidth:10}};
const AXIS={ticks:{color:'#94a3b8',font:{size:10}},grid:{color:'#f1f5f9'}};
const stkP={id:'st',afterDatasetsDraw(chart){const{ctx,data,scales}=chart;if(!scales.x||!scales.y||!data.datasets.length)return;ctx.save();ctx.font='bold 9px Segoe UI';ctx.fillStyle='#334155';ctx.textAlign='center';ctx.textBaseline='bottom';for(let i=0;i<data.datasets[0].data.length;i++){const tot=data.datasets.reduce((s,ds)=>s+(ds.data[i]||0),0);if(!tot)continue;const meta=chart.getDatasetMeta(data.datasets.length-1);if(!meta.data[i])continue;ctx.fillText(tot>=1000?(tot/1000).toFixed(1)+'k':tot,meta.data[i].x,scales.y.getPixelForValue(tot)-3);}ctx.restore();}};
function renderCharts(d){
  const{months:ms,canals,cm,sm,ct,st,skus}=d;
  const byC=chartView==='canal';
  const lbl=ms.map(m=>MLBL[MONTHS.indexOf(m)]);
  const bds=byC?canals.map(c=>({label:CANAL_LABELS[c],data:ms.map(m=>cm[c][m]||0),backgroundColor:CANAL_COLORS[c],stack:'s'})):skus.map(s=>({label:s,data:ms.map(m=>(sm[s]&&sm[s][m])||0),backgroundColor:SKU_COLORS[s],stack:'s'}));
  if(CS)CS.destroy();CS=new Chart(document.getElementById('cs'),{type:'bar',data:{labels:lbl,datasets:bds},options:{plugins:{legend:LEG},scales:{x:{...AXIS,stacked:true},y:{...AXIS,stacked:true}},responsive:true,maintainAspectRatio:true},plugins:[stkP]});
  const dd=byC?canals.filter(c=>ct[c]>0):skus.filter(s=>(st[s]||0)>0);
  const dv=byC?dd.map(c=>ct[c]):dd.map(s=>st[s]||0);
  const dc=byC?dd.map(c=>CANAL_COLORS[c]):dd.map(s=>SKU_COLORS[s]);
  const dl=byC?dd.map(c=>CANAL_LABELS[c]):dd;
  if(CD)CD.destroy();CD=new Chart(document.getElementById('cd'),{type:'doughnut',data:{labels:dl,datasets:[{data:dv,backgroundColor:dc,borderWidth:2,borderColor:'#fff'}]},options:{plugins:{legend:LEG},responsive:true,maintainAspectRatio:true}});
  const fml=MONTHS.map(m=>MLBL[MONTHS.indexOf(m)]);
  if(byC){
    const fc={};canals.forEach(c=>{fc[c]={};MONTHS.forEach(m=>{fc[c][m]=0;});});skus.forEach(s=>canals.forEach(c=>MONTHS.forEach(m=>{fc[c][m]+=getQty(s,c,m);})));
    if(CL)CL.destroy();CL=new Chart(document.getElementById('cl'),{type:'line',data:{labels:fml,datasets:canals.map(c=>({label:CANAL_LABELS[c],data:MONTHS.map(m=>fc[c][m]||0),borderColor:CANAL_COLORS[c],backgroundColor:CANAL_COLORS[c]+'22',tension:.3,pointRadius:3,borderWidth:2}))},options:{plugins:{legend:LEG},scales:{x:AXIS,y:AXIS},responsive:true,maintainAspectRatio:true}});
  }else{
    const fs={};skus.forEach(s=>{fs[s]={};MONTHS.forEach(m=>{fs[s][m]=canals.reduce((a,c)=>a+getQty(s,c,m),0);});});
    if(CL)CL.destroy();CL=new Chart(document.getElementById('cl'),{type:'line',data:{labels:fml,datasets:skus.map(s=>({label:s,data:MONTHS.map(m=>fs[s][m]||0),borderColor:SKU_COLORS[s],backgroundColor:SKU_COLORS[s]+'22',tension:.3,pointRadius:3,borderWidth:2}))},options:{plugins:{legend:LEG},scales:{x:AXIS,y:AXIS},responsive:true,maintainAspectRatio:true}});
  }
  if(byC){const sc=[...canals].sort((a,b)=>(ct[b]||0)-(ct[a]||0));if(CB)CB.destroy();CB=new Chart(document.getElementById('cb'),{type:'bar',data:{labels:sc.map(c=>CANAL_LABELS[c]),datasets:[{data:sc.map(c=>ct[c]||0),backgroundColor:sc.map(c=>CANAL_COLORS[c]+'99'),borderColor:sc.map(c=>CANAL_COLORS[c]),borderWidth:1}]},options:{indexAxis:'y',plugins:{legend:{display:false}},scales:{x:AXIS,y:AXIS},responsive:true,maintainAspectRatio:true}});}
  else{const ss=[...skus].sort((a,b)=>(st[b]||0)-(st[a]||0));if(CB)CB.destroy();CB=new Chart(document.getElementById('cb'),{type:'bar',data:{labels:ss,datasets:[{data:ss.map(s=>st[s]||0),backgroundColor:ss.map(s=>SKU_COLORS[s]+'99'),borderColor:ss.map(s=>SKU_COLORS[s]),borderWidth:1}]},options:{indexAxis:'y',plugins:{legend:{display:false}},scales:{x:AXIS,y:{ticks:{...AXIS.ticks,font:{size:9}},grid:AXIS.grid}},responsive:true,maintainAspectRatio:true}});}
}
loadData();
</script>
</body>
</html>"""

async def route_velocity_dashboard(request: Request) -> Response:
    """GET /velocity?token=... OR ?pin=nd2018 â standalone Chrome-compatible velocity dashboard."""
    pin = request.query_params.get("pin", "")
    token = request.query_params.get("token", "")
    if pin == VELOCITY_SHORT_PIN:
        return HTMLResponse(VELOCITY_DASHBOARD_HTML)
    if not MCP_API_KEY or token != MCP_API_KEY:
        return Response("Unauthorized â pass ?token=<MCP_API_KEY> or ?pin=nd2018", status_code=401)
    return HTMLResponse(VELOCITY_DASHBOARD_HTML)

routes = [
    # Public Ã¢ no auth required.
    Route("/health",                            route_health),
    Route("/login",                             page_login,          methods=["GET"]),
    Route("/login",                             login_post,          methods=["POST"]),
    Route("/logout",                            logout),

    # Our setup wizard + management API, all under /setup/* (cookie-auth guarded).
    Route("/setup",                             page_index),
    Route("/setup/",                            page_index),
    Route("/setup/api/config",                  api_config_get,      methods=["GET"]),
    Route("/setup/api/config",                  api_config_put,      methods=["PUT"]),
    Route("/setup/api/status",                  api_status),
    Route("/setup/api/logs",                    api_logs),
    Route("/setup/api/gateway/start",           api_gw_start,        methods=["POST"]),
    Route("/setup/api/gateway/stop",            api_gw_stop,         methods=["POST"]),
    Route("/setup/api/gateway/restart",         api_gw_restart,      methods=["POST"]),
    Route("/setup/api/config/reset",            api_config_reset,    methods=["POST"]),
    Route("/setup/api/pairing/pending",         api_pairing_pending),
    Route("/setup/api/pairing/approve",         api_pairing_approve, methods=["POST"]),
    Route("/setup/api/pairing/deny",            api_pairing_deny,    methods=["POST"]),
    Route("/setup/api/pairing/approved",        api_pairing_approved),
    Route("/setup/api/pairing/revoke",          api_pairing_revoke,  methods=["POST"]),

    # /setup/* typos return a real 404 Ã¢ not a silent proxy fallthrough.
    Route("/setup/{path:path}",                 route_setup_404,     methods=ANY_METHOD),

    # Reverse-proxy hermes's dashboard WebSockets (Chat tab + sidecar).
    # WebSocketRoute is matched independently of HTTP routes, so order
    # relative to the catch-all HTTP `Route("/{path:path}", ...)` below
    # doesn't matter Ã¢ but listing them as a group keeps the surface
    # area auditable. Only paths in PROXIED_WS_PATHS are forwarded;
    # /api/pub is intentionally omitted.
    WebSocketRoute("/api/pty",                  ws_proxy),
    WebSocketRoute("/api/ws",                   ws_proxy),
    WebSocketRoute("/api/events",               ws_proxy),

    # Velocity data â Bearer-token guarded (also ?token= for web_fetch).
    Route("/api/velocity",  route_velocity_get,  methods=["GET"]),
    Route("/api/velocity",  route_velocity_put,  methods=["PUT"]),
    Route("/api/vdata",     route_velocity_vdata, methods=["GET", "OPTIONS"]),

        Route("/velocity",              route_velocity_dashboard,    methods=["GET"]),

    # MCP server â Bearer-token guarded, for Claude Desktop / mcp-remote.
    Route("/mcp",          route_mcp,          methods=["GET", "POST"]),
    Route("/mcp/messages", route_mcp_messages, methods=["POST"]),

    # Root: redirect to /setup if unconfigured, otherwise proxy the dashboard.
    Route("/",                                  route_root,          methods=ANY_METHOD),

    # Catch-all: everything else proxies to the Hermes dashboard subprocess.
    Route("/{path:path}",                       route_proxy,         methods=ANY_METHOD),
]

# No middleware Ã¢ auth is enforced per-handler via guard(). This keeps /health
# and /login truly unauthenticated without middleware gymnastics.
app = Starlette(routes=routes, lifespan=lifespan)

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8080"))
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info", loop="asyncio")
    server = uvicorn.Server(config)

    def _shutdown():
        loop.create_task(gw.stop())
        loop.create_task(dash.stop())
        server.should_exit = True

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _shutdown)

    loop.run_until_complete(server.serve())

