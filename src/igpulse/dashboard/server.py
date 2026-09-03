"""Local control dashboard.

    python run.py dashboard

Serves a small editor on 127.0.0.1 for the three config files, with a live cost
estimate that reruns the planner on every change — so the effect of raising
`results_per_term` is visible in dollars before a run, not after.

Built on ``http.server`` rather than Flask or FastAPI on purpose: a config
editor is not worth another dependency that has to resolve on the operator's
Python, and this project has already lost a session to a wheel that did not
exist for 3.14.

Bound to 127.0.0.1 only. There is no authentication, because the surface is a
loopback socket on the operator's own machine — do not expose it. Binding to
0.0.0.0 would put unauthenticated write access to the collection targets on the
network, so the bind address is not configurable.
"""

from __future__ import annotations

import json
import logging
import threading
import webbrowser
from datetime import datetime, timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from igpulse.analyze.planner import plan_cycle
from igpulse.config import AppConfig, Settings, load_config
from igpulse.config_writer import (
    ConfigWriteError,
    save_narratives,
    save_public_figures,
    save_settings,
)

logger = logging.getLogger(__name__)

_STATIC = Path(__file__).resolve().parent / "static"


def _encode(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    raise TypeError(f"cannot serialise {type(value).__name__}")


class DashboardState:
    """Holds the config dir and the last loaded config, guarded by a lock.

    ThreadingHTTPServer handles requests concurrently, and two browser tabs
    saving at once would otherwise interleave a read-modify-write.
    """

    def __init__(self, config_dir: Path) -> None:
        self.config_dir = config_dir
        self._lock = threading.Lock()
        self._config = load_config(config_dir)

    @property
    def config(self) -> AppConfig:
        with self._lock:
            return self._config

    def reload(self) -> AppConfig:
        with self._lock:
            self._config = load_config(self.config_dir)
            return self._config

    def mutate(self, fn) -> AppConfig:
        with self._lock:
            fn(self._config)
            self._config = load_config(self.config_dir)
            return self._config


def _snapshot(cfg: AppConfig) -> dict[str, Any]:
    plan = plan_cycle(cfg)
    return {
        "settings": cfg.settings.model_dump(mode="json", by_alias=True),
        "narratives": [
            n.model_dump(mode="json") for n in cfg.narratives.narratives
        ],
        "public_figures": [
            f.model_dump(mode="json") for f in cfg.public_figures.figures
        ],
        "fingerprint": cfg.fingerprint(),
        "plan": {
            "lenses": [
                {
                    "lens": l.lens,
                    "actor_runs": l.actor_runs,
                    "max_posts": l.max_posts,
                    "max_comments": l.max_comments,
                    "detail": l.detail,
                }
                for l in plan.lenses
            ],
            "total_actor_runs": plan.total_actor_runs,
            "total_posts": plan.total_posts,
            "total_comments": plan.total_comments,
            "total_items": plan.total_items,
            "cost": plan.cost(cfg),
            "warnings": plan.warnings,
        },
    }


def _estimate_from_payload(
    base: AppConfig, settings_payload: dict, narratives: list[dict]
) -> dict[str, Any]:
    """Cost estimate for unsaved edits.

    Lets the browser show what a change would cost before it is committed,
    which is the whole point of putting the planner behind a UI.
    """
    from igpulse.config import NarrativeList

    settings = Settings.model_validate(settings_payload)
    narrative_list = NarrativeList.model_validate({"narratives": narratives})
    candidate = AppConfig(
        settings=settings,
        public_figures=base.public_figures,
        narratives=narrative_list,
    )
    plan = plan_cycle(candidate)
    return {
        "total_actor_runs": plan.total_actor_runs,
        "total_posts": plan.total_posts,
        "total_comments": plan.total_comments,
        "total_items": plan.total_items,
        "cost": plan.cost(candidate),
        "warnings": plan.warnings,
    }


def _recent_runs(cfg: AppConfig) -> list[dict[str, Any]]:
    """Last few ingest runs. Absent Postgres is normal, not an error."""
    try:
        from igpulse.store.db import Database, DatabaseUnavailable

        try:
            with Database(cfg.settings.database) as db:
                return db.fetch(
                    """
                    SELECT id, lens, status, started_at, finished_at,
                           items_ingested, config_fingerprint, error_detail
                      FROM ingest_run ORDER BY id DESC LIMIT 10
                    """
                )
        except DatabaseUnavailable:
            return []
    except Exception:  # noqa: BLE001 - the dashboard must open regardless
        return []


class Handler(BaseHTTPRequestHandler):
    state: DashboardState

    server_version = "ig-pulse-dashboard"

    def log_message(self, fmt: str, *args: Any) -> None:  # quieter default
        logger.debug("%s - %s", self.address_string(), fmt % args)

    # -- helpers ---------------------------------------------------------- #
    def _send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, default=_encode).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> Any:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    # -- routes ----------------------------------------------------------- #
    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/", "/index.html"):
            body = (_STATIC / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path == "/api/config":
            self._send_json(_snapshot(self.state.config))
            return

        if self.path == "/api/runs":
            self._send_json({"runs": _recent_runs(self.state.config)})
            return

        self._send_json({"error": "not found"}, status=404)

    def do_POST(self) -> None:  # noqa: N802
        try:
            payload = self._read_json()
        except json.JSONDecodeError as exc:
            self._send_json({"error": f"invalid JSON: {exc}"}, status=400)
            return

        try:
            if self.path == "/api/estimate":
                self._send_json(
                    _estimate_from_payload(
                        self.state.config,
                        payload["settings"],
                        payload["narratives"],
                    )
                )
                return

            if self.path == "/api/settings":
                save_settings(self.state.config_dir, payload)
                cfg = self.state.reload()
                self._send_json({"ok": True, **_snapshot(cfg)})
                return

            if self.path == "/api/narratives":
                save_narratives(self.state.config_dir, payload["narratives"])
                cfg = self.state.reload()
                self._send_json({"ok": True, **_snapshot(cfg)})
                return

            if self.path == "/api/public_figures":
                save_public_figures(
                    self.state.config_dir,
                    payload["figures"],
                    settings=self.state.config.settings,
                )
                cfg = self.state.reload()
                self._send_json({"ok": True, **_snapshot(cfg)})
                return

        except ConfigWriteError as exc:
            # Validation failure: nothing was written. Surface the reason.
            self._send_json({"error": str(exc)}, status=422)
            return
        except KeyError as exc:
            self._send_json({"error": f"missing field {exc}"}, status=400)
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("dashboard request failed")
            self._send_json({"error": str(exc)}, status=500)
            return

        self._send_json({"error": "not found"}, status=404)


def serve(config_dir: Path, *, port: int = 8765, open_browser: bool = True) -> int:
    Handler.state = DashboardState(config_dir)
    # 127.0.0.1, never 0.0.0.0: there is no auth, and the editable surface is
    # what the pipeline collects and who it names.
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"ig-pulse dashboard: {url}")
    print(f"editing config in: {config_dir}")
    print("Ctrl-C to stop.")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
    return 0
