from __future__ import annotations

from io import BytesIO
import os
import socket
import threading
import webbrowser
import math
from pathlib import Path

import pandas as pd
from flask import Flask, jsonify, render_template, request, send_file, send_from_directory

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024
ROOT = Path(__file__).parent
SAMPLE = ROOT / "sample_sales.csv"
PUBLIC = ROOT / "public"


def demo_frame():
    if SAMPLE.exists():
        return pd.read_csv(SAMPLE)
    return pd.DataFrame()


def clean_frame(frame: pd.DataFrame):
    if frame.empty or len(frame.columns) == 0:
        raise ValueError("The CSV is empty. Add a header row and sales records.")
    if len(frame.columns) > 100:
        raise ValueError("This CSV has too many columns. Keep it to 100 columns or fewer.")
    # Normalize common column names into the small canonical schema.
    aliases = {
        "date": ["date", "order date", "transaction date", "invoice date"],
        "product": ["product", "product name", "item", "description"],
        "category": ["category", "product category"],
        "region": ["region", "country", "state", "location"],
        "customer": ["customer", "customer id", "customer_id", "client id"],
        "order_id": ["order id", "order_id", "invoice", "invoice id", "transaction id"],
        "quantity": ["quantity", "qty", "units"],
        "unit_price": ["unit price", "unit_price", "price", "selling price"],
        "revenue": ["revenue", "sales", "amount", "total", "total sales"],
    }
    lookup = {str(c).strip().lower(): c for c in frame.columns}
    renames = {}
    for target, names in aliases.items():
        for name in names:
            if name in lookup:
                renames[lookup[name]] = target
                break
    frame = frame.rename(columns=renames).copy()
    if "date" not in frame:
        raise ValueError("CSV needs a date column (for example: date or order date).")
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    if "quantity" in frame:
        frame["quantity"] = pd.to_numeric(frame["quantity"], errors="coerce")
    if "unit_price" in frame:
        frame["unit_price"] = pd.to_numeric(frame["unit_price"], errors="coerce")
    if "revenue" in frame:
        frame["revenue"] = pd.to_numeric(frame["revenue"], errors="coerce")
    elif {"quantity", "unit_price"}.issubset(frame.columns):
        frame["revenue"] = frame["quantity"] * frame["unit_price"]
    else:
        raise ValueError("CSV needs a revenue/sales column, or both quantity and unit price columns.")
    finite_revenue = frame.revenue.map(lambda value: math.isfinite(float(value)) if pd.notna(value) else False)
    frame = frame.loc[finite_revenue].dropna(subset=["date"]).drop_duplicates()
    # Negative revenue denotes returns/refunds: retain these in net sales.
    for col in ("product", "category", "region", "customer"):
        if col not in frame:
            frame[col] = "Unknown"
        frame[col] = frame[col].fillna("Unknown").astype(str)
    if "quantity" not in frame:
        frame["quantity"] = 1
    if "order_id" in frame:
        frame["order_id"] = frame["order_id"].fillna("").astype(str)
    return frame


def analyze(frame):
    total = float(frame.revenue.sum())
    orders = int(frame.order_id[frame.order_id != ""].nunique()) if "order_id" in frame and (frame.order_id != "").any() else int(len(frame))
    monthly = frame.assign(month=frame.date.dt.to_period("M").astype(str)).groupby("month", as_index=False).revenue.sum()
    months = monthly.to_dict(orient="records")
    products = frame.groupby("product", as_index=False).revenue.sum().sort_values("revenue", ascending=False).head(6)
    regions = frame.groupby("region", as_index=False).revenue.sum().sort_values("revenue", ascending=False).head(5)
    current = frame.date.max()
    prev_start = current - pd.Timedelta(days=60)
    recent_start = current - pd.Timedelta(days=30)
    recent = frame.loc[frame.date >= recent_start, "revenue"].sum()
    prior = frame.loc[(frame.date >= prev_start) & (frame.date < recent_start), "revenue"].sum()
    growth = ((recent - prior) / abs(prior) * 100) if prior else 0
    customer_rows = frame[frame.customer != "Unknown"]
    if "order_id" in customer_rows and (customer_rows.order_id != "").any():
        cust = customer_rows[customer_rows.order_id != ""].groupby("customer").order_id.nunique()
    else:
        cust = customer_rows.groupby("customer").size()
    repeat = float((cust[cust > 1].count() / cust.count() * 100)) if len(cust) else 0
    return {
        "kpis": {"revenue": round(total, 2), "orders": orders,
                 "average_order": round(total / orders, 2) if orders else 0,
                 "growth": round(float(growth), 1), "repeat_rate": round(repeat, 1)},
        "months": months,
        "products": [{"name": str(r.product), "value": round(float(r.revenue), 2)} for r in products.itertuples()],
        "regions": [{"name": str(r.region), "value": round(float(r.revenue), 2)} for r in regions.itertuples()],
        "range": {"from": frame.date.min().strftime("%b %d, %Y"), "to": current.strftime("%b %d, %Y")},
        "rows": orders,
        "filter_options": {
            "categories": sorted(frame.category.dropna().astype(str).unique().tolist()),
            "regions": sorted(frame.region.dropna().astype(str).unique().tolist()),
        },
    }


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


@app.route("/api/analytics", methods=["GET", "POST"])
def analytics():
    try:
        if request.method == "POST":
            file = request.files.get("file")
            if not file:
                return jsonify({"error": "Choose a CSV file to analyze."}), 400
            frame = clean_frame(pd.read_csv(BytesIO(file.read())))
            start, end = request.form.get("from"), request.form.get("to")
            category, region = request.form.get("category"), request.form.get("region")
        else:
            frame = clean_frame(demo_frame())
            start, end = request.args.get("from"), request.args.get("to")
            category, region = request.args.get("category"), request.args.get("region")
        categories = sorted(frame.category.dropna().astype(str).unique().tolist())
        regions = sorted(frame.region.dropna().astype(str).unique().tolist())
        if start:
            frame = frame[frame.date >= pd.Timestamp(start)]
        if end:
            frame = frame[frame.date < pd.Timestamp(end) + pd.Timedelta(days=1)]
        if category:
            frame = frame[frame.category == category]
        if region:
            frame = frame[frame.region == region]
        if frame.empty:
            return jsonify({"error": "No sales rows in that date range."}), 400
        result = analyze(frame)
        result["filter_options"] = {"categories": categories, "regions": regions}
        return jsonify(result)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/upload")
def upload():
    file = request.files.get("file")
    if not file or not file.filename.lower().endswith(".csv"):
        return jsonify({"error": "Choose a CSV file to upload."}), 400
    try:
        raw = pd.read_csv(BytesIO(file.read()))
        frame = clean_frame(raw)
        if frame.empty:
            raise ValueError("No valid sales rows found. Check the date and revenue values.")
        return jsonify({"message": f"Loaded {len(frame):,} valid sales rows.", **analyze(frame)})
    except Exception as exc:
        return jsonify({"error": f"Could not read this file: {exc}"}), 400


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
