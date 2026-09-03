# ig-pulse

Instagram discourse monitoring for political communications work. One pipeline,
three lenses, one report.

- **Narrative lens** — what issues are moving, in what language, with what
  audience reaction. Aggregate.
- **Public-figure lens** — named tracking of elected officials, party accounts
  and registered media, from a curated allowlist.
- **Own-side lens** — how the client's own accounts perform against both.

Collection runs on Apify. Sentiment runs on Apify. Storage is Postgres. Output
is a JSON dataset plus a self-contained HTML dashboard; a Word report is
available by adding `docx` to `report.formats`.

## Control dashboard

```bash
python run.py dashboard
```

Opens a local editor on 127.0.0.1:8765 for all three config files: narratives
(hashtags and caption filters), the public-figure allowlist, and every tunable
parameter. A live cost estimate reruns the planner as you type, so raising
`results_per_term` shows its dollar effect before you save, not after you run.

Saves are validated through the same Pydantic models the pipeline uses — an
invalid edit is rejected with the reason and never reaches disk — written
atomically, and the previous version is kept under `config/.backups/`.

Loopback only, and deliberately not configurable: there is no authentication,
and what the page edits is what the pipeline collects and who it names.

## Output formats

`report.formats` in `settings.yaml` decides what a run writes. Default is
`["json", "html"]`.

**JSON** is the full dataset, not a summary: every post with its likes,
comments, engagement and sentiment; every comment nested under its post; the
hashtags that collected each narrative and their tag URLs; themes with
frequencies; and the aggregate metrics. Every published figure can be recomputed
from the raw rows in the same file, so a client can check the numbers instead of
trusting them. `schema_version` is pinned so downstream code can rely on the
shape.

The privacy invariant holds in the export as it does in the database: narrative
authors are per-run pseudonyms and no handle field exists to serialise. Only
allowlisted public figures are named, and each carries the written justification
that put them on the list.

Size control lives under `report.json`: `include_posts`, `include_comments`,
`include_themes`, `max_posts_per_narrative`, `caption_max_chars`. Prefer capping
post counts over truncating captions — a clipped caption silently corrupts any
downstream text analysis.

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
| Author of a quoted post (top N by engagement) | Handle, for citation | **No** — separate short retention |
| Every other author, and every commenter | `HMAC(run_salt, handle)` | **No** |

The middle row exists because a quoted post needs an attributable author or the
citation cannot be checked. It is bounded on three axes: posts only (never
comments), at most `attribution_top_n_per_narrative` per narrative per run
(capped at 50 in the schema), and its own retention which must expire before
the aggregate rows do. `post_attribution` has no index on handle and no
per-handle aggregate — adding either turns a citation table into a profile
store, and a test asserts neither appears.

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

### 1. Install

All command blocks in this README are comment-free so they can be pasted
directly. Interactive zsh does not treat `#` as a comment by default, so a
trailing explanation on a command line becomes an argument and breaks it.

Requires **Python 3.11 or newer**. On macOS and most Linux distributions the
command is `python3`, not `python` — plain `python` usually does not exist.

```bash
python3 --version
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt
```

Confirm the first line reports 3.11 or newer. On Windows the activate command
is `.venv\Scripts\activate`.

Inside an activated venv, `python` and `pip` are correct — that is what the
venv is for. Outside one, use `python3` and `python3 -m pip`.

**Activate the venv before installing.** If `python3 -m venv` fails and you run
`pip install` anyway, the packages land in your system Python. That is what
produces the "Requirement already satisfied" lines pointing at
`/Library/Frameworks/...` instead of `.venv/`.

Verify the install:

```bash
python -m pytest -q
```

Expect `92 passed` (or `111 passed` once a database is reachable — 19 integration
tests skip automatically without one).

### 2. Get your Apify API token

