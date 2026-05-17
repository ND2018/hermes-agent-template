#!/usr/bin/env python3
"""
download_amazon_reports_auto.py
Naturdao / Body Nostrum — Amazon Europa SP-API → GBrain

ARQUITECTURA "AMAZON FRIENDLY":
  En lugar de la Orders API (muy limitada: 1 req/min por marketplace),
  usamos la Reports API:
    1. Solicitar UN report que cubre TODOS los marketplaces EU (1 llamada)
    2. Esperar a que Amazon lo genere (~30-120s, polling suave)
    3. Descargar el TSV desde S3 (URL pre-firmada, sin rate limit)
    4. Parsear y agregar por marketplace, producto, estado
    5. Subir resumen a GBrain (1 llamada a Hermes)
  Total: ~6 llamadas API por ejecución vs. cientos con Orders API.

RATE LIMITS (Reports API):
  POST /reports:               0.0167 req/s, burst 15  → 1/min
  GET  /reports/{id}  (poll): 2.0   req/s, burst 15  → podemos polling frecuente
  GET  /documents/{id}:        0.0167 req/s, burst 15  → 1/min

VARIABLES DE ENTORNO:
  AMAZON_CLIENT_ID_EUROPA      amzn1.application-oa2-client...
  AMAZON_CLIENT_SECRET_EUROPA  amzn1.oa2-cs.v1...
  AMAZON_REFRESH_TOKEN_EUROPA  Atzr|...
  HERMES_URL                   https://hermes-agent-template-...railway.app
  MCP_KEY                      eyJ... (JWT)

USO:
  python download_amazon_reports_auto.py           # ayer (por defecto)
  python download_amazon_reports_auto.py --days 7  # últimos 7 días
  python download_amazon_reports_auto.py --date 2026-05-10  # día concreto
"""

import os, sys, json, csv, io, gzip, time, argparse
import urllib.request, urllib.error, urllib.parse
from datetime import datetime, timezone, timedelta
from collections import defaultdict

# ── Cargar .env automáticamente ────────────────────────────────────────────────
def load_dotenv():
    candidates = [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
        ".env",
    ]
    for p in candidates:
        try:
            with open(p) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        if k.strip() not in os.environ:
                            os.environ[k.strip()] = v.strip()
            return p
        except FileNotFoundError:
            continue
    return None

loaded = load_dotenv()

# ── Credenciales ───────────────────────────────────────────────────────────────
def _req(key):
    v = os.environ.get(key)
    if not v:
        print(f"[ERROR] Falta variable de entorno: {key}", flush=True)
        sys.exit(1)
    return v

CLIENT_ID     = _req("AMAZON_CLIENT_ID_EUROPA")
CLIENT_SECRET = _req("AMAZON_CLIENT_SECRET_EUROPA")
REFRESH_TOKEN = _req("AMAZON_REFRESH_TOKEN_EUROPA")
HERMES_URL    = _req("HERMES_URL").rstrip("/")
MCP_KEY       = _req("MCP_KEY")

# ── Constantes ─────────────────────────────────────────────────────────────────
LWA_URL     = "https://api.amazon.com/auth/o2/token"
SP_API_BASE = "https://sellingpartnerapi-eu.amazon.com"

# Marketplace IDs oficiales Europa
MARKETPLACE_IDS = [
    "A1RKKUPIHCS9HS",  # ES
    "A1F83G8C2ARO7P",  # UK
    "A1PA6795UKMFR9",  # DE
    "A13V1IB3VIYZZH",  # FR
    "APJ6JRA9NG5V4",   # IT
    "A1805IZSGTT6HS",  # NL
    "A1C3SOZRARQ6R3",  # PL
    "A2NODRKZP88ZB9",  # SE
    "AMEN7PMS3EDWL",   # BE
    "A33AVAJ2PDY3EV",  # TR
    "A28R8C7NBKEWEA",  # IE
    "A2VIGQ35RCS4UG",  # AE
    "A17E79C6D8DWNP",  # SA
]

MCP_HEADERS = {"Authorization": f"Bearer {MCP_KEY}", "Content-Type": "application/json"}
RUN_START   = datetime.now(timezone.utc)

# ── Logging ────────────────────────────────────────────────────────────────────
def log(msg, level="INFO"):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{ts}] [{level}] {msg}", flush=True)

