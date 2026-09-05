from __future__ import annotations

import argparse
import os

from app import create_app
from app.services.seed import seed


def main() -> None:
    parser = argparse.ArgumentParser(description="Moneda Technologies administration")
    parser.add_argument("command", choices=["seed", "create-superadmin"])
    args = parser.parse_args()
    # Management commands decide explicitly when bootstrap data is written.
    app = create_app({"AUTO_SEED": False})
    with app.app_context():
        store = app.extensions["store"]
        if args.command == "seed":
            seed(store, app.config["DATA_DIRECTORY"], demo_mode=app.config["DEMO_MODE"])
            print("Seed data and indexes are ready.")
            return
        email = os.getenv("INITIAL_SUPERADMIN_EMAIL", "").strip().lower()
        name = os.getenv("INITIAL_SUPERADMIN_NAME", "").strip()
        if not email or "@" not in email or len(name) < 2:
            raise SystemExit("Set INITIAL_SUPERADMIN_EMAIL and INITIAL_SUPERADMIN_NAME first.")
        existing = store.find_one("users", {"email": email})
        if existing:
            store.update_one("users", {"_id": existing["_id"]}, {"role_id": "superadmin", "active": True})
            print("Existing account promoted to superadmin.")
        else:
            store.insert_one("users", {
                "email": email, "name": name, "phone": os.getenv("INITIAL_SUPERADMIN_PHONE", ""),
                "role_id": "superadmin", "company_ids": [], "active": True, "currency_preference": "USD",
            })
            print("Superadmin created. Use email OTP to sign in.")


if __name__ == "__main__":
    main()
