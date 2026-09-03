"""Safe write-back for the YAML config files.

The dashboard edits live config, so a bad save is a broken pipeline. Three
properties make that survivable:

1. **Validate before writing.** The candidate document is parsed through the
   same Pydantic models the pipeline uses. A rejected edit never touches disk,
   and the caller gets the validation error to show the user.
2. **Atomic replace.** Written to a temp file in the same directory and moved
   into place, so a crash mid-write cannot leave a truncated config.
3. **Timestamped backup.** The previous version is kept, because a config that
   validates can still be wrong, and "undo" should not mean "retype it".

Comments in the YAML are not preserved — round-tripping them needs ruamel, and
a dependency to keep comments on a file the dashboard now owns is a poor trade.
The generated files carry a header saying where the documentation lives.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from igpulse.config import (
    AppConfig,
    NarrativeList,
    PublicFigureList,
    Settings,
    load_config,
)

logger = logging.getLogger(__name__)

BACKUP_DIRNAME = ".backups"
MAX_BACKUPS = 20

_HEADERS = {
    "settings.yaml": (
        "# ig-pulse settings. Written by the dashboard (run.py dashboard).\n"
        "# Field documentation lives in README.md; inline comments are not\n"
        "# preserved across dashboard saves.\n"
    ),
    "narratives.yaml": (
        "# Narrative targets. Written by the dashboard (run.py dashboard).\n"
        "#\n"
        "# hashtags COLLECT posts (one Apify run each). terms FILTER captions\n"
        "# in what the hashtags returned — Instagram has no caption search, so\n"
        "# a narrative with no hashtags collects nothing and is rejected.\n"
    ),
    "public_figures.yaml": (
        "# Public-figure allowlist. Written by the dashboard.\n"
        "#\n"
        "# The only source of persistently-named accounts. Every entry needs a\n"
        "# justification of at least 20 characters explaining why the account\n"
        "# qualifies as a public figure.\n"
    ),
}


class ConfigWriteError(RuntimeError):
    """The proposed config did not validate; nothing was written."""


def _backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    backup_dir = path.parent / BACKUP_DIRNAME
    backup_dir.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = backup_dir / f"{path.stem}.{stamp}{path.suffix}"
    shutil.copy2(path, target)

    # Keep the directory from growing without bound.
    existing = sorted(backup_dir.glob(f"{path.stem}.*{path.suffix}"))
    for stale in existing[:-MAX_BACKUPS]:
        stale.unlink(missing_ok=True)
    return target


def _atomic_write(path: Path, text: str) -> None:
    """Write via a temp file in the same directory, then rename.

    Same filesystem, so the rename is atomic: readers see either the old file
    or the new one, never a half-written one.
    """
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def _dump(name: str, payload: dict[str, Any]) -> str:
    header = _HEADERS.get(name, "")
    return header + yaml.dump(
        payload, sort_keys=False, allow_unicode=True, width=88
    )


def save_settings(config_dir: Path, payload: dict[str, Any]) -> Path:
    """Validate and persist settings.yaml."""
    try:
        Settings.model_validate(payload)
    except ValidationError as exc:
        raise ConfigWriteError(str(exc)) from None
    return _write(config_dir / "settings.yaml", payload)


def save_narratives(config_dir: Path, narratives: list[dict[str, Any]]) -> Path:
    payload = {"narratives": narratives}
    try:
        NarrativeList.model_validate(payload)
    except ValidationError as exc:
        raise ConfigWriteError(str(exc)) from None
    return _write(config_dir / "narratives.yaml", payload)


def save_public_figures(
    config_dir: Path, figures: list[dict[str, Any]], *, settings: Settings
) -> Path:
    payload = {"figures": figures}
    try:
        allowlist = PublicFigureList.model_validate(payload)
    except ValidationError as exc:
        raise ConfigWriteError(str(exc)) from None

    # The allowlist cap lives on AppConfig, not PublicFigureList, so check it
    # explicitly rather than letting an over-cap file save and fail at run time.
    cap = settings.privacy.max_public_figures
    if len(allowlist.figures) > cap:
        raise ConfigWriteError(
            f"{len(allowlist.figures)} entries exceeds max_public_figures "
            f"({cap}). That cap is a deliberate tripwire — review the entries "
            f"rather than raising it."
        )
    return _write(config_dir / "public_figures.yaml", payload)


def _write(path: Path, payload: dict[str, Any]) -> Path:
    backup = _backup(path)
    _atomic_write(path, _dump(path.name, payload))
    logger.info(
        "wrote %s%s", path, f" (backup: {backup.name})" if backup else ""
    )
    return path


def reload_config(config_dir: Path) -> AppConfig:
    """Re-read all three files after a save, so the caller sees what landed."""
    return load_config(config_dir)
