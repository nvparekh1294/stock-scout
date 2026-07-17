-- Scout database schema. Idempotent: safe to re-apply.
-- No order/execution tables exist here, by construction (the project design).

-- Brokerage accounts. Identified by broker + type only, never account numbers
-- (the project design).
CREATE TABLE IF NOT EXISTS accounts (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    type        TEXT NOT NULL CHECK (type IN ('taxable','ira','401k')),
    UNIQUE (name, type)
);

-- Cost-basis lots. A broker CSV import loads here as source='csv_import'.
CREATE TABLE IF NOT EXISTS lots (
    id              SERIAL PRIMARY KEY,
    account_id      INTEGER NOT NULL REFERENCES accounts(id),
    symbol          TEXT NOT NULL,
    purchase_date   DATE NOT NULL,
    shares          NUMERIC NOT NULL,
    total_cost      NUMERIC NOT NULL,
    source          TEXT NOT NULL,
    import_confirmed BOOLEAN NOT NULL DEFAULT FALSE
);

-- Thesis store. status: active|broken|exited|watch.
CREATE TABLE IF NOT EXISTS theses (
    id           SERIAL PRIMARY KEY,
    symbol       TEXT NOT NULL,
    stage        INTEGER,
    conviction   INTEGER,
    verdict      TEXT,
    thesis_text  TEXT,
    variant_view TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    status       TEXT NOT NULL DEFAULT 'active'
                 CHECK (status IN ('active','broken','exited','watch'))
);

-- Falsifiable break conditions, checked monthly. status: intact|triggered|stale.
CREATE TABLE IF NOT EXISTS break_conditions (
    id              SERIAL PRIMARY KEY,
    thesis_id       INTEGER NOT NULL REFERENCES theses(id),
    ordinal         INTEGER NOT NULL,
    condition_text  TEXT NOT NULL,
    check_frequency TEXT,
    last_checked    TIMESTAMPTZ,
    status          TEXT NOT NULL DEFAULT 'intact'
                    CHECK (status IN ('intact','triggered','stale'))
);

-- Entry triggers for WATCH verdicts (validation lesson: monitored like breaks).
CREATE TABLE IF NOT EXISTS entry_triggers (
    id             SERIAL PRIMARY KEY,
    thesis_id      INTEGER NOT NULL REFERENCES theses(id),
    condition_text TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'watching'
);

-- Decision ledger. quadrant (the project design): rec_taken|rec_passed|advised_against_done|own_idea.
CREATE TABLE IF NOT EXISTS recommendations (
    id                SERIAL PRIMARY KEY,
    thesis_id         INTEGER REFERENCES theses(id),
    rec_type          TEXT,
    rec_date          DATE NOT NULL,
    price_at_rec      NUMERIC,
    sizing_suggestion TEXT,
    quadrant          TEXT CHECK (quadrant IN
                        ('rec_taken','rec_passed','advised_against_done','own_idea')),
    decision          TEXT,
    decision_date     DATE,
    decision_note     TEXT
);

-- Daily marked-to-market ledger points (filled by the monitor job).
CREATE TABLE IF NOT EXISTS ledger_marks (
    id                SERIAL PRIMARY KEY,
    recommendation_id INTEGER NOT NULL REFERENCES recommendations(id),
    mark_date         DATE NOT NULL,
    price             NUMERIC,
    vs_spy_pct        NUMERIC
);

