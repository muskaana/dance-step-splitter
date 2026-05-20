"""Email/password auth backed by SQLite — stdlib only, no new dependencies.

- Passwords hashed with scrypt (`hashlib.scrypt`), per-user random salt.
- Sessions are opaque random tokens stored in the DB and set as an HttpOnly
  cookie called `session`. Lifetime is 30 days.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Optional

# PBKDF2-HMAC-SHA256. Higher iterations = slower auth = harder to brute-force.
# 600k matches OWASP 2023 guidance for PBKDF2-SHA256.
PBKDF2_ITERATIONS = 600_000
PBKDF2_DKLEN = 64
SALT_LEN = 16

SESSION_LIFETIME = timedelta(days=30)
SESSION_COOKIE = "session"

_USERNAME_RE = re.compile(r"^[A-Za-z0-9_-]{3,32}$")

_DB_PATH: Optional[Path] = None


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------


def _migrate_schema_if_needed(c: sqlite3.Connection) -> None:
    """Idempotent schema migrations for users that pre-date a rename.

    Currently: rename `users.email` (legacy) → `users.username` (current).
    SQLite supports ALTER TABLE RENAME COLUMN since 3.25 (2018).
    """
    info = c.execute("PRAGMA table_info(users)").fetchall()
    cols = {row[1] for row in info}
    if cols and "email" in cols and "username" not in cols:
        c.execute("ALTER TABLE users RENAME COLUMN email TO username")
        print("[auth] migrated users.email → users.username")


def init_auth(db_path: Path) -> None:
    """Open / create the auth SQLite DB. Idempotent."""
    global _DB_PATH
    _DB_PATH = db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with _conn() as c:
        _migrate_schema_if_needed(c)
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL COLLATE NOCASE,
                password_hash BLOB NOT NULL,
                password_salt BLOB NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

            CREATE TABLE IF NOT EXISTS shares (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                video_id TEXT NOT NULL,
                shared_with_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                permission TEXT NOT NULL CHECK(permission IN ('view', 'edit')),
                created_at TEXT NOT NULL,
                UNIQUE(owner_id, video_id, shared_with_id)
            );
            CREATE INDEX IF NOT EXISTS idx_shares_by_recipient ON shares(shared_with_id);
            CREATE INDEX IF NOT EXISTS idx_shares_by_video ON shares(owner_id, video_id);

            CREATE TABLE IF NOT EXISTS share_invites (
                token TEXT PRIMARY KEY,
                owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                video_id TEXT NOT NULL,
                permission TEXT NOT NULL CHECK(permission IN ('view', 'edit')),
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_share_invites_by_video ON share_invites(owner_id, video_id);
            """
        )


@contextmanager
def _conn() -> Iterator[sqlite3.Connection]:
    if _DB_PATH is None:
        raise RuntimeError("auth.init_auth() must be called before any DB op")
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class User:
    id: int
    username: str


def _hash(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        PBKDF2_ITERATIONS,
        dklen=PBKDF2_DKLEN,
    )


class AuthError(Exception):
    """Raised for bad input, taken emails, wrong credentials, etc."""


def create_user(username: str, password: str) -> User:
    username = (username or "").strip()
    if not _USERNAME_RE.match(username):
        raise AuthError(
            "Username must be 3–32 characters, letters / numbers / underscore / dash only."
        )
    if len(password) < 8:
        raise AuthError("Password must be at least 8 characters.")

    salt = os.urandom(SALT_LEN)
    pw_hash = _hash(password, salt)
    with _conn() as c:
        try:
            cur = c.execute(
                "INSERT INTO users(username, password_hash, password_salt, created_at) "
                "VALUES (?, ?, ?, ?)",
                (username, pw_hash, salt, datetime.now(timezone.utc).isoformat()),
            )
        except sqlite3.IntegrityError:
            raise AuthError("That username is already taken.")
        return User(id=int(cur.lastrowid), username=username)


