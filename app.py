import os, re, json, time
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
from zoneinfo import ZoneInfo

import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

app = FastAPI(title="Ledger Agent (No AI Token)", version="2.0.0")

# Put the complete assignment-specific questionData URL here as an environment variable.
# The app changes only its `path` query parameter to reach /, /orders, /rates, and /export.
LEDGER_API_URL = os.getenv("LEDGER_API_URL", "").strip()
TIMEOUT = float(os.getenv("UPSTREAM_TIMEOUT_SECONDS", "8"))
IST = ZoneInfo("Asia/Kolkata")
session = requests.Session()

class QuestionRequest(BaseModel):
    question: str = Field(min_length=3, max_length=2000)

def endpoint_url(path: str) -> str:
    if not LEDGER_API_URL:
        raise RuntimeError("LEDGER_API_URL is not configured")
    parsed = urlparse(LEDGER_API_URL)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["path"] = path if path.startswith("/") else "/" + path
    return urlunparse(parsed._replace(query=urlencode(query)))

def get_json(path, params=None):
    url = endpoint_url(path)
    response = session.get(url, params=params, timeout=TIMEOUT)
    if not response.ok:
        raise RuntimeError(f"{path}: HTTP {response.status_code}: {response.text[:200]}")
    try:
        return response.json(), response.url
    except ValueError:
        raise RuntimeError(f"{path}: expected JSON, got {response.headers.get('content-type')}; body={response.text[:160]!r}")

def extract_rows(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("results", "data", "orders", "items", "records", "rows"):
            if isinstance(payload.get(key), list):
                return payload[key]
        if any(k in payload for k in ("order_id", "id")):
            return [payload]
    raise RuntimeError("API response did not contain a recognized list; inspect explore_api.py output")

def fetch_all(path):
    url = endpoint_url(path)
    all_rows, seen_urls = [], set()
    params = None
    for page_count in range(250):
        r = session.get(url, params=params, timeout=TIMEOUT)
        if not r.ok:
            raise RuntimeError(f"{path}: HTTP {r.status_code}: {r.text[:200]}")
        try:
            payload = r.json()
        except ValueError:
            raise RuntimeError(f"{path}: expected JSON, got {r.headers.get('content-type')}; body={r.text[:160]!r}")
        all_rows.extend(extract_rows(payload))
        if not isinstance(payload, dict):
            break

        nxt = None
        for key in ("next", "next_url", "nextPage", "next_page_url"):
            if isinstance(payload.get(key), str) and payload[key]:
                nxt = payload[key]
                break
        if not nxt:
            for key in ("links", "pagination", "meta"):
                obj = payload.get(key)
                if isinstance(obj, dict):
                    for k in ("next", "next_url", "nextPage", "next_page_url"):
                        if isinstance(obj.get(k), str) and obj[k]:
                            nxt = obj[k]
                            break
                if nxt:
                    break
        if nxt:
            if nxt in seen_urls:
                break
            seen_urls.add(nxt)
            url, params = requests.compat.urljoin(r.url, nxt), None
            continue

        current = payload.get("page", payload.get("current_page"))
        total = payload.get("total_pages", payload.get("pages"))
        if isinstance(current, int) and isinstance(total, int) and current < total:
            url, params = endpoint_url(path), {"page": current + 1}
            continue
        if payload.get("has_more") is True or payload.get("hasMore") is True:
            url, params = endpoint_url(path), {"page": page_count + 2}
            continue
        break
    return all_rows

def load_ledger():
    orders = fetch_all("/orders")
    rates = fetch_all("/rates")
    try:
        exports = fetch_all("/export")
    except Exception:
        exports = []
    # Keep the latest revision of each order ID, if updated_at is provided.
    latest = {}
    for row in orders:
        if not isinstance(row, dict):
            continue
        oid = row.get("order_id", row.get("id"))
        if oid is None:
            continue
        old = latest.get(str(oid))
        stamp = str(row.get("updated_at", row.get("updatedAt", "")))
        old_stamp = str(old.get("updated_at", old.get("updatedAt", ""))) if old else ""
        if old is None or stamp >= old_stamp:
            latest[str(oid)] = row
    return list(latest.values()), rates, exports

def val(row, *keys, default=None):
    for key in keys:
        if key in row and row[key] is not None:
            return row[key]
    return default

def norm(value):
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())

MONTHS = {
    "january":1, "jan":1, "february":2, "feb":2, "march":3, "mar":3,
    "april":4, "apr":4, "may":5, "june":6, "jun":6, "july":7, "jul":7,
    "august":8, "aug":8, "september":9, "sep":9, "sept":9, "october":10,
    "oct":10, "november":11, "nov":11, "december":12, "dec":12
}
ALIASES = {
    "north":"north", "northern":"north", "south":"south", "southern":"south",
    "east":"east", "eastern":"east", "west":"west", "western":"west",
    "central":"central", "centre":"central", "center":"central"
}

