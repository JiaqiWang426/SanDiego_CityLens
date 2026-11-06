-- Explicit, repeatable migration. Never loaded by ingestion's db.py init.
CREATE SCHEMA IF NOT EXISTS citylens;
CREATE SCHEMA IF NOT EXISTS citylens_checkpoints;
CREATE TABLE IF NOT EXISTS citylens.sessions (
    session_id text PRIMARY KEY CHECK (session_id ~ '^[0-9a-f]{32}$'),
    title text NOT NULL DEFAULT 'New conversation' CHECK (length(title) BETWEEN 1 AND 100),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS sessions_recent_idx
    ON citylens.sessions (updated_at DESC, session_id);