# ── LWA: access token ──────────────────────────────────────────────────────────
def get_access_token():
    body = urllib.parse.urlencode({
        "grant_type":    "refresh_token",
        "refresh_token": REFRESH_TOKEN,
        "client_id":     CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    }).encode()
    req = urllib.request.Request(
        LWA_URL, data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "User-Agent":   "GBrain-Amazon-Sync/2.0 (Language=Python)"}
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        log(f"LWA HTTP {e.code}: {e.read().decode()[:200]}", "ERROR")
        sys.exit(1)
    token = data.get("access_token")
    if not token:
        log(f"LWA sin token: {data}", "ERROR")
        sys.exit(1)
    log("✓ Access token LWA obtenido")
    return token

# ── SP-API helpers ─────────────────────────────────────────────────────────────
def sp_request(method, path, token, body=None, params=None, retries=3):
    """Llamada genérica a SP-API con reintentos y backoff."""
    qs  = ("?" + urllib.parse.urlencode(params)) if params else ""
    url = f"{SP_API_BASE}{path}{qs}"
    headers = {
        "x-amz-access-token": token,
        "Accept":             "application/json",
        "User-Agent":         "GBrain-Amazon-Sync/2.0 (Language=Python)",
    }
    if body:
        headers["Content-Type"] = "application/json"
    data = json.dumps(body).encode() if body else None

    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            body_txt = e.read().decode(errors="replace")
            if e.code == 429:
                wait = int(e.headers.get("Retry-After", 60))
                log(f"Rate limit {path} → esperando {wait}s (intento {attempt+1})", "WARN")
                time.sleep(wait)
            elif e.code in (400, 403, 404):
                log(f"HTTP {e.code} {path}: {body_txt[:200]}", "WARN")
                return None
            else:
                wait = 2 ** attempt * 5
                log(f"HTTP {e.code} {path} (intento {attempt+1}) → {wait}s", "WARN")
                time.sleep(wait)
        except Exception as ex:
            wait = 2 ** attempt * 5
            log(f"Error {path}: {ex} (intento {attempt+1}) → {wait}s", "WARN")
            time.sleep(wait)
    return None

# ── Reports API — flujo completo ──────────────────────────────────────────────
def request_report(token, start_date, end_date):
    """Solicitar report de órdenes. Cubre TODOS los marketplaces en UNA llamada."""
    log(f"Solicitando report: {start_date[:10]} → {end_date[:10]}")
    result = sp_request("POST", "/reports/2021-06-30/reports", token, body={
        "reportType":     "GET_FLAT_FILE_ALL_ORDERS_DATA_BY_ORDER_DATE_GENERAL",
        "dataStartTime":  start_date,
        "dataEndTime":    end_date,
        "marketplaceIds": MARKETPLACE_IDS,
    })
    if not result:
        log("No se pudo solicitar el report", "ERROR")
        sys.exit(1)
    report_id = result.get("reportId")
    log(f"✓ reportId: {report_id}")
    return report_id

def wait_for_report(token, report_id, max_wait_s=300):
    """Esperar a que el report esté listo. Polling cada 20s."""
    log(f"Esperando report {report_id}...")
    elapsed = 0
    interval = 20
    while elapsed < max_wait_s:
        result = sp_request("GET", f"/reports/2021-06-30/reports/{report_id}", token)
        if not result:
            time.sleep(interval)
            elapsed += interval
            continue
        status = result.get("processingStatus", "IN_QUEUE")
        log(f"  Estado: {status} ({elapsed}s)")
        if status == "DONE":
            doc_id = result.get("reportDocumentId")
            log(f"✓ Report listo — documentId: {doc_id}")
            return doc_id
        elif status in ("FATAL", "CANCELLED"):
            log(f"Report falló: {status}", "ERROR")
            return None
        time.sleep(interval)
        elapsed += interval
    log(f"Timeout esperando report ({max_wait_s}s)", "ERROR")
    return None