def authenticate(username: str, password: str) -> Optional[User]:
    username = (username or "").strip()
    with _conn() as c:
        row = c.execute(
            "SELECT id, username, password_hash, password_salt FROM users WHERE username = ?",
            (username,),
        ).fetchone()
    if not row:
        return None
    computed = _hash(password, row["password_salt"])
    if not secrets.compare_digest(computed, row["password_hash"]):
        return None
    return User(id=row["id"], username=row["username"])


def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    expires = now + SESSION_LIFETIME
    with _conn() as c:
        c.execute(
            "INSERT INTO sessions(token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (token, user_id, now.isoformat(), expires.isoformat()),
        )
    return token


def user_from_session(token: Optional[str]) -> Optional[User]:
    if not token:
        return None
    with _conn() as c:
        row = c.execute(
            """SELECT u.id, u.username FROM sessions s
               JOIN users u ON u.id = s.user_id
               WHERE s.token = ? AND s.expires_at > ?""",
            (token, datetime.now(timezone.utc).isoformat()),
        ).fetchone()
    return User(id=row["id"], username=row["username"]) if row else None


def delete_session(token: Optional[str]) -> None:
    if not token:
        return
    with _conn() as c:
        c.execute("DELETE FROM sessions WHERE token = ?", (token,))


def count_users() -> int:
    with _conn() as c:
        row = c.execute("SELECT COUNT(*) FROM users").fetchone()
    return int(row[0])


def update_username(user_id: int, new_username: str) -> User:
    """Rename `user_id` to `new_username`. Raises AuthError if invalid or taken."""
    new_username = (new_username or "").strip()
    if not _USERNAME_RE.match(new_username):
        raise AuthError(
            "Username must be 3–32 characters, letters / numbers / underscore / dash only."
        )
    with _conn() as c:
        if not c.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone():
            raise AuthError("Account not found.")
        try:
            c.execute(
                "UPDATE users SET username = ? WHERE id = ?", (new_username, user_id)
            )
        except sqlite3.IntegrityError:
            raise AuthError("That username is already taken.")
    return User(id=user_id, username=new_username)


def find_user_by_username(username: str) -> Optional[User]:
    username = (username or "").strip()
    with _conn() as c:
        row = c.execute(
            "SELECT id, username FROM users WHERE username = ?", (username,)
        ).fetchone()
    return User(id=row["id"], username=row["username"]) if row else None


def get_user(user_id: int) -> Optional[User]:
    with _conn() as c:
        row = c.execute(
            "SELECT id, username FROM users WHERE id = ?", (user_id,)
        ).fetchone()
    return User(id=row["id"], username=row["username"]) if row else None


# ---------------------------------------------------------------------------
# Sharing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Share:
    owner_id: int
    video_id: str
    shared_with_id: int
    shared_with_username: str
    permission: str  # 'view' | 'edit'
    created_at: str


