"""Postgres access layer.

All writes go through this module so the author-policy invariant is enforced in
one place. Note the asymmetry, which is deliberate:

  * :meth:`upsert_public_figure` takes a handle, and is reachable only from the
    allowlist sync path.
  * :meth:`insert_narrative_post` takes an ``Author`` and refuses to write a
    ``NamedAuthor``'s handle into a narrative table — the column does not exist.

Connection parameters come from the standard PG* environment variables, so the
same code works against a local socket, a container, or a managed instance
without a config change.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from igpulse.config import DatabaseCfg, PublicFigureList
from igpulse.privacy.author_policy import (
    Author,
    NamedAuthor,
    PseudonymousAuthor,
)

logger = logging.getLogger(__name__)

_SCHEMA_PATH = Path(__file__).resolve().parents[3] / "sql" / "schema.sql"


class DatabaseUnavailable(RuntimeError):
    """Postgres could not be reached, with a hint about the likely cause."""


# Substring -> actionable hint. Matched against libpq's first error line.
_CONNECTION_HINTS: tuple[tuple[str, str], ...] = (
    (
        "does not exist",
        "The PGUSER or PGDATABASE in your .env does not exist on this server. "
        "With a Homebrew install, Postgres creates a superuser named after "
        "your macOS account, not 'igpulse'. Either create the role and "
        "database the config expects:\n"
        "    createuser -s igpulse\n"
        "    createdb -O igpulse igpulse\n"
        "or set PGUSER to your macOS username in .env and leave PGPASSWORD "
        "empty.",
    ),
    (
        "password authentication failed",
        "PGPASSWORD in .env does not match this role's password.",
    ),
    (
        "no such file or directory",
        "No Postgres is listening. Start it with "
        "`brew services start postgresql@16`, or `docker start igpulse-pg` "
        "if you used the container.",
    ),
    (
        "connection refused",
        "Postgres is not accepting connections on that host/port. Check "
        "PGHOST and PGPORT, and that the server is running.",
    ),
)


def _connection_hint(detail: str) -> str:
    lowered = detail.lower()
    for needle, hint in _CONNECTION_HINTS:
        if needle in lowered:
            return hint
    return (
        "Check PGHOST, PGPORT, PGDATABASE, PGUSER and PGPASSWORD in .env."
    )


def _pseudonym_of(author: Author) -> str:
    """Extract the pseudonym for a narrative-lens write.

    A NamedAuthor reaching this function is not an error — allowlisted figures
    do post under tracked hashtags. Their handle still must not land in a
    narrative table, so they are pseudonymised like anyone else there; their
    named record lives in the public-figure lens.
    """
    if isinstance(author, PseudonymousAuthor):
        return author.pseudonym
    if isinstance(author, NamedAuthor):
        # Stable within the run, unlinkable outside it, and distinguishable
        # from an ordinary pseudonym only by the figure lens.
        return f"pf:{author.handle}"
    raise TypeError(f"unexpected author type: {type(author).__name__}")


class Database:
    def __init__(self, cfg: DatabaseCfg, *, conninfo: str = "") -> None:
        self._preflight(conninfo)
        self._pool = ConnectionPool(
            conninfo=conninfo,  # empty string => libpq reads PG* env vars
            min_size=cfg.pool_min_size,
            max_size=cfg.pool_max_size,
            kwargs={
                "row_factory": dict_row,
                # statement_timeout is set through libpq's `options` at connect
                # time, NOT via a `configure` callback running SET.
                #
                # psycopg3 connections are not autocommit, so a SET inside
                # configure() opens a transaction and returns the connection in
                # INTRANS state. ConnectionPool requires configure() to leave a
                # connection idle, so it discards every one it builds and
                # retries until the pool times out — the failure reads as a
                # connection problem when the database is perfectly healthy.
                #
                # Setting it here costs no extra round trip and cannot leave an
                # open transaction behind.
                "options": f"-c statement_timeout={cfg.statement_timeout_ms}",
            },
            open=True,
        )

    @staticmethod
    def _preflight(conninfo: str) -> None:
        """One short-timeout connection before the pool opens.

        ConnectionPool retries for 30 seconds and logs a warning per attempt.
        That is correct for a transient outage mid-run and wrong for a bad
        password or a missing role — those will not resolve themselves, and
        thirty seconds of stack traces buries the one line that says why.
        """
        try:
            with psycopg.connect(conninfo, connect_timeout=5):
                pass
        except psycopg.OperationalError as exc:
            detail = str(exc).strip().splitlines()[0]
            raise DatabaseUnavailable(
                f"{detail}\n\n{_connection_hint(detail)}"
            ) from None

    def close(self) -> None:
        self._pool.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def connection(self) -> Iterator[psycopg.Connection]:
        with self._pool.connection() as conn:
            yield conn

    # -- schema ----------------------------------------------------------- #
    def apply_schema(self) -> None:
        sql = _SCHEMA_PATH.read_text(encoding="utf-8")
        with self.connection() as conn:
            conn.execute(sql)
        logger.info("schema applied from %s", _SCHEMA_PATH)

    # -- allowlist -------------------------------------------------------- #
    def sync_public_figures(self, allowlist: PublicFigureList) -> dict[str, int]:
        """Mirror config/public_figures.yaml into the database.

        Entries removed from the YAML are deactivated rather than deleted, so
        historical figure_post rows keep a valid foreign key and past reports
        remain reproducible.
        """
        handles = list(allowlist.handles)
        with self.connection() as conn, conn.transaction():
            for fig in allowlist.figures:
                conn.execute(
                    """
                    INSERT INTO public_figure
                        (handle, display_name, category, jurisdiction,
                         justification, is_active, updated_at)
                    VALUES (%s, %s, %s, %s, %s, TRUE, now())
                    ON CONFLICT (handle) DO UPDATE SET
                        display_name  = EXCLUDED.display_name,
                        category      = EXCLUDED.category,
                        jurisdiction  = EXCLUDED.jurisdiction,
                        justification = EXCLUDED.justification,
                        is_active     = TRUE,
                        updated_at    = now()
                    """,
                    (
                        fig.handle,
                        fig.display_name,
                        fig.category,
                        fig.jurisdiction,
                        fig.justification,
                    ),
                )
            if handles:
                deactivated = conn.execute(
                    """
                    UPDATE public_figure SET is_active = FALSE, updated_at = now()
                    WHERE is_active AND handle <> ALL(%s)
                    """,
                    (handles,),
                ).rowcount
            else:
                deactivated = conn.execute(
                    "UPDATE public_figure SET is_active = FALSE WHERE is_active"
                ).rowcount

        logger.info(
            "allowlist synced: %d active, %d deactivated",
            len(handles), deactivated or 0,
        )
        return {"active": len(handles), "deactivated": deactivated or 0}

    def figure_ids(self) -> dict[str, int]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT id, handle FROM public_figure WHERE is_active"
            ).fetchall()
        return {r["handle"]: r["id"] for r in rows}

    # -- run lifecycle ---------------------------------------------------- #
    def start_run(self, lens: str, config_fingerprint: str) -> int:
        with self.connection() as conn:
            row = conn.execute(
                """
                INSERT INTO ingest_run (lens, config_fingerprint)
                VALUES (%s, %s) RETURNING id
                """,
                (lens, config_fingerprint),
            ).fetchone()
        assert row is not None
        return int(row["id"])

    def finish_run(
        self,
        run_id: int,
        *,
        status: str,
        items: int = 0,
        actor_run_ids: Sequence[str] = (),
        error: str | None = None,
    ) -> None:
        import json

        with self.connection() as conn:
            conn.execute(
                """
                UPDATE ingest_run
                   SET status = %s, finished_at = now(), items_ingested = %s,
                       actor_run_ids = %s::jsonb, error_detail = %s
                 WHERE id = %s
                """,
                (status, items, json.dumps(list(actor_run_ids)), error, run_id),
            )

    # -- narrative lens --------------------------------------------------- #
    def insert_narrative_post(
        self,
        conn: psycopg.Connection,
        *,
        run_id: int,
        narrative_key: str,
        shortcode: str,
        author: Author,
        author_is_verified: bool | None,
        posted_at: datetime,
        caption: str | None,
        like_count: int | None,
        comment_count: int | None,
        is_video: bool | None,
        view_count: int | None,
    ) -> int | None:
        row = conn.execute(
            """
            INSERT INTO narrative_post
                (run_id, narrative_key, post_shortcode, author_pseudonym,
                 author_is_verified, posted_at, caption, like_count,
                 comment_count, is_video, view_count)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (run_id, post_shortcode) DO NOTHING
            RETURNING id
            """,
            (
                run_id, narrative_key, shortcode, _pseudonym_of(author),
                author_is_verified, posted_at, caption, like_count,
                comment_count, is_video, view_count,
            ),
        ).fetchone()
        return int(row["id"]) if row else None

    def insert_narrative_comment(
        self,
        conn: psycopg.Connection,
        *,
        run_id: int,
        post_id: int,
        external_id: str,
        author: Author,
        posted_at: datetime | None,
        body: str | None,
        like_count: int | None,
    ) -> int | None:
        row = conn.execute(
            """
            INSERT INTO narrative_comment
                (run_id, post_id, comment_external_id, author_pseudonym,
                 posted_at, body, like_count)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (run_id, comment_external_id) DO NOTHING
            RETURNING id
            """,
            (
                run_id, post_id, external_id, _pseudonym_of(author),
                posted_at, body, like_count,
            ),
        ).fetchone()
        return int(row["id"]) if row else None

    def record_attribution(
        self, rows: Sequence[tuple[int, int, str]]
    ) -> int:
        """rows: (run_id, post_id, author_handle) for citable posts only.

        Called with a capped set chosen by engagement. There is no code path
        that writes this table for comments, and none that aggregates it by
        handle — see the schema comment for why that matters.
        """
        if not rows:
            return 0
        with self.connection() as conn, conn.transaction():
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO post_attribution (run_id, post_id, author_handle)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (post_id) DO UPDATE
                        SET author_handle = EXCLUDED.author_handle
                    """,
                    rows,
                )
        return len(rows)

    def purge_expired_attribution(self, retention_days: int) -> int:
        """Names expire before the aggregate rows they annotate."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
        with self.connection() as conn, conn.transaction():
            deleted = conn.execute(
                "DELETE FROM post_attribution WHERE captured_at < %s", (cutoff,)
            ).rowcount
        logger.info(
            "attribution purge: %d handle(s) older than %s removed",
            deleted or 0, cutoff.date(),
        )
        return deleted or 0

    # -- public figure lens ----------------------------------------------- #
    def insert_figure_snapshot(
        self,
        conn: psycopg.Connection,
        *,
        run_id: int,
        figure_id: int,
        follower_count: int | None,
        following_count: int | None,
        post_count: int | None,
        is_verified: bool | None,
        biography: str | None,
    ) -> None:
        conn.execute(
            """
            INSERT INTO figure_snapshot
                (run_id, figure_id, follower_count, following_count,
                 post_count, is_verified, biography)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (run_id, figure_id) DO NOTHING
            """,
            (
                run_id, figure_id, follower_count, following_count,
                post_count, is_verified, biography,
            ),
        )

    def insert_figure_post(
        self,
        conn: psycopg.Connection,
        *,
        run_id: int,
        figure_id: int,
        shortcode: str,
        posted_at: datetime,
        caption: str | None,
        like_count: int | None,
        comment_count: int | None,
        is_video: bool | None,
        view_count: int | None,
    ) -> int | None:
        row = conn.execute(
            """
            INSERT INTO figure_post
                (run_id, figure_id, post_shortcode, posted_at, caption,
                 like_count, comment_count, is_video, view_count)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (figure_id, post_shortcode) DO UPDATE SET
                like_count    = EXCLUDED.like_count,
                comment_count = EXCLUDED.comment_count,
                view_count    = EXCLUDED.view_count
            RETURNING id
            """,
            (
                run_id, figure_id, shortcode, posted_at, caption,
                like_count, comment_count, is_video, view_count,
            ),
        ).fetchone()
        return int(row["id"]) if row else None

    def insert_figure_post_comment(
        self,
        conn: psycopg.Connection,
        *,
        run_id: int,
        figure_post_id: int,
        external_id: str,
        author: Author,
        posted_at: datetime | None,
        body: str | None,
        like_count: int | None,
    ) -> int | None:
        row = conn.execute(
            """
            INSERT INTO figure_post_comment
                (run_id, figure_post_id, comment_external_id, author_pseudonym,
                 posted_at, body, like_count)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (run_id, comment_external_id) DO NOTHING
            RETURNING id
            """,
            (
                run_id, figure_post_id, external_id, _pseudonym_of(author),
                posted_at, body, like_count,
            ),
        ).fetchone()
        return int(row["id"]) if row else None

    # -- sentiment and themes --------------------------------------------- #
    def upsert_sentiment(
        self,
        rows: Sequence[tuple[str, int, str, float, str, str]],
    ) -> int:
        """rows: (source_table, source_id, label, confidence, provider, actor_id)."""
        if not rows:
            return 0
        with self.connection() as conn, conn.transaction():
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO sentiment_score
                        (source_table, source_id, label, confidence,
                         provider, actor_id)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (source_table, source_id, provider)
                    DO UPDATE SET label = EXCLUDED.label,
                                  confidence = EXCLUDED.confidence,
                                  scored_at = now()
                    """,
                    rows,
                )
        return len(rows)

    def upsert_theme_terms(
        self, rows: Sequence[tuple[int, str, str, int, int, float]]
    ) -> int:
        if not rows:
            return 0
        with self.connection() as conn, conn.transaction():
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO theme_term
                        (run_id, narrative_key, term, frequency,
                         doc_frequency, score)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (run_id, narrative_key, term)
                    DO UPDATE SET frequency = EXCLUDED.frequency,
                                  doc_frequency = EXCLUDED.doc_frequency,
                                  score = EXCLUDED.score
                    """,
                    rows,
                )
        return len(rows)

    def unscored_texts(
        self, source_table: str, provider: str, *, run_id: int, min_chars: int
    ) -> list[tuple[int, str]]:
        """Rows in `source_table` from this run with no sentiment score yet."""
        text_column = {
            "narrative_post": "caption",
            "narrative_comment": "body",
            "figure_post": "caption",
            "figure_post_comment": "body",
        }[source_table]

        with self.connection() as conn:
            rows = conn.execute(
                f"""
                SELECT s.id, s.{text_column} AS body
                  FROM {source_table} s
                  LEFT JOIN sentiment_score ss
                         ON ss.source_table = %s
                        AND ss.source_id = s.id
                        AND ss.provider = %s
                 WHERE s.run_id = %s
                   AND ss.id IS NULL
                   AND s.{text_column} IS NOT NULL
                   AND length(btrim(s.{text_column})) >= %s
                """,
                (source_table, provider, run_id, min_chars),
            ).fetchall()
        return [(int(r["id"]), r["body"]) for r in rows]

    # -- retention -------------------------------------------------------- #
    def purge_expired_narrative_rows(self, retention_days: int) -> int:
        """Delete narrative-lens rows past their retention window.

        Cascades to narrative_comment and to sentiment rows via application
        cleanup below. Public-figure rows are exempt: those subjects are on the
        curated allowlist.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
        with self.connection() as conn, conn.transaction():
            deleted = conn.execute(
                "DELETE FROM narrative_post WHERE posted_at < %s", (cutoff,)
            ).rowcount
            # sentiment_score has no FK (it is polymorphic), so orphans are
            # swept explicitly rather than relying on ON DELETE CASCADE.
            conn.execute(
                """
                DELETE FROM sentiment_score ss
                 WHERE ss.source_table = 'narrative_post'
                   AND NOT EXISTS (
                       SELECT 1 FROM narrative_post p WHERE p.id = ss.source_id
                   )
                """
            )
            conn.execute(
                """
                DELETE FROM sentiment_score ss
                 WHERE ss.source_table = 'narrative_comment'
                   AND NOT EXISTS (
                       SELECT 1 FROM narrative_comment c WHERE c.id = ss.source_id
                   )
                """
            )
        logger.info(
            "retention purge: %d narrative posts older than %s removed",
            deleted or 0, cutoff.date(),
        )
        return deleted or 0

    # -- read side for reports -------------------------------------------- #
    def sample_posts(
        self, *, run_id: int, per_narrative: int = 5
    ) -> dict[str, list[dict[str, Any]]]:
        """Top posts per narrative by engagement, for the report's evidence.

        A discourse report without examples cannot be checked. "5 posts about
        railway infrastructure" is unverifiable until you can read one; a net
        sentiment figure is unauditable until you can see what was scored.

        Author handles are deliberately absent — the rows do not contain one.
        What is returned is the public post itself: date, engagement, caption
        and its permalink. That keeps the report evidential without turning it
        into a list of people.
        """
        rows = self.fetch(
            """
            SELECT p.narrative_key, p.post_shortcode, p.posted_at, p.caption,
                   p.like_count, p.comment_count, p.is_video,
                   a.author_handle,
                   COALESCE(p.like_count, 0) + COALESCE(p.comment_count, 0)
                       AS engagement,
                   ROW_NUMBER() OVER (
                       PARTITION BY p.narrative_key
                       ORDER BY COALESCE(p.like_count, 0)
                              + COALESCE(p.comment_count, 0) DESC,
                                p.posted_at DESC
                   ) AS rank
              FROM narrative_post p
              LEFT JOIN post_attribution a ON a.post_id = p.id
             WHERE p.run_id = %s
            """,
            (run_id,),
        )
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            if row["rank"] > per_narrative:
                continue
            row["url"] = (
                f"https://www.instagram.com/p/{row['post_shortcode']}/"
            )
            grouped.setdefault(row["narrative_key"], []).append(row)
        return grouped

    def full_posts(
        self, *, run_id: int, with_comments: bool, max_per_narrative: int = 0
    ) -> dict[str, list[dict[str, Any]]]:
        """Every collected post for a run, grouped by narrative.

        Includes each post's sentiment row and, optionally, its comments with
        theirs. This is the raw export — the aggregate tables in the reports are
        derived from exactly these rows, so a JSON consumer can recompute any
        published figure and check it.

        Author identity is the per-run pseudonym, never a handle: the columns
        do not exist. Two posts sharing a pseudonym are the same author within
        this run and unrelatable to any other run.
        """
        posts = self.fetch(
            """
            SELECT p.id, p.narrative_key, p.post_shortcode, p.author_pseudonym,
                   p.author_is_verified, p.posted_at, p.caption, p.like_count,
                   p.comment_count, p.is_video, p.view_count,
                   COALESCE(p.like_count, 0) + COALESCE(p.comment_count, 0)
                       AS engagement,
                   a.author_handle,
                   ss.label AS sentiment_label,
                   ss.confidence AS sentiment_confidence
              FROM narrative_post p
              LEFT JOIN post_attribution a ON a.post_id = p.id
              LEFT JOIN sentiment_score ss
                     ON ss.source_table = 'narrative_post' AND ss.source_id = p.id
             WHERE p.run_id = %s
             ORDER BY p.narrative_key,
                      COALESCE(p.like_count, 0) + COALESCE(p.comment_count, 0) DESC
            """,
            (run_id,),
        )

        comments_by_post: dict[int, list[dict[str, Any]]] = {}
        if with_comments:
            for row in self.fetch(
                """
                SELECT c.id, c.post_id, c.comment_external_id,
                       c.author_pseudonym, c.posted_at, c.body, c.like_count,
                       ss.label AS sentiment_label,
                       ss.confidence AS sentiment_confidence
                  FROM narrative_comment c
                  LEFT JOIN sentiment_score ss
                         ON ss.source_table = 'narrative_comment'
                        AND ss.source_id = c.id
                 WHERE c.run_id = %s
                 ORDER BY c.post_id, COALESCE(c.like_count, 0) DESC
                """,
                (run_id,),
            ):
                comments_by_post.setdefault(row["post_id"], []).append(row)

        grouped: dict[str, list[dict[str, Any]]] = {}
        for post in posts:
            bucket = grouped.setdefault(post["narrative_key"], [])
            if max_per_narrative and len(bucket) >= max_per_narrative:
                continue
            post["url"] = (
                f"https://www.instagram.com/p/{post['post_shortcode']}/"
            )
            post["comments"] = comments_by_post.get(post["id"], [])
            bucket.append(post)
        return grouped

    def figure_export(self, *, run_id: int) -> list[dict[str, Any]]:
        """Allowlisted figures with their snapshot and posts for this run."""
        return self.fetch(
            """
            SELECT f.handle, f.display_name, f.category, f.jurisdiction,
                   f.justification,
                   s.follower_count, s.following_count, s.post_count,
                   s.is_verified, s.biography,
                   fp.post_shortcode, fp.posted_at, fp.caption,
                   fp.like_count, fp.comment_count, fp.is_video, fp.view_count
              FROM public_figure f
              LEFT JOIN figure_snapshot s
                     ON s.figure_id = f.id AND s.run_id = %s
              LEFT JOIN figure_post fp
                     ON fp.figure_id = f.id AND fp.run_id = %s
             WHERE f.is_active
             ORDER BY f.handle, fp.posted_at DESC
            """,
            (run_id, run_id),
        )

    def run_provenance(self, run_id: int) -> dict[str, Any] | None:
        """Everything needed to reproduce or audit a run."""
        rows = self.fetch(
            """
            SELECT id, lens, started_at, finished_at, status, actor_run_ids,
                   config_fingerprint, items_ingested, error_detail
              FROM ingest_run WHERE id = %s
            """,
            (run_id,),
        )
        return rows[0] if rows else None

    def collection_window(self, run_id: int) -> dict[str, Any] | None:
        """Oldest and newest post actually collected.

        Distinct from the configured lookback: if the window says 7 days but
        the oldest post is 2 days old, the tag simply had no older activity —
        which changes how a volume figure should be read.
        """
        rows = self.fetch(
            """
            SELECT MIN(posted_at) AS oldest, MAX(posted_at) AS newest,
                   COUNT(*) AS n
              FROM narrative_post WHERE run_id = %s
            """,
            (run_id,),
        )
        return rows[0] if rows and rows[0]["n"] else None

    def fetch(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        with self.connection() as conn:
            return conn.execute(sql, params).fetchall()
