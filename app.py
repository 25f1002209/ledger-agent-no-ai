import os
import re
import calendar
import time
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
from zoneinfo import ZoneInfo
from threading import Lock

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

load_dotenv()

app = FastAPI(title="Acme Appliances Ledger Agent (No AI)", version="3.0.0")

# Set LEDGER_API_URL in .env locally and in Render Environment.
# It must be the complete assignment questionData URL with path=/ and all query parameters.
LEDGER_API_URL = os.getenv("LEDGER_API_URL", "").strip()
UPSTREAM_TIMEOUT_SECONDS = float(os.getenv("UPSTREAM_TIMEOUT_SECONDS", "10"))
CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "300"))
IST = ZoneInfo("Asia/Kolkata")

session = requests.Session()
_cache_lock = Lock()
_cache = {"loaded_at": 0.0, "orders": None, "rates": None}


class QuestionRequest(BaseModel):
    question: str = Field(min_length=3, max_length=2000)


def endpoint_url(path: str) -> str:
    if not LEDGER_API_URL:
        raise RuntimeError(
            "LEDGER_API_URL is missing. Set it in your local .env file and Render Environment settings."
        )
    parsed = urlparse(LEDGER_API_URL)
    query_items = parse_qsl(parsed.query, keep_blank_values=True)
    query = dict(query_items)
    query["path"] = path if path.startswith("/") else "/" + path
    query.pop("page", None)
    return urlunparse(parsed._replace(query=urlencode(query)))


def get_json_url(url: str):
    response = session.get(url, timeout=UPSTREAM_TIMEOUT_SECONDS)
    if not response.ok:
        raise RuntimeError(
            f"Upstream returned HTTP {response.status_code}: {response.text[:200]}"
        )
    try:
        return response.json(), response.url
    except ValueError as exc:
        raise RuntimeError(
            f"Expected JSON from {urlparse(response.url).path}; "
            f"content-type={response.headers.get('content-type')}; "
            f"body={response.text[:160]!r}"
        ) from exc


def fetch_all_orders():
    """Read the real API's {page, pages, next, orders:[...]} structure."""
    url = endpoint_url("/orders")
    params = {"page": 1}
    collected = []
    seen_next = set()

    for _ in range(200):
        if params is None:
            payload, response_url = get_json_url(url)
        else:
            response = session.get(url, params=params, timeout=UPSTREAM_TIMEOUT_SECONDS)
            if not response.ok:
                raise RuntimeError(
                    f"/orders returned HTTP {response.status_code}: {response.text[:200]}"
                )
            try:
                payload = response.json()
            except ValueError as exc:
                raise RuntimeError(
                    f"/orders returned non-JSON content: {response.text[:160]!r}"
                ) from exc
            response_url = response.url

        if not isinstance(payload, dict) or not isinstance(payload.get("orders"), list):
            keys = list(payload.keys()) if isinstance(payload, dict) else type(payload).__name__
            raise RuntimeError(f"Unexpected /orders response; expected 'orders' list, got {keys}")

        collected.extend(row for row in payload["orders"] if isinstance(row, dict))
        next_url = payload.get("next")
        if not next_url:
            break
        if next_url in seen_next:
            raise RuntimeError("Pagination loop detected in the API's next URL")
        seen_next.add(next_url)
        url, params = next_url, None
    else:
        raise RuntimeError("Stopped after 200 pages to prevent an infinite pagination loop")

    # The API explicitly says that the latest updated_at row is current for each ID.
    latest = {}
    for row in collected:
        order_id = row.get("id", row.get("order_id"))
        if order_id is None:
            continue
        old = latest.get(str(order_id))
        new_stamp = str(row.get("updated_at", ""))
        old_stamp = str(old.get("updated_at", "")) if old else ""
        if old is None or new_stamp > old_stamp:
            latest[str(order_id)] = row

    return list(latest.values())


def fetch_rates():
    payload, _ = get_json_url(endpoint_url("/rates"))
    # Confirmed response: {"description": "...", "usd_per_unit": {"USD":1,"EUR":1.08,...}}
    rate_data = payload.get("usd_per_unit") if isinstance(payload, dict) else None
    if not isinstance(rate_data, dict):
        keys = list(payload.keys()) if isinstance(payload, dict) else type(payload).__name__
        raise RuntimeError(f"Unexpected /rates response; expected usd_per_unit object, got {keys}")

    rates = {}
    for currency, raw_rate in rate_data.items():
        try:
            rates[str(currency).upper()] = Decimal(str(raw_rate))
        except Exception:
            continue
    if "USD" not in rates:
        rates["USD"] = Decimal("1")
    return rates


