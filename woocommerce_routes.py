"""
WooCommerce B2B orders endpoint for Hermes server.

GET /api/b2b-orders?after=YYYY-MM-DD&before=YYYY-MM-DD

Returns B2B orders queried directly from Railway PostgreSQL DB,
filtered by channel='B2B', excluding bank-transfer payments (bacs/transferencia)
and the Fedfarma customer (customer_id=1 is reserved; excluded by known IDs list).

NOTE: The Railway PostgreSQL DB (naturdao) does NOT have a customers table with
roles. B2B filtering is done via the 'channel' column ('B2B') stored in woo_orders.
There is no 'company' field in the DB — the company field in the response will
contain a placeholder with the customer_id for downstream enrichment.
"""

from datetime import date, datetime
from typing import Any

import psycopg2
import psycopg2.extras
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

# ---------------------------------------------------------------------------
# Railway PostgreSQL connection settings
# ---------------------------------------------------------------------------
DB_CONFIG = {
    "host": "caboose.proxy.rlwy.net",
    "port": 25831,
    "user": "naturdao",
    "password": "Naturdao2026SecureDB!",
    "dbname": "naturdao",
}

# Payment methods to exclude (lowercase)
EXCLUDED_PAYMENT_METHODS = {"bacs", "bank_transfer", "transferencia"}

# Known Fedfarma customer IDs to exclude (add more as needed)
# Since there is no company name in the DB, we maintain this exclusion list.
EXCLUDED_CUSTOMER_IDS: set[int] = set()  # e.g. {1234, 5678}

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET",
    "Access-Control-Allow-Headers": "*",
}


def _get_db_connection():
    """Open and return a new psycopg2 connection to Railway PostgreSQL."""
    return psycopg2.connect(**DB_CONFIG)


def _query_b2b_orders(after_str: str, before_str: str) -> list[dict]:
    """
    Query woo_orders for B2B orders in the given date range.
    Returns one row per line item; we aggregate by order_id afterwards.

    Filters applied at SQL level:
      - channel = 'B2B'
      - date >= after (inclusive, start of day UTC)
      - date <= before (inclusive, end of day UTC)
    """
    sql = """
        SELECT
            order_id,
            date,
            payment_method,
            customer_id,
            order_total_eur,
            ARRAY_AGG(sku ORDER BY line_item_id) AS skus
        FROM woo_orders
        WHERE channel = 'B2B'
          AND date >= %(after)s::timestamptz
          AND date <= %(before)s::timestamptz
        GROUP BY order_id, date, payment_method, customer_id, order_total_eur
        ORDER BY date DESC
    """
    params = {
        "after": f"{after_str}T00:00:00+00:00",
        "before": f"{before_str}T23:59:59+00:00",
    }
    conn = _get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _format_order(row: dict) -> dict:
    """Convert a raw DB row into the B2B response shape."""
    # No company name in DB — use customer_id as placeholder
    company = f"customer_{row['customer_id']}"
    return {
        "order_id": row["order_id"],
        "date": row["date"].strftime("%Y-%m-%d") if row["date"] else "",
        "company": company,
        "skus": [s for s in (row["skus"] or []) if s],
        "amount": float(row["order_total_eur"]) if row["order_total_eur"] is not None else 0.0,
        "pm": row["payment_method"] or "",
        "customer_id": row["customer_id"],
    }


async def route_b2b_orders(request: Request) -> Response:
    """GET /api/b2b-orders?after=YYYY-MM-DD&before=YYYY-MM-DD

    Queries Railway PostgreSQL directly. The woo_orders table does NOT contain
    a 'company' or 'billing_company' column; the 'company' field in the response
    is set to 'customer_<id>' as a placeholder — enrich downstream via WooCommerce
    API or a separate customer lookup if needed.

    B2B filter: channel = 'B2B' (stored in woo_orders at sync time).
    No 'professionals' role table exists in this DB.

    Exclusions applied:
      - payment_method IN ('bacs', 'bank_transfer', 'transferencia') → excluded[]
      - customer_id IN EXCLUDED_CUSTOMER_IDS (Fedfarma IDs) → excluded[]
    """

    # Handle preflight OPTIONS
    if request.method == "OPTIONS":
        return Response(status_code=204, headers=CORS_HEADERS)

    after_str = request.query_params.get("after", "")
    before_str = request.query_params.get("before", "")

    if not after_str or not before_str:
        return JSONResponse(
            {"error": "Query params 'after' and 'before' are required (YYYY-MM-DD)"},
            status_code=400,
            headers=CORS_HEADERS,
        )

    try:
        datetime.strptime(after_str, "%Y-%m-%d")
        datetime.strptime(before_str, "%Y-%m-%d")
    except ValueError:
        return JSONResponse(
            {"error": "Dates must be in YYYY-MM-DD format"},
            status_code=400,
            headers=CORS_HEADERS,
        )

    try:
        rows = _query_b2b_orders(after_str, before_str)
    except Exception as exc:
        return JSONResponse(
            {"error": f"Database query failed: {exc}"},
            status_code=500,
            headers=CORS_HEADERS,
        )

    included = []
    excluded = []

    for row in rows:
        formatted = _format_order(row)
        pm = (row.get("payment_method") or "").lower()
        cid = row.get("customer_id")

        # Exclude by payment method (bacs / bank transfer)
        if pm in EXCLUDED_PAYMENT_METHODS:
            formatted["exclusion_reason"] = f"payment_method={pm}"
            excluded.append(formatted)
            continue

        # Exclude by known Fedfarma customer IDs
        if cid in EXCLUDED_CUSTOMER_IDS:
            formatted["exclusion_reason"] = f"fedfarma customer_id={cid}"
            excluded.append(formatted)
            continue

        included.append(formatted)

    payload = {
        "orders": included,
        "excluded": excluded,
        "generated": date.today().isoformat(),
        "meta": {
            "source": "railway_postgresql",
            "table": "woo_orders",
            "filter": "channel='B2B'",
            "note": "No company name in DB; company field = customer_<id>. No professionals role table exists.",
        },
    }
    return JSONResponse(payload, headers=CORS_HEADERS)
