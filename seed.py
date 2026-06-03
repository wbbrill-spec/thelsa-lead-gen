"""Seed the database with the 5 known Thelsa users.

Run once after first deploy:
    python seed.py

Safe to re-run — skips users that already exist (matched by full_name).
"""

from __future__ import annotations
import sys
from models import create_all_tables, User
from db import get_db

USERS = [
    {
        "full_name": "Armando Silveyra",
        "specialty": "Household goods, personal effects, destination/settling-in, immigration",
        "oauth_provider": "google",
        "is_active": True,
    },
    {
        "full_name": "Gustavo Gonzalez",
        "specialty": "Commercial and office moving",
        "oauth_provider": "google",
        "is_active": True,
    },
    {
        "full_name": "Bill Brill",
        "specialty": None,
        "oauth_provider": "google",
        "is_active": True,
    },
    {
        "full_name": "Sergio Garza",
        "specialty": None,
        "oauth_provider": "google",
        "is_active": True,
    },
    {
        "full_name": "Martha Vanegas",
        "specialty": None,
        "oauth_provider": "google",
        "is_active": True,
    },
]


def seed():
    print("Creating tables...")
    create_all_tables()

    print("Seeding users...")
    with get_db() as db:
        for u in USERS:
            existing = db.query(User).filter_by(full_name=u["full_name"]).first()
            if existing:
                print(f"  ↷ {u['full_name']} already exists, skipping.")
            else:
                user = User(**u)
                db.add(user)
                print(f"  ✓ Added {u['full_name']}")

    print("Seed complete.")


if __name__ == "__main__":
    seed()
