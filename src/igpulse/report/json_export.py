"""JSON export: the full dataset behind a run.

Where the HTML dashboard summarises, this writes everything — every post, its
likes, comments and engagement, the hashtags that collected it, the themes, the
sentiment rows, and the metrics derived from them. Every published figure can
be recomputed from the raw rows in the same file, so a client can check the
numbers rather than take them on trust.

Two properties worth relying on:

* **Stable schema.** ``schema_version`` changes only on a breaking change, so
  downstream code can pin against it. Field names match the database columns.
* **The privacy invariant holds here too.** Narrative authors appear as their
  per-run pseudonym; no handle column exists to export. Only allowlisted public
  figures are named, and their entries carry the written justification that put
  them on the list, so an auditor can see the basis for every named account.

Timestamps are ISO-8601 with an explicit UTC offset. Storage is UTC throughout;
the client-facing local time appears once, in ``generated_at_local``.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from igpulse.analyze.metrics import FigureMetrics, NarrativeMetrics, SentimentMix
from igpulse.analyze.sentiment import SentimentSummary
from igpulse.analyze.themes import ThemeTerm
from igpulse.config import AppConfig

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "1.0"


def _encode(value: Any) -> Any:
    """JSON encoder for the types psycopg returns."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    raise TypeError(f"cannot serialise {type(value).__name__}")


def _mix(mix: SentimentMix) -> dict[str, Any]:
    return {
        "positive": mix.positive,
        "negative": mix.negative,
        "neutral": mix.neutral,
        "uncertain": mix.uncertain,
        "total": mix.total,
        "confident_total": mix.confident_total,
        # null, not 0.0 — nothing scored confidently means unknown, and a 0.0
        # here would be read downstream as "neutral".
        "net_sentiment": mix.net_sentiment,
        "coverage": mix.coverage,
    }


def _clip(text: str | None, limit: int) -> str | None:
    if text is None or limit <= 0:
        return text
    return text if len(text) <= limit else text[:limit] + "…"


