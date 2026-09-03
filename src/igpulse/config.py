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


class CostCfg(BaseModel):
    """Indicative rates for the `plan` estimator only.

    Nothing in the ingest or analysis path reads these — they exist so a cost
    estimate can be produced without a network call, and so the numbers live in
    config rather than being buried in a print statement.
    """

    instagram_scraper_per_1k: float = Field(ge=0)
    comment_scraper_per_1k: float = Field(ge=0)
    sentiment_per_1k: float = Field(ge=0)


class PrivacyCfg(BaseModel):
    narrative_retention_days: int = Field(gt=0)
    store_narrative_text: bool
    max_public_figures: int = Field(gt=0)
    # Handles are stored for at most this many posts per narrative per run, so
    # quoted posts can be attributed. Capped at 50: past that it stops being
    # citation and starts being a named corpus.
    attribution_top_n_per_narrative: int = Field(ge=0, le=50)
    attribution_retention_days: int = Field(gt=0)

    @model_validator(mode="after")
    def _attribution_expires_first(self) -> "PrivacyCfg":
        if self.attribution_retention_days > self.narrative_retention_days:
            raise ValueError(
                "attribution_retention_days must not exceed "
                "narrative_retention_days — names should not outlive the "
                "aggregate data they annotate"
            )
        return self


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


class JsonCfg(BaseModel):
    include_posts: bool
    include_comments: bool
    include_themes: bool
    max_posts_per_narrative: int = Field(ge=0)
    caption_max_chars: int = Field(ge=0)
    indent: int = Field(ge=0, le=8)


class DocxCfg(BaseModel):
    template: str | None
    include_sections: list[str]


class HtmlCfg(BaseModel):
    self_contained: bool
    include_sections: list[str]


class ReportCfg(BaseModel):
    model_config = {"populate_by_name": True}

    output_dir: str
    formats: list[Literal["json", "html", "docx"]]
    # Aliased: the YAML key is `json`, but a field literally named `json`
    # shadows BaseModel.json and emits a warning on every import.
    json_options: JsonCfg = Field(alias="json")
    docx: DocxCfg
    html: HtmlCfg

    @model_validator(mode="after")
    def _at_least_one_format(self) -> "ReportCfg":
        if not self.formats:
            raise ValueError(
                "report.formats is empty — a run would analyse everything and "
                "write nothing. Pick at least one of json, html, docx."
            )
        if len(set(self.formats)) != len(self.formats):
            raise ValueError(f"duplicate entries in report.formats: {self.formats}")
        return self


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
    cost: CostCfg
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
    def _bare_tags(cls, v: list[str]) -> list[str]:
        """Store hashtags as bare lowercase tokens, no leading '#'.

        That is the form Instagram's tag URL takes
        (/explore/tags/<tag>/), so keeping it canonical here means the URL
        builder never has to guess. Accepts either form in YAML.
        """
        out: list[str] = []
        for tag in v:
            t = tag.strip().lstrip("#").strip().lower()
            if not t:
                continue
            if " " in t:
                raise ValueError(
                    f"hashtag {tag!r} contains a space. Instagram tags are "
                    f"single tokens — put multi-word phrases in `terms`, "
                    f"which filter captions instead."
                )
            out.append(t)
        return out

    @model_validator(mode="after")
    def _has_collectable_targets(self) -> "Narrative":
        # Hashtags are the only thing that can COLLECT posts. Instagram has no
        # public caption keyword search, so a narrative defined purely by terms
        # would run, cost money, and return nothing.
        if not self.hashtags:
            raise ValueError(
                f"narrative {self.key!r} has no hashtags. Instagram has no "
                f"caption keyword search, so hashtags are the only way to "
                f"collect posts; `terms` are applied as caption filters to "
                f"what the hashtags return. Add at least one hashtag."
            )
        return self

    @property
    def search_terms(self) -> list[str]:
        """Collection inputs. One Apify run is issued per entry."""
        return list(self.hashtags)

    def matches_terms(self, caption: str | None) -> list[str]:
        """Which of this narrative's keyword terms appear in a caption.

        Terms narrow and label what the hashtags collected; they never trigger
        their own actor run. An empty `terms` list matches everything, so a
        hashtag-only narrative keeps all its posts.
        """
        if not self.terms:
            return []
        if not caption:
            return []
        lowered = caption.lower()
        return [t for t in self.terms if t.lower() in lowered]


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
