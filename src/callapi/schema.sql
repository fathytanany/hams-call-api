-- Applied at startup. One schema version, so no migrations (ADR-011).

CREATE TABLE IF NOT EXISTS servers (
    id           text PRIMARY KEY,
    heartbeat_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS calls (
    sid                     text PRIMARY KEY,
    to_number               text NOT NULL,
    from_number             text NOT NULL,
    status                  text NOT NULL DEFAULT 'queued' CHECK (status IN (
        'queued', 'ringing', 'in-progress', 'completed',
        'busy', 'no-answer', 'failed', 'canceled')),
    stream_url              text NOT NULL,
    stream_params           jsonb NOT NULL,
    status_callback         text,
    status_callback_method  text NOT NULL,
    status_callback_events  text[] NOT NULL,
    ring_timeout            integer NOT NULL,
    -- NULL while queued; every write by the runner is fenced on it (ADR-003).
    owner                   text,
    -- Set by an API hangup on any server; the owner polls it and ends the call (ADR-003).
    hangup_requested        boolean NOT NULL DEFAULT false,
    -- SequenceNumber of the next status callback, bumped with the status (ADR-009).
    callback_seq            integer NOT NULL DEFAULT 0,
    date_created            timestamptz NOT NULL DEFAULT now(),
    date_updated            timestamptz NOT NULL DEFAULT now(),
    start_time              timestamptz,
    end_time                timestamptz,
    duration                integer,
    queue_time              integer
);

-- List order is newest first; sid breaks ties so offset paging is stable.
CREATE INDEX IF NOT EXISTS calls_created_idx ON calls (date_created DESC, sid DESC);