def get_ledger(force_refresh=False):
    now = time.time()
    with _cache_lock:
        is_fresh = (
            _cache["orders"] is not None
            and _cache["rates"] is not None
            and now - _cache["loaded_at"] < CACHE_TTL_SECONDS
        )
        if is_fresh and not force_refresh:
            return _cache["orders"], _cache["rates"]

        orders = fetch_all_orders()
        rates = fetch_rates()
        _cache.update(loaded_at=time.time(), orders=orders, rates=rates)
        return orders, rates


def norm(value):
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9, "october": 10,
    "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}
REGION_ALIASES = {
    "north": "North", "northern": "North",
    "south": "South", "southern": "South",
    "east": "East", "eastern": "East",
    "west": "West", "western": "West",
    "central": "Central", "centre": "Central", "center": "Central",
}


def parse_question(question):
    q = question.lower()
    parsed = {
        "metric": "revenue",
        "start_date": None,
        "end_date": None,
        "region": None,
        "product": None,
        "customer": None,
        "group_by": None,
        "operation": "sum",
    }

    if re.search(r"\b(refunds?|refunded|money returned)\b", q):
        parsed["metric"] = "refunds"
    elif re.search(r"\b(how many|number of|count of|count)\b.*\borders?\b|\border count\b", q):
        parsed["metric"], parsed["operation"] = "orders", "count"
    elif re.search(r"\b(how many|number of|count of|count)\b.*\b(customers?|clients|buyers)\b", q):
        parsed["metric"], parsed["operation"] = "customers", "count"
    elif re.search(r"\b(how many|number of|count of|count)\b.*\b(products?|items)\b", q):
        parsed["metric"], parsed["operation"] = "products", "count"
    elif re.search(r"\b(list|show me|which|what are the)\b.*\borders?\b", q):
        parsed["metric"], parsed["operation"] = "list", "list"

    # Month/year, e.g. "March 2026".
    for month_name, month_number in MONTHS.items():
        match = re.search(rf"\b{month_name}\s+(20\d{{2}})\b", q)
        if match:
            year = int(match.group(1))
            parsed["start_date"] = date(year, month_number, 1)
            parsed["end_date"] = date(
                year, month_number, calendar.monthrange(year, month_number)[1]
            )
            break

    # ISO dates: one date means that single day; two dates mean inclusive range.
    iso_dates = re.findall(r"\b(20\d{2}-\d{2}-\d{2})\b", q)
    if len(iso_dates) >= 2:
        parsed["start_date"] = date.fromisoformat(iso_dates[0])
        parsed["end_date"] = date.fromisoformat(iso_dates[1])
    elif len(iso_dates) == 1:
        parsed["start_date"] = parsed["end_date"] = date.fromisoformat(iso_dates[0])

    today = datetime.now(IST).date()
    if "last month" in q:
        end = date.fromordinal(today.replace(day=1).toordinal() - 1)
        parsed["start_date"], parsed["end_date"] = end.replace(day=1), end
    elif "this month" in q:
        parsed["start_date"] = today.replace(day=1)
        parsed["end_date"] = today.replace(
            day=calendar.monthrange(today.year, today.month)[1]
        )
    elif "last quarter" in q:
        current_quarter = (today.month - 1) // 3
        end_month = current_quarter * 3 or 12
        end_year = today.year if current_quarter else today.year - 1
        start_month = end_month - 2
        parsed["start_date"] = date(end_year, start_month, 1)
        parsed["end_date"] = date(
            end_year, end_month, calendar.monthrange(end_year, end_month)[1]
        )

    for alias, canonical in REGION_ALIASES.items():
        if re.search(rf"\b{re.escape(alias)}\b", q):
            parsed["region"] = canonical
            break

    for group_name in ("product", "customer", "region", "month", "currency"):
        if re.search(
            rf"\bby {group_name}\b|\bper {group_name}\b|\beach {group_name}\b",
            q,
        ):
            parsed["group_by"] = group_name
            break

    if re.search(r"\b(average|mean)\b", q):
        parsed["operation"] = "average"
    # Ranking/superlative questions need grouped calculations. Include "most".
    if re.search(r"\b(top|highest|largest|best|most|greatest|leading)\b", q):
        parsed["operation"] = "top"
        for group_name in ("product", "customer", "region"):
            if re.search(rf"\b{group_name}s?\b", q):
                parsed["group_by"] = group_name
                break

    # "Which product earned the most..." implies grouping even without "by product".
    if re.search(r"\b(which|what)\b", q) and re.search(
        r"\b(most|highest|largest|greatest|best)\b", q
    ):
        for group_name in ("product", "customer", "region"):
            if re.search(rf"\b{group_name}s?\b", q):
                parsed["group_by"] = group_name
                parsed["operation"] = "top"
                break

    return parsed


