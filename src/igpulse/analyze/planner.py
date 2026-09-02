"""Dry-run planner: what a cycle would do, and roughly what it would cost.

Pure function of config — no network, no database, no credits. Exists because
Apify bills per result and the volume is a product of several config values, so
the difference between a $4 run and a $400 run is one number nobody re-read.

Every figure here is an UPPER BOUND. Actual results are usually lower:
Instagram search returns fewer posts than requested for narrow terms, not every
post has the requested number of comments, and text below `min_chars` is never
sent to the sentiment actor. Treat the estimate as a ceiling to approve, not a
forecast to budget against.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from igpulse.config import AppConfig
from igpulse.ingest.figures import LENS_CATEGORIES


@dataclass(slots=True)
class LensPlan:
    lens: str
    actor_runs: int
    max_posts: int
    max_comments: int
    detail: list[str] = field(default_factory=list)

    @property
    def max_items(self) -> int:
        return self.max_posts + self.max_comments


@dataclass(slots=True)
class CyclePlan:
    lenses: list[LensPlan]
    warnings: list[str] = field(default_factory=list)

    @property
    def total_actor_runs(self) -> int:
        return sum(l.actor_runs for l in self.lenses)

    @property
    def total_posts(self) -> int:
        return sum(l.max_posts for l in self.lenses)

    @property
    def total_comments(self) -> int:
        return sum(l.max_comments for l in self.lenses)

    @property
    def total_items(self) -> int:
        return self.total_posts + self.total_comments

    def cost(self, cfg: AppConfig) -> dict[str, float]:
        """Upper-bound USD estimate, split by what drives it."""
        rates = cfg.settings.cost
        scrape = self.total_posts / 1000 * rates.instagram_scraper_per_1k
        comments = self.total_comments / 1000 * rates.comment_scraper_per_1k
        # Every collected post and comment carries text that may be scored.
        sentiment = self.total_items / 1000 * rates.sentiment_per_1k
        return {
            "scraping": scrape,
            "comments": comments,
            "sentiment": sentiment,
            "total": scrape + comments + sentiment,
        }


def plan_cycle(cfg: AppConfig) -> CyclePlan:
    lenses: list[LensPlan] = []
    warnings: list[str] = []

    # -- narrative lens: one actor run per search term ---------------------- #
    ncfg = cfg.settings.ingest.narrative
    terms = [
        (n.key, term)
        for n in cfg.narratives.narratives
        for term in n.search_terms
    ]
    narrative_posts = len(terms) * ncfg.results_per_term
    narrative_comments = narrative_posts * ncfg.comments_per_post
    # One scrape run per term, plus one comment run per term's post batch.
    narrative_runs = len(terms) * (2 if ncfg.comments_per_post else 1)

    lenses.append(
        LensPlan(
            lens="narrative",
            actor_runs=narrative_runs,
            max_posts=narrative_posts,
            max_comments=narrative_comments,
            detail=[
                f"{len(terms)} search term(s) across "
                f"{len(cfg.narratives.narratives)} narrative(s)",
                f"{ncfg.results_per_term} results/term, "
                f"{ncfg.comments_per_post} comments/post",
                f"lookback {ncfg.lookback}",
            ],
        )
    )

    # -- figure lenses: one run for all handles, plus comments -------------- #
    for lens, categories in LENS_CATEGORIES.items():
        figures = [
            f for f in cfg.public_figures.figures if f.category in categories
        ]
        icfg = (
            cfg.settings.ingest.public_figure
            if lens == "public_figure"
            else cfg.settings.ingest.own_side
        )
        posts = len(figures) * icfg.posts_per_handle
        comments = posts * icfg.comments_per_post
        runs = 0 if not figures else (2 if icfg.comments_per_post else 1)

        lenses.append(
            LensPlan(
                lens=lens,
                actor_runs=runs,
                max_posts=posts,
                max_comments=comments,
                detail=[
                    f"{len(figures)} allowlisted handle(s)",
                    f"{icfg.posts_per_handle} posts/handle, "
                    f"{icfg.comments_per_post} comments/post",
                    f"lookback {icfg.lookback}",
                ],
            )
        )

    plan = CyclePlan(lenses=lenses, warnings=warnings)

    # -- sanity warnings ---------------------------------------------------- #
    if not terms:
        warnings.append(
            "No narratives configured — the narrative lens will collect nothing."
        )
    if not cfg.public_figures.figures:
        warnings.append(
            "Allowlist is empty — the public-figure and own-side lenses will "
            "collect nothing."
        )
    if plan.total_items > 50_000:
        warnings.append(
            f"This cycle could collect up to {plan.total_items:,} items. That "
            f"is a large first run — consider lowering results_per_term and "
            f"comments_per_post, running one lens, and checking actual "
            f"consumption in the Apify console before scaling up."
        )
    scored_batches = -(-plan.total_items // cfg.settings.sentiment.batch_size)
    if scored_batches > 500:
        warnings.append(
            f"Sentiment would run in {scored_batches:,} batches, each a "
            f"separate actor run. Expect the analyse stage to dominate "
            f"wall-clock time."
        )
    return plan
