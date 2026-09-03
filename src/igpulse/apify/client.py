"""Apify REST client: run actors, poll to completion, page dataset items.

Endpoints (Apify API v2):
    POST /v2/acts/{actor_id}/runs          start a run
    GET  /v2/actor-runs/{run_id}           run status
    GET  /v2/datasets/{dataset_id}/items   paged results

Actor IDs use the tilde form in paths (``apify~instagram-scraper``).

Concurrency note: this client is synchronous by design. Apify runs are
minutes-long and the bottleneck is actor execution, not local I/O, so an async
client would add failure modes without shortening wall-clock time. Parallelism,
where it helps, belongs at the run level — start several actor runs, then poll
them together via :meth:`wait_for_runs`.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterator

import httpx

from igpulse.config import ApifyCfg

logger = logging.getLogger(__name__)

# Apify run states. Anything not in _TERMINAL means keep polling.
_TERMINAL_OK = {"SUCCEEDED"}
_TERMINAL_BAD = {"FAILED", "ABORTED", "TIMED-OUT"}
_TERMINAL = _TERMINAL_OK | _TERMINAL_BAD


class ApifyError(RuntimeError):
    """Base for Apify client failures."""


# Real tokens are `apify_api_` plus ~36 random alphanumerics. The shipped
# .env.example uses a run of x's, and every other placeholder convention below
# shows up in copied config too. Catching these is worth a few lines: an
# unfilled placeholder otherwise produces a 401, which sends you looking for a
# revoked or wrong-account token instead of an unedited .env.
_PLACEHOLDER_MARKERS = ("xxxx", "your_", "yourtoken", "<", "changeme",
                        "change-me", "replace", "placeholder", "example")


def looks_like_placeholder(token: str) -> bool:
    lowered = token.strip().lower()
    if not lowered:
        return True
    if any(marker in lowered for marker in _PLACEHOLDER_MARKERS):
        return True
    # A real token is comfortably longer than its prefix.
    return len(lowered) < 20


class ApifyRunFailed(ApifyError):
    def __init__(self, run_id: str, status: str, detail: str | None = None) -> None:
        super().__init__(f"actor run {run_id} ended in state {status}: {detail or '-'}")
        self.run_id = run_id
        self.status = status


class ApifyRunTimeout(ApifyError):
    pass


@dataclass(frozen=True, slots=True)
class ActorRun:
    run_id: str
    actor_id: str
    dataset_id: str
    status: str
    started_at: datetime

    @property
    def succeeded(self) -> bool:
        return self.status in _TERMINAL_OK


class _TokenBucket:
    """Thread-safe token bucket.

    Sized from config rather than hardcoded, because Apify's own rate limits are
    account-tier dependent. Blocking (rather than raising) is correct here: the
    caller's alternative is a 429 and a longer backoff.
    """

    def __init__(self, rate_per_second: float, burst: int) -> None:
        if rate_per_second <= 0:
            raise ValueError("rate_per_second must be positive")
        self._rate = rate_per_second
        self._capacity = float(burst)
        self._tokens = float(burst)
        self._updated = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                elapsed = now - self._updated
                self._updated = now
                self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                deficit = (1.0 - self._tokens) / self._rate
            # Sleep outside the lock so other threads can refill/observe.
            time.sleep(deficit)


class ApifyClient:
    def __init__(self, token: str, cfg: ApifyCfg) -> None:
        if looks_like_placeholder(token):
            raise ValueError(
                "APIFY_TOKEN is unset or still holds the placeholder value "
                "from .env.example. Get a real token from the Apify console: "
                "Settings -> API & Integrations -> Personal API token, then "
                "put it in .env as APIFY_TOKEN=apify_api_..."
            )
        self._cfg = cfg
        self._bucket = _TokenBucket(
            cfg.rate_limit.requests_per_second, cfg.rate_limit.burst
        )
        self._client = httpx.Client(
            base_url=cfg.base_url,
            timeout=httpx.Timeout(
                connect=cfg.timeouts.connect_seconds,
                read=cfg.timeouts.read_seconds,
                write=cfg.timeouts.read_seconds,
                pool=cfg.timeouts.connect_seconds,
            ),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "User-Agent": "ig-pulse/1.0",
            },
            follow_redirects=True,
        )

    def __enter__(self) -> "ApifyClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # -- transport -------------------------------------------------------- #
    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any | None = None,
    ) -> httpx.Response:
        retry = self._cfg.retry
        backoff = retry.initial_backoff_seconds
        last_exc: Exception | None = None

        for attempt in range(1, retry.max_attempts + 1):
            self._bucket.acquire()
            try:
                response = self._client.request(
                    method, path, params=params, json=json_body
                )
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_exc = exc
                if attempt == retry.max_attempts:
                    break
                logger.warning(
                    "%s %s transport error (attempt %d/%d): %s",
                    method, path, attempt, retry.max_attempts, exc,
                )
            else:
                if response.status_code not in retry.retry_on_status:
                    response.raise_for_status()
                    return response
                last_exc = ApifyError(
                    f"{method} {path} -> HTTP {response.status_code}"
                )
                if attempt == retry.max_attempts:
                    break
                # Honour Retry-After when the server sends one; it is more
                # accurate than our exponential guess.
                retry_after = response.headers.get("Retry-After")
                if retry_after:
                    try:
                        backoff = min(float(retry_after), retry.max_backoff_seconds)
                    except ValueError:
                        pass
                logger.warning(
                    "%s %s -> HTTP %d (attempt %d/%d), sleeping %.1fs",
                    method, path, response.status_code, attempt,
                    retry.max_attempts, backoff,
                )

            time.sleep(backoff)
            backoff = min(backoff * 2, retry.max_backoff_seconds)

        raise ApifyError(
            f"{method} {path} failed after {retry.max_attempts} attempts"
        ) from last_exc

    # -- account / connectivity ------------------------------------------- #
    def whoami(self) -> dict[str, Any]:
        """Verify the token and return account details.

        This is the cheapest possible authenticated call — no actor starts, no
        compute units consumed. Used by `run.py test-connection` so a bad token
        surfaces before a run rather than twenty minutes into one.
        """
        return self._request("GET", "/users/me").json()["data"]

    def actor_exists(self, actor_id: str) -> bool:
        """Check that an actor ID resolves and is visible to this account.

        A renamed or unpublished store actor is a common and confusing failure:
        the run starts, then dies on a 404 that reads like a network problem.
        """
        try:
            self._request("GET", f"/acts/{actor_id}")
        except (ApifyError, httpx.HTTPStatusError):
            # _request raises HTTPStatusError for a 404 (not a retryable
            # status) and ApifyError once retries are exhausted. Both mean
            # "this actor is not usable from this account".
            return False
        return True

    # -- runs ------------------------------------------------------------- #
    def start_run(self, actor_id: str, run_input: dict[str, Any]) -> ActorRun:
        """Start an actor run asynchronously and return immediately."""
        logger.info("starting actor %s", actor_id)
        response = self._request(
            "POST", f"/acts/{actor_id}/runs", json_body=run_input
        )
        data = response.json()["data"]
        return ActorRun(
            run_id=data["id"],
            actor_id=actor_id,
            dataset_id=data["defaultDatasetId"],
            status=data["status"],
            started_at=datetime.now(timezone.utc),
        )

    def get_run(self, run_id: str) -> ActorRun:
        response = self._request("GET", f"/actor-runs/{run_id}")
        data = response.json()["data"]
        return ActorRun(
            run_id=data["id"],
            actor_id=data.get("actId", ""),
            dataset_id=data["defaultDatasetId"],
            status=data["status"],
            started_at=datetime.now(timezone.utc),
        )

    def wait_for_run(self, run: ActorRun) -> ActorRun:
        return self.wait_for_runs([run])[0]

    def wait_for_runs(self, runs: list[ActorRun]) -> list[ActorRun]:
        """Poll several runs concurrently until all reach a terminal state.

        Raises ApifyRunFailed on the first non-SUCCEEDED terminal state so a
        partial dataset never silently becomes a report.
        """
        polling = self._cfg.run_polling
        deadline = time.monotonic() + polling.max_wait_seconds
        pending = {r.run_id: r for r in runs}
        finished: dict[str, ActorRun] = {}

        while pending:
            if time.monotonic() > deadline:
                raise ApifyRunTimeout(
                    f"runs {sorted(pending)} did not finish within "
                    f"{polling.max_wait_seconds:.0f}s"
                )
            for run_id in list(pending):
                current = self.get_run(run_id)
                if current.status in _TERMINAL:
                    if current.status in _TERMINAL_BAD:
                        raise ApifyRunFailed(run_id, current.status)
                    finished[run_id] = current
                    pending.pop(run_id)
                    logger.info("actor run %s succeeded", run_id)
            if pending:
                time.sleep(polling.interval_seconds)

        # Preserve caller ordering.
        return [finished[r.run_id] for r in runs]

    # -- datasets --------------------------------------------------------- #
    def iter_dataset_items(
        self, dataset_id: str, *, max_items: int | None = None
    ) -> Iterator[dict[str, Any]]:
        """Yield dataset items, paging under Apify's per-request limit.

        Uses offset paging with a `total` guard, so a dataset that grows while
        being read terminates rather than looping.
        """
        page_size = self._cfg.dataset.page_size
        offset = 0
        yielded = 0

        while True:
            remaining = None if max_items is None else max_items - yielded
            if remaining is not None and remaining <= 0:
                return
            limit = page_size if remaining is None else min(page_size, remaining)

            response = self._request(
                "GET",
                f"/datasets/{dataset_id}/items",
                params={"offset": offset, "limit": limit, "clean": "true"},
            )
            items = response.json()
            if not isinstance(items, list):
                raise ApifyError(
                    f"dataset {dataset_id} returned {type(items).__name__}, "
                    "expected a JSON array"
                )
            if not items:
                return

            for item in items:
                yield item
                yielded += 1

            if len(items) < limit:
                return
            offset += len(items)

    def run_and_collect(
        self,
        actor_id: str,
        run_input: dict[str, Any],
        *,
        max_items: int | None = None,
    ) -> tuple[ActorRun, list[dict[str, Any]]]:
        """Convenience: start, wait, and collect. Use for single-run steps."""
        run = self.wait_for_run(self.start_run(actor_id, run_input))
        items = list(self.iter_dataset_items(run.dataset_id, max_items=max_items))
        logger.info("actor %s returned %d items", actor_id, len(items))
        return run, items
