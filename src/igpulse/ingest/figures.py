"""Public-figure and own-side lenses.

These two lenses are the same collector with a different category filter, so
they share an implementation rather than being copy-pasted. The distinction
that matters is upstream: both draw exclusively from the curated allowlist, so
every handle they touch has already passed the public-figure test.

Comments on a public figure's post are still written by private individuals, so
they follow narrative rules — pseudonym, not handle.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from igpulse.apify.actors import (
    build_comment_input,
    build_profile_input,
    normalise_comment,
    normalise_post,
    normalise_profile,
)
from igpulse.apify.client import ApifyClient
from igpulse.config import AppConfig, FigureCategory
from igpulse.privacy.author_policy import AuthorPolicy
from igpulse.store.db import Database

logger = logging.getLogger(__name__)

# Which allowlist categories feed which lens.
LENS_CATEGORIES: dict[str, tuple[FigureCategory, ...]] = {
    "public_figure": ("elected_official", "party_official", "registered_media"),
    "own_side": ("own_side",),
}


@dataclass(slots=True)
class FigureIngestResult:
    run_id: int
    lens: str
    handles: int
    posts: int
    comments: int
    actor_run_ids: list[str]


def collect_figures(
    cfg: AppConfig,
    client: ApifyClient,
    db: Database,
    policy: AuthorPolicy,
    *,
    lens: str,
) -> FigureIngestResult:
    if lens not in LENS_CATEGORIES:
        raise ValueError(f"unknown figure lens: {lens!r}")

    categories = LENS_CATEGORIES[lens]
    figures = [f for f in cfg.public_figures.figures if f.category in categories]
    ingest_cfg = (
        cfg.settings.ingest.public_figure
        if lens == "public_figure"
        else cfg.settings.ingest.own_side
    )

    run_id = db.start_run(lens, cfg.fingerprint())
    actor_run_ids: list[str] = []
    total_posts = 0
    total_comments = 0

    if not figures:
        logger.warning(
            "lens %s has no allowlisted handles in categories %s", lens, categories
        )
        db.finish_run(run_id, status="succeeded", items=0)
        return FigureIngestResult(run_id, lens, 0, 0, 0, [])

    figure_ids = db.figure_ids()

    try:
        # Every handle here is allowlisted by construction, but assert it at the
        # boundary anyway: this is the one place a handle is written literally,
        # and a config/database drift would otherwise pass silently.
        handles = [policy.assert_persistable_handle(f.handle) for f in figures]

        run, items = client.run_and_collect(
            cfg.settings.apify.actors.instagram_scraper,
            build_profile_input(
                handles,
                posts_per_handle=ingest_cfg.posts_per_handle,
                lookback=ingest_cfg.lookback,
            ),
        )
        actor_run_ids.append(run.run_id)

        post_ids: dict[str, int] = {}
        post_urls: list[str] = []

        with db.connection() as conn, conn.transaction():
            for record in items:
                profile = normalise_profile(record)
                if profile is None:
                    continue
                figure_id = figure_ids.get(profile.handle)
                if figure_id is None:
                    logger.warning(
                        "actor returned handle %s which is not active in the "
                        "database allowlist; skipping", profile.handle,
                    )
                    continue

                db.insert_figure_snapshot(
                    conn,
                    run_id=run_id,
                    figure_id=figure_id,
                    follower_count=profile.follower_count,
                    following_count=profile.following_count,
                    post_count=profile.post_count,
                    is_verified=profile.is_verified,
                    biography=profile.biography,
                )

                # The details resultsType nests recent posts under the profile.
                nested = record.get("latestPosts") or record.get("posts") or []
                for post_record in nested:
                    post = normalise_post(post_record)
                    if post is None:
                        continue
                    post_id = db.insert_figure_post(
                        conn,
                        run_id=run_id,
                        figure_id=figure_id,
                        shortcode=post.shortcode,
                        posted_at=post.posted_at,
                        caption=post.caption,
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
            total_comments = _collect_figure_comments(
                cfg, client, db, policy,
                run_id=run_id,
                post_urls=post_urls,
                post_ids=post_ids,
                comments_per_post=ingest_cfg.comments_per_post,
                actor_run_ids=actor_run_ids,
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
    logger.info(
        "lens %s: %d handles, %d posts, %d comments",
        lens, len(figures), total_posts, total_comments,
    )
    return FigureIngestResult(
        run_id=run_id,
        lens=lens,
        handles=len(figures),
        posts=total_posts,
        comments=total_comments,
        actor_run_ids=actor_run_ids,
    )


def _collect_figure_comments(
    cfg: AppConfig,
    client: ApifyClient,
    db: Database,
    policy: AuthorPolicy,
    *,
    run_id: int,
    post_urls: list[str],
    post_ids: dict[str, int],
    comments_per_post: int,
    actor_run_ids: list[str],
) -> int:
    run, items = client.run_and_collect(
        cfg.settings.apify.actors.instagram_comment_scraper,
        build_comment_input(
            post_urls,
            comments_per_post=comments_per_post,
            include_nested=False,
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
                continue
            # Commenters are members of the public: pseudonymised, always.
            author = policy.resolve(comment.author_handle)
            if db.insert_figure_post_comment(
                conn,
                run_id=run_id,
                figure_post_id=parent_id,
                external_id=comment.external_id,
                author=author,
                posted_at=comment.posted_at,
                body=comment.body,
                like_count=comment.like_count,
            ) is not None:
                inserted += 1
    return inserted
