"""
db.py — Supabase Postgres storage layer
----------------------------------------
Replaces the old premium_users.json / usage.json flat files with a real
Postgres table (hosted on Supabase, but this is just standard psycopg2 —
it would work against any Postgres database).

Schema (also created automatically by init_db() if it doesn't exist yet):

    CREATE TABLE users (
        id SERIAL PRIMARY KEY,
        email TEXT UNIQUE NOT NULL,
        created_at DATE DEFAULT CURRENT_DATE NOT NULL,
        attempts INT DEFAULT 0,
        weekly_attempts INT DEFAULT 0 NOT NULL,
        week_start DATE DEFAULT CURRENT_DATE NOT NULL,
        is_premium BOOLEAN NOT NULL DEFAULT FALSE,
        last_updated DATE DEFAULT CURRENT_DATE NOT NULL,
        end_date DATE
    );

Behaviour, matching what was asked for:
- `attempts` is a lifetime usage counter — increments on every successful
  /organize call, premium or not. Good for analytics ("how much is this
  person actually using it"), but it is NOT what the free-tier limit is
  checked against.
- `weekly_attempts` + `week_start` are what actually enforce the limit.
  Free (non-premium) users get FREE_WEEKLY_LIMIT (4) organizes, and the
  count resets on a rolling 7-day window: the first time someone organizes
  after 7+ days have passed since their week_start, weekly_attempts resets
  to 0 and week_start moves to today. This is a per-user rolling window,
  not a shared Monday-Sunday calendar week — simpler, and "weekly" from
  each person's own first use rather than a fixed calendar boundary. Ask if
  you'd rather have it aligned to calendar weeks instead.
- On successful payment, last_updated = today, is_premium = TRUE,
  end_date = today + 29 days.
- Premium automatically expires: any time a user's status is checked, if
  end_date has passed, is_premium is flipped back to FALSE in the same
  call. Nobody has to remember to run a cleanup job for this.

Connecting: set either DATABASE_URL directly, or the five separate
SUPABASE_DB_HOST / SUPABASE_DB_PORT / SUPABASE_DB_NAME / SUPABASE_DB_USER /
SUPABASE_DB_PASSWORD variables — see _build_database_url() below, which
builds the connection string from those if DATABASE_URL isn't set.
"""

import os
from datetime import date, timedelta
from urllib.parse import quote_plus

import psycopg2
import psycopg2.pool
from psycopg2.extras import RealDictCursor


def _build_database_url():
    """Prefer a single DATABASE_URL if it's set. Otherwise, build one from
    the separate SUPABASE_DB_* pieces Supabase's dashboard also shows you
    (Project Settings → Database → Connection parameters)."""
    url = os.environ.get("DATABASE_URL", "")
    if url:
        return url

    host = os.environ.get("SUPABASE_DB_HOST", "")
    port = os.environ.get("SUPABASE_DB_PORT", "5432")
    name = os.environ.get("SUPABASE_DB_NAME", "")
    user = os.environ.get("SUPABASE_DB_USER", "")
    password = os.environ.get("SUPABASE_DB_PASSWORD", "")

    if not (host and name and user and password):
        return ""

    # Password is URL-encoded in case it contains characters like @ : / ? #
    # that would otherwise break the connection string.
    return f"postgresql://{user}:{quote_plus(password)}@{host}:{port}/{name}?sslmode=require"


DATABASE_URL = _build_database_url()
FREE_WEEKLY_LIMIT = 4

_pool = None


def init_pool():
    """Call once at startup. Creates a small connection pool — reusing
    connections is much faster than opening a new one per request."""
    global _pool
    if not DATABASE_URL:
        return
    _pool = psycopg2.pool.SimpleConnectionPool(1, 5, dsn=DATABASE_URL)


