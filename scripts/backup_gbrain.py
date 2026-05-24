#!/usr/bin/env python3
"""
backup_gbrain.py — Backup diari GBrain Hermes → Local + GitHub

Variables d'entorn: MCP_API_KEY (o MCP_KEY), HERMES_URL, GITHUB_TOKEN, GITHUB_BACKUP_REPO

NOTA 2026-05-24: gbrain_get_page té un bug conegut (timeout). Aquest script
intenta diversos enfocaments per llegir cada pàgina (get + query fallback)
i SEMPRE surt amb exit 0 — així Railway no marca crash diari encara que
algunes pàgines no es puguin recuperar.
"""
import os, sys, json, base64, urllib.request, traceback
from datetime import datetime

HERMES_URL   = os.environ.get("HERMES_URL", "https://hermes-agent-template-production-fb9a.up.railway.app").rstrip("/")
MCP_KEY      = os.environ.get("MCP_API_KEY") or os.environ.get("MCP_KEY", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO  = os.environ.get("GITHUB_BACKUP_REPO", "ND2018/gbrain-backup")
BACKUP_DIR   = os.path.dirname(os.path.abspath(__file__))

PAGES = [
    "empresa-overview", "vendorati-resumen", "catalogo-productos-naturdao",
    "infraestructura-tech", "kpis-negocio", "amazon-mercados",
    "vendorati-2026-ytd", "producto-dao-naturdao",
    "woocommerce-overview", "woocommerce-pedidos",
    "woocommerce-clientes", "woocommerce-productos",
    # Noves pàgines afegides 2026-05-24
    "woocommerce-stock", "velocity-resum",
    "amazon-inventari-eu", "amazon-inventari-usa", "amazon-inventari-global",
    "amazon-europa-pl-diario", "amazon-usa-pl-diario",
    "emails-naturdao", "credentials-vault", "_context",
    "railway-projecte-naturdao",
]

MCP_H = {"Authorization": f"Bearer {MCP_KEY}", "Content-Type": "application/json"}
GH_H  = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json", "Content-Type": "application/json"}


def _telegram_alert(msg):
    """Envia alerta Telegram. Falla silenciosament."""
    try:
        tok  = os.environ.get("TELEGRAM_TOKEN", "")
        chat = os.environ.get("TELEGRAM_CHAT_ID", "")
        if not tok or not chat:
            return
        body = json.dumps({"chat_id": chat, "text": msg, "parse_mode": "HTML"}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{tok}/sendMessage",
            data=body, headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception:
        pass


def mcp_get_page(slug, timeout=55):
    """Intenta llegir la pàgina sencera via gbrain_get_page."""
    body = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "gbrain_get_page", "arguments": {"slug": slug}}
    }).encode()
    req = urllib.request.Request(f"{HERMES_URL}/mcp", data=body, headers=MCP_H, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read())
    content_list = d.get("result", {}).get("content", [])
    return "\n".join(c.get("text", "") for c in content_list)


def mcp_query_page(slug, timeout=30):
    """Fallback: gbrain_query per obtenir snippets si get_page falla."""
    body = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "gbrain_query", "arguments": {"question": f"contingut de la pàgina {slug}"}}
    }).encode()
    req = urllib.request.Request(f"{HERMES_URL}/mcp", data=body, headers=MCP_H, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read())
    content_list = d.get("result", {}).get("content", [])
    return "\n".join(c.get("text", "") for c in content_list)


def gh_sha(path):
    req = urllib.request.Request(f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}", headers=GH_H)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read()).get("sha")
    except Exception:
        return None


def gh_push(path, text, msg):
    sha = gh_sha(path)
    body = {"message": msg, "content": base64.b64encode(text.encode()).decode()}
    if sha:
        body["sha"] = sha
    req = urllib.request.Request(
        f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}",
        data=json.dumps(body).encode(), headers=GH_H, method="PUT"
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.status


def main():
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    print(f"=== GBrain Backup | {ts} ===", flush=True)
    ok, err, partial = 0, 0, 0
    errors_detail = []
    for slug in PAGES:
        try:
            # Intent 1: gbrain_get_page (pot petar)
            content = ""
            source = "get_page"
            try:
                content = mcp_get_page(slug)
                if not content or "page_not_found" in content.lower() or content.startswith("[gbrain error]"):
                    raise ValueError(f"get_page invalid: {content[:60]}")
            except Exception as e1:
                # Intent 2: gbrain_query (snippets)
                try:
                    content = mcp_query_page(slug)
                    source = "query (partial)"
                    if not content:
                        raise ValueError("query també buida")
                    partial += 1
                except Exception as e2:
                    raise ValueError(f"get_page: {e1} | query: {e2}")
            data = {
                "slug": slug,
                "content": content,
                "backup_date": ts,
                "source": source,
            }
            content_str = json.dumps(data, ensure_ascii=False, indent=2)
            # Local
            try:
                with open(f"{BACKUP_DIR}/{slug}.json", "w", encoding="utf-8") as f:
                    f.write(content_str)
            except Exception:
                pass
            # GitHub
            if GITHUB_TOKEN:
                gh_push(f"pages/{slug}.json", content_str, f"backup {slug} | {ts}")
            print(f"  ✓ {slug} [{source}] ({len(content)} bytes)", flush=True)
            ok += 1
        except Exception as e:
            print(f"  ✗ {slug}: {str(e)[:120]}", flush=True)
            errors_detail.append(f"{slug}: {str(e)[:60]}")
            err += 1

    index = {
        "backup_date": ts, "pages": PAGES,
        "ok": ok, "errors": err, "partial": partial,
        "errors_detail": errors_detail[:20],
    }
    if GITHUB_TOKEN:
        try:
            gh_push("index.json", json.dumps(index, indent=2), f"index | {ts}")
        except Exception as e:
            print(f"  ✗ index.json: {e}", flush=True)

    status_emoji = "✅" if err == 0 else ("⚠️" if ok > 0 else "🚨")
    msg = f"{status_emoji} {ok}/{len(PAGES)} ok · {partial} partial · {err} err"
    print(f"\n{msg}", flush=True)

    # Telegram alert només si hi ha errors crítics o canvi d'estat
    if err > len(PAGES) / 2:
        _telegram_alert(f"⚠️ <b>backup_gbrain</b>\n{msg}\nUltims errors:\n" + "\n".join(errors_detail[:5]))


# ── Wrapper anti-crash absolut ───────────────────────────────────────────────
if __name__ == "__main__":
    try:
        main()
        sys.exit(0)
    except BaseException as e:
        tb = traceback.format_exc()
        print(f"[ERROR] CRASH inesperat: {e}\n{tb}", flush=True)
        try:
            _telegram_alert(
                f"🚨 <b>backup_gbrain CRASH</b>\n"
                f"<code>{type(e).__name__}: {str(e)[:200]}</code>"
            )
        except Exception:
            pass
        sys.exit(0)  # MAI crash Railway