def build_json(
    cfg: AppConfig,
    *,
    narratives: list[NarrativeMetrics],
    figures: list[FigureMetrics],
    themes: list[ThemeTerm],
    sentiment: SentimentSummary,
    generated_at: datetime,
    posts: dict[str, list[dict[str, Any]]] | None = None,
    figure_rows: list[dict[str, Any]] | None = None,
    provenance: dict[str, Any] | None = None,
    window: dict[str, Any] | None = None,
    output_path: Path | None = None,
) -> Path:
    jcfg = cfg.settings.report.json_options
    out_dir = Path(cfg.settings.report.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    local_time = generated_at.astimezone(cfg.timezone)
    if output_path is None:
        output_path = out_dir / (
            f"{cfg.settings.project.client_label}_"
            f"{local_time:%Y%m%d_%H%M}_report.json"
        )

    themes_by_narrative: dict[str, list[ThemeTerm]] = {}
    for term in themes:
        themes_by_narrative.setdefault(term.narrative_key, []).append(term)

    narrative_config = {n.key: n for n in cfg.narratives.narratives}
    ncfg = cfg.settings.ingest.narrative

    document: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at_utc": generated_at.isoformat(),
        "generated_at_local": local_time.isoformat(),
        "client": cfg.settings.project.client_label,
        "timezone": cfg.settings.project.timezone,
        "provenance": provenance,
        "collection_window": window,
        "search_definition": {
            "lookback": ncfg.lookback,
            "results_per_term": ncfg.results_per_term,
            "comments_per_post": ncfg.comments_per_post,
            "narratives": [
                {
                    "key": n.key,
                    "label": n.label,
                    "hashtags": n.hashtags,
                    "hashtag_urls": [
                        f"https://www.instagram.com/explore/tags/{t}/"
                        for t in n.hashtags
                    ],
                    "caption_filter_terms": n.terms,
                }
                for n in cfg.narratives.narratives
            ],
        },
        "sentiment_config": {
            "provider": cfg.settings.sentiment.provider,
            "actor_id": cfg.settings.sentiment.actor_id,
            "min_confidence": cfg.settings.sentiment.min_confidence,
            "min_chars": cfg.settings.sentiment.min_chars,
            "scored": sentiment.scored,
            "uncertain": sentiment.uncertain,
            "coverage": sentiment.coverage,
        },
        "totals": {
            "posts": sum(n.post_count for n in narratives),
            "comments": sum(n.comment_count for n in narratives),
            "engagement": sum(n.total_engagement for n in narratives),
            "distinct_authors": sum(n.distinct_authors for n in narratives),
            "narratives": len(narratives),
        },
        "narratives": [],
        "public_figures": [],
        "notes": {
            "author_identity": (
                "Narrative authors are per-run pseudonyms. The salt is "
                "generated at run start and never persisted, so the same "
                "author yields a different pseudonym in every run and cannot "
                "be tracked across them. author_handle is present only for the "
                "capped set of highest-engagement posts eligible to be quoted, "
                "so a citation can be checked; those names expire on their own "
                "shorter retention. Comments are never attributed. Allowlisted "
                "public figures are named throughout."
            ),
            "engagement": (
                "engagement = like_count + comment_count, absolute. Rates are "
                "not given for the narrative lens because follower counts are "
                "collected only for allowlisted accounts."
            ),
            "net_sentiment": (
                "(positive - negative) / confident_total, range -1 to 1. Null "
                "when nothing scored above min_confidence: unknown, not "
                "neutral."
            ),
            "sampling": (
                "Instagram returns a ranked subset, not a census. Volume is "
                "comparable between runs of this pipeline, not an estimate of "
                "total platform activity."
            ),
        },
    }

    for metrics in narratives:
        cfg_entry = narrative_config.get(metrics.narrative_key)
        entry: dict[str, Any] = {
            "key": metrics.narrative_key,
            "label": metrics.label,
            "hashtags": cfg_entry.hashtags if cfg_entry else [],
            "caption_filter_terms": cfg_entry.terms if cfg_entry else [],
            "metrics": {
                "post_count": metrics.post_count,
                "comment_count": metrics.comment_count,
                "total_engagement": metrics.total_engagement,
                "distinct_authors": metrics.distinct_authors,
                "share_of_voice": metrics.share_of_voice,
                "volume_delta_pct": metrics.volume_delta_pct,
            },
            "sentiment": {
                "posts": _mix(metrics.post_sentiment),
                "comments": _mix(metrics.comment_sentiment),
            },
        }

        if jcfg.include_themes:
            entry["themes"] = [
                {
                    "term": t.term,
                    "frequency": t.frequency,
                    "doc_frequency": t.doc_frequency,
                    "score": round(t.score, 4),
                }
                for t in themes_by_narrative.get(metrics.narrative_key, [])
            ]

        if jcfg.include_posts and posts:
            entry["posts"] = [
                {
                    "shortcode": p["post_shortcode"],
                    "url": p["url"],
                    "posted_at": p["posted_at"],
                    "author_pseudonym": p["author_pseudonym"],
                    # Present only for the capped set of citable posts; null
                    # for everything else, and never present on comments.
                    "author_handle": p.get("author_handle"),
                    "author_is_verified": p["author_is_verified"],
                    "caption": _clip(p["caption"], jcfg.caption_max_chars),
                    "like_count": p["like_count"],
                    "comment_count": p["comment_count"],
                    "engagement": p["engagement"],
                    "is_video": p["is_video"],
                    "view_count": p["view_count"],
                    "matched_terms": (
                        cfg_entry.matches_terms(p["caption"]) if cfg_entry else []
                    ),
                    "sentiment": (
                        {
                            "label": p["sentiment_label"],
                            "confidence": p["sentiment_confidence"],
                        }
                        if p["sentiment_label"]
                        else None
                    ),
                    **(
                        {
                            "comments": [
                                {
                                    "external_id": c["comment_external_id"],
                                    "author_pseudonym": c["author_pseudonym"],
                                    "posted_at": c["posted_at"],
                                    "body": _clip(
                                        c["body"], jcfg.caption_max_chars
                                    ),
                                    "like_count": c["like_count"],
                                    "sentiment": (
                                        {
                                            "label": c["sentiment_label"],
                                            "confidence": c[
                                                "sentiment_confidence"
                                            ],
                                        }
                                        if c["sentiment_label"]
                                        else None
                                    ),
                                }
                                for c in p.get("comments", [])
                            ]
                        }
                        if jcfg.include_comments
                        else {}
                    ),
                }
                for p in posts.get(metrics.narrative_key, [])
            ]

        document["narratives"].append(entry)

    # Public figures: named, with the justification that put them on the list.
    by_handle: dict[str, dict[str, Any]] = {}
    for row in figure_rows or []:
        handle = row["handle"]
        record = by_handle.setdefault(
            handle,
            {
                "handle": handle,
                "display_name": row["display_name"],
                "category": row["category"],
                "jurisdiction": row["jurisdiction"],
                "justification": row["justification"],
                "snapshot": {
                    "follower_count": row["follower_count"],
                    "following_count": row["following_count"],
                    "post_count": row["post_count"],
                    "is_verified": row["is_verified"],
                    "biography": row["biography"],
                },
                "posts": [],
            },
        )
        if row["post_shortcode"]:
            record["posts"].append(
                {
                    "shortcode": row["post_shortcode"],
                    "url": (
                        f"https://www.instagram.com/p/{row['post_shortcode']}/"
                    ),
                    "posted_at": row["posted_at"],
                    "caption": _clip(row["caption"], jcfg.caption_max_chars),
                    "like_count": row["like_count"],
                    "comment_count": row["comment_count"],
                    "is_video": row["is_video"],
                    "view_count": row["view_count"],
                }
            )

    metrics_by_handle = {f.handle: f for f in figures}
    for handle, record in by_handle.items():
        fm = metrics_by_handle.get(handle)
        if fm:
            record["metrics"] = {
                "post_count": fm.post_count,
                "total_engagement": fm.total_engagement,
                "avg_engagement_per_post": fm.avg_engagement_per_post,
                "engagement_rate": fm.engagement_rate,
            }
            record["audience_sentiment"] = _mix(fm.audience_sentiment)
        document["public_figures"].append(record)

    output_path.write_text(
        json.dumps(
            document,
            indent=jcfg.indent or None,
            default=_encode,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    size_kb = output_path.stat().st_size / 1024
    logger.info("wrote %s (%.1f KB)", output_path, size_kb)
    return output_path
