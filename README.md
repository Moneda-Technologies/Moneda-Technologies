# Moneda Technologies Business Platform

Moneda is a modular product calculator, quotation, CRM, and sales-management platform for printing and converting materials. The legacy CGI material is used only as a catalog and workflow reference; no legacy code, interface, architecture, secrets, pricing, or branding is used.

The implementation is a strict TypeScript/Vite client backed by a blueprint-based Flask REST API, service layer, and MongoDB repository. The public API contract is `/api/v1`.

## Release baseline

- Customer-first workflow: sign in, select a permitted customer company, select a product family, configure the product, add to cart, preview, generate, share, and follow up. Moneda Technologies is always the issuing company.
- Three active calculator families: Printing Blankets, Underpacking, and Chemicals & Maintenance. Bar options and underlay variants are embedded inside the blanket data model; bars are never shown as a standalone family.
- 39 unique release products across the three canonical family catalogues. Missing commercial prices remain explicitly pending; they are never fabricated.
- EUR master catalog with backend-only EUR-to-USD/INR conversion, cached provider metadata, and visible stale-rate state.
- Decimal pricing for fixed, per-piece, per-square-metre, litre, weight, length, and Underpacking formula products.
- Cut Format and Bar Format are the only blanket formats. The catalogued 5% cut-format surcharge is deliberately disabled for this release.
- Company tax defaults (Indian companies 18% by default, international companies no tax by default) with an optional product-only override. There is no quotation-level tax override.
- Persistent cart, `MON_Q0001` quotations, immutable commercial snapshots, `MON_ORD00001` orders, PDF/email/print actions, and an explicitly mocked WhatsApp action in demo mode.
- CRM leads, reminders, dashboard, reports, audit history, roles, permissions, settings, customer-company scope, and a dedicated Moneda issuer configuration.
- Username/user-id/password login plus email OTP flows for login, signup, and password reset.

## Pricing contract

```text
master catalog configuration
        -> approved EUR base price
        -> selected EUR / USD / INR currency
        -> trusted product adjustments and commercial rules
        -> authorized line discount
        -> taxable amount
        -> product tax override OR company default
        -> exclusive / inclusive / no-tax calculation
        -> immutable quotation snapshot
```

All totals are recalculated on the backend. Attempts to submit tax overrides through the calculator, cart, or quotation endpoints return `422`.

## Architecture

```text
frontend/                       Vite and strict TypeScript application
  src/api/                      centralized Fetch API client
  src/components/               dialogs, toasts, icons, and page primitives
  src/layouts/                  permission-aware responsive shell
  src/pages/                    auth, company picker, calculator, cart, CRM, admin
  src/state/                    user/company/currency/cart state
backend/
  app/auth/                     password and OTP lifecycle
  app/catalog/                  product/category read models
  app/pricing/                  Decimal pricing and tax engines
  app/exchange_rates/           EUR provider abstraction and cache
  app/quotations/               snapshots, numbering, PDF, and sharing
  app/customers|crm|orders/     company-scoped business modules
  app/admin/                    products, EUR prices, users, settings, imports
  app/repositories/             MongoDB and isolated demo/test repository
  app/middleware/               authentication, permissions, and company scope
data/                           authoritative first-install seed files
templates/quotation/            print-quality Moneda quotation template
```

MongoDB is the production source of truth. `MemoryStore` is available only in explicit demo/test mode and is not a production fallback.

## Local development

Requirements: Python 3.12+, Node.js 20+, and MongoDB for production mode.

Copy `.env.example` to `.env`. For a disposable local workspace, use `DEMO_MODE=true` and leave `MONGODB_URI` empty.
Demo/test mode seeds automatically. Production should use `AUTO_SEED=false` and run the explicit management seed only for an approved first install or migration.

Backend:

```powershell
pip install -r backend\requirements.txt
python backend\run.py
```

The API listens on `http://localhost:5005`.

Frontend:

```powershell
cd frontend
npm install
npm run dev
```

The application listens on `http://localhost:3005` and proxies `/api` to port 5005.

Local superadmin credentials:

- Username or ID: `Admin` (the label `Username Admin` is also accepted)
- Password: `123@Admin`

These credentials exist only in demo mode and must not be used in production.

## Catalog and pricing seed

The active, human-editable seed inputs are exactly:

```text
data/product_types.json
data/blanket_categories.json
data/blanket_options.json
data/blanket_bars.json
data/blankets.json
data/mpack_types.json
data/mpack_options.json
data/mpacks.json
data/chemical_categories.json
data/chemical_options.json
data/chemicals.json
data/pricing_eur.json
data/tax_rules.json
```

Product files own identity and technical specifications only. Shared type,
category and option files own reusable configuration. `pricing_eur.json` owns
all product master prices; `blanket_bars.json` owns bar identity and EUR/bar
pricing; `tax_rules.json` owns tax rules. Underlays remain a distinct section
inside `blankets.json`. No legacy or future catalogue is loaded.

Validate the canonical files with:

```powershell
python scripts\audit_catalog.py
```

Seed the runtime store idempotently with:

```powershell
python backend\manage.py seed
```

JSON files are seed/import data only. MongoDB is the runtime source of truth
after seeding, while the backend pricing service remains authoritative for
calculations. Unknown commercial values stay `null`; they are never inferred
from legacy INR data.

After first installation, MongoDB is authoritative. Each Admin price edit appends an immutable price-history record with the old/new EUR price, price scope, actor, timestamp, unit, status, and optional reason. Managers have read/history access but cannot edit prices.

## Exchange rates

The current provider is the ECB reference-rate feed exposed through Frankfurter. It uses EUR as the base and requests USD and INR targets. MongoDB cache records include base/target currencies, provider date, fetched/expiry timestamps and source. If a refresh fails, the last cached record is returned as stale; if no rate exists, the calculation fails instead of inventing one.

## PDF, email, and WhatsApp

The official logo remains at the configured `brand_logo_path` and is not altered by the application. Change that setting later to replace the file without changing template code.

Quotations use the Moneda HTML/CSS template and WeasyPrint in production/Docker. A ReportLab/SVG fallback keeps local Windows PDF generation functional when the native Pango runtime is unavailable. Both paths include parties, article numbers, product configuration, currency/rate metadata, line discounts, product tax, transport, commercial conditions, totals, page numbers, and authorization space.

Set the Zoho SMTP variables (`MAIL_HOST`, `MAIL_PORT`, `MAIL_USERNAME`, `MAIL_PASSWORD`, `MAIL_QUOTATION_FROM`, `MAIL_ORDER_FROM`, and `MAIL_GENERAL_FROM`) for real email delivery. `MAIL_FROM` remains a backward-compatible alias for the general sender. Missing credentials produce a clear delivery failure; demo mode records messages without claiming delivery. WhatsApp records a clear mock result in demo mode; it never claims a real message was delivered.

## Testing and build

```powershell
python -m pytest backend\tests -q
cd frontend
npm run build
```

The backend suite covers pricing, tax, automatic rules, product overrides, authentication, versioned APIs, company scope, cart behavior, quotation numbering, and snapshot immutability. TypeScript strict checking runs before every frontend build.

See [API reference](docs/API.md) and [architecture notes](docs/ARCHITECTURE.md).
