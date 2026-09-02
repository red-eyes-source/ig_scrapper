"""Theme extraction: what language is actually carrying each narrative.

Deliberately a TF-IDF-style term ranking rather than an LLM topic model. Two
reasons: it is reproducible run-to-run (an LLM's topic labels drift, which
makes week-over-week comparison meaningless), and it keeps the whole pipeline
inside the Apify + Postgres footprint you asked for.

Terms are ranked by frequency x inverse document frequency across narratives,
so a term that appears everywhere ("government") scores low and a term specific
to one narrative scores high.
"""

from __future__ import annotations

import logging
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass

from igpulse.config import AppConfig
from igpulse.store.db import Database

logger = logging.getLogger(__name__)

# Conservative English + transliterated-Hindi stoplist. Extended per client
# via analysis.themes.stopword_extra rather than edited here.
_BASE_STOPWORDS = {
    "the", "and", "for", "that", "this", "with", "you", "are", "not", "but",
    "have", "has", "was", "were", "will", "from", "they", "their", "our",
    "your", "its", "it's", "all", "can", "who", "what", "when", "how", "why",
    "about", "into", "over", "than", "then", "them", "his", "her", "she",
    "him", "one", "two", "out", "get", "got", "just", "like", "also", "more",
    "most", "some", "any", "been", "being", "does", "did", "doing", "would",
    "could", "should", "there", "here", "very", "much", "many", "such",
    "hai", "hain", "nahi", "kya", "aur", "yeh", "woh", "toh", "bhi", "hoga",
    "karo", "kare", "karta", "liye", "wala", "wale", "mein", "par", "koi",
    "https", "http", "www", "com",
}

_TOKEN_RE = re.compile(r"[a-z0-9#@']+")
_URL_RE = re.compile(r"https?://\S+")
_MENTION_RE = re.compile(r"@[A-Za-z0-9._]+")


@dataclass(slots=True)
class ThemeTerm:
    narrative_key: str
    term: str
    frequency: int
    doc_frequency: int
    score: float


def _tokenise(text: str, stopwords: set[str]) -> list[str]:
    # Strip URLs and @mentions before tokenising. Mentions are removed
    # specifically because a frequently-tagged account would otherwise surface
    # as a "theme", turning issue analysis back into person tracking.
    cleaned = _MENTION_RE.sub(" ", _URL_RE.sub(" ", text.lower()))
    return [
        t for t in _TOKEN_RE.findall(cleaned)
        if len(t) > 2 and t not in stopwords and not t.isdigit()
    ]


def _ngrams(tokens: list[str], lo: int, hi: int) -> list[str]:
    out: list[str] = []
    for n in range(lo, hi + 1):
        if n == 1:
            out.extend(tokens)
        else:
            out.extend(
                " ".join(tokens[i : i + n]) for i in range(len(tokens) - n + 1)
            )
    return out


def extract_themes(
    cfg: AppConfig, db: Database, *, run_id: int
) -> list[ThemeTerm]:
    tcfg = cfg.settings.analysis.themes
    stopwords = _BASE_STOPWORDS | {w.lower() for w in tcfg.stopword_extra}
    lo, hi = tcfg.ngram_range

    rows = db.fetch(
        """
        SELECT narrative_key, caption AS body FROM narrative_post
         WHERE run_id = %s AND caption IS NOT NULL
        UNION ALL
        SELECT p.narrative_key, c.body
          FROM narrative_comment c
          JOIN narrative_post p ON p.id = c.post_id
         WHERE c.run_id = %s AND c.body IS NOT NULL
        """,
        (run_id, run_id),
    )
    if not rows:
        logger.warning("no narrative text found for run %d", run_id)
        return []

    # term frequency per narrative, and in how many narratives each term appears
    per_narrative: dict[str, Counter[str]] = defaultdict(Counter)
    doc_counts: dict[str, Counter[str]] = defaultdict(Counter)

    for row in rows:
        key = row["narrative_key"]
        tokens = _tokenise(row["body"], stopwords)
        if not tokens:
            continue
        grams = _ngrams(tokens, lo, hi)
        per_narrative[key].update(grams)
        # doc_frequency counts documents, not occurrences
        doc_counts[key].update(set(grams))

    narrative_count = max(len(per_narrative), 1)
    term_narrative_spread: Counter[str] = Counter()
    for counter in per_narrative.values():
        term_narrative_spread.update(counter.keys())

    results: list[ThemeTerm] = []
    for key, counter in per_narrative.items():
        scored: list[ThemeTerm] = []
        for term, freq in counter.items():
            if freq < tcfg.min_term_frequency:
                continue
            spread = term_narrative_spread[term]
            idf = math.log((narrative_count + 1) / (spread + 1)) + 1.0
            scored.append(
                ThemeTerm(
                    narrative_key=key,
                    term=term,
                    frequency=freq,
                    doc_frequency=doc_counts[key][term],
                    score=freq * idf,
                )
            )
        scored.sort(key=lambda t: t.score, reverse=True)
        results.extend(scored[: tcfg.max_themes_per_report])

    db.upsert_theme_terms(
        [
            (run_id, t.narrative_key, t.term, t.frequency, t.doc_frequency, t.score)
            for t in results
        ]
    )
    logger.info(
        "extracted %d theme terms across %d narratives",
        len(results), len(per_narrative),
    )
    return results
