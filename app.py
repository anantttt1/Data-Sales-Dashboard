from __future__ import annotations

import json
import os
import re
import socket
import threading
import warnings
import webbrowser
from io import BytesIO, StringIO
from pathlib import Path

import pandas as pd
from flask import Flask, jsonify, render_template, request, send_file, send_from_directory

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024
ROOT = Path(__file__).parent
SAMPLE = ROOT / "sample_sales.csv"
PUBLIC = ROOT / "public"

# ---------------------------------------------------------------------------
# Column detection
#
# The dashboard works with a small canonical schema. Instead of demanding
# exact header names, every uploaded file goes through three steps:
#   1. exact alias match      ("Order Date" -> date)
#   2. fuzzy alias match      ("Sales Amount (INR)" -> revenue)
#   3. content-based guessing (a column full of dates is the date column)
# Each match is also checked against the column's *contents*, so a text column
# can never be picked as revenue. Users can override any choice in the UI.
# ---------------------------------------------------------------------------

FIELDS = ("date", "revenue", "product", "category", "region", "customer", "order_id", "quantity", "unit_price")
NUMERIC_FIELDS = {"revenue", "quantity", "unit_price"}
# More specific fields claim their columns first ("invoice date" is a date, not an order id).
RESOLVE_ORDER = ("date", "order_id", "unit_price", "quantity", "revenue", "customer", "category", "product", "region")

ALIASES = {
    "date": ["date", "order date", "transaction date", "invoice date", "purchase date", "sale date", "sales date",
             "bill date", "billing date", "order datetime", "created at", "created date", "timestamp", "datetime"],
    "order_id": ["order id", "order no", "order number", "order ref", "invoice", "invoice id", "invoice no",
                 "invoice number", "transaction id", "txn id", "bill no", "receipt no"],
    "unit_price": ["unit price", "price", "selling price", "price per unit", "rate"],
    "quantity": ["quantity", "qty", "units", "units sold", "unit sold", "pieces", "pcs", "count"],
    "revenue": ["revenue", "sales", "amount", "total", "total sales", "total amount", "net sales", "net revenue",
                "sales amount", "gross sales", "turnover", "order value", "order total", "grand total",
                "line total", "value"],
    "customer": ["customer", "customer id", "customer name", "client", "client id", "client name", "buyer",
                 "user id", "customer no", "cust id"],
    "category": ["category", "product category", "type", "product type", "segment", "department", "group", "class"],
    "product": ["product", "product name", "item", "item name", "description", "sku", "goods", "service"],
    "region": ["region", "country", "state", "location", "city", "area", "zone", "territory", "market", "province"],
}

# Words that disqualify a *fuzzy* match (e.g. "Unit Cost" must not be taken as revenue).
EXCLUDE_TOKENS = {
    "revenue": {"unit", "qty", "quantity", "discount", "tax", "cost", "id", "count", "rate", "per", "margin", "profit"},
    "unit_price": {"total", "net", "gross", "grand", "line"},
    "order_id": {"date", "amount", "total", "value", "price", "qty", "quantity"},
}
ID_TOKENS = {"id", "no", "number", "code", "index", "sr", "sl", "zip", "pin", "postal", "phone", "mobile", "year", "age", "rank"}
MONEY_HINTS = {"amount", "total", "value", "sales", "revenue", "income", "turnover", "price", "cost", "profit",
               "gmv", "net", "gross", "payment", "paid", "fee", "charge"}

CURRENCY_PATTERNS = (
    ("INR", r"₹|\brs\.?\s*\d|\binr\b"),
    ("EUR", r"€|\beur\b"),
    ("GBP", r"£|\bgbp\b"),
    ("JPY", r"¥|\bjpy\b"),
    ("USD", r"\$|\busd\b"),
)
MONEY_TOKENS = re.compile(r"(?i)rs\.?|inr|usd|eur|gbp|[₹$€£¥%]")


class AnalysisError(ValueError):
    """A user-facing error that still carries the detected mapping so the UI can offer a fix."""

    def __init__(self, message: str, meta: dict | None = None):
        super().__init__(message)
        self.meta = meta or {}


def _tokens(name) -> list[str]:
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", str(name))  # OrderDate -> Order Date
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).split()


def _key(name) -> str:
    return "".join(_tokens(name))