def parse_question(question):
    q = question.lower()
    parsed = {
        "metric": "revenue", "start_date": None, "end_date": None,
        "region": None, "product": None, "customer": None,
        "status": None, "group_by": None, "operation": "sum",
        "currency": "USD"
    }

    # Intent: refunds, order/customer/product counts, lists, or revenue/sales.
    if re.search(r"\b(refund|refunded|refunds|money returned)\b", q):
        parsed["metric"] = "refunds"
    elif re.search(r"\b(how many|number of|count of|count)\b.*\b(order|orders)\b|\b(number of orders|order count)\b", q):
        parsed["metric"] = "orders"
        parsed["operation"] = "count"
    elif re.search(r"\b(how many|number of|count of|count)\b.*\bcustomer", q):
        parsed["metric"] = "customers"
        parsed["operation"] = "count"
    elif re.search(r"\b(list|show me|which|what are the)\b.*\borders?\b", q):
        parsed["metric"] = "list"
        parsed["operation"] = "list"
    elif re.search(r"\b(customer|customers|clients|buyers)\b", q) and re.search(r"\b(unique|distinct|number|count|how many)\b", q):
        parsed["metric"] = "customers"
        parsed["operation"] = "count"
    elif re.search(r"\b(product|products|items)\b", q) and re.search(r"\b(unique|distinct|number|count|how many)\b", q):
        parsed["metric"] = "products"
        parsed["operation"] = "count"

    # Month names and year: "March 2026", plus explicit date ranges.
    for month_name, month_num in MONTHS.items():
        m = re.search(rf"\b{month_name}\s+(20\d{{2}})\b", q)
        if m:
            year = int(m.group(1))
            parsed["start_date"] = date(year, month_num, 1).isoformat()
            if month_num == 12:
                parsed["end_date"] = date(year, 12, 31).isoformat()
            else:
                from calendar import monthrange
                parsed["end_date"] = date(year, month_num, monthrange(year, month_num)[1]).isoformat()
            break

    # ISO date or "between YYYY-MM-DD and YYYY-MM-DD".
    dates = re.findall(r"\b(20\d{2}-\d{2}-\d{2})\b", q)
    if len(dates) >= 2:
        parsed["start_date"], parsed["end_date"] = dates[0], dates[1]
    elif len(dates) == 1:
        parsed["start_date"] = parsed["end_date"] = dates[0]

    # Common relative periods; year/month ranges are based on current date in India.
    today = datetime.now(IST).date()
    if "last month" in q:
        first_this = today.replace(day=1)
        last_prev = first_this.fromordinal(first_this.toordinal() - 1)
        parsed["start_date"] = last_prev.replace(day=1).isoformat()
        parsed["end_date"] = last_prev.isoformat()
    elif "this month" in q:
        from calendar import monthrange
        parsed["start_date"] = today.replace(day=1).isoformat()
        parsed["end_date"] = today.replace(day=monthrange(today.year,today.month)[1]).isoformat()
    elif "last quarter" in q:
        current_q = (today.month - 1) // 3
        end_month = current_q * 3
        if end_month == 0:
            end_year, end_month = today.year - 1, 12
        else:
            end_year = today.year
        start_month = end_month - 2
        from calendar import monthrange
        parsed["start_date"] = date(end_year,start_month,1).isoformat()
        parsed["end_date"] = date(end_year,end_month,monthrange(end_year,end_month)[1]).isoformat()

    # Region aliases, only when mentioned as a standalone word.
    for alias, canonical in ALIASES.items():
        if re.search(rf"\b{alias}\b", q):
            parsed["region"] = canonical
            break

    # Group-by intent.
    for word in ("product", "customer", "region", "month", "currency"):
        if re.search(rf"\bby {word}\b|\bper {word}\b|\beach {word}\b", q):
            parsed["group_by"] = word
            break
    if re.search(r"\baverage|mean\b", q):
        parsed["operation"] = "average"
    if re.search(r"\b(top|highest|largest|best)\b", q):
        parsed["operation"] = "top"
        for word in ("product", "customer", "region"):
            if word in q:
                parsed["group_by"] = word
                break

    # The ledger's exact product/customer names cannot safely be guessed from prose.
    # Match a name against known ledger values after the data has been fetched.
    return parsed

