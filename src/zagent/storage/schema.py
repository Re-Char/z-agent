SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS workspaces (
    workspace_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    path TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    workspace_id TEXT REFERENCES workspaces(workspace_id),
    context_version INTEGER NOT NULL DEFAULT 0,
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
CREATE INDEX IF NOT EXISTS idx_archives_session_range
    ON archives(session_id, start_sequence, end_sequence);
CREATE TABLE IF NOT EXISTS checkpoints (
    checkpoint_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    trigger_event_id TEXT NOT NULL REFERENCES events(event_id),
    checkpoint_event_id TEXT NOT NULL REFERENCES events(event_id),
    reason TEXT NOT NULL,
    state_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    resolution_event_id TEXT REFERENCES events(event_id)
);
CREATE INDEX IF NOT EXISTS idx_checkpoints_session_created
    ON checkpoints(session_id, created_at);
CREATE TABLE IF NOT EXISTS tool_invocations (
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    call_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    arguments_sha256 TEXT NOT NULL,
    status TEXT NOT NULL,
    result_event_id TEXT REFERENCES events(event_id),
    created_at TEXT NOT NULL,
    completed_at TEXT,
    PRIMARY KEY(session_id, call_id)
);
CREATE TABLE IF NOT EXISTS permission_requests (
    request_id TEXT PRIMARY KEY,
    session_id TEXT REFERENCES sessions(session_id),
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    action TEXT NOT NULL,
    arguments_sha256 TEXT NOT NULL,
    details_json TEXT NOT NULL,
    status TEXT NOT NULL,
    scope TEXT,
    created_at TEXT NOT NULL,
    decided_at TEXT,
    consumed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_permission_requests_status_created
    ON permission_requests(status, created_at);
CREATE TABLE IF NOT EXISTS permission_grants (
    grant_id TEXT PRIMARY KEY,
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    action TEXT NOT NULL,
    scope TEXT NOT NULL,
    session_id TEXT REFERENCES sessions(session_id),
    created_at TEXT NOT NULL,
    revoked_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_permission_grants_subject
    ON permission_grants(subject_type, subject_id, action, revoked_at);
CREATE TABLE IF NOT EXISTS permission_audit (
    audit_id TEXT PRIMARY KEY,
    request_id TEXT REFERENCES permission_requests(request_id),
    session_id TEXT REFERENCES sessions(session_id),
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    action TEXT NOT NULL,
    outcome TEXT NOT NULL,
    source TEXT NOT NULL,
    timestamp TEXT NOT NULL
);
"""

# Best-effort migrations for databases created before the v2 schema.
# Applied inside try/except: the column already exists on fresh databases.
MIGRATIONS_SQL = [
    # v1 -> v2: workspaces; sessions gain an optional workspace_id column.
    """ALTER TABLE sessions ADD COLUMN workspace_id TEXT REFERENCES workspaces(workspace_id)""",
    # Persist working-set cache invalidation across Core restarts and processes.
    """ALTER TABLE sessions ADD COLUMN context_version INTEGER NOT NULL DEFAULT 0""",
    """ALTER TABLE workspaces ADD COLUMN version INTEGER NOT NULL DEFAULT 0""",
    """ALTER TABLE checkpoints ADD COLUMN resolved_at TEXT""",
    """ALTER TABLE checkpoints ADD COLUMN resolution_event_id TEXT REFERENCES events(event_id)""",
]
