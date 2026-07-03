#!/usr/bin/env python3
"""
Local recovery: reset the MEEDO admin password in SQLite (enables account if disabled).

Run from the project folder (same directory as meedo_revenue.db):

  python reset_admin_password.py --default

That sets username "admin" to the dev default password documented below.
Or pass a password explicitly:

  python reset_admin_password.py "YourNewPassword"

Or use environment variable (no echo in shell history):

  set MEEDO_ADMIN_PASSWORD=YourNewPassword
  python reset_admin_password.py

Change the password after login on any shared machine.
"""
from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from database import Database  # noqa: E402

# Documented local-dev default when using --default (not used in production HTTP).
DEFAULT_DEV_PASSWORD = "admin123"


def main() -> int:
    parser = argparse.ArgumentParser(description="Reset MEEDO admin password (SQLite recovery).")
    parser.add_argument(
        "password",
        nargs="?",
        help="New password (min 4 characters). Omitted: use MEEDO_ADMIN_PASSWORD env.",
    )
    parser.add_argument(
        "--username",
        default=os.environ.get("MEEDO_ADMIN_USERNAME", "admin").strip() or "admin",
        help="Username to reset (default: admin or MEEDO_ADMIN_USERNAME).",
    )
    parser.add_argument(
        "--default",
        action="store_true",
        help=f"Set password to local dev default: {DEFAULT_DEV_PASSWORD}",
    )
    args = parser.parse_args()

    if args.default:
        new_pw = DEFAULT_DEV_PASSWORD
    elif args.password is not None and str(args.password).strip():
        new_pw = str(args.password).strip()
    else:
        new_pw = str(os.environ.get("MEEDO_ADMIN_PASSWORD", "") or "").strip()

    if not new_pw or len(new_pw) < 4:
        print(
            "Error: need a password (min 4 characters).\n"
            "  python reset_admin_password.py --default\n"
            '  python reset_admin_password.py "MyPassword"\n'
            "  set MEEDO_ADMIN_PASSWORD=... then python reset_admin_password.py",
            file=sys.stderr,
        )
        return 1

    db = Database()
    try:
        db.recovery_set_password_and_enable(args.username, new_pw)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 1
    except Exception as e:
        print("Database error:", e, file=sys.stderr)
        return 1

    print(f"[OK] User {args.username!r} password updated and account enabled.")
    if args.default:
        print(f"     Log in with username: {args.username!r}  password: {DEFAULT_DEV_PASSWORD!r}")
        print("     Change this password after login (Account) on any non-private PC.")
    else:
        print(f"     Log in with username: {args.username!r}  (use the password you just set).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
