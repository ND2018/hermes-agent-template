#!/usr/bin/env python3
"""
backup_gbrain.py — Backup diario GBrain Hermes → Local + GitHub
Usa env vars: MCP_API_KEY (o MCP_KEY), HERMES_URL, GITHUB_TOKEN, GITHUB_BACKUP_REPO
"""
import os, json, base64, urllib.request
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
]

MCP_H = {"Authorization": f"Bearer {MCP_KEY}", "Content-Type": "application/json"}
GH_H  = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json", "Content-Type": "application/json"}

def mcp_get_page(slug):
    body = json.dumps({"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"gbrain_get_page","arguments":{"slug":slug}}}).encode()
    req = urllib.request.Request(f"{HERMES_URL}/mcp", data=body, headers=MCP_H, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.loads(r.read())
    content_list = d.get("result", {}).get("content", [])
    return "\n".join(c.get("text", "") for c in content_list)

def gh_sha(path):
    req = urllib.request.Request(f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}", headers=GH_H)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read()).get("sha")
    except: return None

def gh_push(path, text, msg):
    sha = gh_sha(path)
    body = {"message": msg, "content": base64.b64encode(text.encode()).decode()}
    if sha: body["sha"] = sha
    req = urllib.request.Request(f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}",
                                  data=json.dumps(body).encode(), headers=GH_H, method="PUT")
    with urllib.request.urlopen(req, timeout=15) as r: return r.status

def main():
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    print(f"=== GBrain Backup | {ts} ===")
    ok, err = 0, 0
    for slug in PAGES:
        try:
            content = mcp_get_page(slug)
            if "page_not_found" in content or "error" in content.lower()[:50]:
                raise ValueError(content[:100])
            data = {"slug": slug, "content": content, "backup_date": ts}
            with open(f"{BACKUP_DIR}/{slug}.json", "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            status = gh_push(f"pages/{slug}.json", json.dumps(data, ensure_ascii=False, indent=2), f"backup {slug} | {ts}")
            print(f"  ✓ {slug} (GitHub {status})")
            ok += 1
        except Exception as e:
            print(f"  ✗ {slug}: {e}")
            err += 1
    index = {"backup_date": ts, "pages": PAGES, "ok": ok, "errors": err}
    if GITHUB_TOKEN:
        gh_push("index.json", json.dumps(index, indent=2), f"index | {ts}")
    print(f"\n{'✅' if err==0 else '⚠️'} {ok}/{len(PAGES)} páginas — Local + GitHub")
    if err: exit(1)

if __name__ == "__main__":
    main()
