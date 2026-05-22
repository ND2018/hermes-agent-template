#!/usr/bin/env python3
"""
amazon_pl_usa.py
Naturdao / Body Nostrum -- P&L diario Amazon USA (replica metodologia Vendorati)

DIFERENCIAS vs Europa:
  - Endpoint: sellingpartnerapi-na.amazon.com
  - Marketplace: ATVPDKIKX0DER (Amazon.com USA)
  - Moneda: USD -> convertir a EUR (FX configurable)
  - VAT USA: 0% (no IVA federal; sales tax no aplica al seller)
  - Fees USA: referral ~15%, FBA fees en USD

CALIBRACION (pendiente de datos reales USA):
  - Se usan los mismos COGS en EUR convertidos a USD
  - FBA USA es mas barato que EU (~2.50 USD/ud para 1M/PLUS, ~3.80 USD para MAX)
  - Referral rate USA: 15% (sin Digital Services Fee)
  - Sin Input VAT on Fees en USA

USO:
  python amazon_pl_usa.py               # ayer
  python amazon_pl_usa.py --date 2026-05-14
  python amazon_pl_usa.py --days 7
  python amazon_pl_usa.py --no-gbrain   # solo mostrar, no subir
"""

import os, sys, json, csv, io, gzip, time, argparse
import urllib.request, urllib.error, urllib.parse
from datetime import datetime, timezone, timedelta
from collections import defaultdict

# ── .env ──────────────────────────────────────────────────────────────────────
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

CLIENT_ID     = _req("AMAZON_CLIENT_ID_USA")
CLIENT_SECRET = _req("AMAZON_CLIENT_SECRET_USA")
REFRESH_TOKEN = _req("AMAZON_REFRESH_TOKEN_USA")
HERMES_URL    = _req("HERMES_URL").rstrip("/")
MCP_KEY       = _req("MCP_KEY")

# ── Parametros USA ────────────────────────────────────────────────────────────
# FX: USD -> EUR (actualizar si cambio >3%)
USD_TO_EUR = 0.922

# COGS en USD — calibrado vs Vendorati 2026-05-14
# Vendorati mfg avg: $2.65/ud | ship intl avg: $0.81/ud
COGS_MFG_USD = {
    "1#1M":    2.32,   # Calibrado Vendorati (antes 3.03)
    "US1#1M":  2.32,
    "1#PLUS":  3.31,   # Calibrado Vendorati (antes 4.33)
    "US1#PLUS":3.31,
    "1#MAX":   2.99,   # Calibrado Vendorati (antes 3.91)
    "US1#MAX": 2.99,
}
COGS_DEFAULT_USD = 2.32

# Shipping internacional a USA — calibrado Vendorati 2026-05-14
INTL_SHIP_USD = 0.81   # USD/ud (antes 0.55, calibrado: $229.38/282ud)
LOCAL_SHIP_USD= 0.01   # USD/ud

# Fees Amazon USA — calibradas vs Vendorati 2026-05-14
# Referral efectivo Vendorati: 12.70% de gross sales (no 15%)
# FBA efectivo Vendorati: $3.48/ud promedio (antes calibracion Finances API: $3.86)
# Nota: Finances API mide settlements (+2-3 dias lag), Vendorati usa order date
REFERRAL_RATE_USA = 0.1270  # 12.70% efectivo Vendorati (antes 0.15)
FBA_FEE_USD = {
    "1#1M":    3.44,   # Calibrado Vendorati (antes 3.86 de Finances API)
    "US1#1M":  3.44,
    "1#PLUS":  3.44,   # Calibrado Vendorati (antes 3.86)
    "US1#PLUS":3.44,
    "1#MAX":   3.64,   # Calibrado Vendorati, premium leve por tamano (antes 4.09)
    "US1#MAX": 3.64,
}
FBA_DEFAULT_USD   = 3.44
DIGITAL_RATE_USA  = 0.0     # DigitalServicesFee insignificante para suplementos USA
VAT_RATIO_USA     = 0.0     # Sin IVA en fees para USA

# PPC / Advertising — coste real de publicidad Amazon Ads
# Se carga dinamicamente desde ads_spend_{date}.json (generado por amazon_ads_fetch.py)
# Fallback: valor fijo calibrado Vendorati 2026-05-14 ($986.41/dia)
PPC_DAILY_USD_DEFAULT = 986.41   # USD/dia fallback si no hay JSON de Ads
PPC_DAILY_USD         = PPC_DAILY_USD_DEFAULT  # se sobrescribe en main() si hay datos reales