def _sample(series: pd.Series, n: int = 500) -> pd.Series:
    return series.dropna().head(n)


def _to_number(series: pd.Series) -> pd.Series:
    """Parse numbers, tolerating currency symbols, thousands separators and (negative) brackets."""
    direct = pd.to_numeric(series, errors="coerce")
    if pd.api.types.is_numeric_dtype(series):
        return direct
    text = series.astype(str).str.strip()
    negative = text.str.match(r"^\(.*\)$")
    cleaned = text.str.replace(MONEY_TOKENS, "", regex=True).str.replace(r"[\s,()]", "", regex=True)
    valid = cleaned.str.fullmatch(r"[+-]?(\d+\.?\d*|\.\d+)")
    parsed = pd.to_numeric(cleaned.where(valid), errors="coerce")
    parsed = parsed.where(~negative, -parsed.abs())
    return direct.where(direct.notna(), parsed)


def _to_date(series: pd.Series, order: str = "auto"):
    """Parse dates in mixed formats. Returns (dates, ambiguous).

    order: "day" (dd/mm/yyyy), "month" (mm/dd/yyyy) or "auto". In auto mode one order is chosen for the whole
    column from the evidence (a value like 13/01/2025 can only be day-first). If every value is <= 12 the order
    cannot be known: month-first is assumed and `ambiguous` is True so the UI can say so.
    """
    if pd.api.types.is_datetime64_any_dtype(series):
        return series, False
    if pd.api.types.is_numeric_dtype(series):
        return pd.Series(pd.NaT, index=series.index, dtype="datetime64[ns]"), False
    text = series.astype(str).str.strip()
    dayfirst = {"day": True, "month": False}.get(order)
    ambiguous = False
    if dayfirst is None:
        parts = text.str.extract(r"^(\d{1,2})[/.\-](\d{1,2})[/.\-]\d{2,4}")
        first, second = pd.to_numeric(parts[0]), pd.to_numeric(parts[1])
        first_big, second_big = bool((first > 12).any()), bool((second > 12).any())
        if first_big and not second_big:
            dayfirst = True
        elif second_big and not first_big:
            dayfirst = False
        else:
            dayfirst = False
            ambiguous = bool(parts[0].notna().any()) and not (first_big or second_big)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        parsed = pd.to_datetime(text, errors="coerce", format="mixed", dayfirst=dayfirst, utc=True)
    return parsed.dt.tz_localize(None), ambiguous


def _number_rate(series: pd.Series) -> float:
    sample = _sample(series)
    if sample.empty:
        return 0.0
    return float(_to_number(sample).notna().mean())


def _date_rate(series: pd.Series) -> float:
    if pd.api.types.is_numeric_dtype(series) or pd.api.types.is_bool_dtype(series):
        return 0.0  # plain numbers (years, ids, amounts) are never treated as dates
    sample = _sample(series)
    if sample.empty:
        return 0.0
    # Cheap pre-filter so we don't run the date parser on obviously non-date text columns.
    head = sample.head(50).astype(str)
    if head.str.contains(r"\d", regex=True).mean() < 0.6 or head.str.len().median() < 6:
        return 0.0
    return float(_to_date(sample)[0].notna().mean())


def _detect_currency(series: pd.Series, header: str) -> str | None:
    blob = header.lower()
    if not pd.api.types.is_numeric_dtype(series):
        blob += " " + " ".join(_sample(series, 200).astype(str)).lower()
    for code, pattern in CURRENCY_PATTERNS:
        if re.search(pattern, blob):
            return code
    return None


def _find_header_row(counts: list[int]) -> int:
    """Index of the first row that has the file's typical number of fields (skips title/preamble rows)."""
    filled = [c for c in counts if c >= 2]
    if not filled:
        return 0
    typical = max(set(filled), key=lambda c: (filled.count(c), c))
    return next(i for i, c in enumerate(counts) if c == typical)


