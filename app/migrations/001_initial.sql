CREATE TABLE conversations (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    active_repository TEXT,
    last_job_id TEXT,
    memory_generation INTEGER NOT NULL DEFAULT 0 CHECK (memory_generation >= 0),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE (source, channel_id, user_id)
);

CREATE TABLE inbound_requests (
    request_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    external_message_id TEXT NOT NULL,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    content TEXT NOT NULL,
    intent TEXT,
    backend TEXT,
    status TEXT NOT NULL CHECK (status IN ('received', 'processing', 'completed', 'failed')),
    job_id TEXT,
    memory_generation INTEGER NOT NULL CHECK (memory_generation >= 0),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE (source, external_message_id)
);

CREATE INDEX inbound_requests_conversation_created_idx
    ON inbound_requests(conversation_id, created_at);

CREATE TABLE exchanges (
    id INTEGER PRIMARY KEY,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    request_id TEXT NOT NULL UNIQUE REFERENCES inbound_requests(request_id) ON DELETE CASCADE,
    user_text TEXT NOT NULL,
    assistant_text TEXT NOT NULL,
    backend TEXT,
    created_at REAL NOT NULL
);

CREATE INDEX exchanges_conversation_created_idx
    ON exchanges(conversation_id, created_at DESC, id DESC);

CREATE TABLE jobs (
    id INTEGER PRIMARY KEY,
    job_id TEXT NOT NULL UNIQUE,
    request_id TEXT NOT NULL UNIQUE REFERENCES inbound_requests(request_id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('research', 'codex', 'publish', 'aws')),
    repository TEXT,
    state TEXT NOT NULL CHECK (
        state IN ('queued', 'running', 'prepared', 'publishing', 'succeeded', 'failed', 'cancelled')
    ),
    branch TEXT,
    external_reference TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    checkpoint_json TEXT NOT NULL DEFAULT '{}',
    summary TEXT,
    safe_error TEXT,
    validation_status TEXT CHECK (
        validation_status IS NULL OR validation_status IN (
            'passed', 'unchanged_failure', 'failed', 'unavailable', 'timed_out'
        )
    ),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    finished_at REAL
);

CREATE INDEX jobs_state_created_idx ON jobs(state, created_at, job_id);

CREATE TABLE job_events (
    id INTEGER PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    from_state TEXT,
    to_state TEXT NOT NULL,
    message TEXT,
    created_at REAL NOT NULL
);

CREATE INDEX job_events_job_created_idx
    ON job_events(job_id, created_at, id);

CREATE TABLE outbox (
    id INTEGER PRIMARY KEY,
    outbox_id TEXT NOT NULL UNIQUE,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    request_id TEXT NOT NULL REFERENCES inbound_requests(request_id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    dedupe_key TEXT NOT NULL,
    assistant_text TEXT NOT NULL,
    backend TEXT,
    exchange_on_complete INTEGER NOT NULL DEFAULT 0 CHECK (exchange_on_complete IN (0, 1)),
    created_at REAL NOT NULL,
    completed_at REAL,
    UNIQUE (request_id, kind, dedupe_key)
);

CREATE UNIQUE INDEX outbox_one_exchange_per_request_idx
    ON outbox(request_id) WHERE exchange_on_complete = 1;

CREATE INDEX outbox_pending_idx
    ON outbox(completed_at, conversation_id, created_at, outbox_id);

CREATE TABLE outbox_parts (
    outbox_id TEXT NOT NULL REFERENCES outbox(outbox_id) ON DELETE CASCADE,
    part_index INTEGER NOT NULL CHECK (part_index >= 0),
    content TEXT NOT NULL,
    discord_message_id TEXT,
    acked_at REAL,
    created_at REAL NOT NULL,
    PRIMARY KEY (outbox_id, part_index)
);

CREATE INDEX outbox_parts_pending_idx
    ON outbox_parts(acked_at, outbox_id, part_index);
