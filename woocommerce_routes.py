"""
woocommerce_routes.py — B2B orders endpoint for Hermes.

GET /api/b2b-orders?after=YYYY-MM-DD&before=YYYY-MM-DD
  Queries the Railway PostgreSQL DB (internal host) for B2B orders.
  Returns JSON with 'orders' (filtered) and 'excluded' (bank transfer / Fedfarma).
"""

import json
import os
from datetime import date, timedelta

import psycopg2
import psycopg2.extras
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

# ── DB connection params (internal Railway host) ──────────────────────────────
_DB_PARAMS = {
    "host":     os.environ.get("PG_HOST",     "naturdao-postgres.railway.internal"),
    "port":     int(os.environ.get("PG_PORT", "5432")),
    "user":     os.environ.get("PG_USER",     "naturdao"),
    "password": os.environ.get("PG_PASSWORD", "Naturdao2026SecureDB!"),
    "dbname":   os.environ.get("PG_DATABASE", "naturdao"),
    "connect_timeout": 10,
}

_EXCLUDED_PM = {"bacs", "bank_transfer", "transferencia", "wire_transfer"}
_EXCLUDED_CUSTOMER = 3475  # Fedfarma

_CORS_HEADERS = {
    "Access-Control-Allow-Origin":  "*",
    "Access-Control-Allow-Methods": "GET, OPTIONS",
}

_SQL = """
SELECT
    order_id,
    MIN(date)::text                                                  AS date,
    MAX(order_total_eur)::float                                      AS amount,
    payment_method,
    customer_id,
    array_agg(DISTINCT sku) FILTER (WHERE sku IS NOT NULL AND sku != '') AS skus
FROM woo_orders
WHERE channel = 'B2B'
  AND date >= %(after)s::timestamp
  AND date < %(before)s::timestamp + interval '1 day'
  AND status NOT IN ('cancelled', 'failed', 'trash')
GROUP BY order_id, payment_method, customer_id
ORDER BY order_id ASC
"""


async def route_b2b_orders(request: Request) -> Response:
    """GET /api/b2b-orders  — returns B2B orders from Railway PostgreSQL."""

    # ── Handle OPTIONS preflight ──────────────────────────────────────────────
    if request.method == "OPTIONS":
        return Response(status_code=204, headers=_CORS_HEADERS)

    # ── Parse query params ────────────────────────────────────────────────────
    today = date.today()
    default_after = (today - timedelta(days=90)).isoformat()
    default_before = today.isoformat()

    after_str  = request.query_params.get("after",  default_after)
    before_str = request.query_params.get("before", default_before)

    try:
        date.fromisoformat(after_str)
        date.fromisoformat(before_str)
    except ValueError as exc:
        return JSONResponse(
            {"error": f"Invalid date format: {exc}"},
            status_code=400,
            headers=_CORS_HEADERS,
        )

    # ── Query DB ──────────────────────────────────────────────────────────────
    try:
        conn = psycopg2.connect(**_DB_PARAMS)
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(_SQL, {"after": after_str, "before": before_str})
                rows = cur.fetchall()
        finally:
            conn.close()
    except Exception as exc:
        return JSONResponse(
            {"error": f"DB error: {exc}"},
            status_code=500,
            headers=_CORS_HEADERS,
        )

    # ── Split into orders / excluded ──────────────────────────────────────────
    orders   = []
    excluded = []

    for row in rows:
        pm          = row.get("payment_method") or ""
        customer_id = row.get("customer_id")
        skus        = row.get("skus") or []

        entry = {
            "order_id":    row["order_id"],
            "date":        row["date"],
            "amount":      row["amount"],
            "pm":          pm,
            "customer_id": customer_id,
            "skus":        skus,
        }

        if pm.lower() in _EXCLUDED_PM or customer_id == _EXCLUDED_CUSTOMER:
            excluded.append(entry)
        else:
            orders.append(entry)

    payload = {
        "orders":       orders,
        "excluded":     excluded,
        "generated":    today.isoformat(),
        "queried_from": "railway_db",
    }

    return Response(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        media_type="application/json",
        headers=_CORS_HEADERS,
    )