def _read_excel(data: bytes) -> pd.DataFrame:
    try:
        grid = pd.read_excel(BytesIO(data), header=None, sheet_name=0)
    except Exception as exc:
        raise ValueError(f"Could not read this Excel file ({exc}).") from exc
    grid = grid.dropna(how="all").dropna(axis=1, how="all").reset_index(drop=True)
    if grid.empty:
        raise ValueError("The Excel sheet is empty. Add a header row and sales records.")
    head = _find_header_row([int(n) for n in grid.notna().sum(axis=1)])
    names, seen = [], {}
    for i, value in enumerate(grid.iloc[head]):
        name = str(value).strip() if pd.notna(value) else f"Column {i + 1}"
        seen[name] = seen.get(name, 0) + 1
        names.append(name if seen[name] == 1 else f"{name}.{seen[name] - 1}")
    frame = grid.iloc[head + 1:].reset_index(drop=True)
    frame.columns = names
    return frame.infer_objects().dropna(how="all")


def read_table(data: bytes) -> pd.DataFrame:
    """Read CSV/TSV/Excel bytes with encoding, delimiter and header-row detection."""
    if not data or not data.strip():
        raise ValueError("The file is empty. Add a header row and sales records.")
    if data[:4] == b"PK\x03\x04":  # .xlsx is a zip container
        return _read_excel(data)
    encodings = ("utf-16",) if data[:2] in (b"\xff\xfe", b"\xfe\xff") else ("utf-8-sig", "cp1252", "latin-1")
    text = None
    for encoding in encodings:
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise ValueError("Could not decode this file. Try saving it as a UTF-8 CSV.")
    lines = [line for line in text.splitlines() if line.strip()]
    best_sep, best_head, best_score = ",", 0, -1
    for sep in (",", ";", "\t", "|"):
        counts = [line.count(sep) + 1 if line.count(sep) else 0 for line in lines[:200]]
        head = _find_header_row(counts)
        typical = counts[head] if counts else 0
        score = sum(1 for c in counts if c == typical) if typical >= 2 else 0
        if score > best_score:
            best_sep, best_head, best_score = sep, head, score
    try:
        frame = pd.read_csv(StringIO("\n".join(lines[best_head:])), sep=best_sep, skipinitialspace=True,
                            on_bad_lines="skip")
    except (pd.errors.EmptyDataError, pd.errors.ParserError) as exc:
        raise ValueError(f"This does not look like a valid CSV ({exc}).") from exc
    frame.columns = [str(c).strip() for c in frame.columns]
    return frame.dropna(how="all").dropna(axis=1, how="all")


def demo_frame() -> pd.DataFrame:
    if SAMPLE.exists():
        return pd.read_csv(SAMPLE)
    return pd.DataFrame()


def detect_columns(raw: pd.DataFrame, overrides: dict | None = None):
    """Map the file's columns onto the canonical fields.

    Returns (mapping, columns). mapping[field] = {"column": name | None, "source": ...} where source is
    auto | guessed | manual | computed | count | none.
    """
    columns = list(raw.columns)
    tokens = {c: set(_tokens(c)) for c in columns}
    keys = {c: _key(c) for c in columns}
    cache: dict = {}

    def rate(kind: str, col: str) -> float:
        if (kind, col) not in cache:
            cache[(kind, col)] = _date_rate(raw[col]) if kind == "date" else _number_rate(raw[col])
        return cache[(kind, col)]

    def fits(field: str, col: str) -> bool:
        if field == "date":
            return rate("date", col) >= 0.6
        if field in NUMERIC_FIELDS:
            return rate("num", col) >= 0.6
        return True

    mapping: dict = {}
    skipped: set = set()
    used: set = set()

    def claim(field: str, col: str, source: str):
        mapping[field] = {"column": col, "source": source}
        used.add(col)

    # 0. Manual choices from the UI always win. "" means "this file has no such column".
    for field, col in (overrides or {}).items():
        if field not in FIELDS or col == "__auto__":
            continue
        if not col:
            skipped.add(field)
        elif col in columns:
            claim(field, col, "manual")

    # 1 + 2. Alias matching: exact first for every field, then fuzzy.
    pending = [f for f in RESOLVE_ORDER if f not in mapping and f not in skipped]
    for fuzzy in (False, True):
        for field in list(pending):
            hit = None
            for alias in ALIASES[field]:
                alias_key, alias_tokens = _key(alias), set(_tokens(alias))
                for col in columns:
                    if col in used or not fits(field, col):
                        continue
                    if fuzzy:
                        matched = alias_tokens <= tokens[col] and not (tokens[col] & EXCLUDE_TOKENS.get(field, set()))
                    else:
                        matched = keys[col] == alias_key
                    if matched:
                        hit = col
                        break
                if hit:
                    break
            if hit:
                claim(field, hit, "auto")
                pending.remove(field)

    # Revenue can be derived from quantity x unit price.
    if "revenue" in pending and {"quantity", "unit_price"} <= mapping.keys():
        mapping["revenue"] = {"column": None, "source": "computed"}
        pending.remove("revenue")

    # 3. Content-based guessing for what is still missing.
    if "date" in pending:
        scored = [(rate("date", c), c) for c in columns if c not in used]
        best = max(scored, default=(0, None))
        if best[1] and best[0] >= 0.8:
            claim("date", best[1], "guessed")
            pending.remove("date")

    if "revenue" in pending:
        numeric = [c for c in columns if c not in used and rate("num", c) >= 0.8 and not (tokens[c] & ID_TOKENS)]
        hinted = [c for c in numeric if tokens[c] & MONEY_HINTS]
        pool = hinted or numeric
        if pool:
            pick = max(pool, key=lambda c: float(_to_number(raw[c]).abs().sum()))
            claim("revenue", pick, "guessed")
            pending.remove("revenue")

    need_product, need_category = "product" in pending, "category" in pending
    if need_product or need_category:
        pool = []
        for c in columns:
            if c in used or pd.api.types.is_numeric_dtype(raw[c]) or tokens[c] & ID_TOKENS:
                continue
            unique = int(raw[c].nunique(dropna=True))
            if unique < 2 or unique > 0.9 * max(len(raw), 1) or rate("date", c) >= 0.6:
                continue
            pool.append((c, unique))
        if need_product and pool:
            pick = max(pool, key=lambda item: item[1])[0]
            claim("product", pick, "guessed")
            pool = [item for item in pool if item[0] != pick]
        if need_category:
            small = [item for item in pool if item[1] <= 30]
            if small:
                claim("category", min(small, key=lambda item: item[1])[0], "guessed")

    return mapping, columns


