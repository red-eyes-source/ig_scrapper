"""Cross-lens metrics: volume, engagement, sentiment mix, share of voice.

Two correctness notes that bite in practice:

1. Narrative-lens engagement is reported in ABSOLUTE terms, never as a rate.
   Engagement rate needs a follower denominator, and follower counts exist only
   for allowlisted figures. Dividing narrative engagement by a fabricated or
   sampled denominator would produce a confident-looking number with no basis.

2. Trend deltas compare the trailing ``trend_window_days`` against the
   preceding ``comparison_window_days`` MINUS that window — not against the
   whole comparison window, which would overlap itself and damp every movement.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from igpulse.config import AppConfig
from igpulse.store.db import Database

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class SentimentMix:
    positive: int = 0
    negative: int = 0
    neutral: int = 0
    uncertain: int = 0

    @property
    def total(self) -> int:
        return self.positive + self.negative + self.neutral + self.uncertain

    @property
    def confident_total(self) -> int:
        return self.positive + self.negative + self.neutral

    @property
    def net_sentiment(self) -> float | None:
        """(positive - negative) / confident total, in [-1, 1].

        None when nothing scored confidently — an explicit gap, rather than a
        misleading 0.0 that reads as 'neutral'.
        """
        if self.confident_total == 0:
            return None
        return (self.positive - self.negative) / self.confident_total

    @property
    def coverage(self) -> float | None:
        if self.total == 0:
            return None
        return self.confident_total / self.total


@dataclass(slots=True)
class NarrativeMetrics:
    narrative_key: str
    label: str
    post_count: int
    comment_count: int
    total_engagement: int
    distinct_authors: int
    post_sentiment: SentimentMix = field(default_factory=SentimentMix)
    comment_sentiment: SentimentMix = field(default_factory=SentimentMix)
    share_of_voice: float = 0.0
    volume_delta_pct: float | None = None


@dataclass(slots=True)
class FigureMetrics:
    handle: str
    display_name: str
    category: str
    follower_count: int | None
    post_count: int
    total_engagement: int
    avg_engagement_per_post: float
    engagement_rate: float | None
    audience_sentiment: SentimentMix = field(default_factory=SentimentMix)


def _mix_from_rows(rows: list[dict]) -> SentimentMix:
    mix = SentimentMix()
    for row in rows:
        label = row["label"]
        if hasattr(mix, label):
            setattr(mix, label, getattr(mix, label) + int(row["n"]))
    return mix


def narrative_metrics(
    cfg: AppConfig, db: Database, *, run_id: int
) -> list[NarrativeMetrics]:
    labels = {n.key: n.label for n in cfg.narratives.narratives}

    base = db.fetch(
        """
        SELECT p.narrative_key,
               COUNT(*)                                   AS post_count,
               COALESCE(SUM(p.like_count), 0)
                 + COALESCE(SUM(p.comment_count), 0)      AS engagement,
               COUNT(DISTINCT p.author_pseudonym)         AS distinct_authors
          FROM narrative_post p
         WHERE p.run_id = %s
         GROUP BY p.narrative_key
        """,
        (run_id,),
    )
    if not base:
        return []

    comment_counts = {
        r["narrative_key"]: int(r["n"])
        for r in db.fetch(
            """
            SELECT p.narrative_key, COUNT(*) AS n
              FROM narrative_comment c
              JOIN narrative_post p ON p.id = c.post_id
             WHERE c.run_id = %s
             GROUP BY p.narrative_key
            """,
            (run_id,),
        )
    }

    post_sent = _grouped_sentiment(db, run_id, "narrative_post")
    comment_sent = _grouped_sentiment(db, run_id, "narrative_comment")

    total_posts = sum(int(r["post_count"]) for r in base) or 1
    deltas = _volume_deltas(cfg, db)

    out: list[NarrativeMetrics] = []
    for row in base:
        key = row["narrative_key"]
        out.append(
            NarrativeMetrics(
                narrative_key=key,
                label=labels.get(key, key),
                post_count=int(row["post_count"]),
                comment_count=comment_counts.get(key, 0),
                total_engagement=int(row["engagement"]),
                distinct_authors=int(row["distinct_authors"]),
                post_sentiment=post_sent.get(key, SentimentMix()),
                comment_sentiment=comment_sent.get(key, SentimentMix()),
                share_of_voice=int(row["post_count"]) / total_posts,
                volume_delta_pct=deltas.get(key),
            )
        )
    out.sort(key=lambda m: m.post_count, reverse=True)
    return out


def _grouped_sentiment(
    db: Database, run_id: int, source_table: str
) -> dict[str, SentimentMix]:
    join = {
        "narrative_post": (
            "JOIN narrative_post p ON p.id = ss.source_id", "p.narrative_key"
        ),
        "narrative_comment": (
            "JOIN narrative_comment c ON c.id = ss.source_id "
            "JOIN narrative_post p ON p.id = c.post_id", "p.narrative_key"
        ),
    }[source_table]

    rows = db.fetch(
        f"""
        SELECT {join[1]} AS narrative_key, ss.label, COUNT(*) AS n
          FROM sentiment_score ss
          {join[0]}
         WHERE ss.source_table = %s AND p.run_id = %s
         GROUP BY {join[1]}, ss.label
        """,
        (source_table, run_id),
    )
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row["narrative_key"], []).append(row)
    return {k: _mix_from_rows(v) for k, v in grouped.items()}


def _volume_deltas(cfg: AppConfig, db: Database) -> dict[str, float | None]:
    """Trailing-window volume change per narrative, across all runs."""
    mcfg = cfg.settings.analysis.metrics
    now = datetime.now(timezone.utc)
    recent_start = now - timedelta(days=mcfg.trend_window_days)
    prior_start = now - timedelta(days=mcfg.comparison_window_days)
    # Prior window ends where the recent window begins — no overlap.
    rows = db.fetch(
        """
        SELECT narrative_key,
               COUNT(*) FILTER (WHERE posted_at >= %s)                    AS recent,
               COUNT(*) FILTER (WHERE posted_at >= %s AND posted_at < %s) AS prior
          FROM narrative_post
         WHERE posted_at >= %s
         GROUP BY narrative_key
        """,
        (recent_start, prior_start, recent_start, prior_start),
    )
    deltas: dict[str, float | None] = {}
    for row in rows:
        prior = int(row["prior"])
        recent = int(row["recent"])
        deltas[row["narrative_key"]] = (
            None if prior == 0 else (recent - prior) / prior * 100.0
        )
    return deltas


def figure_metrics(
    db: Database, *, run_id: int
) -> list[FigureMetrics]:
    rows = db.fetch(
        """
        SELECT f.handle, f.display_name, f.category,
               s.follower_count,
               COUNT(fp.id)                                AS post_count,
               COALESCE(SUM(fp.like_count), 0)
                 + COALESCE(SUM(fp.comment_count), 0)      AS engagement
          FROM public_figure f
          JOIN figure_snapshot s ON s.figure_id = f.id AND s.run_id = %s
          LEFT JOIN figure_post fp ON fp.figure_id = f.id AND fp.run_id = %s
         WHERE f.is_active
         GROUP BY f.handle, f.display_name, f.category, s.follower_count
        """,
        (run_id, run_id),
    )

    sentiment_rows = db.fetch(
        """
        SELECT f.handle, ss.label, COUNT(*) AS n
          FROM sentiment_score ss
          JOIN figure_post_comment c ON c.id = ss.source_id
          JOIN figure_post fp ON fp.id = c.figure_post_id
          JOIN public_figure f ON f.id = fp.figure_id
         WHERE ss.source_table = 'figure_post_comment' AND c.run_id = %s
         GROUP BY f.handle, ss.label
        """,
        (run_id,),
    )
    by_handle: dict[str, list[dict]] = {}
    for row in sentiment_rows:
        by_handle.setdefault(row["handle"], []).append(row)

    out: list[FigureMetrics] = []
    for row in rows:
        posts = int(row["post_count"])
        engagement = int(row["engagement"])
        followers = row["follower_count"]
        avg = (engagement / posts) if posts else 0.0
        out.append(
            FigureMetrics(
                handle=row["handle"],
                display_name=row["display_name"],
                category=row["category"],
                follower_count=followers,
                post_count=posts,
                total_engagement=engagement,
                avg_engagement_per_post=avg,
                # Engagement rate is only meaningful with a real follower count.
                engagement_rate=(avg / followers) if followers else None,
                audience_sentiment=_mix_from_rows(by_handle.get(row["handle"], [])),
            )
        )
    out.sort(key=lambda m: m.total_engagement, reverse=True)
    return out
