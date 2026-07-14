#!/usr/bin/env python3
"""Bootstrap a platform (ops) user for Evenly console.

Usage (from repo root, with venv + config):

  python -m scripts.create_platform_user \\
    --email ops@example.com \\
    --username ops_admin \\
    --password 'YourStrongPassword' \\
    --display-name '运营'

If no platform admins exist yet, this script still works (bootstrap).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow `python -m scripts.create_platform_user` from backend root.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a platform ops user")
    parser.add_argument("--email", required=True)
    parser.add_argument("--username", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--display-name", default=None)
    args = parser.parse_args()

    from app.database import SessionLocal
    from app.routers.platform_users import create_platform_user_record
    from app.schemas.user import PlatformUserCreate

    db = SessionLocal()
    try:
        user = create_platform_user_record(
            db,
            PlatformUserCreate(
                email=args.email,
                username=args.username,
                password=args.password,
                display_name=args.display_name,
            ),
        )
        print(f"OK platform user created id={user.id} email={user.email} username={user.username}")
        return 0
    except Exception as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
