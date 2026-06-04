"""
WooCommerce B2B orders endpoint for Hermes server.

GET /api/b2b-orders?after=YYYY-MM-DD&before=YYYY-MM-DD

Returns B2B orders filtered to 'professionals' role customers,
excluding fedfarma/federaci farmac companies and bank-transfer payments.
"""

import asyncio
from datetime import date, datetime
from typing import Any

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

# ---------------------------------------------------------------------------
# WooCommerce credentials (naturdao.com)
# ---------------------------------------------------------------------------
WC_BASE_URL = "https://naturdao.com/wp-json/wc/v3"
WC_CONSUMER_KEY = "ck_3ee57d27db80e2825a8b8239507173d79f6910c3"
WC_CONSUMER_SECRET = "cs_76a0e19e53fb93261bf11c5a27124ceab3fab7ac"
WC_AUTH = (WC_CONSUMER_KEY, WC_CONSUMER_SECRET)

# Payment methods to exclude
EXCLUDED_PAYMENT_METHODS = {"bacs", "bank_transfer", "transferencia"}

# Company name fragments to exclude (case-insensitive)
EXCLUDED_COMPANY_FRAGMENTS = ["fedfarma", "federaci farmac"]

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET",
    "Access-Control-Allow-Headers": "*",
}


async def _wc_get_all(client: httpx.AsyncClient, path: str, params: dict) -> list[dict]:
    """Paginate through all pages of a WooCommerce endpoint."""
    results: list[dict] = []
    page = 1
    while True:
        p = {**params, "page": page, "per_page": 100}
        resp = await client.get(f"{WC_BASE_URL}{path}", params=p, auth=WC_AUTH, timeout=60.0)
        resp.raise_for_status()
        data = resp.json()
        if not data:
            break
        results.extend(data)
        if len(data) < 100:
            break
        page += 1
    return results


def _company_excluded(billing: dict) -> bool:
    """Return True if the billing company/name matches an exclusion fragment."""
    company = (billing.get("company") or "").lower()
    first = (billing.get("first_name") or "").lower()
    last = (billing.get("last_name") or "").lower()
    full_name = f"{company} {first} {last}"
    for fragment in EXCLUDED_COMPANY_FRAGMENTS:
        if fragment in full_name:
            return True
    return False


def _format_order(order: dict) -> dict:
    """Convert a raw WooCommerce order into the B2B response shape."""
    billing = order.get("billing", {})
    company = billing.get("company") or f"{billing.get('first_name', '')} {billing.get('last_name', '')}".strip()
    skus = [
        item.get("sku") or item.get("product_id")
        for item in order.get("line_items", [])
    ]
    return {
        "order_id": order["id"],
        "date": order.get("date_created", "")[:10],
        "company": company,
        "skus": skus,
        "amount": order.get("total", "0"),
        "pm": order.get("payment_method", ""),
        "customer_id": order.get("customer_id"),
    }


async def route_b2b_orders(request: Request) -> Response:
    """GET /api/b2b-orders?after=YYYY-MM-DD&before=YYYY-MM-DD"""

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

    # Validate date format
    try:
        datetime.strptime(after_str, "%Y-%m-%d")
        datetime.strptime(before_str, "%Y-%m-%d")
    except ValueError:
        return JSONResponse(
            {"error": "Dates must be in YYYY-MM-DD format"},
            status_code=400,
            headers=CORS_HEADERS,
        )

    # WooCommerce 'after'/'before' expect ISO8601 with time component
    after_iso = f"{after_str}T00:00:00"
    before_iso = f"{before_str}T23:59:59"

    async with httpx.AsyncClient() as client:
        # Fetch all professionals customers (paginated)
        customers = await _wc_get_all(client, "/customers", {"role": "professionals"})
        professional_ids = {c["id"] for c in customers}

        # Fetch all orders in date range (paginated)
        orders_raw = await _wc_get_all(
            client,
            "/orders",
            {
                "after": after_iso,
                "before": before_iso,
                "status": "any",
            },
        )

    included = []
    excluded = []

    for order in orders_raw:
        customer_id = order.get("customer_id")
        billing = order.get("billing", {})
        payment_method = order.get("payment_method", "").lower()
        formatted = _format_order(order)

        # Filter: must be a professional customer
        if customer_id not in professional_ids:
            continue

        # Exclusion checks
        if payment_method in EXCLUDED_PAYMENT_METHODS:
            excluded.append(formatted)
            continue

        if _company_excluded(billing):
            excluded.append(formatted)
            continue

        included.append(formatted)

    payload = {
        "orders": included,
        "excluded": excluded,
        "generated": date.today().isoformat(),
    }
    return JSONResponse(payload, headers=CORS_HEADERS)
