"""Admin one-off: rename a user without touching their library.

Library entries, segments files, downloads, and shares are all keyed by the
user's numeric `id`, so renaming only changes the display name.

Usage (local):
    ./venv/bin/python scripts/rename_user.py 'muskaan.agrawal21@gmail.com' muskaan

Usage (Fly):
    fly ssh console
    cd /app && python scripts/rename_user.py 'muskaan.agrawal21@gmail.com' muskaan
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make `backend` importable when run from repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend import auth


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 1
    current, new_username = sys.argv[1], sys.argv[2]

    # Use the same DB path the FastAPI app does on this host.
    repo_root = Path(__file__).resolve().parents[1]
    data_dir = repo_root / "data"
    if not (data_dir / "users.db").exists():
        # Fly's volume mount uses /persistent/data — try that as a fallback.
        alt = Path("/persistent/data/users.db")
        if alt.exists():
            data_dir = alt.parent
        else:
            print(f"users.db not found in {data_dir} or /persistent/data/")
            return 2

    auth.init_auth(data_dir / "users.db")

    target = auth.find_user_by_username(current)
    if not target:
        print(f"No user with username = {current!r}.")
        # Show what's actually there so the operator can fix the input.
        with auth._conn() as c:
            rows = c.execute("SELECT id, username FROM users").fetchall()
        print("Users in this database:")
        for row in rows:
            print(f"  id={row['id']}  username={row['username']!r}")
        return 3

    try:
        updated = auth.update_username(target.id, new_username)
    except auth.AuthError as e:
        print(f"Rename failed: {e}")
        return 4

    print(f"OK: user id={updated.id}: {current!r} → {updated.username!r}")
    print("Library, segments, downloads, and shares are unchanged (keyed by user_id).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
