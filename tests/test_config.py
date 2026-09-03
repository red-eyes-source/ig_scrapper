"""Config validation tests — the guardrails that fail a run before it costs money."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from igpulse.config import (
    AppConfig,
    Narrative,
    NarrativeList,
    PublicFigure,
    PublicFigureList,
    load_config,
)

GOOD_JUSTIFICATION = "Sitting Member of Parliament; official constituency account."


def _figure(handle: str = "a_handle", **overrides) -> PublicFigure:
    base = dict(
        handle=handle,
        display_name="Example",
        category="elected_official",
        justification=GOOD_JUSTIFICATION,
    )
    base.update(overrides)
    return PublicFigure(**base)


def test_repo_config_loads_and_validates():
    cfg = load_config()
    assert isinstance(cfg, AppConfig)
    assert cfg.settings.project.name == "ig-pulse"
    # Shipped config is intentionally empty of targets.
    assert cfg.public_figures.figures == []
    assert cfg.narratives.narratives == []


def test_fingerprint_is_stable_and_sensitive():
    cfg = load_config()
    assert cfg.fingerprint() == cfg.fingerprint()
    mutated = cfg.model_copy(deep=True)
    mutated.settings.ingest.narrative.results_per_term += 1
    assert mutated.fingerprint() != cfg.fingerprint()


def test_thin_justification_is_rejected():
    with pytest.raises(ValidationError):
        _figure(justification="MP")


def test_handle_is_normalised():
    assert _figure("@Example_MP").handle == "example_mp"


def test_url_shaped_handle_is_rejected():
    with pytest.raises(ValidationError):
        _figure("instagram.com/example")


def test_duplicate_handles_rejected():
    with pytest.raises(ValidationError):
        PublicFigureList(figures=[_figure("dup"), _figure("@DUP")])


def test_allowlist_cap_is_enforced():
    cfg = load_config()
    cfg.settings.privacy.max_public_figures = 2
    with pytest.raises(ValidationError) as exc:
        AppConfig(
            settings=cfg.settings,
            public_figures=PublicFigureList(
                figures=[_figure(f"h{i}") for i in range(3)]
            ),
            narratives=cfg.narratives,
        )
    assert "tripwire" in str(exc.value)


def test_narrative_requires_a_hashtag():
    with pytest.raises(ValidationError):
        Narrative(key="empty", label="Empty")


def test_hashtags_normalise_to_bare_lowercase_tokens():
    """Canonical form is what the tag URL takes: /explore/tags/<tag>/."""
    n = Narrative(key="jobs", label="Jobs", hashtags=["Unemployment", "#MSP"])
    assert n.hashtags == ["unemployment", "msp"]


def test_multi_word_hashtag_rejected_at_config_load():
    with pytest.raises(ValidationError, match="single tokens"):
        Narrative(key="jobs", label="Jobs", hashtags=["minimum support price"])


def test_narrative_without_hashtags_rejected():
    """Terms alone cannot collect: Instagram has no caption search, so such a
    narrative would run, cost money and return nothing."""
    with pytest.raises(ValidationError, match="no hashtags"):
        Narrative(key="jobs", label="Jobs", terms=["job creation"])


def test_terms_filter_captions_case_insensitively():
    n = Narrative(key="jobs", label="Jobs", hashtags=["berozgari"],
                  terms=["Paper Leak", "vacancy"])
    assert n.matches_terms("Another paper leak in the news") == ["Paper Leak"]
    assert n.matches_terms("unrelated caption") == []
    assert n.matches_terms(None) == []


def test_hashtag_only_narrative_keeps_everything():
    n = Narrative(key="jobs", label="Jobs", hashtags=["berozgari"])
    assert n.terms == []
    assert n.search_terms == ["berozgari"]


def test_narrative_key_must_be_slug():
    with pytest.raises(ValidationError):
        Narrative(key="bad key!", label="Bad", hashtags=["x"])


def test_duplicate_narrative_keys_rejected():
    with pytest.raises(ValidationError):
        NarrativeList(
            narratives=[
                Narrative(key="a", label="A", hashtags=["x"]),
                Narrative(key="a", label="B", hashtags=["y"]),
            ]
        )


def test_comparison_window_must_exceed_trend_window():
    cfg = load_config()
    data = cfg.settings.model_dump()
    data["analysis"]["metrics"]["comparison_window_days"] = 3
    data["analysis"]["metrics"]["trend_window_days"] = 7
    with pytest.raises(ValidationError):
        type(cfg.settings).model_validate(data)


def test_label_normalisation_handles_case_and_aliases():
    cfg = load_config()
    norm = cfg.settings.sentiment.label_normalisation
    assert norm.resolve("POSITIVE") == "positive"
    assert norm.resolve("neg") == "negative"
    assert norm.resolve("LABEL_1") == "neutral"
    assert norm.resolve("wildly unexpected") is None