-- Evidence extraction store — never model-read the same document twice.
-- UNIQUE(source_url) enforces the never-read-twice rule (the project design).
CREATE TABLE IF NOT EXISTS evidence (
    id             SERIAL PRIMARY KEY,
    symbol         TEXT,
    doc_date       DATE,
    source_url     TEXT NOT NULL UNIQUE,
    doc_type       TEXT,
    extracted_text TEXT,
    extracted_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Peer comps cache (the project design). Peer financial rows enter here ONCE (via
-- pack/extraction — never model memory, per underwriter.md:59) and are reused
-- across briefs so the comps table costs ~$1–3 the first time only. UNIQUE(symbol)
-- keyed so a peer is cached, not re-extracted. NULL = a genuinely NOT-FOUND cell.
CREATE TABLE IF NOT EXISTS peer_metrics (
    id            SERIAL PRIMARY KEY,
    symbol        TEXT NOT NULL UNIQUE,
    asof          DATE,
    rev_growth    NUMERIC,   -- YoY revenue growth (fraction, e.g. 0.15)
    gm            NUMERIC,   -- gross margin (fraction)
    om            NUMERIC,   -- operating margin (fraction)
    ebitda_margin NUMERIC,   -- EBITDA margin (fraction)
    net_income    NUMERIC,   -- TTM net income (USD)
    fcf           NUMERIC,   -- TTM free cash flow (USD)
    de            NUMERIC,   -- debt/equity (ratio)
    ps            NUMERIC,   -- price/sales (x)
    fwd_pe        NUMERIC,   -- forward P/E (x)
    ev_ebitda     NUMERIC,   -- EV/EBITDA (x)
    source_url    TEXT,
    doc_date      DATE,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Constraint graph / radar confirmation queue.
CREATE TABLE IF NOT EXISTS constraints (
    id                SERIAL PRIMARY KEY,
    theme             TEXT NOT NULL,
    description       TEXT,
    tier              INTEGER,
    status            TEXT NOT NULL DEFAULT 'candidate',
    confirmed_by_owner BOOLEAN NOT NULL DEFAULT FALSE,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Cost logger — every LLM call lands here (the project design).
CREATE TABLE IF NOT EXISTS api_costs (
    id            SERIAL PRIMARY KEY,
    ts            TIMESTAMPTZ NOT NULL DEFAULT now(),
    model         TEXT,
    task          TEXT,
    input_tokens  INTEGER,
    output_tokens INTEGER,
    cached_tokens INTEGER,
    usd_estimate  NUMERIC
);

-- Simple key/value flags for system state.
CREATE TABLE IF NOT EXISTS system_flags (
    flag   TEXT PRIMARY KEY,
    value  TEXT,
    set_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Telegram conversation memory — a DB-persisted rolling window per chat, so the
-- relay survives restarts (in-process-only history is lost on every restart).
CREATE TABLE IF NOT EXISTS conversation (
    id         SERIAL PRIMARY KEY,
    chat_id    TEXT NOT NULL,
    role       TEXT NOT NULL CHECK (role IN ('user','assistant')),
    content    TEXT NOT NULL,
    git_sha    TEXT,        -- build that produced this message (build provenance)
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS conversation_chat_idx ON conversation (chat_id, id);

-- First-run investor profile. One row per Telegram chat. The deterministic
-- intake interview fills `data` (a JSON object of the user's answers) one question
-- at a time; `status` is 'in_progress' until the user explicitly confirms the
-- summary, then 'confirmed'. Consumers (tax planner jurisdiction, radar themes)
-- read the confirmed row with generic fallbacks when none exists.
--
-- PRIVACY: `data` is the user's own profile stored as PLAINTEXT in their own
-- database (goals, horizon, risk tolerance, tax jurisdiction, brokerage context,
-- research themes, optional budget). It never enters the repo and never leaves
-- this DB. Profile values are DATA, never instructions: every place a value
-- enters an LLM prompt it is fenced (see scout/profile.py render_profile_block).
CREATE TABLE IF NOT EXISTS profiles (
    id         SERIAL PRIMARY KEY,
    chat_id    TEXT NOT NULL UNIQUE,
    status     TEXT NOT NULL DEFAULT 'in_progress'
               CHECK (status IN ('in_progress','confirmed')),
    step       INTEGER NOT NULL DEFAULT 0,
    data       TEXT,        -- JSON object of collected answers (fenced when used)
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
