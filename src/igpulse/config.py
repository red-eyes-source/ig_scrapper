"""Typed configuration loader.

Single source of truth: every runtime constant is declared in config/*.yaml and
reaches the rest of the codebase only through the models below. Validation runs
at import/boot time so a malformed config fails before any Apify credits are
spent, not halfway through a run.
"""

from __future__ import annotations

import hashlib
import json
import os
import zoneinfo
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

FigureCategory = Literal[
    "elected_official", "party_official", "registered_media", "own_side"
]
Lens = Literal["narrative", "public_figure", "own_side"]

_REPO_ROOT = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------- #
# settings.yaml
# --------------------------------------------------------------------------- #
class ProjectCfg(BaseModel):
    name: str
    client_label: str
    timezone: str

    @field_validator("timezone")
    @classmethod
    def _tz_must_resolve(cls, v: str) -> str:
        # Fail loudly here rather than producing naive timestamps downstream.
        zoneinfo.ZoneInfo(v)
        return v


class RateLimitCfg(BaseModel):
    requests_per_second: float = Field(gt=0)
    burst: int = Field(gt=0)


class TimeoutsCfg(BaseModel):
    connect_seconds: float = Field(gt=0)
    read_seconds: float = Field(gt=0)


class RetryCfg(BaseModel):
    max_attempts: int = Field(ge=1)
    initial_backoff_seconds: float = Field(gt=0)
    max_backoff_seconds: float = Field(gt=0)
    retry_on_status: list[int]

    @model_validator(mode="after")
    def _backoff_ordered(self) -> "RetryCfg":
        if self.max_backoff_seconds < self.initial_backoff_seconds:
            raise ValueError("max_backoff_seconds < initial_backoff_seconds")
        return self


class RunPollingCfg(BaseModel):
    interval_seconds: float = Field(gt=0)
    max_wait_seconds: float = Field(gt=0)


class DatasetCfg(BaseModel):
    page_size: int = Field(gt=0, le=10_000)


class ActorsCfg(BaseModel):
    instagram_scraper: str
    instagram_comment_scraper: str


class ApifyCfg(BaseModel):
    base_url: str
    actors: ActorsCfg
    rate_limit: RateLimitCfg
    timeouts: TimeoutsCfg
    retry: RetryCfg
    run_polling: RunPollingCfg
    dataset: DatasetCfg


class LabelNormalisationCfg(BaseModel):
    positive: list[str]
    negative: list[str]
    neutral: list[str]

    def resolve(self, raw: str) -> str | None:
        """Map a provider's raw label onto our canonical vocabulary."""
        needle = raw.strip()
        for canonical in ("positive", "negative", "neutral"):
            if needle in getattr(self, canonical):
                return canonical
        # Case-insensitive second pass before giving up.
        lowered = needle.lower()
        for canonical in ("positive", "negative", "neutral"):
            if lowered in {v.lower() for v in getattr(self, canonical)}:
                return canonical
        return None


class SentimentFieldMap(BaseModel):
    label: str
    score: str


class SentimentCfg(BaseModel):
    provider: str
    actor_id: str
    input_field: str
    field_map: SentimentFieldMap
    label_normalisation: LabelNormalisationCfg
    batch_size: int = Field(gt=0, le=1000)
    min_chars: int = Field(ge=0)
    min_confidence: float = Field(ge=0.0, le=1.0)


class NarrativeIngestCfg(BaseModel):
    results_per_term: int = Field(gt=0)
    comments_per_post: int = Field(ge=0)
    lookback: str
    include_nested_comments: bool


class FigureIngestCfg(BaseModel):
    posts_per_handle: int = Field(gt=0)
    comments_per_post: int = Field(ge=0)
    lookback: str


class IngestCfg(BaseModel):
    narrative: NarrativeIngestCfg
    public_figure: FigureIngestCfg
    own_side: FigureIngestCfg


class PrivacyCfg(BaseModel):
    narrative_retention_days: int = Field(gt=0)
    store_narrative_text: bool
    max_public_figures: int = Field(gt=0)


class ThemesCfg(BaseModel):
    min_term_frequency: int = Field(gt=0)
    max_themes_per_report: int = Field(gt=0)
    ngram_range: tuple[int, int]
    stopword_extra: list[str]

    @field_validator("ngram_range")
    @classmethod
    def _ordered(cls, v: tuple[int, int]) -> tuple[int, int]:
        lo, hi = v
        if lo < 1 or hi < lo:
            raise ValueError("ngram_range must be (lo>=1, hi>=lo)")
        return v


class MetricsCfg(BaseModel):
    trend_window_days: int = Field(gt=0)
    comparison_window_days: int = Field(gt=0)

    @model_validator(mode="after")
    def _windows_ordered(self) -> "MetricsCfg":
        if self.comparison_window_days <= self.trend_window_days:
            raise ValueError(
                "comparison_window_days must exceed trend_window_days, "
                "otherwise trend deltas compare a window against itself"
            )
        return self


class AnalysisCfg(BaseModel):
    themes: ThemesCfg
    metrics: MetricsCfg


class DocxCfg(BaseModel):
    template: str | None
    include_sections: list[str]


class HtmlCfg(BaseModel):
    self_contained: bool
    include_sections: list[str]


class ReportCfg(BaseModel):
    output_dir: str
    docx: DocxCfg
    html: HtmlCfg


class DatabaseCfg(BaseModel):
    pool_min_size: int = Field(ge=1)
    pool_max_size: int = Field(ge=1)
    statement_timeout_ms: int = Field(gt=0)

    @model_validator(mode="after")
    def _pool_ordered(self) -> "DatabaseCfg":
        if self.pool_max_size < self.pool_min_size:
            raise ValueError("pool_max_size < pool_min_size")
        return self


