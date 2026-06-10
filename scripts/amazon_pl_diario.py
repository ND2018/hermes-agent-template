#!/usr/bin/env python3
"""
amazon_pl_diario.py
Naturdao / Body Nostrum -- P&L diario Amazon Europa (replica Vendorati)

METODOLOGIA (igual que Vendorati):
  1. Orders Report (purchase date = ayer) -> unidades, precio, SKU, marketplace
  2. Revenue ex-VAT usando FX + VAT por pais:
     - Shipped: item-tax disponible en el report -> restar directamente
     - Pending: item-tax vacio -> aplicar rate del pais (DE 7%, FR 5.5%, UK 20%...)
     - FX: GBP/SEK/PLN -> EUR al tipo de cambio configurado
  3. COGS internos (tabla por SKU) + shipping internacional calibrado
  4. Fees estimadas: Commission (15.36%) + FBA (tabla por SKU) + Digital (0.28%)
  5. Input VAT on Fees (ratio 17.38% calibrado con exports Vendorati)
  6. P&L disponible el mismo dia, sin esperar liquidacion Amazon

CALIBRACION (basada en exports Vendorati 14 mayo 2026):
  - Profit gap vs Vendorati: <0.2% (EUR 3004 vs 3005)
  - Commission: 15.36% del precio ex-VAT de venta
  - FBA 1#1M/1#PLUS: 3.65/ud | FBA 1#MAX: 5.30/ud
  - COGS/ud: 1#1M 2.79 | 1#PLUS 3.99 | 1#MAX 3.60
  - Intl. shipping: 0.40/ud | Local shipping: 0.0065/ud
  - Input VAT ratio: 17.38% sobre fees brutas

VARIABLES DE ENTORNO (en .env):
  AMAZON_CLIENT_ID_EUROPA, AMAZON_CLIENT_SECRET_EUROPA,
  AMAZON_REFRESH_TOKEN_EUROPA, HERMES_URL, MCP_KEY

USO:
  python amazon_pl_diario.py               # ayer
  python amazon_pl_diario.py --date 2026-05-14
  python amazon_pl_diario.py --days 7      # ultimos 7 dias agregados
  python amazon_pl_diario.py --no-gbrain   # solo mostrar, no subir
"""

import os, sys, json, csv, io, gzip, time, argparse
import urllib.request, urllib.error, urllib.parse
from datetime import datetime, timezone, timedelta
from collections import defaultdict

