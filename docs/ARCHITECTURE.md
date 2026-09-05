# Architecture decisions

## Domain boundaries

Routes validate transport concerns and call services. Pricing, tax, FX, quotation creation, email, WhatsApp, persistence, and audit behavior have separate modules. `Store` isolates PyMongo and makes unit/API tests deterministic; the memory implementation is unavailable unless test/demo mode is explicit.

## Money and history

Python `Decimal` is used until values have been currency-rounded. Product configuration produces an EUR master unit price; the ECB reference rates exposed by Frankfurter convert it to USD/INR when required; the authorized line discount is applied; then the product's tax configuration (falling back to company rate where appropriate) produces net, tax and line total. Rate cache metadata and the exact exchange rate are stored in each quotation snapshot, so historical quotations are never rebuilt from current product documents or today's rate.

## Access model

Roles resolve into explicit permission strings. Route decorators enforce a permission; company-sensitive routes additionally verify the target company exists and is within the account scope. The frontend uses the same permissions to remove unavailable navigation/actions, but server enforcement is independent.

## Data ownership

The data directory contains exactly thirteen explicit sources: product types;
blanket categories, options, bars and products; Underpacking types, options and
products; chemical categories, options and products; EUR pricing; and tax
rules. The seed service names every file and never auto-discovers catalogues.
Product documents contain identity/specification only. Product master prices
live only in `pricing_eur.json`, while bar pricing lives with the reusable bar
components in `blanket_bars.json`. MongoDB becomes authoritative after an
approved seed. Production web startup does not reseed when `AUTO_SEED=false`.
No legacy catalogue or JSON business-record fallback exists.

## Extensibility

Provider contracts isolate exchange rates, email and WhatsApp. New product formulas belong in the pricing service and must include tests. Inventory, payments, accounting sync, customer acceptance, and mobile clients can consume the existing REST boundaries without embedding logic in the browser.
