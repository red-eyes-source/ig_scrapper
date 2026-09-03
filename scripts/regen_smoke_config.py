#!/usr/bin/env python3
"""Regenerate config/smoke/settings.yaml from config/settings.yaml.

The smoke config is the production config with the volume knobs turned down.
Keeping it as a hand-maintained copy means it silently drifts out of sync every
time a settings key is added, and the failure is a missing-field ValidationError
at the least useful moment. This derives it instead.

    python scripts/regen_smoke_config.py

Run it after changing the SHAPE of config/settings.yaml. Overrides live in
SMOKE_OVERRIDES below.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
SOURCE = REPO / "config" / "settings.yaml"
TARGET = REPO / "config" / "smoke" / "settings.yaml"

HEADER = """# Smoke-test settings — minimum volume that still exercises every code path.
#
# GENERATED. Do not hand-edit: run scripts/regen_smoke_config.py after changing
# the shape of config/settings.yaml, or this file drifts and fails validation
# with a missing-field error.
#
#   python run.py --config-dir config/smoke plan
#   python run.py --config-dir config/smoke pipeline
#
# A full smoke cycle collects ~5 posts and ~15 comments for a few cents.
#
# min_term_frequency is 1 here because a five-post corpus has no term appearing
# five times; production leaves it at 5, or every typo becomes a "theme".
"""

SMOKE_OVERRIDES: dict[str, dict] = {
    "project": {"client_label": "smoke"},
    "ingest": {
        "narrative": {
            "results_per_term": 5,
            "comments_per_post": 3,
            "lookback": "3 days",
        },
        "public_figure": {"posts_per_handle": 3, "comments_per_post": 3},
        "own_side": {"posts_per_handle": 3, "comments_per_post": 3},
    },
    "analysis": {"themes": {"min_term_frequency": 1}},
    "sentiment": {"batch_size": 20},
    "report": {"output_dir": "out/smoke"},
    "privacy": {
        "max_public_figures": 10,
        "attribution_top_n_per_narrative": 5,
        "attribution_retention_days": 7,
    },
}


def deep_update(base: dict, overrides: dict) -> dict:
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            deep_update(base[key], value)
        else:
            base[key] = value
    return base


def main() -> int:
    settings = yaml.safe_load(SOURCE.read_text(encoding="utf-8"))
    deep_update(settings, SMOKE_OVERRIDES)
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(
        HEADER
        + yaml.dump(settings, sort_keys=False, allow_unicode=True, width=88),
        encoding="utf-8",
    )
    print(f"wrote {TARGET.relative_to(REPO)}")

    # Prove it loads, so a bad override fails here rather than mid-run.
    sys.path.insert(0, str(REPO / "src"))
    from igpulse.config import load_config  # noqa: E402

    cfg = load_config(TARGET.parent)
    print(f"validated: fingerprint {cfg.fingerprint()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