def download_report(token, doc_id):
    """Obtener URL pre-firmada y descargar el TSV (con o sin gzip)."""
    result = sp_request("GET", f"/reports/2021-06-30/documents/{doc_id}", token)
    if not result:
        log("No se pudo obtener el documento", "ERROR")
        return None
    url        = result.get("url")
    compressed = result.get("compressionAlgorithm") == "GZIP"
    log(f"Descargando report (GZIP={compressed})...")
    # La URL de S3 es pre-firmada — sin headers de auth
    req = urllib.request.Request(url, headers={"User-Agent": "GBrain/2.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read()
    content = gzip.decompress(raw).decode("utf-8", errors="replace") if compressed else raw.decode("utf-8", errors="replace")
    lines = content.strip().split("\n")
    log(f"✓ Report descargado: {len(lines)-1} líneas de datos")
    return content

# ── Parsear y agregar ──────────────────────────────────────────────────────────
def parse_report(content, date_label):
    """Parsear TSV y devolver métricas agregadas."""
    reader = csv.DictReader(io.StringIO(content), delimiter="\t")
    rows   = list(reader)

    by_market  = defaultdict(lambda: {
        "orders": set(), "units": 0, "revenue": 0.0,
        "currency": "€", "products": defaultdict(int), "statuses": defaultdict(int)
    })

    for r in rows:
        market   = r.get("sales-channel", "Desconocido")
        order_id = r.get("amazon-order-id", "")
        status   = r.get("order-status", "?")
        qty      = int(r.get("quantity", 0) or 0)
        sku      = r.get("sku", "?")
        product  = r.get("product-name", "?")[:60].strip()
        currency = r.get("currency", "€")

        try:    revenue = float(r.get("item-price", 0) or 0)
        except: revenue = 0.0

        m = by_market[market]
        m["orders"].add(order_id)
        m["units"]    += qty
        m["revenue"]  += revenue
        m["currency"]  = currency
        m["products"][f"{sku} — {product}"] += qty
        m["statuses"][status] += 1

    # Totales globales
    total_orders  = len({oid for m in by_market.values() for oid in m["orders"]})
    total_revenue = sum(m["revenue"] for m in by_market.values())
    total_units   = sum(m["units"]   for m in by_market.values())

    # Top productos global
    all_products = defaultdict(int)
    for m in by_market.values():
        for p, q in m["products"].items():
            all_products[p] += q

    return {
        "date":          date_label,
        "total_orders":  total_orders,
        "total_units":   total_units,
        "total_revenue": total_revenue,
        "by_market":     {k: {**v, "orders": len(v["orders"])} for k, v in by_market.items()},
        "top_products":  sorted(all_products.items(), key=lambda x: x[1], reverse=True)[:15],
        "raw_lines":     len(rows),
    }

def fmt(v, symbol="€"):
    try:    return f"{symbol}{float(v):,.2f}"
    except: return "—"

# ── Generador de página GBrain ─────────────────────────────────────────────────
def build_gbrain_page(data, ts):
    d    = data["date"]
    totO = data["total_orders"]
    totU = data["total_units"]
    totR = data["total_revenue"]

    lines = [
        "# Amazon Europa — Ventas Diarias", "",
        f"_Última sincronización: {ts}_",
        f"_Datos del día: {d}_", "",
        "## Resumen Global Europa", "",
        "| Métrica | Valor |", "|---|---|",
        f"| **Pedidos únicos** | {totO:,} |",
        f"| **Unidades vendidas** | {totU:,} |",
        f"| **Ingresos totales** | {fmt(totR)} |",
        f"| **Marketplaces con actividad** | {len(data['by_market'])} |",
        "",
        "## Desglose por Marketplace", "",
        "| Marketplace | Pedidos | Unidades | Ingresos | Moneda |",
        "|---|---|---|---|---|",
    ]

    for market, m in sorted(data["by_market"].items(),
                             key=lambda x: x[1]["orders"], reverse=True):
        lines.append(
            f"| {market} | {m['orders']:,} | {m['units']:,} | "
            f"{fmt(m['revenue'])} | {m['currency']} |"
        )

    lines += ["", "## Top 15 Productos del Día (unidades)", "",
              "| # | SKU / Producto | Uds |", "|---|---|---|"]
    for i, (p, q) in enumerate(data["top_products"], 1):
        lines.append(f"| {i} | {p} | {q} |")

    # Estado por marketplace
    lines += ["", "## Estados de Pedidos por Marketplace", ""]
    for market, m in sorted(data["by_market"].items()):
        if m["statuses"]:
            lines.append(f"**{market}:** " +
                " · ".join(f"{s}: {n}" for s, n in sorted(m["statuses"].items())))

    lines += [
        "", "## Notas técnicas", "",
        "- **Report type:** `GET_FLAT_FILE_ALL_ORDERS_DATA_BY_ORDER_DATE_GENERAL`",
        "- **Endpoint:** `https://sellingpartnerapi-eu.amazon.com`",
        "- **Marketplaces incluidos:** ES, UK, DE, FR, IT, NL, PL, SE, BE, TR, IE, AE, SA",
        "- **Ventaja:** 1 sola llamada cubre todos los marketplaces (Reports API)",
        f"- **Líneas raw en el report:** {data['raw_lines']}",
        f"- **Script:** `download_amazon_reports_auto.py`",
    ]
    return "\n".join(lines)

# ── GBrain helper ──────────────────────────────────────────────────────────────
def gbrain_put(slug, content, retries=3):
    body = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params":  {"name": "gbrain_put_page", "arguments": {"slug": slug, "content": content}}
    }).encode()
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                f"{HERMES_URL}/mcp", data=body, headers=MCP_HEADERS, method="POST")
            with urllib.request.urlopen(req, timeout=30) as r:
                resp = json.loads(r.read())
                if resp.get("error"):
                    raise RuntimeError(resp["error"])
                log(f"✓ GBrain ← {slug}")
                return True
        except Exception as ex:
            log(f"GBrain error ({attempt+1}): {ex}", "WARN")
            time.sleep(2 ** attempt)
    log(f"✗ GBrain FALLO {slug}", "ERROR")
    return False

# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Amazon Europa SP-API → GBrain")
    parser.add_argument("--days",  type=int,    default=1,    help="Días a fetchear (default: 1 = ayer)")
    parser.add_argument("--date",  type=str,    default=None, help="Fecha concreta YYYY-MM-DD")
    parser.add_argument("--no-gbrain", action="store_true",   help="Solo mostrar, no subir a GBrain")
    args = parser.parse_args()

    if loaded:
        log(f".env: {loaded}")

    log("=" * 60)
    log("AMAZON EUROPA → GBRAIN  |  Reports API  |  START")
    log(f"Hermes: {HERMES_URL}")
    log("=" * 60)

    # Calcular rango de fechas
    now = datetime.now(timezone.utc)
    if args.date:
        target = datetime.strptime(args.date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        start  = target.strftime("%Y-%m-%dT00:00:00Z")
        end    = target.strftime("%Y-%m-%dT23:59:59Z")
        label  = args.date
    else:
        days_back = args.days
        start = (now - timedelta(days=days_back)).strftime("%Y-%m-%dT00:00:00Z")
        end   = (now - timedelta(days=1)).strftime("%Y-%m-%dT23:59:59Z")
        label = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    log(f"Período: {start[:10]} → {end[:10]}")

    # 1. Auth
    token = get_access_token()

    # 2. Solicitar report (1 llamada, todos los marketplaces)
    report_id = request_report(token, start, end)

    # 3. Esperar a que esté listo (polling suave cada 20s)
    doc_id = wait_for_report(token, report_id, max_wait_s=300)
    if not doc_id:
        sys.exit(1)

    # 4. Descargar TSV desde S3 (sin rate limit)
    content = download_report(token, doc_id)
    if not content:
        sys.exit(1)

    # 5. Parsear y agregar
    data  = parse_report(content, label)
    ts    = now.strftime("%Y-%m-%d %H:%M UTC")
    page  = build_gbrain_page(data, ts)

    # Resumen en consola
    log("-" * 60)
    log(f"Pedidos únicos:    {data['total_orders']:,}")
    log(f"Unidades:          {data['total_units']:,}")
    log(f"Ingresos totales:  {fmt(data['total_revenue'])}")
    for market, m in sorted(data["by_market"].items(),
                             key=lambda x: x[1]["orders"], reverse=True):
        log(f"  {market:30} {m['orders']:4} pedidos | {m['currency']}{m['revenue']:,.2f}")
    log("-" * 60)

    # 6. Subir a GBrain
    if args.no_gbrain:
        log("--no-gbrain: mostrando página pero no subiendo")
        print("\n" + page)
    else:
        ok = gbrain_put("amazon-europa-ventas", page)
        elapsed = (datetime.now(timezone.utc) - RUN_START).total_seconds()
        log("=" * 60)
        log(f"SYNC {'✓ COMPLETADO' if ok else '✗ FALLIDO'} — {elapsed:.0f}s")
        log("=" * 60)
        if not ok:
            sys.exit(1)


if __name__ == "__main__":
    main()