def init_db():
    """Creates the users table if it doesn't already exist, and adds any
    new columns to a table that was already created before this change —
    both are safe to run every time the app starts."""
    if not _pool:
        return
    conn = _pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    email TEXT UNIQUE NOT NULL,
                    created_at DATE DEFAULT CURRENT_DATE NOT NULL,
                    attempts INT DEFAULT 0,
                    weekly_attempts INT DEFAULT 0 NOT NULL,
                    week_start DATE DEFAULT CURRENT_DATE NOT NULL,
                    is_premium BOOLEAN NOT NULL DEFAULT FALSE,
                    last_updated DATE DEFAULT CURRENT_DATE NOT NULL,
                    end_date DATE
                );
            """)
            # Migration for a table that already existed before weekly
            # limits were added — no-ops if the columns are already there.
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS weekly_attempts INT DEFAULT 0 NOT NULL;")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS week_start DATE DEFAULT CURRENT_DATE NOT NULL;")
        conn.commit()
    finally:
        _pool.putconn(conn)


def _get_or_create_user(cur, email):
    """Fetches a user row, creating it first if this is a new email.
    Must be called with an already-open cursor (see the public functions
    below for how this composes)."""
    cur.execute("SELECT * FROM users WHERE email = %s", (email,))
    row = cur.fetchone()
    if row is None:
        cur.execute(
            "INSERT INTO users (email) VALUES (%s) RETURNING *",
            (email,),
        )
        row = cur.fetchone()
    return row


def _expire_if_needed(cur, row):
    """If a user's premium end_date has passed, flip is_premium back to
    False right now, so nothing else in the app has to remember to check
    the date separately."""
    if row["is_premium"] and row["end_date"] and row["end_date"] < date.today():
        cur.execute(
            "UPDATE users SET is_premium = FALSE WHERE email = %s",
            (row["email"],),
        )
        row = dict(row)
        row["is_premium"] = False
    return row


def _reset_week_if_needed(cur, row):
    """If 7+ days have passed since this user's week_start, reset their
    weekly_attempts to 0 and move week_start to today. This is what makes
    the weekly limit actually 'weekly' instead of a one-time cap."""
    if date.today() - row["week_start"] >= timedelta(days=7):
        cur.execute(
            "UPDATE users SET weekly_attempts = 0, week_start = CURRENT_DATE WHERE email = %s",
            (row["email"],),
        )
        row = dict(row)
        row["weekly_attempts"] = 0
        row["week_start"] = date.today()
    return row


def get_status(email):
    """Returns a dict describing this user's current plan — used by both
    the /organize paywall check and the /status endpoint the frontend
    calls to show plan info in the UI."""
    if not _pool:
        raise RuntimeError("Database is not configured (DATABASE_URL missing)")

    conn = _pool.getconn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            row = _get_or_create_user(cur, email)
            row = _expire_if_needed(cur, row)
            row = _reset_week_if_needed(cur, row)
            conn.commit()

        days_left = None
        if row["is_premium"] and row["end_date"]:
            days_left = max((row["end_date"] - date.today()).days, 0)

        return {
            "email": row["email"],
            "is_premium": row["is_premium"],
            "attempts": row["attempts"],
            "weekly_attempts": row["weekly_attempts"],
            "weekly_remaining": None if row["is_premium"] else max(FREE_WEEKLY_LIMIT - row["weekly_attempts"], 0),
            "end_date": row["end_date"].isoformat() if row["end_date"] else None,
            "days_left": days_left,
        }
    finally:
        _pool.putconn(conn)


def is_allowed_to_organize(email):
    """True if this email can run one more /organize call right now.
    Premium users are always allowed; free users are capped at
    FREE_WEEKLY_LIMIT organizes per rolling 7-day window."""
    status = get_status(email)
    if status["is_premium"]:
        return True
    return status["weekly_attempts"] < FREE_WEEKLY_LIMIT


def record_attempt(email):
    """Increments both counters by 1 for a successful /organize call:
    `attempts` (lifetime total, never resets — useful for analytics) and
    `weekly_attempts` (what the free-tier limit is actually checked
    against). Runs for premium and free users alike, same as before."""
    conn = _pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET attempts = attempts + 1, weekly_attempts = weekly_attempts + 1 WHERE email = %s",
                (email,),
            )
        conn.commit()
    finally:
        _pool.putconn(conn)


def activate_premium(email):
    """Called after a real payment (webhook) or a manual admin override.
    Sets is_premium = TRUE, last_updated = today, end_date = today + 29
    days. Uses an upsert so it works whether or not this email has ever
    been seen before."""
    conn = _pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO users (email, is_premium, last_updated, end_date)
                VALUES (%s, TRUE, CURRENT_DATE, CURRENT_DATE + 29)
                ON CONFLICT (email) DO UPDATE SET
                    is_premium = TRUE,
                    last_updated = CURRENT_DATE,
                    end_date = CURRENT_DATE + 29
            """, (email,))
        conn.commit()
    finally:
        _pool.putconn(conn)
