SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    sequence INTEGER NOT NULL,
    timestamp TEXT NOT NULL,
    kind TEXT NOT NULL,
    role TEXT NOT NULL,
    payload_inline TEXT,
    payload_ref TEXT,
    payload_sha256 TEXT NOT NULL,
    token_estimate INTEGER NOT NULL,
    parent_event_id TEXT,
    tool_name TEXT,
    tool_call_id TEXT,
    tags TEXT NOT NULL DEFAULT '[]',
    sensitivity TEXT NOT NULL DEFAULT 'normal',
    provenance TEXT NOT NULL DEFAULT 'runtime',
    UNIQUE(session_id, sequence)
);
CREATE VIRTUAL TABLE IF NOT EXISTS event_fts USING fts5(
    event_id UNINDEXED,
    session_id UNINDEXED,
    search_text,
    tokenize='unicode61'
);
CREATE TABLE IF NOT EXISTS pins (
    session_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    rationale TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(session_id, event_id)
);
CREATE TABLE IF NOT EXISTS archives (
    archive_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    start_sequence INTEGER NOT NULL,
    end_sequence INTEGER NOT NULL,
    summary_event_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    state_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