def _full_mapping(mapping: dict) -> dict:
    out = {f: mapping.get(f, {"column": None, "source": "none"}) for f in FIELDS}
    if out["revenue"]["source"] == "none":
        out["revenue"] = {"column": None, "source": "count"}
    return out


def build_frame(raw: pd.DataFrame, mapping: dict, columns: list, date_order: str = "auto"):
    """Turn the raw upload into the canonical frame. Returns (frame, meta)."""
    notes: list[str] = []
    full = _full_mapping(mapping)
    date_col = full["date"]["column"]
    has_date = date_col is not None
    metric, currency = "revenue", None

    meta = {"mapping": full, "columns": columns, "warnings": notes, "has_date": has_date, "metric": metric,
            "currency": "USD", "date_order": date_order, "date_ambiguous": False}

    # Exact duplicates are judged on the *original* columns, so rows that differ only in a column
    # we don't use (a name, a note) are never mistaken for duplicates.
    duplicate = raw.duplicated()
    if duplicate.any():
        notes.append(f"{int(duplicate.sum()):,} exact duplicate row(s) removed.")
    raw = raw[~duplicate]

    out = pd.DataFrame(index=raw.index)
    if has_date:
        out["date"], meta["date_ambiguous"] = _to_date(raw[date_col], date_order)
        if meta["date_ambiguous"]:
            notes.append("Dates like 04/01/2025 can be read as day/month or month/day. They were read month-first; "
                         "change 'Date order' below if your file is day-first.")
    else:
        out["date"] = pd.Series(pd.NaT, index=raw.index, dtype="datetime64[ns]")

    qty_col, price_col = full["quantity"]["column"], full["unit_price"]["column"]
    rev = full["revenue"]
    if rev["column"]:
        out["revenue"] = _to_number(raw[rev["column"]])
        currency = _detect_currency(raw[rev["column"]], rev["column"])
    elif rev["source"] == "computed":
        out["revenue"] = _to_number(raw[qty_col]) * _to_number(raw[price_col])
        currency = _detect_currency(raw[price_col], price_col)
    else:
        out["revenue"] = 1.0
        metric = "count"
        notes.append("No amount column found, so rows are being counted instead of summing revenue. "
                     "Pick an amount column below to see revenue.")
    out["quantity"] = _to_number(raw[qty_col]) if qty_col else 1

    for field in ("product", "category", "region", "customer"):
        col = full[field]["column"]
        if col:
            text = raw[col].astype("string").str.strip().replace("", pd.NA)
            out[field] = text.fillna("Unknown").astype(str)
        else:
            out[field] = "Unknown"
    if full["order_id"]["column"]:
        out["order_id"] = raw[full["order_id"]["column"]].astype("string").str.strip().fillna("").astype(str)

    if not has_date:
        notes.append("No date column found, so the revenue-over-time chart, growth and date filters are unavailable.")

    # Negative revenue denotes returns/refunds: retained in net sales.
    finite = out["revenue"].notna() & ~out["revenue"].isin([float("inf"), float("-inf")])
    if (~finite).any():
        notes.append(f"{int((~finite).sum()):,} row(s) skipped: the amount was missing or not a number.")
    out = out[finite]
    if has_date:
        missing_date = int(out["date"].isna().sum())
        if missing_date:
            notes.append(f"{missing_date:,} row(s) skipped: the date could not be read.")
        out = out.dropna(subset=["date"])

    meta.update(metric=metric, currency=currency or "USD")
    if out.empty:
        used = [f"{field} = '{info['column']}'" for field, info in full.items()
                if info["column"] and field in ("date", "revenue")]
        detail = f" (using {', '.join(used)})" if used else ""
        raise AnalysisError(
            f"No usable rows were found{detail}. Choose the right date and amount columns below and re-analyze.",
            meta)
    return out, meta


