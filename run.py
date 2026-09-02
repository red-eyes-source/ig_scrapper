#!/usr/bin/env python3
"""ig-pulse CLI.

    python run.py plan               # dry-run volume + cost estimate
    python run.py test-connection
    python run.py init-db
    python run.py ingest --lens all
    python run.py analyze --run-id 42
    python run.py report --run-id 42
    python run.py pipeline            # ingest -> analyze -> report
    python run.py purge               # apply the retention window
    python run.py validate            # config check, no network, no credits

`validate` is the cheap pre-flight: it loads and validates all three config
files and prints what a run would do, without touching Apify or Postgres.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from dotenv import load_dotenv  # noqa: E402

from igpulse.analyze import metrics as metrics_mod  # noqa: E402
from igpulse.analyze import planner  # noqa: E402
from igpulse.analyze import sentiment as sentiment_mod  # noqa: E402
from igpulse.analyze import themes as themes_mod  # noqa: E402
from igpulse.apify.client import ApifyClient  # noqa: E402
from igpulse.config import AppConfig, load_config  # noqa: E402
from igpulse.ingest import figures as figures_mod  # noqa: E402
from igpulse.ingest import narrative as narrative_mod  # noqa: E402
from igpulse.privacy.author_policy import AuthorPolicy  # noqa: E402
from igpulse.report.docx_report import build_report  # noqa: E402
from igpulse.report.html_dashboard import build_dashboard  # noqa: E402
from igpulse.store.db import Database  # noqa: E402

logger = logging.getLogger("igpulse")


def _setup_logging(cfg: AppConfig) -> None:
    logging.basicConfig(
        level=getattr(logging, cfg.settings.logging.level.upper(), logging.INFO),
        format=cfg.settings.logging.format,
        stream=sys.stderr,
    )


def _apify_token() -> str:
    token = os.environ.get("APIFY_TOKEN", "").strip()
    if not token:
        sys.exit(
            "APIFY_TOKEN is not set. Copy .env.example to .env and fill it in."
        )
    return token


def cmd_validate(cfg: AppConfig, _args: argparse.Namespace) -> int:
    figures = cfg.public_figures.figures
    print(f"config fingerprint : {cfg.fingerprint()}")
    print(f"narratives         : {len(cfg.narratives.narratives)}")
    for n in cfg.narratives.narratives:
        print(f"  - {n.key}: {len(n.search_terms)} search term(s)")
    print(f"public figures     : {len(figures)}"
          f" (cap {cfg.settings.privacy.max_public_figures})")
    for category in ("elected_official", "party_official",
                     "registered_media", "own_side"):
        count = len(cfg.public_figures.by_category(category))
        if count:
            print(f"  - {category}: {count}")
    print(f"sentiment actor    : {cfg.settings.sentiment.actor_id}")
    print(f"retention          : {cfg.settings.privacy.narrative_retention_days} days")

    if not cfg.narratives.narratives:
        print("\nWARNING: no narratives configured — the narrative lens will "
              "collect nothing.", file=sys.stderr)
    if not figures:
        print("WARNING: allowlist is empty — the public-figure and own-side "
              "lenses will collect nothing.", file=sys.stderr)
    return 0


def cmd_plan(cfg: AppConfig, _args: argparse.Namespace) -> int:
    """Show what a full cycle would collect and roughly cost. No network."""
    plan = planner.plan_cycle(cfg)
    cost = plan.cost(cfg)

    for lens_plan in plan.lenses:
        print(f"{lens_plan.lens}")
        for line in lens_plan.detail:
            print(f"  {line}")
        print(f"  -> {lens_plan.actor_runs} actor run(s), "
              f"up to {lens_plan.max_posts:,} posts + "
              f"{lens_plan.max_comments:,} comments")
        print()

    print(f"{'TOTAL':<22}{plan.total_actor_runs:>8} actor runs")
    print(f"{'':<22}{plan.total_items:>8,} items "
          f"({plan.total_posts:,} posts + {plan.total_comments:,} comments)")
    print()
    print("Upper-bound cost estimate (USD)")
    print(f"  post scraping      {cost['scraping']:>9.2f}")
    print(f"  comment scraping   {cost['comments']:>9.2f}")
    print(f"  sentiment          {cost['sentiment']:>9.2f}")
    print(f"  {'total':<18} {cost['total']:>9.2f}")
    print()
    print("Ceiling, not a forecast: search returns fewer results than "
          "requested for narrow terms,")
    print("not every post has the requested comment count, and short text is "
          "never sent for scoring.")
    print("Rates are from config/settings.yaml -> cost; verify against your "
          "Apify plan.")

    if plan.warnings:
        print()
        for warning in plan.warnings:
            print(f"WARNING: {warning}", file=sys.stderr)
    return 0


def cmd_test_connection(cfg: AppConfig, _args: argparse.Namespace) -> int:
    """Verify Apify and Postgres connectivity without starting an actor.

    Costs nothing: /users/me and /acts/{id} are metadata reads, not runs. Run
    this after setting credentials and before the first real ingest.
    """
    failures = 0

    print("Apify")
    token = os.environ.get("APIFY_TOKEN", "").strip()
    if not token:
        print("  token          : MISSING — set APIFY_TOKEN in .env")
        failures += 1
    else:
        masked = f"{token[:9]}…{token[-4:]}" if len(token) > 16 else "…"
        print(f"  token          : {masked}")
        try:
            with ApifyClient(token, cfg.settings.apify) as client:
                user = client.whoami()
                print(f"  authenticated  : yes (username: "
                      f"{user.get('username', 'unknown')})")
                plan = user.get("plan") or {}
                if isinstance(plan, dict) and plan.get("id"):
                    print(f"  plan           : {plan['id']}")

                actors = [
                    cfg.settings.apify.actors.instagram_scraper,
                    cfg.settings.apify.actors.instagram_comment_scraper,
                    cfg.settings.sentiment.actor_id,
                ]
                for actor_id in actors:
                    ok = client.actor_exists(actor_id)
                    print(f"  actor {actor_id:<38} "
                          f"{'reachable' if ok else 'NOT FOUND'}")
                    if not ok:
                        failures += 1
        except Exception as exc:  # noqa: BLE001 - surface the reason verbatim
            print(f"  authenticated  : NO — {exc}")
            failures += 1

    print("\nPostgres")
    # Direct connection with a short timeout rather than the pool: the pool
    # retries for 30s and logs a warning per attempt, which is right for a
    # long-running ingest and wrong for a diagnostic that should fail fast.
    try:
        import psycopg

        with psycopg.connect("", connect_timeout=5) as conn:
            db_name, version = conn.execute(
                "SELECT current_database(), version()"
            ).fetchone()
            print(f"  connected      : yes ({db_name})")
            print(f"  server         : {version.split(',')[0]}")
            (applied,) = conn.execute(
                "SELECT to_regclass('public.public_figure') IS NOT NULL"
            ).fetchone()
            print(f"  schema applied : "
                  f"{'yes' if applied else 'no — run init-db'}")
            if not applied:
                failures += 1
    except Exception as exc:  # noqa: BLE001
        detail = str(exc).strip().splitlines()[0]
        print(f"  connected      : NO — {detail}")
        print("  hint           : check PGHOST/PGPORT/PGDATABASE/PGUSER/"
              "PGPASSWORD in .env")
        failures += 1

    print()
    if failures:
        print(f"{failures} check(s) failed.", file=sys.stderr)
        return 1
    print("All checks passed. Safe to run `python run.py pipeline`.")
    return 0


def cmd_init_db(cfg: AppConfig, _args: argparse.Namespace) -> int:
    with Database(cfg.settings.database) as db:
        db.apply_schema()
        db.sync_public_figures(cfg.public_figures)
    print("schema applied and allowlist synced")
    return 0


def cmd_ingest(cfg: AppConfig, args: argparse.Namespace) -> int:
    lenses = (
        ["narrative", "public_figure", "own_side"]
        if args.lens == "all" else [args.lens]
    )
    run_ids: list[int] = []

    with Database(cfg.settings.database) as db, \
            ApifyClient(_apify_token(), cfg.settings.apify) as client:
        db.sync_public_figures(cfg.public_figures)
        # One policy instance per invocation: a single run salt across the
        # lenses of one collection cycle, discarded when the process exits.
        policy = AuthorPolicy(cfg.public_figures)

        for lens in lenses:
            if lens == "narrative":
                result = narrative_mod.collect_narratives(cfg, client, db, policy)
                run_ids.append(result.run_id)
                print(f"narrative: run {result.run_id}, {result.posts} posts, "
                      f"{result.comments} comments, "
                      f"{result.distinct_authors} distinct authors")
            else:
                result = figures_mod.collect_figures(
                    cfg, client, db, policy, lens=lens
                )
                run_ids.append(result.run_id)
                print(f"{lens}: run {result.run_id}, {result.handles} handles, "
                      f"{result.posts} posts, {result.comments} comments")

    print("run ids: " + ", ".join(str(r) for r in run_ids))
    return 0


def cmd_analyze(cfg: AppConfig, args: argparse.Namespace) -> int:
    with Database(cfg.settings.database) as db, \
            ApifyClient(_apify_token(), cfg.settings.apify) as client:
        summary = sentiment_mod.score_run(cfg, client, db, run_id=args.run_id)
        themes = themes_mod.extract_themes(cfg, db, run_id=args.run_id)
    print(f"sentiment: {summary.scored} scored, {summary.uncertain} uncertain "
          f"({summary.coverage * 100:.1f}% coverage)")
    print(f"themes: {len(themes)} terms")
    return 0


def cmd_report(cfg: AppConfig, args: argparse.Namespace) -> int:
    generated_at = datetime.now(timezone.utc)
    with Database(cfg.settings.database) as db:
        narratives = metrics_mod.narrative_metrics(cfg, db, run_id=args.run_id)
        figures = metrics_mod.figure_metrics(db, run_id=args.figure_run_id
                                             or args.run_id)
        theme_rows = db.fetch(
            """
            SELECT narrative_key, term, frequency, doc_frequency, score
              FROM theme_term WHERE run_id = %s ORDER BY score DESC
            """,
            (args.run_id,),
        )
        sent_rows = db.fetch(
            """
            SELECT label, COUNT(*) AS n FROM sentiment_score ss
             WHERE EXISTS (
                 SELECT 1 FROM narrative_post p
                  WHERE p.id = ss.source_id AND p.run_id = %s
                    AND ss.source_table = 'narrative_post')
             GROUP BY label
            """,
            (args.run_id,),
        )

    themes = [
        themes_mod.ThemeTerm(
            narrative_key=r["narrative_key"], term=r["term"],
            frequency=r["frequency"], doc_frequency=r["doc_frequency"],
            score=r["score"],
        )
        for r in theme_rows
    ]
    scored = sum(int(r["n"]) for r in sent_rows if r["label"] != "uncertain")
    uncertain = sum(int(r["n"]) for r in sent_rows if r["label"] == "uncertain")
    summary = sentiment_mod.SentimentSummary(
        scored=scored, uncertain=uncertain, skipped_short=0
    )

    kwargs = dict(
        narratives=narratives, figures=figures, themes=themes,
        sentiment=summary, generated_at=generated_at,
    )
    docx_path = build_report(cfg, **kwargs)
    html_path = build_dashboard(cfg, **kwargs)
    print(f"wrote {docx_path}")
    print(f"wrote {html_path}")
    return 0


def cmd_purge(cfg: AppConfig, _args: argparse.Namespace) -> int:
    with Database(cfg.settings.database) as db:
        deleted = db.purge_expired_narrative_rows(
            cfg.settings.privacy.narrative_retention_days
        )
    print(f"purged {deleted} narrative posts past retention")
    return 0


def cmd_pipeline(cfg: AppConfig, args: argparse.Namespace) -> int:
    args.lens = "all"
    if (rc := cmd_ingest(cfg, args)) != 0:
        return rc
    with Database(cfg.settings.database) as db:
        latest = db.fetch(
            """
            SELECT lens, MAX(id) AS run_id FROM ingest_run
             WHERE status = 'succeeded' GROUP BY lens
            """
        )
    by_lens = {r["lens"]: int(r["run_id"]) for r in latest}
    narrative_run = by_lens.get("narrative")
    if narrative_run is None:
        print("no successful narrative run to analyse", file=sys.stderr)
        return 1

    args.run_id = narrative_run
    args.figure_run_id = by_lens.get("public_figure")
    if (rc := cmd_analyze(cfg, args)) != 0:
        return rc
    if (rc := cmd_report(cfg, args)) != 0:
        return rc
    return cmd_purge(cfg, args)


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(prog="ig-pulse")
    parser.add_argument("--config-dir", default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("validate").set_defaults(func=cmd_validate)
    sub.add_parser("plan").set_defaults(func=cmd_plan)
    sub.add_parser("test-connection").set_defaults(func=cmd_test_connection)
    sub.add_parser("init-db").set_defaults(func=cmd_init_db)
    sub.add_parser("purge").set_defaults(func=cmd_purge)

    p_ingest = sub.add_parser("ingest")
    p_ingest.add_argument(
        "--lens", choices=["narrative", "public_figure", "own_side", "all"],
        default="all",
    )
    p_ingest.set_defaults(func=cmd_ingest)

    p_analyze = sub.add_parser("analyze")
    p_analyze.add_argument("--run-id", type=int, required=True)
    p_analyze.set_defaults(func=cmd_analyze)

    p_report = sub.add_parser("report")
    p_report.add_argument("--run-id", type=int, required=True)
    p_report.add_argument("--figure-run-id", type=int, default=None)
    p_report.set_defaults(func=cmd_report)

    p_pipeline = sub.add_parser("pipeline")
    p_pipeline.set_defaults(func=cmd_pipeline, run_id=None, figure_run_id=None)

    args = parser.parse_args(argv)
    cfg = load_config(args.config_dir)
    _setup_logging(cfg)
    try:
        return args.func(cfg, args)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
