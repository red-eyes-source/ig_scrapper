"""Integration tests against a live Postgres.

Skipped automatically when no database is reachable, so `pytest` still runs
clean on a fresh checkout. To run them:

    createuser -s igpulse && createdb -O igpulse igpulse
    PGHOST=localhost PGUSER=igpulse PGDATABASE=igpulse python -m pytest tests/test_integration_db.py

These exist because the unit suite cannot catch pool and transaction-state
bugs. The statement_timeout test below is a direct regression test: setting it
through a `configure` callback left every pooled connection in INTRANS, which
psycopg_pool discards — producing thirty seconds of "error connecting" against
a database that was working perfectly.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

psycopg = pytest.importorskip("psycopg")

from igpulse.config import PublicFigure, PublicFigureList, load_config  # noqa: E402
from igpulse.privacy.author_policy import AuthorPolicy  # noqa: E402
from igpulse.store.db import Database  # noqa: E402


def _database_available() -> bool:
    try:
        with psycopg.connect("", connect_timeout=2):
            return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _database_available(),
    reason="no reachable Postgres (set PGHOST/PGUSER/PGDATABASE to run)",
)

JUSTIFICATION = "Sitting Member of Parliament; official constituency account."


@pytest.fixture()
def db():
    cfg = load_config()
    database = Database(cfg.settings.database)
    database.apply_schema()
    with database.connection() as conn, conn.transaction():
        conn.execute(
            "TRUNCATE ingest_run, public_figure, sentiment_score, theme_term "
            "RESTART IDENTITY CASCADE"
        )
    yield database
    database.close()


@pytest.fixture()
def allowlist():
    return PublicFigureList(
        figures=[
            PublicFigure(
                handle="example_mp",
                display_name="A. N. Example",
                category="elected_official",
                justification=JUSTIFICATION,
            )
        ]
    )


# --------------------------------------------------------------------------- #
# Pool / connection behaviour
# --------------------------------------------------------------------------- #
def test_statement_timeout_is_applied_without_leaving_a_transaction(db):
    """Regression: this is the INTRANS pool bug.

    Setting statement_timeout via a configure() callback ran SET on a
    non-autocommit connection, leaving it INTRANS. psycopg_pool discards such
    connections and retries until timeout, which surfaces as a connection
    failure against a perfectly healthy database.
    """
    cfg = load_config()
    rows = db.fetch("SHOW statement_timeout")
    assert rows[0]["statement_timeout"] == "1min"

    # And the connection must come back idle, not mid-transaction.
    with db.connection() as conn:
        assert conn.info.transaction_status.name in {"IDLE", "ACTIVE"}
    _ = cfg  # config is the source of the timeout; asserted above


def test_pool_serves_many_sequential_connections(db):
    """A discarded-connection bug shows up as exhaustion under repeat use."""
    for _ in range(20):
        assert db.fetch("SELECT 1 AS n")[0]["n"] == 1


def test_schema_is_idempotent(db):
    db.apply_schema()
    db.apply_schema()
    tables = {
        r["tablename"]
        for r in db.fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        )
    }
    assert {
        "public_figure", "ingest_run", "narrative_post", "narrative_comment",
        "figure_snapshot", "figure_post", "figure_post_comment",
        "sentiment_score", "theme_term",
    } <= tables


# --------------------------------------------------------------------------- #
# Schema-level privacy invariant
# --------------------------------------------------------------------------- #
def test_narrative_tables_have_no_handle_column(db):
    """The guarantee is structural. If a handle column ever appears here, the
    pseudonymisation can be bypassed by any future code path."""
    for table in ("narrative_post", "narrative_comment"):
        columns = {
            r["column_name"]
            for r in db.fetch(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = %s",
                (table,),
            )
        }
        assert not {"handle", "username", "author_handle"} & columns


def test_thin_justification_rejected_by_database_not_just_pydantic(db):
    with pytest.raises(psycopg.errors.CheckViolation):
        with db.connection() as conn:
            conn.execute(
                "INSERT INTO public_figure "
                "(handle, display_name, category, justification) "
                "VALUES ('x', 'X', 'elected_official', 'MP')"
            )


def test_invalid_category_rejected(db):
    with pytest.raises(psycopg.errors.CheckViolation):
        with db.connection() as conn:
            conn.execute(
                "INSERT INTO public_figure "
                "(handle, display_name, category, justification) "
                "VALUES ('y', 'Y', 'political_opponent', %s)",
                (JUSTIFICATION,),
            )


# --------------------------------------------------------------------------- #
# Allowlist sync
# --------------------------------------------------------------------------- #
def test_allowlist_sync_inserts_and_deactivates(db, allowlist):
    db.sync_public_figures(allowlist)
    assert set(db.figure_ids()) == {"example_mp"}

    # Removing an entry deactivates rather than deleting, so historical rows
    # keep a valid foreign key and old reports stay reproducible.
    db.sync_public_figures(PublicFigureList(figures=[]))
    assert db.figure_ids() == {}
    still_there = db.fetch(
        "SELECT handle, is_active FROM public_figure WHERE handle = 'example_mp'"
    )
    assert len(still_there) == 1
    assert still_there[0]["is_active"] is False


def test_allowlist_sync_is_idempotent(db, allowlist):
    db.sync_public_figures(allowlist)
    db.sync_public_figures(allowlist)
    assert len(db.fetch("SELECT id FROM public_figure")) == 1


# --------------------------------------------------------------------------- #
# End-to-end privacy guarantee, through real storage
# --------------------------------------------------------------------------- #
def _insert_post(db, policy, run_id, handle, shortcode):
    with db.connection() as conn, conn.transaction():
        return db.insert_narrative_post(
            conn,
            run_id=run_id,
            narrative_key="test",
            shortcode=shortcode,
            author=policy.resolve(handle),
            author_is_verified=False,
            posted_at=datetime.now(timezone.utc),
            caption="a caption long enough to be scored",
            like_count=1,
            comment_count=0,
            is_video=False,
            view_count=None,
        )


def test_same_author_is_unlinkable_across_runs(db, allowlist):
    """The query from the runbook, asserted in code.

    Two runs, same private author. The stored pseudonyms must differ, or a
    longitudinal profile could be assembled by joining on author_pseudonym.
    """
    cfg = load_config()
    for i in (1, 2):
        run_id = db.start_run("narrative", cfg.fingerprint())
        policy = AuthorPolicy(allowlist)          # new run, new salt
        _insert_post(db, policy, run_id, "private_citizen", f"code{i}")
        db.finish_run(run_id, status="succeeded", items=1)

    leaked = db.fetch(
        """
        SELECT author_pseudonym, COUNT(DISTINCT run_id) AS runs
          FROM narrative_post
         WHERE author_pseudonym NOT LIKE 'pf:%%'
         GROUP BY 1 HAVING COUNT(DISTINCT run_id) > 1
        """
    )
    assert leaked == [], f"pseudonym reused across runs: {leaked}"


def test_pseudonym_is_stable_within_one_run(db, allowlist):
    cfg = load_config()
    run_id = db.start_run("narrative", cfg.fingerprint())
    policy = AuthorPolicy(allowlist)
    _insert_post(db, policy, run_id, "private_citizen", "a1")
    _insert_post(db, policy, run_id, "private_citizen", "a2")

    rows = db.fetch(
        "SELECT DISTINCT author_pseudonym FROM narrative_post WHERE run_id = %s",
        (run_id,),
    )
    assert len(rows) == 1, "same author in one run must share a pseudonym"


def test_allowlisted_author_is_marked_but_not_stored_raw_in_narrative(db, allowlist):
    cfg = load_config()
    run_id = db.start_run("narrative", cfg.fingerprint())
    policy = AuthorPolicy(allowlist)
    _insert_post(db, policy, run_id, "example_mp", "b1")

    value = db.fetch(
        "SELECT author_pseudonym FROM narrative_post WHERE run_id = %s",
        (run_id,),
    )[0]["author_pseudonym"]
    # Prefixed so the figure lens can identify it, but it lives in the
    # narrative table as a marker, not as a queryable identity record.
    assert value == "pf:example_mp"


# --------------------------------------------------------------------------- #
# Run lifecycle and retention
# --------------------------------------------------------------------------- #
def test_run_lifecycle_records_status_and_actor_ids(db):
    cfg = load_config()
    run_id = db.start_run("narrative", cfg.fingerprint())
    db.finish_run(
        run_id, status="failed", items=3,
        actor_run_ids=["abc123", "def456"], error="actor aborted",
    )
    row = db.fetch("SELECT * FROM ingest_run WHERE id = %s", (run_id,))[0]
    assert row["status"] == "failed"
    assert row["items_ingested"] == 3
    assert row["actor_run_ids"] == ["abc123", "def456"]
    assert row["error_detail"] == "actor aborted"
    assert row["finished_at"] is not None


def test_invalid_run_status_rejected(db):
    cfg = load_config()
    run_id = db.start_run("narrative", cfg.fingerprint())
    with pytest.raises(psycopg.errors.CheckViolation):
        with db.connection() as conn:
            conn.execute(
                "UPDATE ingest_run SET status = 'kind-of-worked' WHERE id = %s",
                (run_id,),
            )


def test_retention_purge_drops_old_narrative_rows_only(db, allowlist):
    cfg = load_config()
    db.sync_public_figures(allowlist)
    run_id = db.start_run("narrative", cfg.fingerprint())
    policy = AuthorPolicy(allowlist)

    old = datetime.now(timezone.utc) - timedelta(days=200)
    with db.connection() as conn, conn.transaction():
        db.insert_narrative_post(
            conn, run_id=run_id, narrative_key="test", shortcode="old1",
            author=policy.resolve("someone"), author_is_verified=False,
            posted_at=old, caption="old post", like_count=0,
            comment_count=0, is_video=False, view_count=None,
        )
    _insert_post(db, policy, run_id, "someone", "new1")

    deleted = db.purge_expired_narrative_rows(90)
    assert deleted == 1
    remaining = db.fetch(
        "SELECT post_shortcode FROM narrative_post WHERE run_id = %s", (run_id,)
    )
    assert [r["post_shortcode"] for r in remaining] == ["new1"]
    # Allowlist rows are exempt from retention.
    assert set(db.figure_ids()) == {"example_mp"}


def test_sentiment_upsert_is_idempotent_and_bounded(db, allowlist):
    cfg = load_config()
    run_id = db.start_run("narrative", cfg.fingerprint())
    policy = AuthorPolicy(allowlist)
    post_id = _insert_post(db, policy, run_id, "someone", "s1")

    db.upsert_sentiment([("narrative_post", post_id, "negative", 0.9,
                          "apify_actor", "easyapi~text-sentiment-analysis")])
    db.upsert_sentiment([("narrative_post", post_id, "positive", 0.7,
                          "apify_actor", "easyapi~text-sentiment-analysis")])

    rows = db.fetch(
        "SELECT label, confidence FROM sentiment_score "
        "WHERE source_table = 'narrative_post' AND source_id = %s", (post_id,)
    )
    assert len(rows) == 1, "re-scoring must update, not duplicate"
    assert rows[0]["label"] == "positive"

    with pytest.raises(psycopg.errors.CheckViolation):
        with db.connection() as conn:
            conn.execute(
                "INSERT INTO sentiment_score "
                "(source_table, source_id, label, confidence, provider, actor_id) "
                "VALUES ('narrative_post', %s, 'negative', 1.4, 'p', 'a')",
                (post_id,),
            )