def analyze(frame: pd.DataFrame, has_date: bool = True):
    total = float(frame.revenue.sum())
    has_orders = "order_id" in frame and (frame.order_id != "").any()
    orders = int(frame.order_id[frame.order_id != ""].nunique()) if has_orders else int(len(frame))

    months, growth, date_range = [], None, None
    if has_date:
        monthly = (frame.assign(month=frame.date.dt.to_period("M").astype(str))
                   .groupby("month", as_index=False).revenue.sum())
        months = monthly.to_dict(orient="records")
        current = frame.date.max()
        recent_start = current - pd.Timedelta(days=30)
        prev_start = current - pd.Timedelta(days=60)
        recent = frame.loc[frame.date >= recent_start, "revenue"].sum()
        prior = frame.loc[(frame.date >= prev_start) & (frame.date < recent_start), "revenue"].sum()
        growth = round(float((recent - prior) / abs(prior) * 100), 1) if prior else 0.0
        date_range = {"from": frame.date.min().strftime("%b %d, %Y"), "to": current.strftime("%b %d, %Y")}

    if (frame["product"] != "Unknown").any():
        products = frame.groupby("product", as_index=False).revenue.sum().sort_values("revenue", ascending=False).head(6)
    else:
        products = frame.iloc[0:0].groupby("product", as_index=False).revenue.sum()
    if (frame["region"] != "Unknown").any():
        regions = frame.groupby("region", as_index=False).revenue.sum().sort_values("revenue", ascending=False).head(5)
    else:
        regions = frame.iloc[0:0].groupby("region", as_index=False).revenue.sum()

    customer_rows = frame[frame.customer != "Unknown"]
    repeat = None
    if len(customer_rows):
        if has_orders and (customer_rows.order_id != "").any():
            cust = customer_rows[customer_rows.order_id != ""].groupby("customer").order_id.nunique()
        else:
            cust = customer_rows.groupby("customer").size()
        repeat = round(float(cust[cust > 1].count() / cust.count() * 100), 1) if len(cust) else 0.0

    return {
        "kpis": {"revenue": round(total, 2), "orders": orders,
                 "average_order": round(total / orders, 2) if orders else 0,
                 "growth": growth, "repeat_rate": repeat},
        "months": months,
        "products": [{"name": str(r.product), "value": round(float(r.revenue), 2)} for r in products.itertuples()],
        "regions": [{"name": str(r.region), "value": round(float(r.revenue), 2)} for r in regions.itertuples()],
        "range": date_range,
        "rows": int(len(frame)),
    }


def parse_overrides(raw) -> dict:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        raise ValueError("The column mapping was not valid JSON.")
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if (k in FIELDS or k == "date_order") and (v is None or isinstance(v, str))}


def _looks_like_sales(full: dict) -> bool:
    """A file needs at least a date, or an amount column we are confident about (named, computed or chosen)."""
    return full["date"]["column"] is not None or full["revenue"]["source"] in ("auto", "manual", "computed")


