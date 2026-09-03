"""Tests for the pure-analysis helpers (no database or network required)."""

from __future__ import annotations

from datetime import datetime, timezone

from igpulse.analyze.metrics import SentimentMix
from igpulse.analyze.themes import _ngrams, _tokenise
from igpulse.apify.actors import (
    _parse_timestamp,
    build_hashtag_input,
    build_sentiment_input,
    normalise_post,
)


# -- SentimentMix ----------------------------------------------------------- #
def test_net_sentiment_excludes_uncertain_from_the_denominator():
    mix = SentimentMix(positive=30, negative=10, neutral=10, uncertain=50)
    # 50 uncertain rows must not drag the score toward zero.
    assert mix.net_sentiment == (30 - 10) / 50
    assert mix.coverage == 50 / 100


def test_net_sentiment_is_none_when_nothing_scored_confidently():
    mix = SentimentMix(uncertain=25)
    # None, not 0.0 — a 0.0 here would be read as "neutral" rather than "unknown".
    assert mix.net_sentiment is None
    assert mix.coverage == 0.0


def test_empty_mix_reports_no_coverage():
    assert SentimentMix().coverage is None


# -- timestamps ------------------------------------------------------------- #
def test_naive_timestamps_are_treated_as_utc():
    parsed = _parse_timestamp("2026-03-01T10:30:00")
    assert parsed == datetime(2026, 3, 1, 10, 30, tzinfo=timezone.utc)


def test_zulu_and_offset_timestamps_normalise_to_utc():
    assert _parse_timestamp("2026-03-01T10:30:00Z") == _parse_timestamp(
        "2026-03-01T16:00:00+05:30"
    )


def test_epoch_timestamps_parse():
    assert _parse_timestamp(0) == datetime(1970, 1, 1, tzinfo=timezone.utc)


def test_unparseable_timestamp_returns_none():
    assert _parse_timestamp("not a date") is None
    assert _parse_timestamp(None) is None


# -- post normalisation ----------------------------------------------------- #
def test_post_without_shortcode_is_dropped():
    assert normalise_post({"timestamp": "2026-03-01T10:00:00Z"}) is None


def test_post_without_timestamp_is_dropped():
    assert normalise_post({"shortCode": "abc"}) is None


def test_post_records_missing_fields_rather_than_failing():
    post = normalise_post(
        {"shortCode": "abc123", "timestamp": "2026-03-01T10:00:00Z"}
    )
    assert post is not None
    assert post.shortcode == "abc123"
    assert "caption" in post.missing_fields
    assert "ownerUsername" in post.missing_fields
    assert post.url.endswith("/p/abc123/")


def test_owner_nested_username_is_found():
    post = normalise_post(
        {
            "shortCode": "abc",
            "timestamp": "2026-03-01T10:00:00Z",
            "owner": {"username": "someone"},
        }
    )
    assert post is not None
    assert post.author_handle == "someone"


# -- actor inputs ----------------------------------------------------------- #
def test_hashtag_input_uses_explore_tags_url_not_search():
    """Regression: `search` + searchType=hashtag is hashtag DISCOVERY.

    It returns one entity record (name, postsCount, related tags) and no posts
    at all, so a run reports "returned 1 items" and stores nothing. Post
    collection has to go through the explore/tags URL.
    """
    built = build_hashtag_input("msp", results_limit=50, lookback="7 days")
    assert built["directUrls"] == [
        "https://www.instagram.com/explore/tags/msp/"
    ]
    assert built["resultsType"] == "posts"
    assert "search" not in built
    assert "searchType" not in built


def test_hash_prefix_is_stripped_and_case_normalised():
    """Instagram tag URLs take the bare token: /explore/tags/msp/, not /%23MSP/."""
    for variant in ("#MSP", "MSP", "  #msp  "):
        built = build_hashtag_input(variant, results_limit=10, lookback="1 day")
        assert built["directUrls"] == [
            "https://www.instagram.com/explore/tags/msp/"
        ]


def test_results_limit_and_lookback_are_passed_through():
    built = build_hashtag_input("x", results_limit=1000, lookback="3 days")
    assert built["resultsLimit"] == 1000
    assert built["onlyPostsNewerThan"] == "3 days"


def test_multi_word_hashtag_rejected():
    """A space means it is a caption phrase, not a tag — fail loudly rather
    than building a URL that returns nothing."""
    import pytest

    with pytest.raises(ValueError, match="space"):
        build_hashtag_input("minimum support price", results_limit=10,
                            lookback="1 day")


def test_empty_hashtag_rejected():
    import pytest

    with pytest.raises(ValueError):
        build_hashtag_input("#", results_limit=10, lookback="1 day")


def test_sentiment_input_collapses_newlines_to_keep_batches_aligned():
    built = build_sentiment_input(["line one\nstill one", "two"], "text")
    # Two inputs must produce exactly two lines, or results desynchronise.
    assert built["text"].count("\n") == 1
    assert built["text"] == "line one still one\ntwo"


# -- theme extraction ------------------------------------------------------- #
def test_tokenise_strips_mentions_urls_and_stopwords():
    tokens = _tokenise(
        "The policy is bad @someactivist https://x.com/y reform needed",
        {"the", "is"},
    )
    assert "someactivist" not in tokens
    assert "https" not in tokens
    assert "policy" in tokens and "reform" in tokens


def test_mentions_are_stripped_so_accounts_cannot_become_themes():
    tokens = _tokenise("@bigaccount @bigaccount @bigaccount issue", set())
    assert "bigaccount" not in tokens


def test_ngrams_produce_expected_shapes():
    grams = _ngrams(["a", "b", "c"], 1, 2)
    assert grams == ["a", "b", "c", "a b", "b c"]