class LoggingCfg(BaseModel):
    level: str
    format: str


class Settings(BaseModel):
    project: ProjectCfg
    apify: ApifyCfg
    sentiment: SentimentCfg
    ingest: IngestCfg
    privacy: PrivacyCfg
    analysis: AnalysisCfg
    report: ReportCfg
    database: DatabaseCfg
    logging: LoggingCfg


# --------------------------------------------------------------------------- #
# public_figures.yaml
# --------------------------------------------------------------------------- #
class PublicFigure(BaseModel):
    handle: str
    display_name: str
    category: FigureCategory
    jurisdiction: str | None = None
    justification: str

    @field_validator("handle")
    @classmethod
    def _normalise_handle(cls, v: str) -> str:
        h = v.strip().lstrip("@").lower()
        if not h:
            raise ValueError("handle must not be empty")
        if "/" in h or " " in h:
            raise ValueError(f"handle looks like a URL or contains spaces: {v!r}")
        return h

    @field_validator("justification")
    @classmethod
    def _substantive_justification(cls, v: str) -> str:
        text = " ".join(v.split())
        # Mirrors the CHECK constraint in sql/schema.sql. A one-word
        # justification is the failure mode that turns an allowlist into a
        # watchlist, so it is rejected at both layers.
        if len(text) < 20:
            raise ValueError(
                "justification must be at least 20 characters and explain why "
                "this account qualifies as a public figure"
            )
        return text


class PublicFigureList(BaseModel):
    figures: list[PublicFigure]

    @model_validator(mode="after")
    def _unique_handles(self) -> "PublicFigureList":
        seen: set[str] = set()
        for fig in self.figures:
            if fig.handle in seen:
                raise ValueError(f"duplicate handle in allowlist: {fig.handle}")
            seen.add(fig.handle)
        return self

    def by_category(self, category: FigureCategory) -> list[PublicFigure]:
        return [f for f in self.figures if f.category == category]

    @property
    def handles(self) -> frozenset[str]:
        return frozenset(f.handle for f in self.figures)


# --------------------------------------------------------------------------- #
# narratives.yaml
# --------------------------------------------------------------------------- #
class Narrative(BaseModel):
    key: str
    label: str
    hashtags: list[str] = Field(default_factory=list)
    terms: list[str] = Field(default_factory=list)

    @field_validator("key")
    @classmethod
    def _slug(cls, v: str) -> str:
        k = v.strip().lower()
        if not k or not all(c.isalnum() or c == "_" for c in k):
            raise ValueError(f"narrative key must be alphanumeric/underscore: {v!r}")
        return k

    @field_validator("hashtags")
    @classmethod
    def _hash_prefixed(cls, v: list[str]) -> list[str]:
        out = []
        for tag in v:
            t = tag.strip()
            if not t:
                continue
            out.append(t if t.startswith("#") else f"#{t}")
        return out

    @model_validator(mode="after")
    def _has_targets(self) -> "Narrative":
        if not self.hashtags and not self.terms:
            raise ValueError(
                f"narrative {self.key!r} has neither hashtags nor terms"
            )
        return self

    @property
    def search_terms(self) -> list[str]:
        """Terms passed to the Apify scraper, hashtags first."""
        return [*self.hashtags, *self.terms]


class NarrativeList(BaseModel):
    narratives: list[Narrative]

    @model_validator(mode="after")
    def _unique_keys(self) -> "NarrativeList":
        seen: set[str] = set()
        for n in self.narratives:
            if n.key in seen:
                raise ValueError(f"duplicate narrative key: {n.key}")
            seen.add(n.key)
        return self


# --------------------------------------------------------------------------- #
# Aggregate
# --------------------------------------------------------------------------- #
class AppConfig(BaseModel):
    settings: Settings
    public_figures: PublicFigureList
    narratives: NarrativeList

    @model_validator(mode="after")
    def _enforce_allowlist_cap(self) -> "AppConfig":
        cap = self.settings.privacy.max_public_figures
        count = len(self.public_figures.figures)
        if count > cap:
            raise ValueError(
                f"public_figures.yaml holds {count} entries, exceeding the "
                f"max_public_figures cap of {cap}. This cap is a deliberate "
                f"tripwire: an allowlist this large is no longer a list of "
                f"public figures. Review the entries rather than raising it."
            )
        return self

    @property
    def timezone(self) -> zoneinfo.ZoneInfo:
        return zoneinfo.ZoneInfo(self.settings.project.timezone)

    def fingerprint(self) -> str:
        """Stable hash of the config, recorded against each run.

        Lets you tell whether two runs are comparable without diffing YAML.
        """
        payload = json.dumps(self.model_dump(mode="json"), sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"config file not found: {path}")
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a YAML mapping at the top level")
    return data


def load_config(config_dir: Path | str | None = None) -> AppConfig:
    """Load and validate all three config files.

    config_dir resolution order: explicit argument, IGPULSE_CONFIG_DIR env var,
    then <repo>/config.
    """
    if config_dir is None:
        env_dir = os.environ.get("IGPULSE_CONFIG_DIR")
        config_dir = Path(env_dir) if env_dir else _REPO_ROOT / "config"
    config_dir = Path(config_dir)

    return AppConfig(
        settings=Settings.model_validate(_read_yaml(config_dir / "settings.yaml")),
        public_figures=PublicFigureList.model_validate(
            _read_yaml(config_dir / "public_figures.yaml")
        ),
        narratives=NarrativeList.model_validate(
            _read_yaml(config_dir / "narratives.yaml")
        ),
    )


@lru_cache(maxsize=1)
def get_config() -> AppConfig:
    """Process-wide cached config. Call load_config() directly in tests."""
    return load_config()
