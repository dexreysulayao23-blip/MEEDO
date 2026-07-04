# MEEDO Revenue Intelligence

Web-based revenue tracking and forecasting system for the **Municipal Economic Enterprise & Development Office (MEEDO), Tagbina**. Staff enter monthly income by category, view live totals across the office, and get short-term forecasts powered by ARIMA models to support budgeting and reports.

![Python](https://img.shields.io/badge/Python-3.10+-3776AB?logo=python&logoColor=white)
![Flask](https://img.shields.io/badge/Flask-2.3-000000?logo=flask&logoColor=white)
![SQLite](https://img.shields.io/badge/SQLite-local-003B57?logo=sqlite&logoColor=white)

---

## Features

| Area | What it does |
|------|----------------|
| **Revenue workspace** | Enter monthly revenue (pesos or tickets sold) per income category and year, with optional day-by-day breakdown |
| **Live tracker sync** | Shared SQLite-backed data with Server-Sent Events and long-polling so multiple users see updates in real time |
| **ARIMA forecasting** | Per-category time-series models predict monthly and yearly revenue with confidence scores |
| **Decision support** | Forecast insights, year-over-year comparison, and coaching-style recommendations in the UI |
| **Ticket pads** | Register numbered ticket stubs/books, assign them from a waiting list, and tie sales to categories |
| **Reports** | Generate period summaries and export CSV reports |
| **User & role management** | Session-based login with admin/staff roles, custom roles, and account settings |
| **Password recovery** | Security-question reset or email OTP (when SMTP is configured) |

### Default income categories

On first run, the system seeds ten MEEDO revenue sources:

- Bus Ticket 1 & 2 (`BUS-1`, `BUS-2`)
- Delivery Truck, Motorized Tricycle
- Toilet/Lavatory, Street Foods
- Market Liner, Tabo
- Stall Rental, Market Electric

Admins can add, rename, disable, or remove categories from the dashboard.

---

## Tech stack

| Layer | Technology |
|-------|------------|
| Backend | [Flask](https://flask.palletsprojects.com/) |
| Database | SQLite (`meedo_revenue.db`, WAL mode) |
| Forecasting | [statsmodels](https://www.statsmodels.org/) ARIMA, [scikit-learn](https://scikit-learn.org/) metrics |
| Data processing | [pandas](https://pandas.pydata.org/), [openpyxl](https://openpyxl.readthedocs.io/) |
| Frontend | HTML templates, vanilla JavaScript, [Chart.js](https://www.chartjs.org/) |
| Model persistence | [joblib](https://joblib.readthedocs.io/) (`.pkl` files in `models/`) |

---

## Project structure

```
MeedoWeb/
├── app.py                 # Flask routes, auth, API, SSE live sync
├── database.py            # SQLite schema, queries, user/category management
├── arima_predictor.py     # ARIMA training, prediction, model persistence
├── data_loader.py         # Historical Excel import and sample data fallback
├── reset_admin_password.py # Local admin password recovery CLI
├── requirements.txt
├── .env.example           # SMTP and optional dev settings (copy to .env)
├── templates/
│   ├── index.html         # Main dashboard
│   └── login.html
├── static/
│   ├── css/style.css
│   └── js/script.js
├── models/                # Trained ARIMA models (created at runtime)
├── docs/                  # Objective-to-code traceability map
└── dataclean.ipynb        # Data cleaning notebook (optional reference)
```

---

## Requirements

- **Python 3.10+** recommended (tested with dependencies in `requirements.txt`)
- pip
- A modern web browser

Optional:

- `Newcleandata.xlsx` in the project root for historical training data (see [Historical data](#historical-data))
- Gmail App Password (or other SMTP) for email-based password reset

---

## Quick start

### 1. Clone and install

```bash
git clone https://github.com/dexreysulayao23-blip/Meedo-tagbina.git
cd Meedo-tagbina
python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

### 2. Configure environment (optional)

Copy the example env file and edit as needed:

```bash
copy .env.example .env   # Windows
# cp .env.example .env   # macOS / Linux
```

See [Environment variables](#environment-variables) for all supported keys.

### 3. Create an admin account

On first launch, if the `users` table is empty:

- Set `MEEDO_ADMIN_PASSWORD` in `.env` before starting the app, **or**
- Run the recovery script after the database is created:

```bash
python reset_admin_password.py --default
```

Default dev credentials when using `--default`:

| Field | Value |
|-------|-------|
| Username | `admin` |
| Password | `admin123` |

Change this password immediately after login on any shared machine (**Account → Change password**).

### 4. Run the application

```bash
# Development (auto-reload, optional OTP in API responses without SMTP)
set MEEDO_DEV=1
python app.py
```

Open **http://localhost:3000** in your browser and sign in.

The server listens on `0.0.0.0:3000`. Model training runs in a background thread on startup so the UI is available while ARIMA models load.

---

## Environment variables

| Variable | Description |
|----------|-------------|
| `MEEDO_DEV` | Set to `1` for Flask debug/reload and dev-only helpers (e.g. OTP in JSON when SMTP is off) |
| `MEEDO_ADMIN_USERNAME` | Username for the first admin account (default: `admin`) |
| `MEEDO_ADMIN_PASSWORD` | Password used when seeding the first admin on an empty database |
| `MEEDO_SMTP_HOST` | SMTP host (e.g. `smtp.gmail.com`) |
| `MEEDO_SMTP_PORT` | SMTP port (default: `587`) |
| `MEEDO_SMTP_USER` | SMTP login |
| `MEEDO_SMTP_PASS` | SMTP password (use a Gmail **App Password**, not your normal password) |
| `MEEDO_SMTP_FROM` | From address for outbound mail |
| `MEEDO_SMTP_TLS` | Enable STARTTLS (`1` / `0`, default `1`) |

After editing `.env`, restart `python app.py`.

---

## Historical data

At startup, the system tries to load **`Newcleandata.xlsx`** (sheet `Sheet1`) from the project root. Column names are mapped to income categories (e.g. `Stall Rental` → `MARKET-RENTAL STALL-SPACE`).

If the file is missing or invalid, **synthetic sample data** is used so forecasting and the UI still work for development.

Place your cleaned Excel export in the root as `Newcleandata.xlsx`, or adapt `data_loader.py` for your file layout.

---

## Usage overview

### Staff workflow

1. Sign in at `/login`.
2. Choose an **income category** and **year** in the revenue workspace.
3. Enter monthly amounts (pesos or ticket counts, depending on category settings).
4. Open **Forecast & insights** to view ARIMA projections and recommendations.
5. Use **All revenue** for a cross-category summary table.

### Admin workflow

Admins additionally manage:

- **Users** — create accounts, assign roles, enable/disable users, reset passwords
- **Roles** — define custom roles with or without admin privileges
- **Categories** — add/edit/disable income categories and ticket unit prices
- **Ticket pads** — maintain stub inventory and the unassigned waiting list
- **Tracker reset** — wipe shared tracker data for a given year (API: `/api/tracker/reset-year`)

---

## API overview

Most routes require an authenticated session (`login_required`). Admin-only routes use `admin_required`.

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/me` | Current user profile |
| GET | `/api/categories` | List income categories |
| GET/POST | `/api/tracker/<source>/<year>` | Read/write monthly tracker entries |
| GET | `/api/tracker/stream/<source>/<year>` | SSE stream for live tracker updates |
| GET | `/auto-predict/<source>` | Auto forecast for a category |
| POST | `/predict` | Manual prediction from user-supplied monthly income |
| POST | `/generate-report` | Report generation (CSV/JSON) |
| GET | `/yoy-comparison/<source>` | Year-over-year totals and growth |
| POST | `/retrain` | Retrain ARIMA models (admin) |

Full route definitions live in `app.py`.

---

## Database

SQLite file: **`meedo_revenue.db`** (created automatically).

Main tables include:

- `historical_data` — imported Excel history
- `tracker_monthly` / tracker daily tables — shared live revenue entries
- `categories` — income sources
- `users`, `roles` — authentication and authorization
- `predictions`, `user_income_inputs` — forecast history and manual inputs
- `ticket_pads` — stub/book tracking

WAL journaling and busy timeouts are enabled to reduce lock errors under concurrent use.

---

## Troubleshooting

| Issue | Suggestion |
|-------|------------|
| Cannot log in / admin disabled | Run `python reset_admin_password.py --default` or set `MEEDO_ADMIN_PASSWORD` and reset via script |
| Email reset fails (SMTP 535) | Use a Gmail App Password in `MEEDO_SMTP_PASS`; confirm `.env` is loaded and restart the server |
| Forecasts show “training” or empty | Wait for background initialization to finish; ensure historical or sample data exists |
| `database is locked` | Retry the action; avoid multiple heavy writes at once; WAL mode should mitigate most cases |
| Stale UI in dev | Hard-refresh the browser; dev mode disables caching on HTML and static assets |

---

## Development notes

- Set `MEEDO_DEV=1` for template auto-reload and Flask debug mode.
- Trained models are saved under `models/` as `arima_<source>.pkl`.
- Objective-to-code mapping for documentation/reviews: `docs/objective-line-map.csv` and `docs/objective-line-map.html`.
- **Do not commit** `.env` or `meedo_revenue.db` with real credentials or production data (`.env` is gitignored).

---

## Security considerations

This project is intended for **local or trusted municipal network** deployment:

- Change the Flask `secret_key` in `app.py` before production use.
- Use strong admin passwords and disable unused accounts.
- Configure HTTPS and a production WSGI server (e.g. Gunicorn + reverse proxy) for internet-facing installs.
- Restrict SMTP and admin recovery tools to authorized personnel.

---

## License

No license file is included in this repository. Contact MEEDO Tagbina or the repository owner for usage terms.

---

## Acknowledgments

Built for **MEEDO Tagbina** to support municipal economic enterprise revenue planning, reporting, and data-driven decision making.