def parse_order_date(row):
    raw = row.get("created_at")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=IST)
        return dt.astimezone(IST).date()
    except Exception:
        try:
            return date.fromisoformat(str(raw)[:10])
        except Exception:
            return None


def match_known_name(question, orders, field):
    q_normalized = norm(question)
    candidates = {
        str(row[field])
        for row in orders
        if row.get(field) is not None and str(row[field]).strip()
    }
    matches = [
        name for name in candidates
        if norm(name) and norm(name) in q_normalized
    ]
    return max(matches, key=len) if matches else None


def matches_order(row, parsed):
    order_date = parse_order_date(row)

    if parsed["start_date"] and (
        order_date is None or order_date < parsed["start_date"]
    ):
        return False
    if parsed["end_date"] and (
        order_date is None or order_date > parsed["end_date"]
    ):
        return False
    if parsed["region"] and norm(row.get("region")) != norm(parsed["region"]):
        return False
    if parsed["product"] and norm(row.get("product")) != norm(parsed["product"]):
        return False
    if parsed["customer"] and norm(row.get("customer")) != norm(parsed["customer"]):
        return False

    if parsed["metric"] == "revenue" and norm(row.get("status")) != "paid":
        return False
    if parsed["metric"] == "refunds" and norm(row.get("status")) != "refunded":
        return False
    return True


def usd_amount(row, rates):
    try:
        amount = Decimal(str(row.get("amount", 0)))
        currency = str(row.get("currency", "USD")).upper()
        if currency not in rates:
            raise RuntimeError(f"No USD conversion rate available for currency {currency}")
        return amount * rates[currency]
    except (ArithmeticError, ValueError, TypeError) as exc:
        raise RuntimeError(f"Invalid amount/currency in order {row.get('id')}: {exc}") from exc


def money(amount):
    return float(Decimal(amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def answer_question(question, orders, rates):
    parsed = parse_question(question)
    parsed["product"] = match_known_name(question, orders, "product")
    parsed["customer"] = match_known_name(question, orders, "customer")

    selected = [row for row in orders if matches_order(row, parsed)]

    if parsed["metric"] == "orders":
        return len(selected)
    if parsed["metric"] == "customers":
        return len({row.get("customer") for row in selected if row.get("customer") is not None})
    if parsed["metric"] == "products":
        return len({row.get("product") for row in selected if row.get("product") is not None})
    if parsed["metric"] == "list":
        return selected[:100]

    if parsed["metric"] == "refunds":
        total = Decimal("0")
        for row in selected:
            # Use an explicit refund field when present; otherwise this ledger's
            # refunded status indicates the order amount is the refund amount.
            raw_amount = row.get("refund_amount", row.get("refunded_amount", row.get("amount", 0)))
            currency = str(row.get("currency", "USD")).upper()
            if currency not in rates:
                raise RuntimeError(f"No USD conversion rate available for currency {currency}")
            total += Decimal(str(raw_amount)) * rates[currency]
        return money(total)

    if parsed["group_by"]:
        group_by = parsed["group_by"]
        totals = {}
        for row in selected:
            if group_by == "product":
                key = row.get("product", "Unknown")
            elif group_by == "customer":
                key = row.get("customer", "Unknown")
            elif group_by == "region":
                key = row.get("region", "Unknown")
            elif group_by == "currency":
                key = row.get("currency", "USD")
            else:
                dt = parse_order_date(row)
                key = dt.strftime("%Y-%m") if dt else "unknown"
            totals[str(key)] = totals.get(str(key), Decimal("0")) + usd_amount(row, rates)

        result = {key: money(amount) for key, amount in sorted(totals.items())}
        if parsed["operation"] == "top":
            ranked = sorted(result.items(), key=lambda item: item[1], reverse=True)
            # "Which product/customer/region..." expects the winning name.
            if re.search(r"\\b(which|what)\\b", question.lower()) and parsed["group_by"] in (
                "product", "customer", "region"
            ):
                return ranked[0][0] if ranked else None
            return dict(ranked[:10])
        return result

    amounts = [usd_amount(row, rates) for row in selected]
    if parsed["operation"] == "average":
        return money(sum(amounts, Decimal("0")) / len(amounts)) if amounts else 0
    return money(sum(amounts, Decimal("0")))


@app.get("/")
def root():
    return {
        "service": "Acme Appliances order ledger agent",
        "status": "ok",
        "endpoint": "POST /answer",
        "ai_token_required": False,
    }


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/answer")
def answer(req: QuestionRequest):
    try:
        orders, rates = get_ledger()
        return {"answer": answer_question(req.question, orders, rates)}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)[:500])
