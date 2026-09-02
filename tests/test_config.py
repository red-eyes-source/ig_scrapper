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


def test_narrative_requires_targets():
    with pytest.raises(ValidationError):
        Narrative(key="empty", label="Empty")


def test_hashtags_get_prefixed():
    n = Narrative(key="jobs", label="Jobs", hashtags=["unemployment", "#msp"])
    assert n.hashtags == ["#unemployment", "#msp"]


def test_narrative_key_must_be_slug():
    with pytest.raises(ValidationError):
        Narrative(key="bad key!", label="Bad", terms=["x"])


def test_duplicate_narrative_keys_rejected():
    with pytest.raises(ValidationError):
        NarrativeList(
            narratives=[
                Narrative(key="a", label="A", terms=["x"]),
                Narrative(key="a", label="B", terms=["y"]),
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
