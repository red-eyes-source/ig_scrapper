"""JSON export tests.

The export carries the raw rows a client will actually compute against, so two
things matter: the numbers must reconcile with the aggregates published
alongside them, and the privacy invariant must survive serialisation. A handle
that leaks here leaks into a file that gets emailed.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from igpulse.analyze.metrics import FigureMetrics, NarrativeMetrics, SentimentMix
from igpulse.analyze.sentiment import SentimentSummary
from igpulse.analyze.themes import ThemeTerm
from igpulse.config import Narrative, NarrativeList, load_config
from igpulse.report.json_export import SCHEMA_VERSION, build_json


@pytest.fixture()
def cfg(tmp_path):
    c = load_config()
    c.settings.report.output_dir = str(tmp_path)
    c.narratives = NarrativeList(
        narratives=[
            Narrative(
                key="jobs", label="Jobs and unemployment",
                hashtags=["berozgari", "unemployment"],
                terms=["paper leak"],
            )
        ]
    )
    return c


@pytest.fixture()
def payload():
    return dict(
        narratives=[
            NarrativeMetrics(
                narrative_key="jobs", label="Jobs and unemployment",
                post_count=2, comment_count=3, total_engagement=5400,
                distinct_authors=2,
                post_sentiment=SentimentMix(positive=1, negative=1),
                comment_sentiment=SentimentMix(
                    positive=1, negative=2, uncertain=1
                ),
                share_of_voice=1.0, volume_delta_pct=12.5,
            )
        ],
        figures=[],
        themes=[
            ThemeTerm("jobs", "paper leak", 9, 7, 21.4),
            ThemeTerm("jobs", "vacancy", 4, 4, 9.1),
        ],
        sentiment=SentimentSummary(scored=4, uncertain=1, skipped_short=0),
        generated_at=datetime(2026, 9, 2, 6, 30, tzinfo=timezone.utc),
        posts={
            "jobs": [
                {
                    "id": 1, "narrative_key": "jobs",
                    "post_shortcode": "AbC123",
                    "url": "https://www.instagram.com/p/AbC123/",
                    "author_pseudonym": "9f2c4a1b77de0031",
                    "author_is_verified": False,
                    "posted_at": datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc),
                    "caption": "Another paper leak in the recruitment exam",
                    "like_count": 5000, "comment_count": 400,
                    "engagement": 5400, "is_video": False, "view_count": None,
                    "sentiment_label": "negative", "sentiment_confidence": 0.88,
                    "comments": [
                        {
                            "id": 11, "post_id": 1,
                            "comment_external_id": "c1",
                            "author_pseudonym": "aa11bb22cc33dd44",
                            "posted_at": datetime(
                                2026, 9, 1, 9, 0, tzinfo=timezone.utc
                            ),
                            "body": "Same every year", "like_count": 12,
                            "sentiment_label": "negative",
                            "sentiment_confidence": 0.8,
                        }
                    ],
                }
            ]
        },
        figure_rows=[],
        provenance={
            "id": 7, "lens": "narrative", "status": "succeeded",
            "started_at": datetime(2026, 9, 2, 6, 0, tzinfo=timezone.utc),
            "finished_at": None, "actor_run_ids": ["RUN1", "RUN2"],
            "config_fingerprint": "abc123", "items_ingested": 5,
            "error_detail": None,
        },
        window={
            "oldest": datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc),
            "newest": datetime(2026, 9, 2, 5, 0, tzinfo=timezone.utc),
            "n": 2,
        },
    )


def _load(cfg, payload) -> dict:
    return json.loads(build_json(cfg, **payload).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
def test_document_is_valid_json_with_a_pinned_schema_version(cfg, payload):
    doc = _load(cfg, payload)
    assert doc["schema_version"] == SCHEMA_VERSION


def test_search_definition_records_hashtags_and_their_urls(cfg, payload):
    """The report must say what was searched, not just what came back."""
    entry = _load(cfg, payload)["search_definition"]["narratives"][0]
    assert entry["hashtags"] == ["berozgari", "unemployment"]
    assert entry["hashtag_urls"] == [
        "https://www.instagram.com/explore/tags/berozgari/",
        "https://www.instagram.com/explore/tags/unemployment/",
    ]
    assert entry["caption_filter_terms"] == ["paper leak"]


def test_posts_carry_likes_comments_and_engagement(cfg, payload):
    post = _load(cfg, payload)["narratives"][0]["posts"][0]
    assert post["like_count"] == 5000
    assert post["comment_count"] == 400
    assert post["engagement"] == 5400
    assert post["url"] == "https://www.instagram.com/p/AbC123/"
    assert post["sentiment"] == {"label": "negative", "confidence": 0.88}


def test_engagement_reconciles_with_the_published_aggregate(cfg, payload):
    """Raw rows must sum to the headline figure, or the export is decorative."""
    doc = _load(cfg, payload)
    posts = doc["narratives"][0]["posts"]
    assert sum(p["engagement"] for p in posts) == doc["totals"]["engagement"]
    for post in posts:
        assert post["engagement"] == post["like_count"] + post["comment_count"]


def test_comments_are_nested_under_their_post(cfg, payload):
    comments = _load(cfg, payload)["narratives"][0]["posts"][0]["comments"]
    assert len(comments) == 1
    assert comments[0]["body"] == "Same every year"
    assert comments[0]["like_count"] == 12
    assert comments[0]["sentiment"]["label"] == "negative"


def test_caption_filter_matches_are_recorded_per_post(cfg, payload):
    post = _load(cfg, payload)["narratives"][0]["posts"][0]
    assert post["matched_terms"] == ["paper leak"]


def test_themes_included_with_frequency_and_score(cfg, payload):
    themes = _load(cfg, payload)["narratives"][0]["themes"]
    assert themes[0]["term"] == "paper leak"
    assert themes[0]["frequency"] == 9
    assert themes[0]["doc_frequency"] == 7


def test_net_sentiment_is_null_not_zero_when_nothing_scored(cfg, payload):
    payload["narratives"][0].comment_sentiment = SentimentMix(uncertain=5)
    mix = _load(cfg, payload)["narratives"][0]["sentiment"]["comments"]
    # null means unknown. 0.0 would be read as neutral.
    assert mix["net_sentiment"] is None
    assert mix["uncertain"] == 5


def test_uncertain_rows_excluded_from_net_sentiment_denominator(cfg, payload):
    mix = _load(cfg, payload)["narratives"][0]["sentiment"]["comments"]
    assert mix["confident_total"] == 3
    assert mix["net_sentiment"] == pytest.approx((1 - 2) / 3)


def test_provenance_and_window_are_serialised(cfg, payload):
    doc = _load(cfg, payload)
    assert doc["provenance"]["actor_run_ids"] == ["RUN1", "RUN2"]
    assert doc["provenance"]["config_fingerprint"] == "abc123"
    assert doc["collection_window"]["n"] == 2


def test_timestamps_are_iso_with_offset(cfg, payload):
    post = _load(cfg, payload)["narratives"][0]["posts"][0]
    parsed = datetime.fromisoformat(post["posted_at"])
    assert parsed.tzinfo is not None


# --------------------------------------------------------------------------- #
# Privacy
# --------------------------------------------------------------------------- #
def test_no_handle_fields_anywhere_in_narrative_output(cfg, payload):
    """The invariant has to survive serialisation, not just storage."""
    doc = _load(cfg, payload)
    for narrative in doc["narratives"]:
        for post in narrative.get("posts", []):
            assert "handle" not in post
            assert "username" not in post
            assert post["author_pseudonym"]
            for comment in post.get("comments", []):
                assert "handle" not in comment
                assert "username" not in comment


def test_pseudonyms_are_opaque_not_derived_from_a_handle(cfg, payload):
    post = _load(cfg, payload)["narratives"][0]["posts"][0]
    value = post["author_pseudonym"]
    assert len(value) == 16
    assert all(c in "0123456789abcdef" for c in value)


def test_export_explains_the_identity_model_to_its_reader(cfg, payload):
    """A downstream consumer must not mistake pseudonyms for stable user IDs."""
    notes = _load(cfg, payload)["notes"]["author_identity"]
    assert "never persisted" in notes
    assert "cannot be tracked across" in notes


def test_named_figures_carry_their_justification(cfg, payload):
    payload["figure_rows"] = [
        {
            "handle": "example_mp", "display_name": "A. N. Example",
            "category": "elected_official", "jurisdiction": "Lok Sabha",
            "justification": "Sitting MP; official constituency account.",
            "follower_count": 120000, "following_count": 50, "post_count": 10,
            "is_verified": True, "biography": "MP",
            "post_shortcode": None, "posted_at": None, "caption": None,
            "like_count": None, "comment_count": None, "is_video": None,
            "view_count": None,
        }
    ]
    payload["figures"] = [
        FigureMetrics("example_mp", "A. N. Example", "elected_official",
                      120000, 10, 5000, 500.0, 500.0 / 120000)
    ]
    figure = _load(cfg, payload)["public_figures"][0]
    # Naming an account requires a written basis, and that basis travels with
    # the export so it can be audited.
    assert figure["justification"].startswith("Sitting MP")
    assert figure["metrics"]["post_count"] == 10


# --------------------------------------------------------------------------- #
# Config switches
# --------------------------------------------------------------------------- #
def test_comments_can_be_excluded(cfg, payload):
    cfg.settings.report.json_options.include_comments = False
    post = _load(cfg, payload)["narratives"][0]["posts"][0]
    assert "comments" not in post


def test_posts_can_be_excluded_for_a_metrics_only_export(cfg, payload):
    cfg.settings.report.json_options.include_posts = False
    narrative = _load(cfg, payload)["narratives"][0]
    assert "posts" not in narrative
    assert narrative["metrics"]["post_count"] == 2


def test_caption_cap_truncates_with_an_ellipsis(cfg, payload):
    cfg.settings.report.json_options.caption_max_chars = 10
    post = _load(cfg, payload)["narratives"][0]["posts"][0]
    assert post["caption"] == "Another pa…"


def test_empty_formats_list_is_rejected(cfg):
    from pydantic import ValidationError

    data = cfg.settings.report.model_dump(by_alias=True)
    data["formats"] = []
    with pytest.raises(ValidationError, match="write nothing"):
        type(cfg.settings.report).model_validate(data)