1. Sign in at [console.apify.com](https://console.apify.com).
2. **Settings → API & Integrations** → copy the **Personal API token**
   (starts `apify_api_`).
3. Treat it like a password. It has full access to your account, including
   billing-relevant actions — anyone holding it can spend your credits.

Token scope note: a Personal API token covers everything this pipeline does.
If you would rather scope it down, create a **scoped token** limited to
"Run Actors" plus "Read datasets" — that is the full set of permissions
`ig-pulse` needs.

### 3. Credentials

```bash
cp .env.example .env
```

Fill in:

| Variable | What it is |
|---|---|
| `APIFY_TOKEN` | the token from step 2 |
| `PGHOST` / `PGPORT` | Postgres host and port (`localhost` / `5432` locally) |
| `PGDATABASE` | database name (`igpulse`) |
| `PGUSER` / `PGPASSWORD` | Postgres credentials |

`.env` is gitignored. It should never be committed — if it ever is, rotate the
token in the Apify console immediately rather than just deleting the file, as
the value stays in git history.

For a scheduled deployment, set the same variables as real environment
variables (systemd unit, container env, CI secret) rather than shipping a
`.env` file. `load_dotenv()` will not override variables already present in the
environment, so both work side by side.

### 4. Database

`createdb` ships with Postgres, so `command not found` means Postgres itself
isn't installed. Pick one:

**Docker** — cleanest, matches the `.env.example` defaults exactly, and is the
same thing you'd deploy:

```bash
docker run --name igpulse-pg -d -p 5432:5432 \
  -e POSTGRES_USER=igpulse -e POSTGRES_PASSWORD=igpulse \
  -e POSTGRES_DB=igpulse postgres:16
```

Then `.env` needs no edits beyond `PGPASSWORD=igpulse`. Restart later with
`docker start igpulse-pg`.

**Homebrew** — no Docker needed. Run these **one line at a time**;
`brew install` asks a y/n question, and a pasted block feeds the next command
into that prompt.

```bash
brew install postgresql@16
brew services start postgresql@16
echo 'export PATH="/opt/homebrew/opt/postgresql@16/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc
createuser -s igpulse
createdb -O igpulse igpulse
```

Two things to know:

- The formula is **keg-only**, so without that PATH line `createdb` stays
  missing even after a successful install.
- Homebrew creates a superuser named after **your macOS account**, not
  `igpulse` or `postgres`. The `createuser`/`createdb` pair above adds the role
  the shipped `.env.example` expects, so no `.env` edit is needed. Local
  connections use trust auth, so `PGPASSWORD` is ignored — leave it as-is.

Skipping `createuser` is what produces
`FATAL: role "igpulse" does not exist`.

**Postgres.app** — download from postgresapp.com, drag to Applications, click
Initialize, then run the same `createuser`/`createdb` pair.

Verify before moving on:

```bash
python run.py test-connection
```

### 5. Verify before spending anything

```bash
python run.py validate
python run.py test-connection
python run.py init-db
```

- `validate` — config only, no network at all
- `test-connection` — Apify auth, actor reachability, Postgres, schema state
- `init-db` — apply schema, sync allowlist

`test-connection` costs nothing. `/users/me` and the actor metadata reads are
not actor runs, so no compute units are consumed. It confirms four things: the
token authenticates, all three configured actors resolve from your account,
Postgres is reachable, and the schema is applied. Expected output:

```
Apify
  token          : apify_api…9f2c
  authenticated  : yes (username: yourname)
  plan           : PERSONAL
  actor apify~instagram-scraper                  reachable
  actor apify~instagram-comment-scraper          reachable
  actor easyapi~text-sentiment-analysis          reachable

Postgres
  connected      : yes (igpulse)
  server         : PostgreSQL 16.2
  schema applied : yes

All checks passed. Safe to run `python run.py pipeline`.
```

A `NOT FOUND` on an actor usually means it was renamed or unpublished in the
store — change the ID in `settings.yaml` rather than editing code.

### 6. Fill in your targets

`config/narratives.yaml` and `config/public_figures.yaml` ship empty on
purpose. A starter set of issue-shaped narratives for Indian political
discourse is in `config/examples/narratives.india-issues.yaml` — copy it and
cut it down rather than running it whole; at default volumes the full file is
roughly $2,100 per cycle.

`validate` lists every search term it would use, and `plan` prices them:

```bash
python run.py validate
python run.py plan
```

### Cost control before the first real run

Apify bills per result. The Instagram Scraper is around **$1.50 per 1,000
results** and the sentiment actor around **$2.99 per 1,000**, so a careless
first run gets expensive fast. Before running `pipeline` in anger:

- Set `ingest.narrative.results_per_term` to something small (25) and
  `comments_per_post` to 10 for a first pass.
- Run one lens alone: `python run.py ingest --lens narrative`.
- Check actual consumption in the Apify console, then scale the numbers up.

`validate` prints how many search terms are configured — multiply that by
`results_per_term` for a rough upper bound on results per run.

## Running

```bash
python run.py pipeline
python run.py ingest --lens narrative
python run.py analyze --run-id 42
python run.py report  --run-id 42
python run.py purge
```

`pipeline` runs ingest, analyse, report and the retention purge in sequence.
The rest let you run one stage at a time.

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
tests/           111 tests; 19 need Postgres, the rest need nothing
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
