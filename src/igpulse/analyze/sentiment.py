"""Sentiment scoring via a configurable Apify actor.

Provider is pluggable because the Apify store's sentiment actors are
third-party, thinly documented, and variable in quality — particularly on
code-mixed Hinglish, which is a large share of Indian political commentary.
Swapping providers is a config change (actor_id + field_map), not a code change.

Two safeguards worth knowing about:

1. The response shape is validated against ``field_map`` on the first batch. A
   provider that silently renames its output fields fails loudly at batch one
   rather than writing a run's worth of nulls.

2. Rows scoring below ``min_confidence`` are stored as ``uncertain`` rather
   than being forced into positive/negative. Reports then quote an explicit
   coverage figure instead of laundering low-confidence noise as signal — which
   matters when the headline number is "sentiment moved 6 points".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from igpulse.apify.actors import build_sentiment_input
from igpulse.apify.client import ApifyClient
from igpulse.config import AppConfig
from igpulse.store.db import Database

logger = logging.getLogger(__name__)

SCOREABLE_TABLES = (
    "narrative_post",
    "narrative_comment",
    "figure_post",
    "figure_post_comment",
)


class SentimentSchemaError(RuntimeError):
    """The actor's output does not match the configured field_map."""


@dataclass(slots=True)
class SentimentSummary:
    scored: int
    uncertain: int
    skipped_short: int

    @property
    def coverage(self) -> float:
        total = self.scored + self.uncertain
        return (self.scored / total) if total else 0.0


def _chunk(seq: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def _validate_shape(record: dict[str, Any], label_key: str, score_key: str) -> None:
    missing = [k for k in (label_key, score_key) if k not in record]
    if missing:
        raise SentimentSchemaError(
            f"sentiment actor response is missing configured field(s) "
            f"{missing}. Response keys were: {sorted(record)}. Update "
            f"sentiment.field_map in config/settings.yaml to match."
        )


def score_run(
    cfg: AppConfig,
    client: ApifyClient,
    db: Database,
    *,
    run_id: int,
    tables: Sequence[str] = SCOREABLE_TABLES,
) -> SentimentSummary:
    scfg = cfg.settings.sentiment
    summary = SentimentSummary(scored=0, uncertain=0, skipped_short=0)
    shape_validated = False

    for table in tables:
        if table not in SCOREABLE_TABLES:
            raise ValueError(f"table {table!r} is not scoreable")

        pending = db.unscored_texts(
            table, scfg.provider, run_id=run_id, min_chars=scfg.min_chars
        )
        if not pending:
            continue
        logger.info("scoring %d rows from %s", len(pending), table)

        for batch in _chunk(pending, scfg.batch_size):
            ids = [row_id for row_id, _ in batch]
            texts = [text for _, text in batch]

            _, items = client.run_and_collect(
                scfg.actor_id,
                build_sentiment_input(texts, scfg.input_field),
            )

            if not items:
                logger.warning(
                    "sentiment actor returned no items for a batch of %d; "
                    "leaving these rows unscored", len(batch),
                )
                continue

            if not shape_validated:
                _validate_shape(
                    items[0], scfg.field_map.label, scfg.field_map.score
                )
                shape_validated = True

            if len(items) != len(batch):
                # A line-oriented actor can drop blank lines, desynchronising
                # the response from the request. Refusing to guess is correct:
                # mis-aligned sentiment is worse than absent sentiment.
                logger.error(
                    "sentiment actor %s returned %d result(s) for %d input "
                    "texts on %s. Skipping the batch rather than assigning "
                    "scores to the wrong rows.\n"
                    "  This is the newline-batching assumption failing: the "
                    "actor is treating the whole batch as ONE document instead "
                    "of one result per line.\n"
                    "  Fix by setting sentiment.batch_size to 1 in "
                    "settings.yaml (correct, but one actor run per row - "
                    "slow and expensive), or switch sentiment.actor_id to an "
                    "actor that accepts an array of texts.",
                    scfg.actor_id, len(items), len(batch), table,
                )
                continue

            rows: list[tuple[str, int, str, float, str, str]] = []
            for row_id, item in zip(ids, items):
                raw_label = str(item.get(scfg.field_map.label, "")).strip()
                try:
                    confidence = float(item.get(scfg.field_map.score) or 0.0)
                except (TypeError, ValueError):
                    confidence = 0.0
                confidence = max(0.0, min(1.0, confidence))

                canonical = scfg.label_normalisation.resolve(raw_label)
                if canonical is None:
                    logger.warning(
                        "unmapped sentiment label %r; add it to "
                        "sentiment.label_normalisation", raw_label,
                    )
                    canonical, confidence = "uncertain", 0.0
                elif confidence < scfg.min_confidence:
                    canonical = "uncertain"

                if canonical == "uncertain":
                    summary.uncertain += 1
                else:
                    summary.scored += 1

                rows.append(
                    (table, row_id, canonical, confidence,
                     scfg.provider, scfg.actor_id)
                )

            db.upsert_sentiment(rows)

    if summary.scored == 0 and summary.uncertain == 0:
        logger.warning(
            "sentiment scored nothing at all for run %d. Either no rows had "
            "text of at least %d characters, or every batch was skipped - "
            "check the errors above. Reports will show 0%% coverage and 'n/a' "
            "for every sentiment figure, which is correct: unknown, not "
            "neutral.",
            run_id, scfg.min_chars,
        )
    logger.info(
        "sentiment: %d scored, %d uncertain (%.1f%% coverage)",
        summary.scored, summary.uncertain, summary.coverage * 100,
    )
    return summary
