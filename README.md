# SalesScope — Sales Intelligence Dashboard

SalesScope is a portfolio project for exploring retail sales performance. It pairs a responsive dashboard with a real Python/Flask + pandas API: the backend reads, validates, normalizes, filters, and aggregates CSV transactions before returning analytics to the UI.

## What it answers

- How much net revenue did we make, and how is it changing?
- How many orders were placed and what was the average order value?
- Which products and regions contribute the most revenue?
- What share of identified customers placed more than one order?
- How do the answers change by date range, product category, or region?

## Features

- KPI cards, monthly revenue chart, top products, regional breakdown, and a data-generated quick read.
- Working date, category, and region filters backed by the Flask analytics endpoint.
- CSV upload with common header aliases; supports revenue or quantity × unit price.
- Distinct order counts when order IDs are present; negative revenue is retained as returns.
- Uploads are processed in memory and are not saved or retained by the app.
- Responsive layouts for desktop, tablet, and mobile; subtle 3D hover motion respects reduced-motion settings.
- Fictional sample transactions are included so the dashboard works immediately.

## Run locally (Windows)

Double-click **Start SalesScope.bat**. The first run creates `.venv`, installs the requirements, and opens the app. Keep the command window open while browsing; close it with Ctrl+C to stop the server.

Or run manually:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

The server checks ports starting at `5001` and opens the first available address. To analyze your own data, use **Import CSV**. The maximum upload is 4 MB.

## Deploy to Vercel

The repository is configured as a Flask project for Vercel's Python Functions. The root-level `app.py` exports `app`; `requirements.txt` installs Flask and pandas; `vercel.json` explicitly bundles the HTML template and sample CSV used at runtime. `public/` contains CDN-served CSS and JavaScript. There is no database, secret, or environment variable to configure for the demo.

1. In Vercel, choose **Add New → Project** and import `anantttt1/Data-Sales-Dashboard` from GitHub.
2. Keep the project root at the repository root and use Vercel's detected Flask/Python settings.
3. Deploy. The dashboard page and `/api/health` should both respond; the latter returns a small JSON health check.

The Python API validates every upload and keeps it in request memory only. Vercel Functions have a 4.5 MB request-body limit, so the app caps CSV files at 4 MB. Do not upload confidential customer data to this public demo. For a real multi-user product, add authentication, private storage, and explicit retention controls first.

## CSV format

Required: a date field (`date`, `order date`, `transaction date`, or `invoice date`) and either a revenue field (`revenue`, `sales`, `amount`, `total`) or both `quantity` and `unit price`.

Optional fields include order ID, product, category, region/country/state, and customer/customer ID. Common header variants are normalized automatically; missing optional dimensions appear as `Unknown`.

## API

| Endpoint | Purpose |
| --- | --- |
| `GET /api/health` | Backend deployment health check |
| `GET /api/analytics?from=YYYY-MM-DD&to=YYYY-MM-DD&category=...&region=...` | Analyze bundled sample data |
| `POST /api/analytics` | Analyze an uploaded CSV with optional date/category/region filters |
| `POST /api/upload` | Validate an uploaded CSV and return its initial analytics |
| `GET /api/sample` | Download the example CSV |

## Project structure

```text
app.py                 Flask API and pandas analytics
templates/index.html   Dashboard markup
public/styles.css      Responsive visual system
public/app.js          Charts, filters, upload flow, API integration
sample_sales.csv       Fictional example data
vercel.json            Function file inclusion for deployment
requirements.txt       Python dependencies
```
