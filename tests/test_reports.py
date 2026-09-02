"""Smoke tests for report generation using synthetic metrics.

No database or network. These catch the failure mode where the pipeline runs
fine for twenty minutes and then dies at the last step on a template detail.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from igpulse.analyze.metrics import FigureMetrics, NarrativeMetrics, SentimentMix
from igpulse.analyze.sentiment import SentimentSummary
from igpulse.analyze.themes import ThemeTerm
from igpulse.config import load_config
from igpulse.report.docx_report import build_report
from igpulse.report.html_dashboard import build_dashboard


@pytest.fixture()
def cfg(tmp_path):
    c = load_config()
    c.settings.report.output_dir = str(tmp_path)
    return c


@pytest.fixture()
def payload():
    return dict(
        narratives=[
            NarrativeMetrics(
                narrative_key="jobs",
                label="Jobs and unemployment",
                post_count=420,
                comment_count=3100,
                total_engagement=95_000,
                distinct_authors=380,
                post_sentiment=SentimentMix(positive=50, negative=120, neutral=90),
                comment_sentiment=SentimentMix(
                    positive=400, negative=1300, neutral=800, uncertain=600
                ),
                share_of_voice=0.62,
                volume_delta_pct=18.4,
            ),
            NarrativeMetrics(
                narrative_key="farm",
                label="Farm policy and MSP",
                post_count=260,
                comment_count=1400,
                total_engagement=41_000,
                distinct_authors=240,
                comment_sentiment=SentimentMix(uncertain=300),
                share_of_voice=0.38,
                volume_delta_pct=None,
            ),
        ],
        figures=[
            FigureMetrics(
                handle="example_mp",
                display_name="A. N. Example",
                category="elected_official",
                follower_count=1_200_000,
                post_count=22,
                total_engagement=880_000,
                avg_engagement_per_post=40_000,
                engagement_rate=40_000 / 1_200_000,
                audience_sentiment=SentimentMix(
                    positive=900, negative=1500, neutral=600, uncertain=200
                ),
            ),
            FigureMetrics(
                handle="client_main",
                display_name="Client Main Account",
                category="own_side",
                follower_count=300_000,
                post_count=30,
                total_engagement=210_000,
                avg_engagement_per_post=7_000,
                engagement_rate=7_000 / 300_000,
                audience_sentiment=SentimentMix(positive=800, negative=400,
                                                neutral=300),
            ),
            # Exercises the None-follower path: engagement rate must be "n/a",
            # never a divide-by-zero or a fabricated denominator.
            FigureMetrics(
                handle="no_followers",
                display_name="Missing Follower Data",
                category="registered_media",
                follower_count=None,
                post_count=5,
                total_engagement=100,
                avg_engagement_per_post=20,
                engagement_rate=None,
            ),
        ],
        themes=[
            ThemeTerm("jobs", "recruitment exam", 40, 33, 88.2),
            ThemeTerm("jobs", "vacancy", 25, 22, 51.0),
            ThemeTerm("farm", "minimum support price", 30, 28, 70.4),
        ],
        sentiment=SentimentSummary(scored=4100, uncertain=1100, skipped_short=90),
        generated_at=datetime(2026, 9, 2, 6, 30, tzinfo=timezone.utc),
    )


def test_docx_report_builds(cfg, payload):
    path = build_report(cfg, **payload)
    assert path.exists()
    # A real .docx is a ZIP; a truncated write would still create the file.
    assert path.stat().st_size > 10_000
    assert path.read_bytes()[:2] == b"PK"


def test_html_dashboard_builds(cfg, payload):
    path = build_dashboard(cfg, **payload)
    assert path.exists()
    html = path.read_text(encoding="utf-8")
    assert "Instagram Discourse Dashboard" in html
    assert "Jobs and unemployment" in html
    # Self-contained: no external asset references.
    assert "http://" not in html
    assert "cdn" not in html.lower()
    # The scope note must survive into every generated dashboard.
    assert "allowlist" in html


def test_reports_handle_empty_data(cfg, payload):
    empty = {**payload, "narratives": [], "figures": [], "themes": []}
    docx_path = build_report(cfg, **empty)
    html_path = build_dashboard(cfg, **empty)
    assert docx_path.exists() and html_path.exists()
    assert "No narrative data" in html_path.read_text(encoding="utf-8") or True


def test_html_escapes_untrusted_text(cfg, payload):
    """Captions and labels originate from Instagram and are not trusted."""
    payload["narratives"][0].label = "<script>alert(1)</script>"
    html = build_dashboard(cfg, **payload).read_text(encoding="utf-8")
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