# ── .env ──────────────────────────────────────────────────────────────────────────────
def load_dotenv():
    for p in [os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"), ".env"]:
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

def _req(key):
    v = os.environ.get(key)
    if not v:
        print(f"[ERROR] Variable requerida no encontrada: {key}")
        sys.exit(1)
    return v

CLIENT_ID     = _req("AMAZON_CLIENT_ID_EUROPA")
CLIENT_SECRET = _req("AMAZON_CLIENT_SECRET_EUROPA")
REFRESH_TOKEN = _req("AMAZON_REFRESH_TOKEN_EUROPA")
HERMES_URL    = (os.environ.get("HERMES_URL") or "https://hermes-agent-template-production-fb9a.up.railway.app").rstrip("/")
MCP_KEY       = os.environ.get("MCP_KEY") or os.environ.get("MCP_API_KEY") or _req("MCP_KEY")

# ── GBrain Naturdao v0.41.2.0 ───────────────────────────────────────────────────────────────────
GBRAIN2_URL   = "https://gbrain-naturdao-production.up.railway.app"
def _get_gbrain_token():
    """Token GBrain fresc via OAuth client_credentials (fix healer 2026-06-10: el token estatic dona 401)."""
    _gu = os.environ.get("GBRAIN2_URL", "https://gbrain-naturdao-production.up.railway.app")
    _ci = os.environ.get("GBRAIN2_CLIENT_ID", "")
    _cs = os.environ.get("GBRAIN2_CLIENT_SECRET", "")
    if _ci and _cs:
        try:
            _b = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": _ci, "client_secret": _cs}).encode()
            _rq = urllib.request.Request(f"{_gu}/token", data=_b, headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
            with urllib.request.urlopen(_rq, timeout=15) as _r:
                _t = json.loads(_r.read()).get("access_token", "")
            if _t:
                return _t
        except Exception:
            pass
    return os.environ.get("GBRAIN2_TOKEN", "")

GBRAIN2_TOKEN = _get_gbrain_token()
if not GBRAIN2_TOKEN:
    raise SystemExit("FATAL: GBRAIN2_TOKEN env var not set — refusing to run without explicit credential. Set GBRAIN2_TOKEN in Railway env vars.")

def _gbrain2_put_page(slug, content, timeout=30):
    """Escriu una pagina al GBrain Naturdao v0.41.2.0 via MCP SSE."""
    body = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "put_page", "arguments": {"slug": slug, "content": content}}
    }).encode()
    req = urllib.request.Request(
        f"{GBRAIN2_URL}/mcp",
        data=body,
        headers={
            "Authorization": f"Bearer {GBRAIN2_TOKEN}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            for raw_line in r:
                line = raw_line.decode("utf-8").strip()
                if line.startswith("data:"):
                    return json.loads(line[5:])
    except Exception as e:
        print(f"[GBrain2] Error put_page {slug}: {e}", flush=True)
    return None

# ── Parametros calibrados con Vendorati ─────────────────────────────────────────────────────────────
# COGS manufacturing por SKU (EUR/unidad)
COGS_MFG = {
    "1#1M":    2.79,
    "US1#1M":  2.79,
    "1#PLUS":  3.99,
    "US1#PLUS":3.99,
    "1#MAX":   3.60,
    "US1#MAX": 3.60,
}
COGS_DEFAULT    = 2.79
INTL_SHIP_PER_U = 0.400   # EUR/ud
LOCAL_SHIP_PER_U= 0.0065  # EUR/ud

REFERRAL_RATE   = 0.1536  # 15.36% sobre precio ex-VAT
FBA_FEE = {
    "1#1M":    3.65,
    "US1#1M":  3.65,
    "1#PLUS":  3.65,
    "US1#PLUS":3.65,
    "1#MAX":   5.30,
    "US1#MAX": 5.30,
}
FBA_DEFAULT     = 3.65
DIGITAL_RATE    = 0.0028  # 0.28% Digital Services Fee
VAT_RATIO       = 0.1738  # Input VAT on fees / abs(fees)

# PPC / Advertising Europa
# Se carga dinamicamente desde ads_spend_{date}.json (generado por amazon_ads_fetch.py)
# Fallback: 0.0 (sin dato historico Europa calibrado aun)
PPC_DAILY_EUR_DEFAULT = 0.0
PPC_DAILY_EUR         = PPC_DAILY_EUR_DEFAULT

def load_ppc_from_cache_eu(date_label):
    """Lee gasto PPC Europa del JSON generado por amazon_ads_fetch.py."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    cache_path = os.path.join(script_dir, f"ads_spend_{date_label}.json")
    if not os.path.exists(cache_path):
        return None
    try:
        with open(cache_path) as f:
            data = json.load(f)
        eu = data.get("Europa", {})
        total_eur = eu.get("total_eur", 0)
        source = eu.get("source", "?")
        log(f"PPC Europa desde cache ({source}): EUR {total_eur:.2f}")
        return total_eur
    except Exception as e:
        log(f"Error leyendo cache PPC Europa: {e}", "WARN")
        return None

# Tipos de cambio a EUR (actualizar si cambio >5%, fuente: BCE)
FX_TO_EUR = {
    "EUR": 1.000,
    "GBP": 1.185,    # libra esterlina
    "SEK": 0.0875,   # corona sueca
    "PLN": 0.232,    # zloty polaco
    "USD": 0.922,    # dolar
    "CZK": 0.0415,   # corona checa
    "HUF": 0.00253,  # forinto hungaro
    "RON": 0.201,    # leu rumano
    "DKK": 0.134,    # corona danesa
    "NOK": 0.0883,   # corona noruega
    "CHF": 1.038,    # franco suizo
    "TRY": 0.0268,   # lira turca
}

# IVA por marketplace para suplemento DAO (calibrado vs Shipped orders)
# Solo se aplica a lineas Pending (item-tax vacio en el Orders Report)
VAT_BY_MARKET = {
    "Amazon.de":     0.07,   # Alemania: tipo reducido alimentacion
    "Amazon.fr":     0.055,  # Francia: tipo reducido suplementos
    "Amazon.it":     0.10,   # Italia: tipo reducido suplementos
    "Amazon.es":     0.10,   # Espana: tipo reducido suplementos
    "Amazon.nl":     0.09,   # Paises Bajos: tipo reducido BTW
    "Amazon.co.uk":  0.20,   # UK: tipo general
    "Amazon.pl":     0.08,   # Polonia: tipo reducido alimentacion
    "Amazon.se":     0.12,   # Suecia: tipo reducido alimentacion
    "Amazon.ie":     0.00,   # Irlanda: exento alimentacion
    "Amazon.com.be": 0.06,   # Belgica: tipo reducido
    "Amazon.tr":     0.10,   # Turquia
}

# ── Constantes SP-API ──────────────────────────────────────────────────────────────────────────────
LWA_URL     = "https://api.amazon.com/auth/o2/token"
SP_API_BASE = "https://sellingpartnerapi-eu.amazon.com"
MARKETPLACE_IDS = [
    "A1RKKUPIHCS9HS","A1F83G8C2ARO7P","A1PA6795UKMFR9","A13V1IB3VIYZZH",
    "APJ6JRA9NG5V4","A1805IZSGTT6HS","A1C3SOZRARQ6R3","A2NODRKZP88ZB9",
    "AMEN7PMS3EDWL","A33AVAJ2PDY3EV","A28R8C7NBKEWEA","A2VIGQ35RCS4UG",
]
MCP_HEADERS = {"Authorization": f"Bearer {MCP_KEY}", "Content-Type": "application/json"}
RUN_START   = datetime.now(timezone.utc)

# ── Logging ─────────────────────────────────────────────────────────────────────────────────
def log(msg, level="INFO"):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{ts}] [{level}] {msg}", flush=True)

# ── Auth ───────────────────────────────────────────────────────────────────────────────────
def get_token():
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token", "refresh_token": REFRESH_TOKEN,
        "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
    }).encode()
    req = urllib.request.Request(LWA_URL, data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "User-Agent": "GBrain-PL/1.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read())
    t = data.get("access_token")
    if not t:
        log(f"LWA error: {data}", "ERROR"); sys.exit(1)
    log("Token LWA OK")
    return t

# ── SP-API ────────────────────────────────────────────────────────────────────────────────────
def sp_post(path, token, body_dict, retries=3):
    url = f"{SP_API_BASE}{path}"
    headers = {"x-amz-access-token": token, "Accept": "application/json",
               "Content-Type": "application/json", "User-Agent": "GBrain-PL/1.0"}
    data = json.dumps(body_dict).encode()
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=data, method="POST", headers=headers)
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            if e.code == 429:
                wait = int(e.headers.get("Retry-After", 60))
                log(f"Rate limit -> {wait}s", "WARN"); time.sleep(wait)
            else:
                log(f"HTTP {e.code}: {body[:150]}", "WARN"); time.sleep(2**attempt)
    return None

def sp_get(path, token, params=None, retries=3):
    qs  = ("?" + urllib.parse.urlencode(params)) if params else ""
    url = f"{SP_API_BASE}{path}{qs}"
    headers = {"x-amz-access-token": token, "Accept": "application/json",
               "User-Agent": "GBrain-PL/1.0"}
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            if e.code == 429:
                wait = int(e.headers.get("Retry-After", 60))
                log(f"Rate limit -> {wait}s", "WARN"); time.sleep(wait)
            elif e.code in (400, 403, 404):
                log(f"HTTP {e.code} {path}: {body[:150]}", "WARN"); return None
            else:
                log(f"HTTP {e.code} (intento {attempt+1})", "WARN"); time.sleep(2**attempt)
    return None

# ── Reports API ─────────────────────────────────────────────────────────────────────────────────
def get_orders_report(token, start_date, end_date):
    log(f"Solicitando Orders Report: {start_date[:10]} -> {end_date[:10]}")
    result = sp_post("/reports/2021-06-30/reports", token, {
        "reportType":     "GET_FLAT_FILE_ALL_ORDERS_DATA_BY_ORDER_DATE_GENERAL",
        "dataStartTime":  start_date,
        "dataEndTime":    end_date,
        "marketplaceIds": MARKETPLACE_IDS,
    })
    if not result:
        log("Error solicitando report", "ERROR"); sys.exit(1)
    report_id = result["reportId"]
    log(f"  reportId: {report_id} -- esperando...")

    for i in range(20):
        time.sleep(15)
        r = sp_get(f"/reports/2021-06-30/reports/{report_id}", token)
        if not r: continue
        status = r.get("processingStatus", "IN_QUEUE")
        log(f"  [{i+1}] {status}")
        if status == "DONE":
            doc_id = r["reportDocumentId"]
            break
        elif status in ("FATAL", "CANCELLED"):
            log(f"Report fallo: {status}", "ERROR"); sys.exit(1)
    else:
        log("Timeout esperando report", "ERROR"); sys.exit(1)

    doc = sp_get(f"/reports/2021-06-30/documents/{doc_id}", token)
    if not doc:
        log("Error obteniendo documento", "ERROR"); sys.exit(1)
    url = doc["url"]
    compressed = doc.get("compressionAlgorithm") == "GZIP"
    req = urllib.request.Request(url, headers={"User-Agent": "GBrain-PL/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read()
    content = gzip.decompress(raw).decode("utf-8", errors="replace") if compressed else raw.decode("utf-8", errors="replace")
    rows = list(csv.DictReader(io.StringIO(content), delimiter="\t"))
    log(f"Report descargado: {len(rows)} lineas")
    return rows

# ── Calcular P&L ──────────────────────────────────────────────────────────────────────────────────
def calc_pl(rows, date_label):
    """Replica la metodologia de Vendorati. Profit gap <0.2% calibrado."""
    totals = {
        "units": 0, "sales_lines": 0,
        "sales": 0.0, "refunds": 0.0,
        "cogs_mfg": 0.0, "cogs_ship": 0.0, "cogs_local": 0.0,
        "commission": 0.0, "fba": 0.0, "digital": 0.0,
    }
    by_market  = defaultdict(lambda: {"units":0,"sales":0.0,"commission":0.0,"fba":0.0,"cogs":0.0,"vat":0.0,"profit":0.0})
    by_product = defaultdict(lambda: {"units":0,"sales":0.0,"profit":0.0})

    for r in rows:
        status = r.get("order-status","")
        if status == "Cancelled": continue

        sku    = r.get("sku","").strip()
        qty    = int(r.get("quantity",0) or 0)
        market = r.get("sales-channel","?")
        cur    = (r.get("currency","") or "EUR").strip() or "EUR"
        fx     = FX_TO_EUR.get(cur, 1.0)
        vat    = VAT_BY_MARKET.get(market, 0.10)

        try:   principal_nat  = float(r.get("item-price",0)              or 0)
        except: principal_nat = 0.0
        try:   item_tax_nat   = float(r.get("item-tax",0)                or 0)
        except: item_tax_nat  = 0.0
        try:   shipping_nat   = float(r.get("shipping-price",0)          or 0)
        except: shipping_nat  = 0.0
        try:   ship_tax_nat   = float(r.get("shipping-tax",0)            or 0)
        except: ship_tax_nat  = 0.0
        try:   promo_nat      = float(r.get("item-promotion-discount",0)  or 0)
        except: promo_nat     = 0.0
        try:   ship_promo_nat = float(r.get("ship-promotion-discount",0)  or 0)
        except: ship_promo_nat= 0.0

        # Convertir a EUR
        principal   = principal_nat  * fx
        shipping    = shipping_nat   * fx
        promo       = promo_nat      * fx
        ship_promo  = ship_promo_nat * fx

        # Revenue ex-VAT (= Principal en Vendorati)
        # Shipped: item-tax exacto disponible
        # Pending: item-tax vacio -> aplicar VAT rate del pais
        if item_tax_nat > 0:
            item_revenue = (principal - item_tax_nat * fx) + (shipping - ship_tax_nat * fx) + promo + ship_promo
        else:
            item_revenue = principal / (1 + vat) + shipping / (1 + vat) + promo + ship_promo

        totals["units"]       += qty
        totals["sales_lines"] += 1
        totals["sales"]       += item_revenue

        # Fees
        ref_fee = item_revenue * REFERRAL_RATE
        fba_fee = FBA_FEE.get(sku, FBA_DEFAULT) * qty
        dig_fee = item_revenue * DIGITAL_RATE
        totals["commission"] -= ref_fee
        totals["fba"]        -= fba_fee
        totals["digital"]    -= dig_fee

        # COGS
        mfg = COGS_MFG.get(sku, COGS_DEFAULT)
        totals["cogs_mfg"]   += mfg * qty
        totals["cogs_ship"]  += INTL_SHIP_PER_U * qty
        totals["cogs_local"] += LOCAL_SHIP_PER_U * qty

        # Por marketplace / producto
        item_fees   = -(ref_fee + fba_fee + dig_fee)
        item_cogs   = -(mfg + INTL_SHIP_PER_U + LOCAL_SHIP_PER_U) * qty
        item_vat    = abs(ref_fee + fba_fee + dig_fee) * VAT_RATIO
        item_profit = item_revenue + item_cogs + item_fees + item_vat

        by_market[market]["units"]      += qty
        by_market[market]["sales"]      += item_revenue
        by_market[market]["commission"] += ref_fee
        by_market[market]["fba"]        += fba_fee
        by_market[market]["cogs"]       += abs(item_cogs)
        by_market[market]["vat"]        += item_vat
        by_market[market]["profit"]     += item_profit

        by_product[sku]["units"]  += qty
        by_product[sku]["sales"]  += item_revenue
        by_product[sku]["profit"] += item_profit

    revenue_net = totals["sales"] + totals["refunds"]
    total_cogs   = -(totals["cogs_mfg"] + totals["cogs_ship"] + totals["cogs_local"])
    total_fees   = totals["commission"] + totals["fba"] + totals["digital"]
    vat_on_fees  = abs(total_fees) * VAT_RATIO
    profit_preppc= revenue_net + total_cogs + total_fees + vat_on_fees
    ppc_eur      = -PPC_DAILY_EUR
    profit       = profit_preppc + ppc_eur
    margin       = profit / revenue_net * 100 if revenue_net else 0
    roi          = profit / abs(total_cogs) * 100 if total_cogs else 0

    return {
        "date": date_label, "units": totals["units"],
        "sales_lines": totals["sales_lines"], "refund_lines": 0,
        "sales": totals["sales"], "refunds": totals["refunds"],
        "revenue": revenue_net,
        "cogs_mfg": -totals["cogs_mfg"], "cogs_ship": -totals["cogs_ship"],
        "cogs_local": -totals["cogs_local"], "total_cogs": total_cogs,
        "commission": totals["commission"], "fba": totals["fba"],
        "digital": totals["digital"], "total_fees": total_fees,
        "vat_on_fees": vat_on_fees,
        "ppc_eur": ppc_eur, "profit_preppc": profit_preppc,
        "profit": profit, "margin": margin, "roi": roi,
        "by_market": dict(by_market),
        "by_product": dict(sorted(by_product.items(),
                                   key=lambda x: x[1]["units"], reverse=True)),
    }

# ── GBrain (escriu al nou GBrain Naturdao v0.41.2.0) ───────────────────────────────────────────────────────
def gbrain_put(slug, content, retries=3):
    for attempt in range(retries):
        try:
            result = _gbrain2_put_page(slug, content)
            if result is not None:
                log(f"GBrain2 <- {slug}")
                return True
            raise RuntimeError("resposta buida")
        except Exception as ex:
            log(f"GBrain2 error ({attempt+1}): {ex}", "WARN")
            time.sleep(2**attempt)
    log(f"GBrain2 FALLO {slug}", "ERROR")
    return False

# ── Formateo ─────────────────────────────────────────────────────────────────────────────────────
def fmt(v, s="EUR "):
    try: return f"{s}{float(v):,.2f}"
    except: return "--"

def build_page(d, ts):
    p = d["profit"]; rv = d["revenue"]; m = d["margin"]; roi = d["roi"]
    lines = [
        "# Amazon Europa -- P&L Diario",
        "",
        f"_Metodologia: replica Vendorati (Orders Report + fee estimation)_",
        f"_Ultima sincronizacion: {ts}_",
        f"_Datos del dia: **{d['date']}**_",
        "",
        f"## P&L {d['date']}",
        "",
        "| Item | Valor |", "|---|---|",
        f"| **Units** | {d['units']:,} |",
        f"| **Sales (lineas)** | {d['sales_lines']:,} |",
        "| | |",
        f"| Revenue (ventas brutas) | {fmt(d['sales'])} |",
        f"| Refunds | {fmt(d['refunds'])} |",
        f"| **Revenue neto** | **{fmt(d['revenue'])}** |",
        "| | |",
        f"| COGS manufacturing | {fmt(d['cogs_mfg'])} |",
        f"| COGS intl. shipping | {fmt(d['cogs_ship'])} |",
        f"| COGS local shipping | {fmt(d['cogs_local'])} |",
        f"| **COGS total** | **{fmt(d['total_cogs'])}** |",
        "| | |",
        f"| Commission (referral) | {fmt(d['commission'])} |",
        f"| FBA Fulfillment Fees | {fmt(d['fba'])} |",
        f"| Digital Services Fee | {fmt(d['digital'])} |",
        f"| **Amazon Fees total** | **{fmt(d['total_fees'])}** |",
        "| | |",
        f"| Input VAT on Fees | {fmt(d['vat_on_fees'])} |",
        "| | |",
        f"| **Profit pre-PPC** | **{fmt(d.get('profit_preppc', p))}** |",
        f"| PPC / Advertising | {fmt(d.get('ppc_eur', 0.0))} |",
        "| | |",
        f"| **PROFIT NETO** | **{fmt(p)}** |",
        f"| **MARGIN** | **{m:.0f}%** |",
        f"| **ROI** | **{roi:.0f}%** |",
        "",
        "## Desglose por Marketplace",
        "",
        "| Marketplace | Uds | Revenue | Fees | COGS | Profit | Margen |",
        "|---|---|---|---|---|---|---|",
    ]
    for market, md in sorted(d["by_market"].items(), key=lambda x: x[1]["sales"], reverse=True):
        item_fees = -(md["commission"]+md["fba"])
        pct = md["profit"]/md["sales"]*100 if md["sales"] else 0
        lines.append(
            f"| {market} | {md['units']} | {fmt(md['sales'])} | "
            f"{fmt(item_fees)} | {fmt(-md['cogs'])} | {fmt(md['profit'])} | {pct:.0f}% |"
        )

    lines += [
        "",
        "## Top Productos del Dia",
        "",
        "| SKU | Uds | Revenue | Profit |",
        "|---|---|---|---|",
    ]
    for sku, pd in list(d["by_product"].items())[:10]:
        lines.append(f"| {sku} | {pd['units']} | {fmt(pd['sales'])} | {fmt(pd['profit'])} |")

    lines += [
        "",
        "## Notas metodologicas",
        "",
        f"- **Fuente revenue:** Orders Report SP-API (purchase date = {d['date']})",
        f"- **Fees:** Commission {REFERRAL_RATE*100:.2f}% + FBA {FBA_FEE['1#1M']}/ud (1M/PLUS) {FBA_FEE['1#MAX']}/ud (MAX)",
        f"- **COGS:** 1#1M {COGS_MFG['1#1M']} | 1#PLUS {COGS_MFG['1#PLUS']} | 1#MAX {COGS_MFG['1#MAX']} + {INTL_SHIP_PER_U:.3f}/ud shipping",
        "- **FX:** GBP x1.185, SEK x0.0875, PLN x0.232, EUR x1.0 (actualizar si cambio >5%)",
        "- **VAT por pais:** Shipped=item-tax exacto; Pending=DE 7%, FR 5.5%, NL 9%, UK 20%, IT/ES 10%, PL 8%, SE 12%",
        "- **Calibracion vs Vendorati:** profit gap <0.2% (testado 14-may-2026: 3004 vs 3005 EUR)",
        "- **Reconciliacion exacta:** disponible via Finances API 2-3 dias despues",
        "- Script: amazon_pl_diario.py",
    ]
    return "\n".join(lines)

# ── Main ───────────────────────────────────────────────────────────────────────────────────────

def velocity_update_amz(by_product, label, channel):
    """Afegeix les ventes Amazon del dia `label` (YYYY-MM-DD) a GBrain2 velocity-data.

    Idempotent: si el dia ja s'ha processat per aquest canal, fa skip.
    Seed-aware: dies anteriors al meta.max_date inicial es consideren coberts
    pel seed manual (no es sumen, nomes es marquen com processats).

    Ara escriu DIRECTAMENT a GBrain2 (ja no al JSON de Hermes /api/velocity).

    Args:
        by_product: dict {SKU: {units, sales, profit}}
        label: str "YYYY-MM-DD"
        channel: "AMZ_EU" o "AMZ_USA"
    """
    month = label[:7]
    try:
        # Llegir JSON actual des de GBrain2 (slug: velocity-data)
        result = _gbrain2_put_page("velocity-data", json.dumps({"__ping__": True}))
        d = {}
        if result and "result" in result:
            # get_page via GBrain2 MCP
            get_body = json.dumps({
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "get_page", "arguments": {"slug": "velocity-data"}}
            }).encode()
            get_req = urllib.request.Request(
                f"{GBRAIN2_URL}/mcp",
                data=get_body,
                headers={
                    "Authorization": f"Bearer {GBRAIN2_TOKEN}",
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                },
                method="POST"
            )
            try:
                with urllib.request.urlopen(get_req, timeout=20) as r:
                    for raw_line in r:
                        line = raw_line.decode("utf-8").strip()
                        if line.startswith("data:"):
                            gr = json.loads(line[5:])
                            if "result" in gr:
                                ct = gr["result"].get("content", [])
                                for c in ct:
                                    if isinstance(c, dict) and c.get("type") == "text":
                                        text = c["text"]
                                        # El text conte frontmatter YAML + compiled_truth
                                        # El JSON esta dins d'un fenc de codi ```json ... ```
                                        import re as _re
                                        m = _re.search(r'```json\n(.+?)\n```', text, _re.DOTALL)
                                        if m:
                                            d = json.loads(m.group(1))
                                        elif text.startswith("{"):
                                            d = json.loads(text)
            except Exception as e:
                log(f"get_page velocity-data: {e} (creant nou)", "INFO")
                d = {}
        if not d or not isinstance(d, dict):
            d = {}
        # Inicialitzar estructures
        d.setdefault("processed_days", {})
        d["processed_days"].setdefault(channel, [])
        d.setdefault("MONTHS", [])
        d.setdefault("SKU_DATA", {})
        d.setdefault("meta", {})
        # Idempotencia 1: dia ja processat -> skip
        if label in d["processed_days"][channel]:
            log(f"velocity_update_amz {channel} {label}: ja processat (skip)")
            return
        # Idempotencia 2: dia cobert pel seed inicial
        seed_max = d["meta"].get("max_date", "")
        is_seed_covered = (
            label <= seed_max and not d["processed_days"][channel]
        )
        if is_seed_covered:
            # Nomes marcar com processat, NO sumar (ja esta al seed)
            d["processed_days"][channel].append(label)
            log(f"velocity_update_amz {channel} {label}: cobert pel seed (skip sum)")
        else:
            # Sumar units per SKU al mes
            if month not in d["MONTHS"]:
                d["MONTHS"].append(month)
                d["MONTHS"].sort()
            added = 0
            for sku, info in by_product.items():
                units = int(info.get("units", 0) or 0) if isinstance(info, dict) else 0
                if units <= 0:
                    continue
                d["SKU_DATA"].setdefault(sku, {}).setdefault(channel, {})
                prev = d["SKU_DATA"][sku][channel].get(month, 0)
                d["SKU_DATA"][sku][channel][month] = prev + units
                added += units
            d["processed_days"][channel].append(label)
            log(f"velocity_update_amz {channel} {label}: +{added} units a {month}")
        d["processed_days"][channel].sort()
        if label > d["meta"].get("max_date", ""):
            d["meta"]["max_date"] = label
        d["meta"]["updated"] = label
        d["meta"]["source"] = "gbrain2-velocity-data"
        # Escriure a GBrain2 (el JSON va dins d'un bloc ```json)
        content = (
            "---\ntype: concept\ntitle: Velocity Data (Dashboard)\n"
            "updated: " + label + "\n---\n\n"
            "# Velocity Data — Dashboard\n\n"
            "> Dades agregades de vendes per SKU + canal + mes.\n"
            "> Alimenta el dashboard de velocitat.\n"
            "> Actualitzat automaticament cada nit pels scripts P&L.\n\n"
            "```json\n" + json.dumps(d, ensure_ascii=False, indent=2) + "\n```\n"
        )
        result = _gbrain2_put_page("velocity-data", content)
        if result is not None:
            log(f"velocity_data GBrain2 OK: slug=velocity-data {channel} {label}")
        else:
            log(f"velocity_data GBrain2 WARN: resposta buida", "WARN")
    except Exception as e:
        log(f"velocity_update_amz ERROR: {e}", "WARN")


def velocity_update_gbrain():
    """Llegeix el JSON de velocitat de Hermes, calcula resums 30/60/90d i escriu a GBrain2 velocity-resum."""
    import datetime, calendar as _cal
    VELOCITY_URL = f"{HERMES_URL}/api/velocity"
    TOKEN        = MCP_KEY
    CANAL_ORDER  = ["AMZ_EU","AMZ_USA","WEB_B2C","B2B","MAJORISTA_NATURITAS"]
    CANAL_LABELS = {"AMZ_EU":"Amazon EU","AMZ_USA":"Amazon USA","WEB_B2C":"Web B2C","B2B":"B2B","MAJORISTA_NATURITAS":"Majorista Naturitas"}
    SKU_ORDER    = ["1#1M","1#3M","1#PLUS","1#MAX","US1#1M","US1#PLUS","US1#MAX"]
    try:
        # Llegir directament de GBrain2 (slug: velocity-data); fallback a Hermes /api/velocity
        import re as _re
        d = {}
        try:
            get_body = json.dumps({
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "get_page", "arguments": {"slug": "velocity-data"}}
            }).encode()
            get_req = urllib.request.Request(
                f"{GBRAIN2_URL}/mcp",
                data=get_body,
                headers={
                    "Authorization": f"Bearer {GBRAIN2_TOKEN}",
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                },
                method="POST"
            )
            with urllib.request.urlopen(get_req, timeout=20) as r:
                for raw_line in r:
                    line = raw_line.decode("utf-8").strip()
                    if line.startswith("data:"):
                        gr = json.loads(line[5:])
                        if "result" in gr:
                            for c in gr["result"].get("content", []):
                                if isinstance(c, dict) and c.get("type") == "text":
                                    # c["text"] es el page OBJECT JSON-stringified.
                                    # Cal parsejar-lo i extreure compiled_truth, on hi ha el ```json block.
                                    try:
                                        _page = json.loads(c["text"])
                                        _ct = _page.get("compiled_truth", "") if isinstance(_page, dict) else ""
                                    except Exception:
                                        _page, _ct = None, c["text"]
                                    m = _re.search(r'```json\s*({.*?})\s*```', _ct, _re.DOTALL)
                                    if m:
                                        d = json.loads(m.group(1))
                                    elif _ct.lstrip().startswith("{"):
                                        d = json.loads(_ct)
        except Exception as e:
            log(f"velocity_update_gbrain GBrain2 read: {e} — fallback Hermes", "WARN")
            d = {}
        if not d or not isinstance(d, dict) or not d.get("MONTHS"):
            log("velocity_update_gbrain: no data a GBrain2, fallback a Hermes /api/velocity", "WARN")
            req = urllib.request.Request(f"{VELOCITY_URL}?token={TOKEN}", headers={"Cache-Control":"no-cache"})
            with urllib.request.urlopen(req, timeout=20) as r:
                d = json.loads(r.read())
        SKU_DATA = d.get("SKU_DATA", {})
        MONTHS   = sorted(d.get("MONTHS", []))
        updated  = d.get("meta", {}).get("updated", str(datetime.date.today()))
        if not MONTHS:
            log("velocity_update_gbrain: sense dades, skip", "WARN"); return
        lm = MONTHS[-1]
        y, mo = int(lm[:4]), int(lm[5:7])
        max_date = min(datetime.date(y, mo, _cal.monthrange(y, mo)[1]), datetime.date.today())
        def gq(sku, canal, m): return SKU_DATA.get(sku,{}).get(canal,{}).get(m,0)
        def compute(days):
            start = max_date - datetime.timedelta(days=days-1)
            tc, ts = {c:0 for c in CANAL_ORDER}, {s:0 for s in SKU_ORDER}
            for m in MONTHS:
                my,mm = int(m[:4]),int(m[5:7])
                ms=datetime.date(my,mm,1); me=datetime.date(my,mm,_cal.monthrange(my,mm)[1])
                if ms>max_date or me<start: continue
                frac=(min(me,max_date)-max(ms,start)).days+1
                avail=(me-ms).days+1
                f=frac/avail if avail>0 else 0
                for s in SKU_ORDER:
                    for c in CANAL_ORDER:
                        q=round(gq(s,c,m)*f); tc[c]+=q; ts[s]+=q
            return tc, ts
        w30c,w30s=compute(30); w60c,w60s=compute(60); w90c,w90s=compute(90)
        t30,t60,t90=sum(w30c.values()),sum(w60c.values()),sum(w90c.values())
        def tbl_c(wc,tot):
            rows=["| Canal | Unitats | % |","|---|---:|---:|"]  
            for c in CANAL_ORDER:
                v=wc[c]; rows.append(f"| {CANAL_LABELS[c]} | {v:,} | {v/tot*100:.0f}% |" if tot else f"| {CANAL_LABELS[c]} | {v:,} | 0% |")
            rows.append(f"| **TOTAL** | **{tot:,}** | **100%** |"); return "\n".join(rows)
        def tbl_s(ws):
            rows=["| SKU | Unitats |","|---|---:|"]
            for s in SKU_ORDER:
                if ws[s]>0: rows.append(f"| `{s}` | {ws[s]:,} |")
            return "\n".join(rows)
        recent=MONTHS[-4:] if len(MONTHS)>=4 else MONTHS
        hdr="| Canal | "+" | ".join(m[5:]+"/"+m[2:4] for m in recent)+" |"
        sep="|---|"+"---:|"*len(recent)
        rows=[hdr,sep]
        for c in CANAL_ORDER:
            row=f"| {CANAL_LABELS[c]} |"
            for m in recent: row+=f" {sum(gq(s,c,m) for s in SKU_ORDER):,} |"
            rows.append(row)
        hdr2="| SKU | "+" | ".join(m[5:]+"/"+m[2:4] for m in recent)+" |"
        rows2=[hdr2,sep]
        for s in SKU_ORDER:
            if sum(sum(gq(s,c,m) for c in CANAL_ORDER) for m in recent)==0: continue
            row=f"| `{s}` |"
            for m in recent: row+=f" {sum(gq(s,c,m) for c in CANAL_ORDER):,} |"
            rows2.append(row)
        content=(
            f"# Velocity Vendes -- Resum\n\n"
            f"**Ultima actualitzacio**: {updated}\n"
            f"**Rang de dades**: {MONTHS[0]} -> {MONTHS[-1]}\n\n---\n\n"
            f"## Ultims 30 dies - {t30:,} unitats\n\n{tbl_c(w30c,t30)}\n\n{tbl_s(w30s)}\n\n---\n\n"
            f"## Ultims 60 dies - {t60:,} unitats\n\n{tbl_c(w60c,t60)}\n\n---\n\n"
            f"## Ultims 90 dies - {t90:,} unitats\n\n{tbl_c(w90c,t90)}\n\n---\n\n"
            "## Historial mensual per canal\n\n" + chr(10).join(rows) + "\n\n"
            "## Historial mensual per SKU\n\n" + chr(10).join(rows2) + "\n\n"
            f"*Actualitzat automaticament cada nit per Railway*\n"
        )
        # Escriure al nou GBrain Naturdao v0.41.2.0
        result = _gbrain2_put_page("velocity-resum", content)
        if result is not None:
            log(f"GBrain2 velocity-resum OK: {t30:,}u/30d - {t60:,}u/60d - {t90:,}u/90d")
        else:
            log("GBrain2 velocity-resum: resposta buida (pot ser OK si SSE)", "WARN")
    except Exception as e:
        log(f"velocity_update_gbrain ERROR: {e}", "WARN")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date",      type=str, default=None)
    parser.add_argument("--days",      type=int, default=1)
    parser.add_argument("--no-gbrain", action="store_true")
    args = parser.parse_args()

    if loaded: log(f".env: {loaded}")
    log("=" * 60)
    log("AMAZON EUROPA P&L DIARIO  |  START")
    log(f"Hermes: {HERMES_URL}")
    log(f"GBrain2: {GBRAIN2_URL}")
    log("=" * 60)

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

    # Cargar PPC Europa real desde cache de Ads si existe
    global PPC_DAILY_EUR
    ppc_from_cache = load_ppc_from_cache_eu(label)
    if ppc_from_cache is not None:
        PPC_DAILY_EUR = ppc_from_cache
    else:
        PPC_DAILY_EUR = PPC_DAILY_EUR_DEFAULT
    log(f"PPC Europa EUR/dia: {PPC_DAILY_EUR:.2f}")

    token = get_token()
    rows  = get_orders_report(token, start, end)
    data  = calc_pl(rows, label)
    ts    = now.strftime("%Y-%m-%d %H:%M UTC")
    page  = build_page(data, ts)

    log("-" * 60)
    log(f"Dia:       {label}")
    log(f"Units:     {data['units']:,}")
    log(f"Revenue:   {fmt(data['revenue'])}")
    log(f"COGS:      {fmt(data['total_cogs'])}")
    log(f"Fees:      {fmt(data['total_fees'])}")
    log(f"VAT:       {fmt(data['vat_on_fees'])}")
    log(f"PPC:       {fmt(data.get('ppc_eur', 0.0))}")
    log(f"PROFIT:    {fmt(data['profit'])}  ({data['margin']:.0f}% margin | ROI {data['roi']:.0f}%)")
    log("-" * 60)

    if args.no_gbrain:
        print("\n" + page)
    else:
        ok = gbrain_put("amazon-europa-pl-diario", page)

        # ── Velocity update (reutilitza dades ja calculades, 0 downloads extra) ──
        velocity_update_amz(data["by_product"], label, "AMZ_EU")
        try:
            velocity_update_gbrain()
        except Exception as ve:
            log(f"velocity_update_gbrain error (no critic): {ve}", "WARN")

        elapsed = (datetime.now(timezone.utc) - RUN_START).total_seconds()
        log("=" * 60)
        log(f"SYNC COMPLETAT en {elapsed:.0f}s")
        log("=" * 60)


def _send_telegram_alert(msg):
    """Helper local per alerta Telegram."""
    try:
        import urllib.request as _ur
        tok  = os.environ.get("TELEGRAM_TOKEN", "")
        chat = os.environ.get("TELEGRAM_CHAT_ID", "")
        if not tok or not chat:
            return
        body = json.dumps({"chat_id": chat, "text": msg, "parse_mode": "HTML"}).encode()
        req = _ur.Request(
            "https://api.telegram.org/bot" + tok + "/sendMessage",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with _ur.urlopen(req, timeout=10):
            pass
    except Exception:
        pass


if __name__ == "__main__":
    import traceback as _tb
    try:
        main()
        sys.exit(0)
    except SystemExit:
        sys.exit(0)
    except BaseException as _e:
        _trace = _tb.format_exc()
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        print("[" + ts + "] [ERROR] CRASH inesperat: " + str(_e), flush=True)
        print(_trace, flush=True)
        try:
            short_tb = _trace[-500:] if len(_trace) > 500 else _trace
            _send_telegram_alert(
                "<b>" + __file__.split("/")[-1] + " EXCEPCIO</b>\n" +
                "<code>" + type(_e).__name__ + ": " + str(_e)[:200] + "</code>\n\n" +
                "<code>" + short_tb + "</code>"
            )
        except Exception:
            pass
        sys.exit(0)