def load_ppc_from_cache(date_label):
    """Lee gasto PPC USA del JSON generado por amazon_ads_fetch.py."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    cache_path = os.path.join(script_dir, f"ads_spend_{date_label}.json")
    if not os.path.exists(cache_path):
        log(f"Sin cache PPC para {date_label} -> usando fallback ${PPC_DAILY_USD_DEFAULT}", "WARN")
        return None
    try:
        with open(cache_path) as f:
            data = json.load(f)
        usa = data.get("USA", {})
        total_eur = usa.get("total_eur", 0)
        # Convertir EUR -> USD para mantener coherencia con el script
        total_usd = total_eur / USD_TO_EUR if USD_TO_EUR else total_eur
        source = usa.get("source", "?")
        log(f"PPC USA desde cache ({source}): EUR {total_eur:.2f} = USD {total_usd:.2f}")
        return total_usd
    except Exception as e:
        log(f"Error leyendo cache PPC: {e}", "WARN")
        return None

# NOTA USA vs Europa:
# - item-price en Orders Report = Principal SIN sales tax (al revés que EU donde incluye VAT)
# - NO hay que restar item-tax del revenue (el tax no está incluido en item-price)
# - Sales tax aparece en Finances API como +Tax (cobrado al cliente) pero el seller lo remite al estado
# - Revenue neto del seller = item-price + shipping-price + promos (sin ajuste de tax)

# ── Constantes SP-API USA ─────────────────────────────────────────────────────
LWA_URL          = "https://api.amazon.com/auth/o2/token"
SP_API_BASE_USA  = "https://sellingpartnerapi-na.amazon.com"
MARKETPLACE_USA  = ["ATVPDKIKX0DER"]   # Amazon.com USA
MCP_HEADERS      = {"Authorization": f"Bearer {MCP_KEY}", "Content-Type": "application/json"}
RUN_START        = datetime.now(timezone.utc)

def log(msg, level="INFO"):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{ts}] [{level}] {msg}", flush=True)

# ── Auth ──────────────────────────────────────────────────────────────────────
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
    log("Token LWA USA OK")
    return t

# ── SP-API ────────────────────────────────────────────────────────────────────
def sp_post(path, token, body_dict, retries=3):
    url = f"{SP_API_BASE_USA}{path}"
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
                log(f"HTTP {e.code}: {body[:200]}", "WARN"); time.sleep(2**attempt)
    return None

def sp_get(path, token, params=None, retries=3):
    qs  = ("?" + urllib.parse.urlencode(params)) if params else ""
    url = f"{SP_API_BASE_USA}{path}{qs}"
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

# ── Reports API USA ───────────────────────────────────────────────────────────
def get_orders_report(token, start_date, end_date):
    log(f"Solicitando Orders Report USA: {start_date[:10]} -> {end_date[:10]}")
    result = sp_post("/reports/2021-06-30/reports", token, {
        "reportType":     "GET_FLAT_FILE_ALL_ORDERS_DATA_BY_ORDER_DATE_GENERAL",
        "dataStartTime":  start_date,
        "dataEndTime":    end_date,
        "marketplaceIds": MARKETPLACE_USA,
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
    log(f"Report USA descargado: {len(rows)} lineas")
    return rows

# ── Calcular P&L USA ──────────────────────────────────────────────────────────
def calc_pl(rows, date_label):
    """P&L USA en EUR. Precios en USD -> convertir a EUR con USD_TO_EUR."""
    totals = {
        "units": 0, "sales_lines": 0,
        "sales_usd": 0.0, "sales_eur": 0.0, "refunds": 0.0,
        "cogs_mfg": 0.0, "cogs_ship": 0.0, "cogs_local": 0.0,
        "commission": 0.0, "fba": 0.0, "digital": 0.0,
    }
    by_product = defaultdict(lambda: {"units":0,"sales_usd":0.0,"sales_eur":0.0,"profit_eur":0.0})

    for r in rows:
        status = r.get("order-status","")
        if status == "Cancelled": continue

        sku = r.get("sku","").strip()
        qty = int(r.get("quantity",0) or 0)

        try:   price_usd  = float(r.get("item-price",0)              or 0)
        except: price_usd = 0.0
        try:   item_tax   = float(r.get("item-tax",0)                or 0)
        except: item_tax  = 0.0
        try:   ship_usd   = float(r.get("shipping-price",0)          or 0)
        except: ship_usd  = 0.0
        try:   ship_tax   = float(r.get("shipping-tax",0)            or 0)
        except: ship_tax  = 0.0
        try:   promo_usd  = float(r.get("item-promotion-discount",0)  or 0)
        except: promo_usd = 0.0
        try:   ship_promo = float(r.get("ship-promotion-discount",0)  or 0)
        except: ship_promo= 0.0

        # USA: no hay VAT federal. item-price es el precio que paga el cliente.
        # Sales tax es recaudado por Amazon (marketplace facilitator) y NO llega al seller.
        # item-tax = sales tax (no es coste del seller, Amazon lo retiene).
        # Revenue del seller = item-price (sin restar item-tax, pues Amazon ya lo separa).
        rev_usd = price_usd + ship_usd + promo_usd + ship_promo
        rev_eur = rev_usd * USD_TO_EUR

        totals["units"]       += qty
        totals["sales_lines"] += 1
        totals["sales_usd"]   += rev_usd
        totals["sales_eur"]   += rev_eur

        # Fees USA en USD -> EUR
        ref_usd = rev_usd * REFERRAL_RATE_USA
        fba_usd = FBA_FEE_USD.get(sku, FBA_DEFAULT_USD) * qty
        ref_eur = ref_usd * USD_TO_EUR
        fba_eur = fba_usd * USD_TO_EUR
        totals["commission"] -= ref_eur
        totals["fba"]        -= fba_eur

        # COGS en USD -> EUR
        mfg_usd = COGS_MFG_USD.get(sku, COGS_DEFAULT_USD)
        mfg_eur = mfg_usd * USD_TO_EUR
        totals["cogs_mfg"]   += mfg_eur * qty
        totals["cogs_ship"]  += INTL_SHIP_USD * USD_TO_EUR * qty
        totals["cogs_local"] += LOCAL_SHIP_USD * USD_TO_EUR * qty

        # Por producto
        item_cogs_eur = -(mfg_eur + INTL_SHIP_USD * USD_TO_EUR + LOCAL_SHIP_USD * USD_TO_EUR) * qty
        item_fees_eur = -(ref_eur + fba_eur)
        item_profit_eur = rev_eur + item_cogs_eur + item_fees_eur
        by_product[sku]["units"]      += qty
        by_product[sku]["sales_usd"]  += rev_usd
        by_product[sku]["sales_eur"]  += rev_eur
        by_product[sku]["profit_eur"] += item_profit_eur

    revenue_eur   = totals["sales_eur"] + totals["refunds"]
    total_cogs    = -(totals["cogs_mfg"] + totals["cogs_ship"] + totals["cogs_local"])
    total_fees    = totals["commission"] + totals["fba"]
    ppc_eur       = -PPC_DAILY_USD * USD_TO_EUR
    profit_preppc = revenue_eur + total_cogs + total_fees
    profit        = profit_preppc + ppc_eur
    margin        = profit / revenue_eur * 100 if revenue_eur else 0
    roi           = profit / abs(total_cogs) * 100 if total_cogs else 0

    return {
        "date": date_label, "units": totals["units"],
        "sales_lines": totals["sales_lines"],
        "sales_usd": totals["sales_usd"], "sales_eur": revenue_eur,
        "cogs_mfg": -totals["cogs_mfg"], "cogs_ship": -totals["cogs_ship"],
        "cogs_local": -totals["cogs_local"], "total_cogs": total_cogs,
        "commission": totals["commission"], "fba": totals["fba"],
        "total_fees": total_fees,
        "ppc_eur": ppc_eur, "ppc_usd": -PPC_DAILY_USD,
        "profit_preppc": profit_preppc,
        "profit": profit, "margin": margin, "roi": roi,
        "by_product": dict(sorted(by_product.items(),
                                   key=lambda x: x[1]["units"], reverse=True)),
    }

# ── GBrain ────────────────────────────────────────────────────────────────────
def gbrain_put(slug, content, retries=3):
    body = json.dumps({
        "jsonrpc":"2.0","id":1,"method":"tools/call",
        "params":{"name":"gbrain_put_page","arguments":{"slug":slug,"content":content}}
    }).encode()
    for attempt in range(retries):
        try:
            req = urllib.request.Request(f"{HERMES_URL}/mcp", data=body, headers=MCP_HEADERS, method="POST")
            with urllib.request.urlopen(req, timeout=30) as r:
                resp = json.loads(r.read())
                if resp.get("error"): raise RuntimeError(resp["error"])
                log(f"GBrain <- {slug}")
                return True
        except Exception as ex:
            log(f"GBrain error ({attempt+1}): {ex}", "WARN")
            time.sleep(2**attempt)
    log(f"GBrain FALLO {slug}", "ERROR")
    return False

def fmt_eur(v):
    try: return f"EUR {float(v):,.2f}"
    except: return "--"
def fmt_usd(v):
    try: return f"USD {float(v):,.2f}"
    except: return "--"

def build_page(d, ts):
    lines = [
        "# Amazon USA -- P&L Diario",
        "",
        "_Metodologia: Orders Report SP-API NA + fee estimation_",
        f"_Ultima sincronizacion: {ts}_",
        f"_Datos del dia: **{d['date']}**_",
        f"_FX aplicado: USD x{USD_TO_EUR} = EUR_",
        "",
        f"## P&L {d['date']}",
        "",
        "| Item | USD | EUR |",
        "|---|---|---|",
        f"| **Units** | {d['units']:,} | |",
        f"| **Sales (lineas)** | {d['sales_lines']:,} | |",
        f"| Revenue bruto | {fmt_usd(d['sales_usd'])} | {fmt_eur(d['sales_eur'])} |",
        f"| COGS manufacturing | | {fmt_eur(d['cogs_mfg'])} |",
        f"| COGS intl. shipping | | {fmt_eur(d['cogs_ship'])} |",
        f"| **COGS total** | | **{fmt_eur(d['total_cogs'])}** |",
        f"| Commission (referral {REFERRAL_RATE_USA*100:.2f}%) | | {fmt_eur(d['commission'])} |",
        f"| FBA Fulfillment Fees | | {fmt_eur(d['fba'])} |",
        f"| **Amazon Fees total** | | **{fmt_eur(d['total_fees'])}** |",
        f"| **Profit pre-PPC** | | **{fmt_eur(d['profit_preppc'])}** |",
        f"| PPC / Advertising | {fmt_usd(d['ppc_usd'])} | {fmt_eur(d['ppc_eur'])} |",
        f"| **PROFIT NETO** | | **{fmt_eur(d['profit'])}** |",
        f"| **MARGIN** | | **{d['margin']:.0f}%** |",
        f"| **ROI** | | **{d['roi']:.0f}%** |",
        "",
        "## Top Productos",
        "",
        "| SKU | Uds | Revenue USD | Revenue EUR | Profit EUR |",
        "|---|---|---|---|---|",
    ]
    for sku, pd in list(d["by_product"].items())[:10]:
        lines.append(f"| {sku} | {pd['units']} | {fmt_usd(pd['sales_usd'])} | {fmt_eur(pd['sales_eur'])} | {fmt_eur(pd['profit_eur'])} |")

    lines += [
        "",
        "## Notas metodologicas",
        "",
        "- **Revenue USA:** item-price (el seller recibe precio completo; sales tax es retenido por Amazon MF)",
        f"- **FX:** USD x{USD_TO_EUR} EUR (actualizar si cambio >3%)",
        f"- **Referral:** {REFERRAL_RATE_USA*100:.0f}% | FBA: USD {FBA_FEE_USD['1#1M']}/ud (1M/PLUS), USD {FBA_FEE_USD['1#MAX']}/ud (MAX)",
        "- **Sin VAT ni Digital Services Fee en USA**",
        f"- **Calibrado vs Vendorati 2026-05-14:** gap <1% | Referral {REFERRAL_RATE_USA*100:.2f}%, FBA ${FBA_DEFAULT_USD}/ud",
        f"- **PPC fijo:** USD {PPC_DAILY_USD:.2f}/dia (actualizar mensualmente con datos reales Ads)",
        "- Script: amazon_pl_usa.py",
    ]
    return "\n".join(lines)

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date",      type=str, default=None)
    parser.add_argument("--days",      type=int, default=1)
    parser.add_argument("--no-gbrain", action="store_true")
    args = parser.parse_args()

    if loaded: log(f".env: {loaded}")
    log("=" * 60)
    log("AMAZON USA P&L DIARIO  |  START")
    log(f"Hermes: {HERMES_URL}")
    log(f"FX: 1 USD = {USD_TO_EUR} EUR")
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

    # Cargar PPC real de Ads cache si existe
    global PPC_DAILY_USD
    ppc_from_cache = load_ppc_from_cache(label)
    if ppc_from_cache is not None:
        PPC_DAILY_USD = ppc_from_cache
    else:
        PPC_DAILY_USD = PPC_DAILY_USD_DEFAULT
    log(f"PPC USD/dia: ${PPC_DAILY_USD:.2f}")

    token = get_token()
    rows  = get_orders_report(token, start, end)
    data  = calc_pl(rows, label)
    ts    = now.strftime("%Y-%m-%d %H:%M UTC")
    page  = build_page(data, ts)

    log("-" * 60)
    log(f"Dia:       {label}")
    log(f"Units:     {data['units']:,}")
    log(f"Sales USD: {fmt_usd(data['sales_usd'])}")
    log(f"Sales EUR: {fmt_eur(data['sales_eur'])}")
    log(f"COGS EUR:  {fmt_eur(data['total_cogs'])}")
    log(f"Fees EUR:  {fmt_eur(data['total_fees'])}")
    log(f"PROFIT:    {fmt_eur(data['profit'])}  ({data['margin']:.0f}% margin | ROI {data['roi']:.0f}%)")
    log("-" * 60)

    if args.no_gbrain:
        print("\n" + page)
    else:
        ok = gbrain_put("amazon-usa-pl-diario", page)

        # ── Velocity update (reutilitza dades ja calculades, 0 downloads extra) ──
        velocity_update_amz(data["by_product"], label, "AMZ_USA")

        elapsed = (datetime.now(timezone.utc) - RUN_START).total_seconds()
        log("=" * 60)
   