def create_share(
    owner_id: int, video_id: str, shared_with_id: int, permission: str
) -> None:
    if permission not in ("view", "edit"):
        raise AuthError("Permission must be 'view' or 'edit'.")
    if owner_id == shared_with_id:
        raise AuthError("Can't share a video with yourself.")
    with _conn() as c:
        try:
            c.execute(
                """INSERT INTO shares(owner_id, video_id, shared_with_id, permission, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    owner_id,
                    video_id,
                    shared_with_id,
                    permission,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
        except sqlite3.IntegrityError:
            # Update permission level if already shared.
            c.execute(
                """UPDATE shares SET permission = ?
                   WHERE owner_id = ? AND video_id = ? AND shared_with_id = ?""",
                (permission, owner_id, video_id, shared_with_id),
            )


def delete_share(owner_id: int, video_id: str, shared_with_id: int) -> bool:
    with _conn() as c:
        cur = c.execute(
            """DELETE FROM shares
               WHERE owner_id = ? AND video_id = ? AND shared_with_id = ?""",
            (owner_id, video_id, shared_with_id),
        )
        return cur.rowcount > 0


def shares_for_video(owner_id: int, video_id: str) -> list[Share]:
    """All accounts the given video has been shared with."""
    with _conn() as c:
        rows = c.execute(
            """SELECT s.owner_id, s.video_id, s.shared_with_id, u.username AS shared_with_username,
                      s.permission, s.created_at
               FROM shares s
               JOIN users u ON u.id = s.shared_with_id
               WHERE s.owner_id = ? AND s.video_id = ?
               ORDER BY s.created_at""",
            (owner_id, video_id),
        ).fetchall()
    return [Share(**dict(r)) for r in rows]


def shares_for_recipient(user_id: int) -> list[Share]:
    """All shares granting `user_id` access (any owner)."""
    with _conn() as c:
        rows = c.execute(
            """SELECT s.owner_id, s.video_id, s.shared_with_id, u.username AS shared_with_username,
                      s.permission, s.created_at
               FROM shares s
               JOIN users u ON u.id = s.shared_with_id
               WHERE s.shared_with_id = ?
               ORDER BY s.created_at DESC""",
            (user_id,),
        ).fetchall()
    return [Share(**dict(r)) for r in rows]


def find_share(owner_id: int, video_id: str, recipient_id: int) -> Optional[Share]:
    with _conn() as c:
        row = c.execute(
            """SELECT s.owner_id, s.video_id, s.shared_with_id, u.username AS shared_with_username,
                      s.permission, s.created_at
               FROM shares s
               JOIN users u ON u.id = s.shared_with_id
               WHERE s.owner_id = ? AND s.video_id = ? AND s.shared_with_id = ?""",
            (owner_id, video_id, recipient_id),
        ).fetchone()
    return Share(**dict(row)) if row else None


# ---------------------------------------------------------------------------
# Share invite links (token-based; recipient redeems after logging in)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ShareInvite:
    token: str
    owner_id: int
    video_id: str
    permission: str
    created_at: str


def create_share_invite(owner_id: int, video_id: str, permission: str) -> ShareInvite:
    if permission not in ("view", "edit"):
        raise AuthError("Permission must be 'view' or 'edit'.")
    token = secrets.token_urlsafe(24)
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as c:
        c.execute(
            """INSERT INTO share_invites(token, owner_id, video_id, permission, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (token, owner_id, video_id, permission, now),
        )
    return ShareInvite(
        token=token,
        owner_id=owner_id,
        video_id=video_id,
        permission=permission,
        created_at=now,
    )


def find_share_invite(token: str) -> Optional[ShareInvite]:
    if not token:
        return None
    with _conn() as c:
        row = c.execute(
            "SELECT token, owner_id, video_id, permission, created_at FROM share_invites WHERE token = ?",
            (token,),
        ).fetchone()
    return ShareInvite(**dict(row)) if row else None


def list_share_invites(owner_id: int, video_id: str) -> list[ShareInvite]:
    with _conn() as c:
        rows = c.execute(
            """SELECT token, owner_id, video_id, permission, created_at
               FROM share_invites
               WHERE owner_id = ? AND video_id = ?
               ORDER BY created_at""",
            (owner_id, video_id),
        ).fetchall()
    return [ShareInvite(**dict(r)) for r in rows]


def delete_share_invite(owner_id: int, token: str) -> bool:
    with _conn() as c:
        cur = c.execute(
            "DELETE FROM share_invites WHERE owner_id = ? AND token = ?",
            (owner_id, token),
        )
        return cur.rowcount > 0


def find_any_share_for_video(video_id: str, recipient_id: int) -> Optional[Share]:
    """Find a share for `video_id` granting access to `recipient_id`, regardless
    of owner (used when we know the video_id but not who owns it)."""
    with _conn() as c:
        row = c.execute(
            """SELECT s.owner_id, s.video_id, s.shared_with_id, u.username AS shared_with_username,
                      s.permission, s.created_at
               FROM shares s
               JOIN users u ON u.id = s.shared_with_id
               WHERE s.video_id = ? AND s.shared_with_id = ?""",
            (video_id, recipient_id),
        ).fetchone()
    return Share(**dict(row)) if row else None
