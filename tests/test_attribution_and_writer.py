"""Attribution bounds and safe config write-back.

Attribution exists so a quoted post can be checked. The tests here pin the
things that keep it a citation mechanism rather than a named corpus: the
per-narrative cap, posts-only, and names expiring before the data they annotate.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from igpulse.config import PrivacyCfg, load_config
from igpulse.config_writer import (
    ConfigWriteError,
    save_narratives,
    save_public_figures,
    save_settings,
)

JUSTIFICATION = "Sitting Member of Parliament; official constituency account."


# --------------------------------------------------------------------------- #
# Attribution bounds
# --------------------------------------------------------------------------- #
def _privacy(**overrides):
    base = dict(
        narrative_retention_days=90,
        store_narrative_text=True,
        max_public_figures=250,
        attribution_top_n_per_narrative=20,
        attribution_retention_days=30,
    )
    base.update(overrides)
    return PrivacyCfg(**base)


def test_attribution_cap_is_bounded_at_fifty():
    """Past ~50 per narrative it stops being citation and becomes a corpus."""
    _privacy(attribution_top_n_per_narrative=50)
    with pytest.raises(ValidationError):
        _privacy(attribution_top_n_per_narrative=51)


def test_attribution_can_be_switched_off_entirely():
    assert _privacy(attribution_top_n_per_narrative=0).\
        attribution_top_n_per_narrative == 0


def test_names_must_not_outlive_the_data_they_annotate():
    with pytest.raises(ValidationError, match="should not outlive"):
        _privacy(attribution_retention_days=120, narrative_retention_days=90)


def test_equal_retentions_allowed():
    assert _privacy(
        attribution_retention_days=90, narrative_retention_days=90
    ).attribution_retention_days == 90


def test_shipped_config_attributes_a_citation_sized_set():
    cfg = load_config()
    p = cfg.settings.privacy
    assert 0 < p.attribution_top_n_per_narrative <= 25
    assert p.attribution_retention_days < p.narrative_retention_days


def test_schema_has_no_handle_index_or_comment_attribution():
    """Guard the two changes that would turn the table into a profile store."""
    sql = (Path(__file__).resolve().parents[1] / "sql" / "schema.sql").read_text()
    assert "CREATE TABLE IF NOT EXISTS post_attribution" in sql
    # No index on the handle itself: lookups by handle are what a watchlist does.
    assert "post_attribution (author_handle)" not in sql
    # And no attribution table for comments at all.
    assert "comment_attribution" not in sql


# --------------------------------------------------------------------------- #
# Config write-back
# --------------------------------------------------------------------------- #
@pytest.fixture()
def config_dir(tmp_path):
    src = Path(__file__).resolve().parents[1] / "config"
    for name in ("settings.yaml", "narratives.yaml", "public_figures.yaml"):
        (tmp_path / name).write_text(
            (src / name).read_text(encoding="utf-8"), encoding="utf-8"
        )
    return tmp_path


def test_valid_narratives_are_written_and_reloadable(config_dir):
    save_narratives(
        config_dir,
        [{"key": "jobs", "label": "Jobs", "hashtags": ["berozgari"],
          "terms": ["paper leak"]}],
    )
    cfg = load_config(config_dir)
    assert cfg.narratives.narratives[0].hashtags == ["berozgari"]


def test_invalid_narrative_is_rejected_and_file_untouched(config_dir):
    before = (config_dir / "narratives.yaml").read_text()
    with pytest.raises(ConfigWriteError):
        save_narratives(
            config_dir,
            [{"key": "jobs", "label": "Jobs", "hashtags": [], "terms": ["x"]}],
        )
    assert (config_dir / "narratives.yaml").read_text() == before


def test_thin_justification_rejected_before_writing(config_dir):
    before = (config_dir / "public_figures.yaml").read_text()
    settings = load_config(config_dir).settings
    with pytest.raises(ConfigWriteError):
        save_public_figures(
            config_dir,
            [{"handle": "x", "display_name": "X",
              "category": "elected_official", "justification": "MP"}],
            settings=settings,
        )
    assert (config_dir / "public_figures.yaml").read_text() == before


def test_allowlist_cap_enforced_on_save(config_dir):
    settings = load_config(config_dir).settings
    settings.privacy.max_public_figures = 2
    figures = [
        {"handle": f"h{i}", "display_name": f"H{i}",
         "category": "elected_official", "justification": JUSTIFICATION}
        for i in range(3)
    ]
    with pytest.raises(ConfigWriteError, match="tripwire"):
        save_public_figures(config_dir, figures, settings=settings)


def test_backup_is_written_before_overwrite(config_dir):
    original = (config_dir / "narratives.yaml").read_text()
    save_narratives(
        config_dir,
        [{"key": "a", "label": "A", "hashtags": ["x"], "terms": []}],
    )
    backups = list((config_dir / ".backups").glob("narratives.*.yaml"))
    assert len(backups) == 1
    assert backups[0].read_text() == original


def test_settings_roundtrip_preserves_every_key(config_dir):
    """A save must not silently drop config the UI does not surface."""
    before = load_config(config_dir).settings
    payload = before.model_dump(mode="json", by_alias=True)
    payload["ingest"]["narrative"]["results_per_term"] = 42
    save_settings(config_dir, payload)

    after = load_config(config_dir).settings
    assert after.ingest.narrative.results_per_term == 42
    # Everything else identical.
    a = after.model_dump(mode="json", by_alias=True)
    b = before.model_dump(mode="json", by_alias=True)
    b["ingest"]["narrative"]["results_per_term"] = 42
    assert a == b


def test_invalid_settings_rejected_without_writing(config_dir):
    before = (config_dir / "settings.yaml").read_text()
    payload = load_config(config_dir).settings.model_dump(
        mode="json", by_alias=True
    )
    # comparison window must exceed trend window
    payload["analysis"]["metrics"]["comparison_window_days"] = 1
    with pytest.raises(ConfigWriteError):
        save_settings(config_dir, payload)
    assert (config_dir / "settings.yaml").read_text() == before


def test_written_yaml_is_plain_and_parseable(config_dir):
    save_narratives(
        config_dir,
        [{"key": "a", "label": "A", "hashtags": ["x"], "terms": []}],
    )
    raw = (config_dir / "narratives.yaml").read_text()
    assert raw.startswith("#")            # header comment retained
    parsed = yaml.safe_load(raw)
    assert parsed["narratives"][0]["key"] == "a"


# --------------------------------------------------------------------------- #
# Dashboard API surface
# --------------------------------------------------------------------------- #
def test_snapshot_is_json_serialisable_and_carries_a_plan(config_dir):
    from igpulse.dashboard.server import _snapshot

    snap = _snapshot(load_config(config_dir))
    text = json.dumps(snap)          # must not raise
    assert "fingerprint" in snap
    assert "cost" in snap["plan"]
    assert len(text) > 100


def test_estimate_reflects_unsaved_edits(config_dir):
    from igpulse.dashboard.server import _estimate_from_payload

    cfg = load_config(config_dir)
    payload = cfg.settings.model_dump(mode="json", by_alias=True)
    narratives = [
        {"key": "a", "label": "A", "hashtags": ["one", "two"], "terms": []}
    ]

    low = _estimate_from_payload(cfg, payload, narratives)
    payload["ingest"]["narrative"]["results_per_term"] *= 4
    high = _estimate_from_payload(cfg, payload, narratives)

    assert high["total_posts"] == low["total_posts"] * 4
    assert high["cost"]["total"] > low["cost"]["total"]


def test_dashboard_binds_loopback_only():
    """No auth, and the editable surface is what gets collected and named.

    Checked against the parsed AST rather than the raw text: the module
    docstring discusses 0.0.0.0 precisely to explain why it is not used, and a
    substring search over source cannot tell prose from code.
    """
    import ast

    source_path = (
        Path(__file__).resolve().parents[1]
        / "src" / "igpulse" / "dashboard" / "server.py"
    )
    tree = ast.parse(source_path.read_text())

    docstrings = {
        node.body[0].value
        for node in ast.walk(tree)
        if isinstance(
            node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        )
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    literals = [
        n.value for n in ast.walk(tree)
        if isinstance(n, ast.Constant)
        and isinstance(n.value, str)
        and n not in docstrings
    ]
    assert "127.0.0.1" in literals, "server must bind loopback explicitly"
    assert "0.0.0.0" not in literals, "bind address must not be reachable off-host"
