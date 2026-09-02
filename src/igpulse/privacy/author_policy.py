"""The single chokepoint through which every author identity passes.

Why this exists
---------------
The narrative lens ingests posts and comments from thousands of accounts, the
overwhelming majority of which belong to private individuals and small
creators. If per-author state from that stream were persisted, the system would
reconstruct a longitudinal profile of every private citizen who posted about a
tracked issue — which is precisely the artefact this architecture is designed
not to produce.

So identity resolution has exactly two outcomes:

  * The handle is on the curated allowlist (config/public_figures.yaml) →
    a NamedAuthor, persisted by handle, tracked over time.
  * Anything else → a PseudonymousAuthor whose identifier is
    HMAC-SHA256(run_salt, handle), truncated.

The run salt is 32 random bytes generated at run start, held only in memory,
and never written to disk, the database, or logs. Consequences:

  * Within a run, the same author yields the same pseudonym — dedupe and
    coordinated-behaviour detection work normally.
  * Across runs, the same author yields an unrelated pseudonym — longitudinal
    profiling is impossible even for someone holding the full database.
  * The mapping is not reversible: recovering a handle would require brute-
    forcing the handle space against an unknown 256-bit salt.

This is a structural guarantee rather than a policy one. The narrative tables
have no handle column to write to even if calling code tried.
"""

from __future__ import annotations

import hmac
import logging
import secrets
from dataclasses import dataclass
from hashlib import sha256
from typing import Final

from igpulse.config import PublicFigureList

logger = logging.getLogger(__name__)

# 16 hex chars = 64 bits. Collision probability across a run of even 10M
# authors is ~2.7e-6 by the birthday bound — negligible for aggregate counting,
# and short enough to keep indexes compact.
_PSEUDONYM_HEX_LEN: Final[int] = 16
_SALT_BYTES: Final[int] = 32


@dataclass(frozen=True, slots=True)
class NamedAuthor:
    """An allowlisted public figure. Safe to persist by handle."""

    handle: str
    display_name: str
    category: str

    is_named: bool = True


@dataclass(frozen=True, slots=True)
class PseudonymousAuthor:
    """Everyone else. Carries no recoverable identity."""

    pseudonym: str

    is_named: bool = False


Author = NamedAuthor | PseudonymousAuthor


class HandleLeakError(RuntimeError):
    """Raised when calling code tries to persist a non-allowlisted handle."""


class AuthorPolicy:
    """Per-run identity resolver.

    Instantiate once per ingest run. The instance owns the run salt; letting it
    go out of scope at run end is what makes the pseudonyms unlinkable.
    """

    def __init__(self, allowlist: PublicFigureList) -> None:
        self._by_handle = {f.handle: f for f in allowlist.figures}
        # secrets.token_bytes draws from the OS CSPRNG. Not stored anywhere.
        self._run_salt: bytes = secrets.token_bytes(_SALT_BYTES)
        self._pseudonym_cache: dict[str, str] = {}
        logger.info(
            "AuthorPolicy initialised: %d allowlisted figures, fresh run salt",
            len(self._by_handle),
        )

    # -- resolution ------------------------------------------------------- #
    @staticmethod
    def normalise(handle: str) -> str:
        """Canonical handle form. Must match config.PublicFigure normalisation."""
        return handle.strip().lstrip("@").lower()

    def is_allowlisted(self, handle: str) -> bool:
        return self.normalise(handle) in self._by_handle

    def resolve(self, handle: str | None) -> Author:
        """Map a raw handle onto either a NamedAuthor or a PseudonymousAuthor.

        A missing handle (deleted account, private post, actor field absent)
        still yields a pseudonym so downstream aggregate counts stay correct;
        it is bucketed under a fixed sentinel rather than dropped.
        """
        if handle is None or not handle.strip():
            return PseudonymousAuthor(pseudonym=self._pseudonymise("\x00unknown"))

        norm = self.normalise(handle)
        figure = self._by_handle.get(norm)
        if figure is not None:
            return NamedAuthor(
                handle=figure.handle,
                display_name=figure.display_name,
                category=figure.category,
            )
        return PseudonymousAuthor(pseudonym=self._pseudonymise(norm))

    def _pseudonymise(self, normalised_handle: str) -> str:
        cached = self._pseudonym_cache.get(normalised_handle)
        if cached is not None:
            return cached
        digest = hmac.new(
            self._run_salt, normalised_handle.encode("utf-8"), sha256
        ).hexdigest()[:_PSEUDONYM_HEX_LEN]
        self._pseudonym_cache[normalised_handle] = digest
        return digest

    # -- write guard ------------------------------------------------------ #
    def assert_persistable_handle(self, handle: str) -> str:
        """Gate for code paths that write a handle to a named table.

        Call this immediately before any INSERT that stores a literal handle.
        Raises rather than silently degrading, because a silent fallback here
        would be indistinguishable from the bug it is meant to catch.
        """
        norm = self.normalise(handle)
        if norm not in self._by_handle:
            raise HandleLeakError(
                f"refusing to persist handle {norm!r}: not on the public-figure "
                f"allowlist. Narrative-lens authors must be written as "
                f"pseudonyms via resolve(). If this account genuinely is a "
                f"public figure, add it to config/public_figures.yaml with a "
                f"written justification."
            )
        return norm

    # -- diagnostics ------------------------------------------------------ #
    @property
    def distinct_authors_seen(self) -> int:
        """Count of distinct non-allowlisted authors seen this run.

        Useful as a reach denominator without retaining anything about them.
        """
        return len(self._pseudonym_cache)

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (
            f"AuthorPolicy(allowlisted={len(self._by_handle)}, "
            f"pseudonymised={len(self._pseudonym_cache)})"
        )
