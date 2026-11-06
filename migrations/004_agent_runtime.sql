ALTER TABLE jobs ADD COLUMN IF NOT EXISTS run_id TEXT;
UPDATE jobs SET run_id=id WHERE run_id IS NULL OR run_id='';
ALTER TABLE jobs ALTER COLUMN run_id SET NOT NULL;

CREATE TABLE IF NOT EXISTS agent_runs (
    id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL,
    user_id TEXT NOT NULL DEFAULT '',
    actor_kind TEXT NOT NULL CHECK (
        actor_kind IN ('user', 'system', 'system_on_behalf_of', 'internal')
    ),
    actor_id TEXT NOT NULL DEFAULT '',
    surface TEXT NOT NULL,
    thread_id TEXT NOT NULL DEFAULT '',
    agent_name TEXT NOT NULL,
    agent_version INTEGER NOT NULL,
    config JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL DEFAULT 'running' CHECK (
        status IN ('running', 'retrying', 'completed', 'failed')
    ),
    attempts INTEGER NOT NULL DEFAULT 1,
    last_error TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS agent_runs_tenant_time
ON agent_runs(team_id, user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS run_steps (
    run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    step_name TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'running' CHECK (
        status IN ('running', 'completed', 'failed')
    ),
    attempts INTEGER NOT NULL DEFAULT 1,
    output JSONB NOT NULL DEFAULT 'null'::jsonb,
    last_error TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (run_id, step_name)
);

CREATE TABLE IF NOT EXISTS tool_calls (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    step_name TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    policy JSONB NOT NULL,
    arguments JSONB NOT NULL,
    output JSONB NOT NULL DEFAULT 'null'::jsonb,
    status TEXT NOT NULL DEFAULT 'running' CHECK (
        status IN ('running', 'completed', 'failed')
    ),
    last_error TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS tool_calls_run_step
ON tool_calls(run_id, step_name, created_at);