def run_analysis(raw: pd.DataFrame, overrides: dict, filters: dict):
    if raw.empty or len(raw.columns) == 0:
        raise ValueError("The CSV is empty. Add a header row and sales records.")
    if len(raw.columns) > 100:
        raise ValueError("This CSV has too many columns. Keep it to 100 columns or fewer.")
    mapping, columns = detect_columns(raw, overrides)
    date_order = overrides.get("date_order") if overrides.get("date_order") in ("day", "month") else "auto"
    frame, meta = build_frame(raw, mapping, columns, date_order)
    has_date = meta["has_date"]
    if not _looks_like_sales(meta["mapping"]):
        meta["warnings"].append(
            "This file has no obvious date or amount column, so the best-matching columns were picked automatically. "
            "Use the column mapping below to choose which columns to analyze.")
    options = {
        "categories": sorted(frame.category.dropna().astype(str).unique().tolist()),
        "regions": sorted(frame.region.dropna().astype(str).unique().tolist()),
    }
    if has_date:
        if filters.get("from"):
            frame = frame[frame.date >= pd.Timestamp(filters["from"])]
        if filters.get("to"):
            frame = frame[frame.date < pd.Timestamp(filters["to"]) + pd.Timedelta(days=1)]
    if filters.get("category"):
        frame = frame[frame.category == filters["category"]]
    if filters.get("region"):
        frame = frame[frame.region == filters["region"]]
    if frame.empty:
        raise AnalysisError("No sales rows match those filters.", meta)
    result = analyze(frame, has_date)
    result.update(meta)
    result["filter_options"] = options
    return result


@app.get("/")
def home():
    return render_template("index.html")


@app.get("/api/health")
def health():
    """Small deploy smoke-check for the Flask analytics backend."""
    return jsonify({"status": "ok", "service": "SalesScope analytics API"})


@app.after_request
def add_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    return response


@app.get("/styles.css")
def css_asset():
    return send_from_directory(PUBLIC, "styles.css")


@app.get("/app.js")
def js_asset():
    return send_from_directory(PUBLIC, "app.js")


def _error(exc: Exception, prefix: str = ""):
    meta = exc.meta if isinstance(exc, AnalysisError) else {}
    return jsonify({"error": f"{prefix}{exc}", **meta}), 400


@app.route("/api/analytics", methods=["GET", "POST"])
def analytics():
    try:
        if request.method == "POST":
            file = request.files.get("file")
            if not file:
                return jsonify({"error": "Choose a CSV file to analyze."}), 400
            raw, source = read_table(file.read()), request.form
        else:
            raw, source = demo_frame(), request.args
        filters = {key: source.get(key) for key in ("from", "to", "category", "region")}
        return jsonify(run_analysis(raw, parse_overrides(source.get("mapping")), filters))
    except Exception as exc:
        return _error(exc)


@app.post("/api/upload")
def upload():
    file = request.files.get("file")
    if not file or not file.filename.lower().endswith((".csv", ".tsv", ".txt", ".xlsx")):
        return jsonify({"error": "Choose a CSV or Excel (.xlsx) file to upload."}), 400
    try:
        result = run_analysis(read_table(file.read()), parse_overrides(request.form.get("mapping")), {})
        return jsonify({"message": f"Loaded {result['rows']:,} valid sales rows.", **result})
    except Exception as exc:
        return _error(exc, "Could not read this file: ")


@app.get("/api/sample")
def sample():
    return send_file(SAMPLE, as_attachment=True, download_name="sample_sales.csv")


@app.errorhandler(413)
def request_too_large(_error):
    return jsonify({"error": "CSV is too large. The current upload limit is 4 MB."}), 413


if __name__ == "__main__":
    requested_port = int(os.environ.get("PORT", "5001"))
    port = requested_port
    for candidate in range(requested_port, min(requested_port + 10, 65536)):
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", candidate))
                port = candidate
                break
            except OSError:
                continue
    else:
        raise RuntimeError(f"No free port found from {requested_port} to {min(requested_port + 9, 65535)}")
    url = f"http://127.0.0.1:{port}"
    print(f"\nSalesScope is ready at {url}\nKeep this window open while using the dashboard.\n", flush=True)
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", debug=False, port=port)