def parse_date(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try: return datetime.fromtimestamp(value, tz=IST).date()
        except Exception: return None
    s = str(value).strip()
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None: dt = dt.replace(tzinfo=IST)
        return dt.astimezone(IST).date()
    except Exception:
        try: return date.fromisoformat(s[:10])
        except Exception: return None

def match_name(question, rows, field_keys):
    qn = norm(question)
    candidates = set()
    for row in rows:
        if isinstance(row, dict):
            value = val(row, *field_keys)
            if value:
                candidates.add(str(value))
    # Longest known exact substring wins.
    matches = [name for name in candidates if norm(name) and norm(name) in qn]
    return max(matches, key=len) if matches else None

def rate_table(rows):
    rates = {"USD": Decimal("1")}
    for row in rows:
        if not isinstance(row, dict): continue
        cur = str(val(row, "currency", "code", "from_currency", "from", default="")).upper()
        raw = val(row, "rate", "usd_rate", "to_usd", "exchange_rate", "value")
        if cur and raw is not None:
            try: rates[cur] = Decimal(str(raw))
            except Exception: pass
    return rates

def usd_amount(row, rates):
    raw = val(row, "total", "amount", "order_total", "revenue", "price", "subtotal", default=0)
    cur = str(val(row, "currency", "currency_code", default="USD")).upper()
    try:
        amount = Decimal(str(raw))
        if cur != "USD":
            if cur not in rates: return None
            amount *= rates[cur]
        return amount
    except Exception:
        return None

def row_matches(row, parsed):
    dt = parse_date(val(row, "order_date", "date", "created_at", "createdAt", "timestamp"))
    if parsed["start_date"] and (dt is None or dt < date.fromisoformat(parsed["start_date"])):
        return False
    if parsed["end_date"] and (dt is None or dt > date.fromisoformat(parsed["end_date"])):
        return False
    region = parsed.get("region")
    if region:
        actual = norm(val(row, "region", "sales_region", "territory", default=""))
        if norm(region) not in actual: return False
    for field, keys in (("product",("product","product_name","item","sku_name","sku")),
                        ("customer",("customer","customer_name","customer_id","buyer"))):
        wanted = parsed.get(field)
        if wanted and norm(wanted) not in norm(val(row,*keys,default="")):
            return False
    if parsed["metric"] == "revenue" and norm(val(row,"status",default="")) != "paid":
        return False
    return True

def money(n):
    return float(Decimal(n).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

def answer_question(question, orders, rates_raw):
    parsed = parse_question(question)
    parsed["product"] = match_name(question, orders, ("product","product_name","item","sku_name","sku"))
    parsed["customer"] = match_name(question, orders, ("customer","customer_name","customer_id","buyer"))
    selected = [r for r in orders if isinstance(r, dict) and row_matches(r, parsed)]
    rates = rate_table(rates_raw)

    if parsed["metric"] == "list":
        return selected[:100]
    if parsed["metric"] == "orders":
        return len(selected)
    if parsed["metric"] == "customers":
        return len({str(val(r,"customer_id","customer","customer_name","buyer",default="")) for r in selected})
    if parsed["metric"] == "products":
        return len({str(val(r,"product","product_name","item","sku_name","sku",default="")) for r in selected})

    if parsed["metric"] == "refunds":
        refunds = []
        for r in selected:
            raw = val(r,"refund_amount","refunded_amount","refunds")
            if raw is None and "refund" not in norm(val(r,"status",default="")):
                continue
            try:
                amount = Decimal(str(raw or 0))
                cur = str(val(r,"currency","currency_code",default="USD")).upper()
                if cur != "USD": amount *= rates.get(cur, Decimal("0"))
                refunds.append(amount)
            except Exception: pass
        return money(sum(refunds, Decimal("0")))

    if parsed.get("group_by"):
        grouped = {}
        group = parsed["group_by"]
        for r in selected:
            if group == "product": key = val(r,"product","product_name","item","sku_name",default="Unknown")
            elif group == "customer": key = val(r,"customer","customer_name","customer_id",default="Unknown")
            elif group == "region": key = val(r,"region","sales_region","territory",default="Unknown")
            elif group == "currency": key = val(r,"currency","currency_code",default="USD")
            else:
                d = parse_date(val(r,"order_date","date","created_at","createdAt"))
                key = d.strftime("%Y-%m") if d else "unknown"
            amount = usd_amount(r,rates) or Decimal("0")
            grouped[str(key)] = grouped.get(str(key), Decimal("0")) + amount
        result = {k: money(v) for k,v in grouped.items()}
        if parsed["operation"] == "top":
            return dict(sorted(result.items(), key=lambda kv: kv[1], reverse=True)[:10])
        return dict(sorted(result.items()))

    amounts = [usd_amount(r,rates) for r in selected]
    amounts = [a for a in amounts if a is not None]
    if parsed["operation"] == "average":
        return money(sum(amounts,Decimal("0"))/len(amounts)) if amounts else 0
    return money(sum(amounts,Decimal("0")))

@app.get("/")
def root():
    return {"service":"Ledger Agent (no AI token)","status":"ok","endpoint":"POST /answer"}

@app.get("/health")
def health():
    return {"ok": True}

@app.post("/answer")
def answer(req: QuestionRequest):
    try:
        orders, rates, _ = load_ledger()
        return {"answer": answer_question(req.question, orders, rates)}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)[:500])
