-- ig-pulse schema.
--
-- Design invariant: an Instagram handle is persisted in exactly one table,
-- public_figure, which is populated only from the curated allowlist. Every
-- other table references either a public_figure row (allowlisted, named) or a
-- per-run pseudonym (everyone else, unlinkable across runs).
--
-- This is enforced structurally, not by convention: narrative_post and
-- narrative_comment have no handle column to write to.

BEGIN;

CREATE TABLE IF NOT EXISTS schema_version (
    version     INT PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Allowlisted, named subjects. Mirrors config/public_figures.yaml.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public_figure (
    id              BIGSERIAL PRIMARY KEY,
    handle          TEXT        NOT NULL UNIQUE,
    display_name    TEXT        NOT NULL,
    category        TEXT        NOT NULL,
    jurisdiction    TEXT,
    justification   TEXT        NOT NULL,
    is_active       BOOLEAN     NOT NULL DEFAULT TRUE,
    added_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT public_figure_category_ck
        CHECK (category IN ('elected_official', 'party_official',
                            'registered_media', 'own_side')),
    CONSTRAINT public_figure_justification_ck
        CHECK (length(btrim(justification)) >= 20)
);

COMMENT ON TABLE public_figure IS
    'The only table in this database permitted to store an Instagram handle. '
    'Populated exclusively from config/public_figures.yaml.';

-- ---------------------------------------------------------------------------
-- Run bookkeeping.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ingest_run (
    id                  BIGSERIAL PRIMARY KEY,
    lens                TEXT        NOT NULL,
    started_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at         TIMESTAMPTZ,
    status              TEXT        NOT NULL DEFAULT 'running',
    actor_run_ids       JSONB       NOT NULL DEFAULT '[]'::jsonb,
    config_fingerprint  TEXT        NOT NULL,
    items_ingested      INT         NOT NULL DEFAULT 0,
    error_detail        TEXT,
    CONSTRAINT ingest_run_lens_ck
        CHECK (lens IN ('narrative', 'public_figure', 'own_side')),
    CONSTRAINT ingest_run_status_ck
        CHECK (status IN ('running', 'succeeded', 'failed', 'aborted'))
);

CREATE INDEX IF NOT EXISTS ingest_run_lens_started_idx
    ON ingest_run (lens, started_at DESC);

-- ---------------------------------------------------------------------------
-- NARRATIVE LENS — aggregate only. No handle column, deliberately.
--
-- author_pseudonym is HMAC(per-run salt, handle). The salt is generated at run
-- start, held in memory, and discarded at run end. Within a run this supports
-- dedupe and coordinated-behaviour detection; across runs the same author
-- yields a different pseudonym, so longitudinal profiling is not possible even
-- with full database access.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS narrative_post (
    id                  BIGSERIAL PRIMARY KEY,
    run_id              BIGINT      NOT NULL REFERENCES ingest_run(id) ON DELETE CASCADE,
    narrative_key       TEXT        NOT NULL,
    post_shortcode      TEXT        NOT NULL,
    author_pseudonym    TEXT        NOT NULL,
    author_is_verified  BOOLEAN,
    posted_at           TIMESTAMPTZ NOT NULL,
    caption             TEXT,
    like_count          INT,
    comment_count       INT,
    is_video            BOOLEAN,
    view_count          INT,
    ingested_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT narrative_post_unique UNIQUE (run_id, post_shortcode)
);

CREATE INDEX IF NOT EXISTS narrative_post_key_time_idx
    ON narrative_post (narrative_key, posted_at DESC);
CREATE INDEX IF NOT EXISTS narrative_post_run_idx
    ON narrative_post (run_id);

COMMENT ON COLUMN narrative_post.author_pseudonym IS
    'Per-run HMAC of the author handle. Salt is never persisted, so this value '
    'is not linkable to the same author in any other run. Do not add a handle '
    'column to this table.';

CREATE TABLE IF NOT EXISTS narrative_comment (
    id                  BIGSERIAL PRIMARY KEY,
    run_id              BIGINT      NOT NULL REFERENCES ingest_run(id) ON DELETE CASCADE,
    post_id             BIGINT      NOT NULL REFERENCES narrative_post(id) ON DELETE CASCADE,
    comment_external_id TEXT        NOT NULL,
    author_pseudonym    TEXT        NOT NULL,
    posted_at           TIMESTAMPTZ,
    body                TEXT,
    like_count          INT,
    ingested_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT narrative_comment_unique UNIQUE (run_id, comment_external_id)
);

CREATE INDEX IF NOT EXISTS narrative_comment_post_idx
    ON narrative_comment (post_id);

-- ---------------------------------------------------------------------------
-- PUBLIC FIGURE LENS — named, persistent, longitudinal. Allowlisted only.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS figure_snapshot (
    id                  BIGSERIAL PRIMARY KEY,
    run_id              BIGINT      NOT NULL REFERENCES ingest_run(id) ON DELETE CASCADE,
    figure_id           BIGINT      NOT NULL REFERENCES public_figure(id) ON DELETE CASCADE,
    captured_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    follower_count      INT,
    following_count     INT,
    post_count          INT,
    is_verified         BOOLEAN,
    biography           TEXT,
    CONSTRAINT figure_snapshot_unique UNIQUE (run_id, figure_id)
);

CREATE INDEX IF NOT EXISTS figure_snapshot_figure_time_idx
    ON figure_snapshot (figure_id, captured_at DESC);

CREATE TABLE IF NOT EXISTS figure_post (
    id                  BIGSERIAL PRIMARY KEY,
    run_id              BIGINT      NOT NULL REFERENCES ingest_run(id) ON DELETE CASCADE,
    figure_id           BIGINT      NOT NULL REFERENCES public_figure(id) ON DELETE CASCADE,
    post_shortcode      TEXT        NOT NULL,
    posted_at           TIMESTAMPTZ NOT NULL,
    caption             TEXT,
    like_count          INT,
    comment_count       INT,
    is_video            BOOLEAN,
    view_count          INT,
    ingested_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT figure_post_unique UNIQUE (figure_id, post_shortcode)
);

CREATE INDEX IF NOT EXISTS figure_post_figure_time_idx
    ON figure_post (figure_id, posted_at DESC);

-- Comments ON a public figure's post are still authored by private
-- individuals, so they follow the narrative rules: pseudonym, not handle.
CREATE TABLE IF NOT EXISTS figure_post_comment (
    id                  BIGSERIAL PRIMARY KEY,
    run_id              BIGINT      NOT NULL REFERENCES ingest_run(id) ON DELETE CASCADE,
    figure_post_id      BIGINT      NOT NULL REFERENCES figure_post(id) ON DELETE CASCADE,
    comment_external_id TEXT        NOT NULL,
    author_pseudonym    TEXT        NOT NULL,
    posted_at           TIMESTAMPTZ,
    body                TEXT,
    like_count          INT,
    ingested_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT figure_post_comment_unique UNIQUE (run_id, comment_external_id)
);

CREATE INDEX IF NOT EXISTS figure_post_comment_post_idx
    ON figure_post_comment (figure_post_id);

-- ---------------------------------------------------------------------------
-- SENTIMENT — polymorphic over the three text-bearing sources.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sentiment_score (
    id              BIGSERIAL PRIMARY KEY,
    source_table    TEXT        NOT NULL,
    source_id       BIGINT      NOT NULL,
    label           TEXT        NOT NULL,
    confidence      REAL        NOT NULL,
    provider        TEXT        NOT NULL,
    actor_id        TEXT        NOT NULL,
    scored_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT sentiment_source_ck
        CHECK (source_table IN ('narrative_post', 'narrative_comment',
                                'figure_post', 'figure_post_comment')),
    CONSTRAINT sentiment_label_ck
        CHECK (label IN ('positive', 'negative', 'neutral', 'uncertain')),
    CONSTRAINT sentiment_confidence_ck
        CHECK (confidence >= 0.0 AND confidence <= 1.0),
    CONSTRAINT sentiment_unique UNIQUE (source_table, source_id, provider)
);

CREATE INDEX IF NOT EXISTS sentiment_source_idx
    ON sentiment_score (source_table, source_id);

-- ---------------------------------------------------------------------------
-- THEMES — extracted terms per narrative, per run.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS theme_term (
    id              BIGSERIAL PRIMARY KEY,
    run_id          BIGINT      NOT NULL REFERENCES ingest_run(id) ON DELETE CASCADE,
    narrative_key   TEXT        NOT NULL,
    term            TEXT        NOT NULL,
    frequency       INT         NOT NULL,
    doc_frequency   INT         NOT NULL,
    score           REAL        NOT NULL,
    CONSTRAINT theme_term_unique UNIQUE (run_id, narrative_key, term)
);

CREATE INDEX IF NOT EXISTS theme_term_run_narrative_idx
    ON theme_term (run_id, narrative_key, score DESC);

INSERT INTO schema_version (version) VALUES (1)
    ON CONFLICT (version) DO NOTHING;

COMMIT;
