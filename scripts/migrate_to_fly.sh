#!/usr/bin/env bash
# Bundle the local users.db + library + videos, push them onto the Fly
# volume, then rename the user account to whatever was passed as $1
# (default: muskaan).
#
# Usage:  bash scripts/migrate_to_fly.sh [new_username]

set -euo pipefail

NEW_USERNAME="${1:-muskaan}"
APP_URL="https://dance-step-splitter.fly.dev"
ARCHIVE="/tmp/dss-migration.tar.gz"

cd "$(dirname "$0")/.."

PY="./venv/bin/python"

echo "=== Step 1/6: Inspect local data ==="
if [ ! -f data/users.db ]; then
    echo "  No data/users.db on this Mac — nothing to migrate."
    echo "  Just sign up fresh at $APP_URL"
    exit 1
fi

LOCAL_USERNAME=$($PY -c "
import sqlite3
rows = list(sqlite3.connect('data/users.db').execute('SELECT username FROM users ORDER BY id LIMIT 1'))
print(rows[0][0] if rows else '')
")

if [ -z "$LOCAL_USERNAME" ]; then
    echo "  Local users.db has no users. Nothing to migrate."
    echo "  Sign up fresh at $APP_URL"
    exit 1
fi

echo "  Local username: $LOCAL_USERNAME"
$PY -c "
import sqlite3, os
users = list(sqlite3.connect('data/users.db').execute('SELECT id, username FROM users'))
print('  Users in DB:', users)
for sub in ('data/users', 'downloads/users'):
    if os.path.isdir(sub):
        print(f'  {sub}:', os.listdir(sub))
"

echo ""
echo "=== Step 2/6: Bundle data + downloads + DB ==="
TAR_ARGS=()
[ -f data/users.db ]      && TAR_ARGS+=("data/users.db")
[ -d data/users ]         && TAR_ARGS+=("data/users")
[ -d downloads/users ]    && TAR_ARGS+=("downloads/users")
# Legacy (pre-auth) locations — include them so _claim_legacy_library can run on Fly too.
[ -f data/library.json ]  && TAR_ARGS+=("data/library.json")
for f in data/*.json downloads/*.mp4 downloads/*.mov downloads/*.webm; do
    [ -e "$f" ] && [[ "$f" != *users.db* ]] && TAR_ARGS+=("$f")
done

if [ ${#TAR_ARGS[@]} -eq 0 ]; then
    echo "  Nothing to bundle. Aborting."
    exit 1
fi

tar -czf "$ARCHIVE" "${TAR_ARGS[@]}"
echo "  Archive: $ARCHIVE ($(du -h "$ARCHIVE" | cut -f1))"

echo ""
echo "=== Step 3/6: Wake the Fly machine ==="
curl -fsS -o /dev/null "$APP_URL/api/health"
sleep 2
echo "  awake."

echo ""
echo "=== Step 4/6: Upload archive to Fly volume ==="
# fly ssh sftp shell reads commands from stdin.
printf 'put %s /persistent/dss-migration.tar.gz\nquit\n' "$ARCHIVE" \
    | fly ssh sftp shell

echo ""
echo "=== Step 5/6: Extract on Fly volume ==="
fly ssh console -C "bash -c 'cd /persistent && tar -xzf dss-migration.tar.gz && rm dss-migration.tar.gz && ls -la data/users.db data/users 2>&1'"

echo ""
echo "=== Step 6/6: Rename user '$LOCAL_USERNAME' → '$NEW_USERNAME' on Fly ==="
fly ssh console -C "python /app/scripts/rename_user.py '$LOCAL_USERNAME' '$NEW_USERNAME'"

rm -f "$ARCHIVE"

echo ""
echo "✓ Migration complete."
echo "  Go to $APP_URL and sign in as: $NEW_USERNAME"
echo "  (Use the password you set when you originally signed up locally.)"
