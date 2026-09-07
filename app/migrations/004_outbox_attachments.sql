CREATE TABLE outbox_attachments (
    outbox_id TEXT PRIMARY KEY REFERENCES outbox(outbox_id) ON DELETE CASCADE,
    filename TEXT NOT NULL CHECK (length(filename) BETWEEN 5 AND 100),
    content_type TEXT NOT NULL CHECK (content_type = 'text/csv'),
    data BLOB NOT NULL CHECK (typeof(data) = 'blob' AND length(data) <= 131072),
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64)
);
