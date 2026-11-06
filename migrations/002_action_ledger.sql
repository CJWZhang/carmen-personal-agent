CREATE TABLE IF NOT EXISTS actions (
    id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('slack', 'gmail', 'calendar', 'github', 'manual')),
    source_id TEXT NOT NULL,
    action_type TEXT NOT NULL CHECK (
        action_type IN ('reply', 'review', 'decision', 'deliverable', 'meeting_prep', 'follow_up')
    ),
    summary TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK (
        status IN ('candidate', 'open', 'waiting', 'responded', 'completed', 'dismissed', 'stale')
    ),
    confidence DOUBLE PRECISION NOT NULL DEFAULT 0.5 CHECK (confidence BETWEEN 0 AND 1),
    priority_score DOUBLE PRECISION NOT NULL DEFAULT 0,
    requester_id TEXT NOT NULL DEFAULT '',
    explicit_ask BOOLEAN NOT NULL DEFAULT FALSE,
    due_at TIMESTAMPTZ,
    channel_id TEXT NOT NULL DEFAULT '',
    thread_id TEXT NOT NULL DEFAULT '',
    permalink TEXT NOT NULL DEFAULT '',
    snoozed_until TIMESTAMPTZ,
    resolution_reason TEXT NOT NULL DEFAULT '',
    last_checked_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(team_id, user_id, source, source_id, action_type),
    FOREIGN KEY (team_id, user_id)
        REFERENCES users(team_id, user_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS actions_open
ON actions(team_id, user_id, status, due_at, updated_at DESC);

CREATE TABLE IF NOT EXISTS action_evidence (
    id BIGSERIAL PRIMARY KEY,
    action_id TEXT NOT NULL REFERENCES actions(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    excerpt TEXT NOT NULL,
    permalink TEXT NOT NULL DEFAULT '',
    actor_id TEXT NOT NULL DEFAULT '',
    observed_at TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(action_id, source, source_id)
);

CREATE TABLE IF NOT EXISTS action_events (
    id BIGSERIAL PRIMARY KEY,
    action_id TEXT NOT NULL REFERENCES actions(id) ON DELETE CASCADE,
    from_status TEXT NOT NULL,
    to_status TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    evidence_source_id TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS action_feedback (
    id BIGSERIAL PRIMARY KEY,
    action_id TEXT NOT NULL REFERENCES actions(id) ON DELETE CASCADE,
    outcome TEXT NOT NULL CHECK (
        outcome IN ('done', 'not_a_task', 'not_mine', 'already_handled', 'wrong_priority', 'snoozed', 'confirmed')
    ),
    detail TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS source_cursors (
    team_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    source TEXT NOT NULL,
    cursor TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (team_id, user_id, source),
    FOREIGN KEY (team_id, user_id)
        REFERENCES users(team_id, user_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS notification_deliveries (
    id BIGSERIAL PRIMARY KEY,
    action_id TEXT NOT NULL REFERENCES actions(id) ON DELETE CASCADE,
    channel TEXT NOT NULL,
    policy_key TEXT NOT NULL,
    delivered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(action_id, channel, policy_key)
);
