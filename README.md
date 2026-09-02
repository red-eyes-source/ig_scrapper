# ig-pulse

Instagram discourse monitoring for political communications work. One pipeline,
three lenses, one report.

- **Narrative lens** — what issues are moving, in what language, with what
  audience reaction. Aggregate.
- **Public-figure lens** — named tracking of elected officials, party accounts
  and registered media, from a curated allowlist.
- **Own-side lens** — how the client's own accounts perform against both.

Collection runs on Apify. Sentiment runs on Apify. Storage is Postgres. Output
is a Word report plus a self-contained HTML dashboard.

---

## Scope, and why it is drawn where it is

This system deliberately does **not** build per-account profiles of private
individuals.

The narrative lens ingests posts and comments from thousands of accounts, most
belonging to private citizens and small creators. If per-author state from that
stream were persisted, the pipeline would quietly assemble a longitudinal
profile of every private person who posted about a tracked issue — a different
kind of artefact with a different risk profile, both ethically and under
India's DPDP Act, Instagram's terms, and Apify's own personal-data policy.

So identity resolution has exactly one chokepoint,
`src/igpulse/privacy/author_policy.py`, with two outcomes:

| Author | Stored as | Tracked across runs |
|---|---|---|
| On the allowlist (`config/public_figures.yaml`) | Handle, named | Yes |
| Everyone else | `HMAC(run_salt, handle)` | **No** |

The run salt is 32 random bytes generated at run start, held in memory, never
written anywhere. Within a run the pseudonym is stable, so dedupe and
coordinated-behaviour detection work normally. Across runs the same author
yields an unrelated value, so longitudinal profiling is impossible even for
someone holding the full database.

This is structural, not policy. `narrative_post` and `narrative_comment` have
no handle column to write to. `tests/test_author_policy.py` asserts the
guarantee; if those tests fail, the property is gone.

Two further guardrails:

- Every allowlist entry needs a written `justification` of at least 20
  characters, enforced in both Pydantic and a Postgres `CHECK`. If you cannot
  write one that stands on its own, the account does not belong there.
- `privacy.max_public_figures` (default 250) fails the run if the allowlist
  grows past it. It is a tripwire, not a performance limit — an allowlist that
  large has stopped being a list of public figures. Review the entries rather
  than raising the cap.

Narrative rows are purged after `privacy.narrative_retention_days` (default
90). Public-figure rows are exempt.

---

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

cp .env.example .env       # add APIFY_TOKEN and PG* credentials
createdb igpulse           # or point PG* at an existing instance

python run.py validate     # config check — no network, no Apify credits
python run.py init-db      # apply schema, sync allowlist
```

Then fill in `config/narratives.yaml` and `config/public_figures.yaml`. Both
ship empty on purpose.

## Running

```bash
python run.py pipeline                  # ingest -> analyse -> report -> purge
python run.py ingest --lens narrative   # one lens at a time
python run.py analyze --run-id 42
python run.py report  --run-id 42
python run.py purge                     # apply retention window
```

`validate` is the cheap pre-flight. Run it after any config change — it catches
malformed YAML, a duplicate handle, a thin justification, or an over-cap
allowlist before a single Apify credit is spent.

## Configuration

Everything tunable lives in `config/`. Nothing in `src/` hardcodes a value that
belongs there.

| File | Holds |
|---|---|
| `settings.yaml` | actors, rate limits, retries, thresholds, retention, report options |
| `public_figures.yaml` | the allowlist — the only source of named accounts |
| `narratives.yaml` | issues and hashtags to track — **issues, not people** |

Narrative buckets must be issue-shaped ("farm policy criticism"), not
actor-shaped ("posts by X"). Actor-shaped buckets defeat the aggregation model.
Theme extraction strips `@mentions` for the same reason: a frequently-tagged
account would otherwise surface as a "theme", turning issue analysis back into
person tracking.

## Layout

```
config/          SSOT — settings, allowlist, narratives
sql/schema.sql   Postgres schema; the privacy invariant is enforced here
src/igpulse/
  config.py      typed loader, boot-time validation
  privacy/       the author-policy chokepoint
  apify/         REST client (token bucket, backoff), actor input/output mapping
  ingest/        narrative.py, figures.py (public_figure + own_side lenses)
  analyze/       sentiment.py, themes.py, metrics.py
  report/        docx_report.py, html_dashboard.py
tests/           45 tests, no network or database required
run.py           CLI
```

## Notes on the Apify layer

- Actor IDs use the tilde form in API paths (`apify~instagram-scraper`) and are
  configured, not hardcoded.
- `instagram-scraper` takes one `search` value per run, so the narrative lens
  issues one run per term. That also keeps attribution unambiguous when a post
  matches two narratives.
- Rate limiting is a client-side token bucket. Apify's own limits are
  account-tier dependent, so the config default sits well under them; raise
  `requests_per_second` if your tier allows.
- `Retry-After` is honoured when the server sends it, in preference to the
  exponential guess.

## Notes on sentiment

The Apify store's sentiment actors are third-party, not Apify-official, and
their output schemas are thinly documented. Two consequences the code handles:

- The provider is pluggable. `sentiment.actor_id` plus `field_map` in
  `settings.yaml` is the whole integration; benchmark alternatives without
  touching code. The response shape is validated against `field_map` on the
  first batch, so a provider that renames its fields fails at batch one rather
  than writing a run's worth of nulls.
- These models are trained mainly on English and are weak on code-mixed
  Hinglish, which is a large share of Indian political commentary. Rows below
  `min_confidence` are stored as `uncertain` rather than forced into a
  polarity, and every report quotes explicit coverage. A headline like "net
  sentiment −0.34" means little without "on 61% of rows" beside it.

If accuracy on Hinglish becomes the binding constraint, the cleanest upgrade is
an LLM scorer behind the same interface — `score_run()` is the only function
that would change.

## Known limitations

- Instagram search returns a ranked subset, not a census. Volume figures are
  comparable between runs of this pipeline but are not estimates of total
  platform activity.
- Follower counts exist only for allowlisted accounts, so narrative engagement
  is reported in absolute terms. Engagement *rate* for the narrative lens would
  need a fabricated denominator, so it is not reported at all.
- Trend deltas compare the trailing window against the preceding window with no
  overlap. Overlapping windows damp every movement, which is a common and
  quiet source of understated trends.
