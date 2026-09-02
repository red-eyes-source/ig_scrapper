"""Planner tests — the estimator is the thing standing between a config typo
and a four-figure Apify bill, so its arithmetic is worth pinning down."""

from __future__ import annotations

from igpulse.analyze.planner import plan_cycle
from igpulse.config import AppConfig, Narrative, NarrativeList, PublicFigure, \
    PublicFigureList, load_config

JUSTIFICATION = "Client's own primary account, tracked as the own-side baseline."


def _cfg(*, terms_per_narrative: int = 2, narratives: int = 3,
         figures: int = 0) -> AppConfig:
    base = load_config()
    return AppConfig(
        settings=base.settings,
        public_figures=PublicFigureList(
            figures=[
                PublicFigure(
                    handle=f"handle{i}",
                    display_name=f"Figure {i}",
                    category="elected_official",
                    justification=JUSTIFICATION,
                )
                for i in range(figures)
            ]
        ),
        narratives=NarrativeList(
            narratives=[
                Narrative(
                    key=f"n{i}",
                    label=f"Narrative {i}",
                    hashtags=[f"#t{i}x{j}" for j in range(terms_per_narrative)],
                )
                for i in range(narratives)
            ]
        ),
    )


def test_narrative_volume_is_terms_times_results_per_term():
    cfg = _cfg(terms_per_narrative=2, narratives=3)
    plan = plan_cycle(cfg)
    narrative = next(l for l in plan.lenses if l.lens == "narrative")
    per_term = cfg.settings.ingest.narrative.results_per_term
    assert narrative.max_posts == 6 * per_term


def test_comments_multiply_on_top_of_posts():
    cfg = _cfg(terms_per_narrative=1, narratives=1)
    plan = plan_cycle(cfg)
    narrative = next(l for l in plan.lenses if l.lens == "narrative")
    ncfg = cfg.settings.ingest.narrative
    assert narrative.max_comments == (
        ncfg.results_per_term * ncfg.comments_per_post
    )


def test_one_scrape_run_and_one_comment_run_per_term():
    cfg = _cfg(terms_per_narrative=2, narratives=2)   # 4 terms
    plan = plan_cycle(cfg)
    narrative = next(l for l in plan.lenses if l.lens == "narrative")
    assert narrative.actor_runs == 8


def test_comment_run_is_skipped_when_comments_disabled():
    cfg = _cfg(terms_per_narrative=1, narratives=1)
    cfg.settings.ingest.narrative.comments_per_post = 0
    plan = plan_cycle(cfg)
    narrative = next(l for l in plan.lenses if l.lens == "narrative")
    assert narrative.actor_runs == 1
    assert narrative.max_comments == 0


def test_empty_allowlist_produces_no_figure_runs():
    plan = plan_cycle(_cfg(figures=0))
    for lens in ("public_figure", "own_side"):
        entry = next(l for l in plan.lenses if l.lens == lens)
        assert entry.actor_runs == 0
        assert entry.max_posts == 0


def test_figures_batch_into_a_single_scrape_run():
    """All allowlisted handles go into one directUrls call, not one each."""
    plan = plan_cycle(_cfg(figures=12))
    entry = next(l for l in plan.lenses if l.lens == "public_figure")
    assert entry.actor_runs == 2          # one profile run + one comment run
    assert entry.max_posts == 12 * 50     # posts_per_handle default


def test_cost_splits_by_driver_and_sums():
    cfg = _cfg(terms_per_narrative=1, narratives=1)
    plan = plan_cycle(cfg)
    cost = plan.cost(cfg)
    assert cost["total"] == (
        cost["scraping"] + cost["comments"] + cost["sentiment"]
    )
    # Sentiment is charged on posts AND comments, so it should exceed the
    # post-scraping line whenever comments are being collected.
    assert cost["sentiment"] > cost["scraping"]


def test_large_cycle_raises_a_volume_warning():
    plan = plan_cycle(_cfg(terms_per_narrative=5, narratives=10))
    assert any("large first run" in w for w in plan.warnings)


def test_empty_config_warns_about_both_lenses():
    plan = plan_cycle(_cfg(narratives=0, figures=0))
    joined = " ".join(plan.warnings)
    assert "No narratives configured" in joined
    assert "Allowlist is empty" in joined


def test_smoke_config_is_cheap_enough_to_be_a_smoke_test():
    """If someone raises the smoke limits, this test should stop them."""
    cfg = load_config("config/smoke")
    plan = plan_cycle(cfg)
    assert plan.total_items <= 200
    assert plan.cost(cfg)["total"] < 1.00
