# Moneda API reference

The public contract is rooted at `/api/v1`. All JSON endpoints return `{ success, data, message, errors }`. Authentication uses the Flask session cookie, and browser clients must send credentials from the configured frontend origin. Unversioned `/api` aliases remain temporarily available for local compatibility.

## Authentication

- `POST /api/v1/auth/login` - `{ identifier, password }`; accepts a username, user id, or registered email.
- `POST /api/v1/auth/request-otp` - `{ email, purpose: login|signup|reset }`; rate limited and enumeration-safe.
- `POST /api/v1/auth/verify-otp` - consumes a challenge once.
- `POST /api/v1/auth/register` - completes a verified signup.
- `POST /api/v1/auth/reset-password` - completes a verified reset.
- `POST /api/v1/auth/logout`
- `GET/PATCH /api/v1/me` - profile, permitted customer companies, selected customer context, fixed Moneda issuer settings, and resolved permissions.

## Catalog and pricing

- `GET /api/v1/categories`
- `GET /api/v1/catalog/families` - exactly `blankets`, `mpacks`, and `chemicals`.
- `GET /api/v1/catalog/blankets/categories` - the seven short blanket groups.
- `GET /api/v1/catalog/blankets/products?category=...`
- `GET /api/v1/catalog/blankets/products/:id`
- `GET /api/v1/products?category=&search=&pricing_status=&page=&limit=`
- `GET /api/v1/products/:id`
- `POST /api/v1/products/:id/price-preview` - authoritative server calculation.
- `POST /api/v1/products` - create a pending-price product.
- `PATCH /api/v1/products/:id`
- `PATCH /api/v1/products/:id/pricing` - approve an EUR price, pricing type/unit, optional product tax override, and reason.
- `GET /api/v1/products/:id/price-history`
- `GET /api/v1/exchange-rates?refresh=true`

Money responses are currency-rounded Decimal results. Tax fields are rejected from price-preview, cart, and quotation payloads; only a product can override the company tax rule.

The first-release calculator exposes exactly three active families: Printing Blankets, Underpacking, and Chemicals & Maintenance. Bar options and underlay variants are configured inside the blanket flow rather than exposed as standalone families.

## Customer companies and contacts

- `GET/POST /api/v1/companies` (customer-company records; `/customer-companies` aliases are explicit)
- `POST /api/v1/companies/select-customer` - sets `selected_customer_company_id` in the server session.
- `GET /api/v1/companies/customer-companies/search?q=...`
- `PATCH /api/v1/companies/:id`
- `GET/POST /api/v1/customers?customer_company_id=...`
- `GET/PATCH/DELETE /api/v1/customers/:id`

Moneda Technologies is the permanent quotation issuer. Every customer-company-sensitive request independently verifies the authenticated user's customer-company scope.

## Cart and quotations

- `GET/DELETE /api/v1/cart?customer_company_id=...&currency=...`
- `POST /api/v1/cart/items`
- `PATCH/DELETE /api/v1/cart/items/:id`
- `GET/POST /api/v1/quotations?customer_company_id=...`
- `POST /api/v1/quotations/preview` - validates and recalculates a full quotation without saving or consuming a number.
- `GET/PATCH /api/v1/quotations/:id` - only non-money fields can change while Draft.
- `GET /api/v1/quotations/:id/pdf?preview=true`
- `POST /api/v1/quotations/:id/send`
- `POST /api/v1/quotations/:id/whatsapp` - recorded mock only in demo mode; no real delivery.
- `POST /api/v1/quotations/:id/convert-to-order`

Cart and quotation mutations require the customer company selected in the server session, not merely a browser-supplied id. Creation reads that customer's persistent cart, reloads each product, obtains one server exchange-rate snapshot, recalculates every line from EUR, atomically assigns `MON_Q####`, and stores issuer, customer company, contact, user, rate, tax, transport, and pricing snapshots.

## CRM, reminders, orders, and reports

- `GET/POST/PATCH /api/v1/leads[/:id]`
- `GET/POST/PATCH /api/v1/reminders[/:id]`
- `POST /api/v1/reminders/:id/complete`
- `GET/PATCH /api/v1/orders[/:id]`
- `GET /api/v1/dashboard?customer_company_id=...`
- `GET /api/v1/reports/summary?customer_company_id=...`
- `GET /api/v1/reports/quotations.csv?customer_company_id=...`
- `GET /api/v1/search?q=...&customer_company_id=...`

## Administration

- `GET /api/v1/admin/pricing/products` - searchable product and blanket-bar EUR pricing view.
- `GET/PATCH /api/v1/admin/pricing/products/:id` - detail and protected EUR-only price edit.
- `GET /api/v1/admin/pricing/history/:id` - immutable product/variant/bar price history.
- `GET/POST/PATCH /api/v1/admin/users[/:id]`
- `GET/PATCH /api/v1/admin/roles[/:id]`
- `GET /api/v1/admin/audit-logs`
- `GET/PATCH /api/v1/settings` - includes the fixed `issuer` block for Moneda Technologies.
- `POST /api/v1/admin/import/products/validate` - multipart CSV validation preview; no commit during validation.

`GET /api/v1/openapi.json` exposes a compact machine-readable endpoint index. Authorization failures use 401/403, validation uses 422, conflicts use 409, missing records use 404, and external-provider outages use 503.
