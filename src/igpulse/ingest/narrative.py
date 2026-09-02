"""Narrative lens: issue-level ingestion.

One Apify run per search term, so a post matching two narratives is attributed
to both unambiguously rather than being assigned to whichever term happened to
be scraped first.

Author identity never leaves this module as a handle — every record is passed
through :class:`AuthorPolicy` before it reaches the database layer.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from igpulse.apify.actors import (
    build_comment_input,
    build_hashtag_search_input,
    normalise_comment,
    normalise_post,
)
from igpulse.apify.client import ApifyClient
from igpulse.config import AppConfig
from igpulse.privacy.author_policy import AuthorPolicy
from igpulse.store.db import Database

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class NarrativeIngestResult:
    run_id: int
    posts: int
    comments: int
    actor_run_ids: list[str]
    distinct_authors: int


def collect_narratives(
    cfg: AppConfig,
    client: ApifyClient,
    db: Database,
    policy: AuthorPolicy,
) -> NarrativeIngestResult:
    ingest_cfg = cfg.settings.ingest.narrative
    actor = cfg.settings.apify.actors.instagram_scraper
    comment_actor = cfg.settings.apify.actors.instagram_comment_scraper

    run_id = db.start_run("narrative", cfg.fingerprint())
    actor_run_ids: list[str] = []
    total_posts = 0
    total_comments = 0

    try:
        for narrative in cfg.narratives.narratives:
            for term in narrative.search_terms:
                run, items = client.run_and_collect(
                    actor,
                    build_hashtag_search_input(
                        [term],
                        results_limit=ingest_cfg.results_per_term,
                        lookback=ingest_cfg.lookback,
                    ),
                    max_items=ingest_cfg.results_per_term,
                )
                actor_run_ids.append(run.run_id)

                post_ids: dict[str, int] = {}
                post_urls: list[str] = []

                with db.connection() as conn, conn.transaction():
                    for record in items:
                        post = normalise_post(record)
                        if post is None:
                            continue
                        if post.missing_fields:
                            logger.debug(
                                "post %s missing fields: %s",
                                post.shortcode, ", ".join(post.missing_fields),
                            )
                        author = policy.resolve(post.author_handle)
                        post_id = db.insert_narrative_post(
                            conn,
                            run_id=run_id,
                            narrative_key=narrative.key,
                            shortcode=post.shortcode,
                            author=author,
                            author_is_verified=post.author_is_verified,
                            posted_at=post.posted_at,
                            caption=post.caption
                            if cfg.settings.privacy.store_narrative_text
                            else None,
                            like_count=post.like_count,
                            comment_count=post.comment_count,
                            is_video=post.is_video,
                            view_count=post.view_count,
                        )
                        if post_id is not None:
                            post_ids[post.shortcode] = post_id
                            post_urls.append(post.url)
                            total_posts += 1

                if ingest_cfg.comments_per_post and post_urls:
                    total_comments += _collect_comments(
                        cfg, client, db, policy,
                        run_id=run_id,
                        comment_actor=comment_actor,
                        post_urls=post_urls,
                        post_ids=post_ids,
                        actor_run_ids=actor_run_ids,
                    )

                logger.info(
                    "narrative %s / term %s: %d posts so far",
                    narrative.key, term, total_posts,
                )

    except Exception as exc:
        db.finish_run(
            run_id, status="failed", items=total_posts + total_comments,
            actor_run_ids=actor_run_ids, error=str(exc)[:2000],
        )
        raise

    db.finish_run(
        run_id, status="succeeded", items=total_posts + total_comments,
        actor_run_ids=actor_run_ids,
    )
    return NarrativeIngestResult(
        run_id=run_id,
        posts=total_posts,
        comments=total_comments,
        actor_run_ids=actor_run_ids,
        distinct_authors=policy.distinct_authors_seen,
    )


def _collect_comments(
    cfg: AppConfig,
    client: ApifyClient,
    db: Database,
    policy: AuthorPolicy,
    *,
    run_id: int,
    comment_actor: str,
    post_urls: list[str],
    post_ids: dict[str, int],
    actor_run_ids: list[str],
) -> int:
    ingest_cfg = cfg.settings.ingest.narrative
    run, items = client.run_and_collect(
        comment_actor,
        build_comment_input(
            post_urls,
            comments_per_post=ingest_cfg.comments_per_post,
            include_nested=ingest_cfg.include_nested_comments,
        ),
    )
    actor_run_ids.append(run.run_id)

    inserted = 0
    with db.connection() as conn, conn.transaction():
        for record in items:
            comment = normalise_comment(record)
            if comment is None or comment.post_shortcode is None:
                continue
            parent_id = post_ids.get(comment.post_shortcode)
            if parent_id is None:
                # Comment on a post we did not persist (dedupe skip, or the
                # comment actor returned a post outside our result window).
                continue
            author = policy.resolve(comment.author_handle)
            if db.insert_narrative_comment(
                conn,
                run_id=run_id,
                post_id=parent_id,
                external_id=comment.external_id,
                author=author,
                posted_at=comment.posted_at,
                body=comment.body
                if cfg.settings.privacy.store_narrative_text
                else None,
                like_count=comment.like_count,
            ) is not None:
                inserted += 1
    return inserted
