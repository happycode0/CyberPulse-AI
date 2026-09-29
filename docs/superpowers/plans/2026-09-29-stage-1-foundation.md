# CyberPulse-AI Stage 1 — Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A deterministic collection-to-publication pipeline that turns ~60 live security feeds into canonical events in PostgreSQL and renders them on a cyberpunk HUD published to GitHub Pages — with no AI and no Proxmox involved.

**Architecture:** A Python 3.13 worker container plus a PostgreSQL 17 + pgvector container, orchestrated by Docker Compose and runnable on a laptop. The worker fetches sources listed in `config/sources.yaml` through a conditional-request HTTP layer, parses them into `NormalisedItem`s, resolves those into canonical `Event` rows through a cheap-to-expensive deterministic ladder, scores them, and emits validated static JSON. A static site reads that JSON directly; a GitHub Actions workflow deploys it.

**Tech Stack:** Python 3.13, Pydantic v2, SQLAlchemy Core + raw SQL migrations, httpx, feedparser, PyYAML, jsonschema, pytest, PostgreSQL 17 (`pgvector/pgvector:0.8.6-pg17-trixie`), vanilla HTML/CSS/JS, d3-geo + topojson-client as ES modules, GitHub Actions.

**Spec:** [`PLAN.md`](../../../PLAN.md) — Stage 1 is defined in §9; the data model in §5; collection in §6; the UI in §8.

## Global Constraints

- Python **3.13**; `strands-agents` is **not** a Stage 1 dependency.
- PostgreSQL image is exactly `pgvector/pgvector:0.8.6-pg17-trixie`; data volume mounts at `/var/lib/postgresql/data` (the PG17 layout — PG18 moved it, do not use PG18).
- `pgvector` extension is created but **unused** in Stage 1. Deterministic matching only. No embeddings.
- **No AI calls anywhere in Stage 1.** No OpenRouter, no Tavily.
- All timestamps stored as UTC `timestamptz`; all display in `Australia/Sydney`.
- Australian English in all user-facing copy ("normalise", "organisation", "prioritise").
- Secrets come only from environment variables read through `worker/settings.py`. Never hardcoded, never logged, never written to `data/`.
- Public JSON must validate against `schemas/event.schema.json` before any write to `data/`.
- Site has **no build step**: plain HTML/CSS/JS, third-party files vendored under `site/assets/vendor/`.
- Every looping animation sits inside `@media (prefers-reduced-motion: no-preference)`.
- The site never displays the word "LIVE"; it displays `LAST COMPLETED COLLECTION <ts> UTC`.
- Schema/pipeline/scoring versions are literals in `worker/version.py`: `SCHEMA_VERSION = "1"`, `PIPELINE_VERSION = "1.0.0"`, `SCORING_VERSION = "1"`, `ENRICHMENT_VERSION = "0"` (no enrichment yet).
- Every HTTP request sends `User-Agent: CyberPulse-AI/1.0 (+https://github.com/happycode0/CyberPulse-AI)`.

## Review Focus

Five failure modes the spec implies that no task's happy-path tests would exercise. Each has a test assigned to the task that owns the code.

1. **A feed returns HTTP 200 with valid XML but its newest item is months old.** Proven real: the Google Security Blog moved and its old feed still serves 200 with fresh-looking metadata. Health status must become `stale`, not `ok`. → Task 11.
2. **Non-standard and 2-digit-year dates.** Proven real: CISA emits `Sun, 27 Sep 26`, CrowdStrike emits `Sep 28, 2026 00:00:00-0400`. Both must parse to correct UTC, not 1926 and not an exception. → Task 6.
3. **An item with no publication date, or one dated in the future.** Must not crash and must not out-rank genuinely fresh items. → Task 8.
4. **Recurring near-identical titles are distinct events.** "Microsoft Patch Tuesday" and "SANS weekly roundup" repeat monthly/weekly with near-identical titles; title similarity must not merge them across periods. → Task 9.
5. **A secret value present in generated output.** The publisher must fail closed and write nothing. → Task 12.

---

## File Structure

| Path | Responsibility |
|---|---|
| `docker-compose.yml`, `Dockerfile.worker`, `.env.example`, `.gitignore` | Local stack |
| `worker/settings.py` | Env-var loading; the only place secrets enter |
| `worker/version.py` | Version literals |
| `worker/models.py` | Pydantic models: `RawItem`, `NormalisedItem`, `Event`, `SourceConfig`, `SourceHealth`, `RunSummary` |
| `schemas/event.schema.json`, `schemas/live.schema.json` | Published JSON Schema |
| `worker/db/migrate.py`, `worker/db/migrations/*.sql`, `worker/db/session.py` | Schema + connection |
| `worker/db/events.py`, `worker/db/sources.py`, `worker/db/runs.py` | Data access |
| `config/sources.yaml`, `config/scoring.yaml`, `config/categories.yaml` | Versioned configuration |
| `worker/sources/registry.py` | Load + validate the registry |
| `worker/collectors/http.py` | Conditional requests, retries, raw cache |
| `worker/collectors/feed.py` | RSS/Atom → `RawItem` |
| `worker/collectors/json_api.py` | JSON API → `RawItem` |
| `worker/collectors/dates.py` | Lenient date parsing → UTC |
| `worker/pipeline/normalise.py` | `RawItem` → `NormalisedItem` |
| `worker/pipeline/resolve.py` | The deterministic identity ladder |
| `worker/pipeline/score.py` | Severity, prominence, decay |
| `worker/pipeline/health.py` | Source health + degradation + staleness |
| `worker/pipeline/run.py` | Orchestrator |
| `worker/publish/build.py` | Generate `data/*.json` |
| `worker/publish/validate.py` | JSON Schema gate + secret scan |
| `worker/publish/push.py` | Orphan-branch force push |
| `worker/scheduler.py`, `worker/main.py` | APScheduler lanes + CLI |
| `site/index.html`, `site/event.html`, `site/history.html` | Pages |
| `site/assets/hud.css` | All styling |
| `site/assets/hud.js`, `site/assets/map.js` | Rendering, filtering, visualisations |
| `.github/workflows/pages.yml` | Deployment |
| `tests/` | `unit/`, `integration/`, `fixtures/` |

---

## Task 1: Project scaffolding and settings

**Files:**
- Create: `docker-compose.yml`, `Dockerfile.worker`, `.env.example`, `.gitignore`, `requirements.txt`, `worker/__init__.py`, `worker/version.py`, `worker/settings.py`
- Test: `tests/unit/test_settings.py`

**Interfaces:**
- Consumes: nothing
- Produces: `Settings` (Pydantic `BaseSettings`) with fields `database_url: str`, `openrouter_api_key: str | None`, `tavily_api_key: str | None`, `nvd_api_key: str | None`, `github_token: str | None`, `github_repository: str`, `telegram_bot_token: str | None`, `telegram_chat_id: str | None`, `raw_cache_dir: Path = Path("/var/cache/cyberpulse")`, `data_dir: Path = Path("data")`, `user_agent: str`; `get_settings() -> Settings` (cached). `worker.version` exports `SCHEMA_VERSION`, `PIPELINE_VERSION`, `SCORING_VERSION`, `ENRICHMENT_VERSION`.

- [ ] **Step 1: Write the failing tests**

```python
def test_settings_reads_database_url(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h/db")
    assert get_settings.__wrapped__().database_url == "postgresql://u:p@h/db"

def test_settings_missing_database_url_raises(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValidationError):
        get_settings.__wrapped__()

def test_optional_keys_default_to_none(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h/db")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert get_settings.__wrapped__().openrouter_api_key is None

def test_repr_does_not_leak_secrets(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:secretpw@h/db")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-TOPSECRET")
    text = repr(get_settings.__wrapped__())
    assert "TOPSECRET" not in text and "secretpw" not in text

def test_user_agent_identifies_project(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h/db")
    assert get_settings.__wrapped__().user_agent == (
        "CyberPulse-AI/1.0 (+https://github.com/happycode0/CyberPulse-AI)"
    )
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/test_settings.py -v`
Expected: FAIL — `ModuleNotFoundError: worker.settings`

- [ ] **Step 3: Implement**

`worker/settings.py`: `Settings(BaseSettings)` with the fields above; secret fields typed `SecretStr | None` so Pydantic redacts them in `repr`. `user_agent` defaults to the Global Constraints value. `get_settings()` wrapped in `functools.lru_cache`.

`worker/version.py`: the four literals from Global Constraints.

`docker-compose.yml`: services `db` (`pgvector/pgvector:0.8.6-pg17-trixie`, volume `pgdata:/var/lib/postgresql/data`, `shm_size: 256mb`, `pg_isready` healthcheck, **no host port published**) and `worker` (built from `Dockerfile.worker`, `env_file: .env`, `depends_on: db: condition: service_healthy`, mounts the repo and a `rawcache` volume at `/var/cache/cyberpulse`, `restart: unless-stopped`).

`Dockerfile.worker`: `python:3.13-slim`, non-root user, `requirements.txt` installed first for layer caching.

`.gitignore` must include `.env`, `__pycache__/`, `.pytest_cache/`, `*.pyc`, `.venv/`.

`.env.example`: every variable name with safe placeholders and a comment block recording the model IDs and prices verified on 2026-09-29 (`PLAN.md` §7.1), noting they must be re-verified before Stage 2.

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/unit/test_settings.py -v && docker compose config >/dev/null && echo COMPOSE_OK`
Expected: PASS and `COMPOSE_OK`

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat: project scaffolding, settings and compose stack"
```

---

## Task 2: Event schema and domain models

**Files:**
- Create: `schemas/event.schema.json`, `schemas/live.schema.json`, `worker/models.py`
- Test: `tests/unit/test_models.py`

**Interfaces:**
- Consumes: `worker.version`
- Produces:
  - `RawItem`: `source_id, url, guid: str | None, title, raw_summary: str | None, published: datetime | None, fetched_at: datetime, payload_hash: str`
  - `NormalisedItem`: `source_id, url, canonical_url, guid, title, normalised_title, summary, published, fetched_at, cves: list[str], url_hash, title_hash, tokens: frozenset[str]`
  - `Event`: every field in `PLAN.md` §5, with `Severity`, `EventStatus`, `SeveritySource`, `EvidenceClass`, `MaterialChange`, `AiSubdomain`, `RelationshipType` as `StrEnum`
  - Nested models used by `Event`: `Risk`, `AuRelevance`, `CveRef`, `CvssScore`, `EpssScore`, `KevEntry`, `MitreTechnique`, `Claim`, `SourceRef`, `TimelineEntry`, `Relationship`
  - `SourceConfig`, `SourceHealth`, `RunSummary`
  - `Event.model_dump_public() -> dict` — the publication shape, excluding internal-only fields

- [ ] **Step 1: Write the failing tests**

```python
def test_event_requires_event_id_pattern():
    with pytest.raises(ValidationError):
        Event(event_id="nope", title="t", summary="s", first_seen=NOW, last_seen=NOW)

def test_event_id_accepts_canonical_form():
    assert Event(event_id="evt-2026-000123", ...).event_id == "evt-2026-000123"

def test_severity_unknown_is_valid_and_is_not_low():
    assert Severity.UNKNOWN in Severity and Severity.UNKNOWN != Severity.LOW

def test_event_defaults_versions_from_version_module():
    e = Event(event_id="evt-2026-000001", ...)
    assert (e.schema_version, e.pipeline_version, e.scoring_version) == (SCHEMA_VERSION, PIPELINE_VERSION, SCORING_VERSION)

def test_public_dump_validates_against_json_schema():
    jsonschema.validate(Event(...).model_dump_public(), json.load(open("schemas/event.schema.json")))

def test_cvss_absent_is_unknown_not_zero():
    cve = CveRef(id="CVE-2026-88772")
    assert cve.cvss is None and cve.epss.status == "unknown"

def test_naive_datetime_is_rejected():
    with pytest.raises(ValidationError):
        Event(event_id="evt-2026-000001", first_seen=datetime(2026, 9, 29), ...)
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/test_models.py -v`
Expected: FAIL — `ModuleNotFoundError: worker.models`

- [ ] **Step 3: Implement**

`worker/models.py` with the models and enums above. `event_id` constrained to `^evt-\d{4}-\d{6}$`. All datetime fields validated as timezone-aware (reject naive). Version fields default from `worker.version`.

`schemas/event.schema.json` (draft 2020-12) mirroring `Event.model_dump_public()`, with `"additionalProperties": false` on the root and every enum listed explicitly from `PLAN.md` §5.

`schemas/live.schema.json`: `{generated_at, last_completed_collection, pipeline_version, counts, events: [event]}`.

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/unit/test_models.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat: event schema and domain models"
```

---

## Task 3: Database schema and migrations

**Files:**
- Create: `worker/db/__init__.py`, `worker/db/session.py`, `worker/db/migrate.py`, `worker/db/migrations/001_initial.sql`
- Test: `tests/integration/test_migrate.py`

**Interfaces:**
- Consumes: `get_settings()`
- Produces: `get_engine() -> Engine`; `run_migrations(engine) -> list[str]` returning applied filenames; `current_version(engine) -> int`

- [ ] **Step 1: Write the failing tests**

```python
def test_migrations_create_expected_tables(pg_engine):
    run_migrations(pg_engine)
    names = set(inspect(pg_engine).get_table_names())
    assert {"events","event_sources","event_timeline","event_relationships","claims",
            "evidence","cves","cve_scores","mitre_techniques","source_registry",
            "source_health","source_lineage","runs","trends","cost_ledger",
            "followup_tasks","incidents","schema_migrations"} <= names

def test_migrations_are_idempotent(pg_engine):
    run_migrations(pg_engine)
    assert run_migrations(pg_engine) == []

def test_required_extensions_present(pg_engine):
    run_migrations(pg_engine)
    exts = {r[0] for r in pg_engine.connect().execute(text("select extname from pg_extension"))}
    assert {"pg_trgm", "vector"} <= exts

def test_event_id_is_unique(pg_engine):
    run_migrations(pg_engine)
    insert_event(pg_engine, "evt-2026-000001")
    with pytest.raises(IntegrityError):
        insert_event(pg_engine, "evt-2026-000001")

def test_url_hash_index_exists(pg_engine):
    run_migrations(pg_engine)
    idx = {i["name"] for i in inspect(pg_engine).get_indexes("event_sources")}
    assert "ix_event_sources_url_hash" in idx
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/integration/test_migrate.py -v`
Expected: FAIL — `ModuleNotFoundError: worker.db.migrate`

- [ ] **Step 3: Implement**

`001_initial.sql`: `CREATE EXTENSION IF NOT EXISTS pg_trgm; CREATE EXTENSION IF NOT EXISTS vector;` then every table from `PLAN.md` §5. Include a `schema_migrations(filename text primary key, applied_at timestamptz default now())` ledger. Indexes: `ix_event_sources_url_hash`, `ix_event_sources_guid`, a GIN trigram index on `events.normalised_title`, and btree indexes on `events.status`, `events.prominence`, `events.last_material_update`. All timestamps `timestamptz`.

`migrate.py`: apply `migrations/*.sql` in filename order inside a transaction each, skipping any already in the ledger. Runnable as `python -m worker.db.migrate`.

Add a `pg_engine` fixture in `tests/conftest.py` that skips the module when `DATABASE_URL` is unset, and creates/drops a scratch database per session.

- [ ] **Step 4: Run to verify pass**

Run: `docker compose up -d db && docker compose run --rm worker pytest tests/integration/test_migrate.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat: database schema and migration runner"
```

---

## Task 4: Source registry

**Files:**
- Create: `config/sources.yaml`, `config/categories.yaml`, `worker/sources/__init__.py`, `worker/sources/registry.py`
- Test: `tests/unit/test_registry.py`

**Interfaces:**
- Consumes: `SourceConfig` from Task 2
- Produces: `load_registry(path: Path) -> list[SourceConfig]`; `sources_for_lane(sources, lane: Lane) -> list[SourceConfig]`; `Lane` StrEnum (`FAST`, `NORMAL`, `DEEP`)

- [ ] **Step 1: Write the failing tests**

```python
def test_registry_loads_and_every_source_is_valid():
    assert len(load_registry(Path("config/sources.yaml"))) >= 50

def test_source_ids_are_unique():
    ids = [s.id for s in load_registry(Path("config/sources.yaml"))]
    assert len(ids) == len(set(ids))

def test_duplicate_id_raises():
    with pytest.raises(ValueError, match="duplicate source id"):
        load_registry(fixture("sources_duplicate_id.yaml"))

def test_fast_lane_contains_acsc_alerts_and_kev():
    fast = {s.id for s in sources_for_lane(load_registry(Path("config/sources.yaml")), Lane.FAST)}
    assert {"acsc_alerts", "cisa_kev"} <= fast

def test_disabled_sources_are_excluded_from_every_lane():
    reg = load_registry(Path("config/sources.yaml"))
    assert all(s.enabled for lane in Lane for s in sources_for_lane(reg, lane))

def test_known_blocked_sources_are_disabled_with_a_reason():
    by_id = {s.id: s for s in load_registry(Path("config/sources.yaml"))}
    for sid in ("securityweek", "x_security_search"):
        assert by_id[sid].enabled is False and by_id[sid].notes
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/test_registry.py -v`
Expected: FAIL — `ModuleNotFoundError: worker.sources.registry`

- [ ] **Step 3: Implement**

`config/sources.yaml` containing every feed validated on 2026-09-29, each with `id, name, type, region, category, class, priority, lane, enabled, url, parser, expected_frequency, notes`. Use the exact validated URLs, including the non-obvious ones:

- `acsc_alerts` `https://www.cyber.gov.au/rss/alerts` · `acsc_advisories` `/rss/advisories` · `acsc_news` `/rss/news` · `acsc_publications` `/rss/publications`
- `cisa_advisories` `https://www.cisa.gov/cybersecurity-advisories/all.xml` · `cisa_ics` `/ics-advisories.xml` · `cisa_news` `https://www.cisa.gov/news.xml`
- `cisa_kev` `https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json` (type `json_api`)
- `cyber_daily` `https://www.cyberdaily.au/news?format=feed&type=rss` (non-standard path — `/feed` and `/rss` are 404)
- `itnews_security` `https://www.itnews.com.au/RSS/rss.ashx?type=Category&ID=32` (ID 32; **ID 37 is Business**)
- `dta` `https://www.dta.gov.au/news-and-blogs/latest/feed/all` (**not** `/rss.xml`, which is stale)
- `securitybrief_au` `https://securitybrief.com.au/feed` · `acsm` `https://australiancybersecuritymagazine.com.au/feed/` · `asm` `https://australiansecuritymagazine.com.au/feed/` · `triskele` `https://www.triskelelabs.com/resources/rss.xml` · `innovationaus` `https://www.innovationaus.com/feed/`
- `thn` `https://feeds.feedburner.com/TheHackersNews` · `bleepingcomputer` `https://www.bleepingcomputer.com/feed/` · `krebs` `https://krebsonsecurity.com/feed/` · `darkreading` `https://www.darkreading.com/rss.xml` · `therecord` `https://therecord.media/feed` (**5 items only → `expected_frequency: hourly`**) · `cyberwire` `https://thecyberwire.com/feeds/rss.xml` · `schneier` `https://www.schneier.com/feed/atom/` · `grahamcluley` `https://grahamcluley.com/feed/` · `sans_isc` `https://isc.sans.edu/rssfeed_full.xml`
- `microsoft_security` `https://www.microsoft.com/en-us/security/blog/feed/` · `crowdstrike` `https://www.crowdstrike.com/en-us/blog/feed` · `unit42` `https://unit42.paloaltonetworks.com/feed/` · `malwarebytes` `https://www.malwarebytes.com/blog/feed/index.xml`
- `simonwillison` `https://simonwillison.net/atom/everything/` · `embracethered` `https://embracethered.com/blog/index.xml` · `openai_news` `https://openai.com/news/rss.xml` · `deepmind` `https://deepmind.google/blog/rss.xml` · `google_security` `https://blog.google/security/rss/` (note the move) · `ms_ai_security` `https://www.microsoft.com/en-us/security/blog/topic/ai-and-machine-learning/feed/` · `owasp_genai` `https://genai.owasp.org/feed/` · `huggingface` `https://huggingface.co/blog/feed.xml` · `arxiv_cs_cr` `https://rss.arxiv.org/rss/cs.CR`
- `enabled: false` with a `notes` reason: `securityweek` (Cloudflare 403), `x_security_search` (cost-gated), `asd`/`msrc_blog`/`anthropic_news`/`oaic` (no feed — Stage 3 `web_page`/`sitemap` adapters), `acsc_threats` (feed valid but empty), `acsc_advice` (evergreen, migration-dated)

`registry.py`: load YAML into `SourceConfig`, raise `ValueError("duplicate source id: …")` on collision, and exclude disabled sources from `sources_for_lane`.

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/unit/test_registry.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat: source registry with live-validated feeds"
```

---

## Task 5: HTTP layer with conditional requests

**Files:**
- Create: `worker/collectors/__init__.py`, `worker/collectors/http.py`
- Test: `tests/unit/test_http.py`

**Interfaces:**
- Consumes: `get_settings()`, `SourceConfig`
- Produces: `FetchResult(status: FetchStatus, body: bytes | None, etag: str | None, last_modified: str | None, duration_ms: int, error: str | None)`; `FetchStatus` StrEnum (`OK`, `NOT_MODIFIED`, `ERROR`, `TIMEOUT`, `SKIPPED`); `async fetch(client, source, etag=None, last_modified=None) -> FetchResult`; `cache_raw(source_id, body) -> Path`; `prune_cache(ttl_days: int) -> int`

- [ ] **Step 1: Write the failing tests**

```python
async def test_fetch_ok_returns_body_and_validators(respx_mock):
    respx_mock.get(URL).respond(200, content=b"<rss/>", headers={"ETag": '"abc"'})
    r = await fetch(client, src)
    assert (r.status, r.body, r.etag) == (FetchStatus.OK, b"<rss/>", '"abc"')

async def test_sends_conditional_headers_when_validators_known(respx_mock):
    route = respx_mock.get(URL).respond(304)
    await fetch(client, src, etag='"abc"', last_modified="Mon, 28 Sep 2026 00:00:00 GMT")
    h = route.calls[0].request.headers
    assert h["if-none-match"] == '"abc"' and "if-modified-since" in h

async def test_304_returns_not_modified_with_no_body(respx_mock):
    respx_mock.get(URL).respond(304)
    r = await fetch(client, src, etag='"abc"')
    assert (r.status, r.body) == (FetchStatus.NOT_MODIFIED, None)

async def test_user_agent_identifies_project(respx_mock):
    route = respx_mock.get(URL).respond(200, content=b"x")
    await fetch(client, src)
    assert "CyberPulse-AI" in route.calls[0].request.headers["user-agent"]

async def test_403_is_error_not_exception(respx_mock):
    respx_mock.get(URL).respond(403)
    r = await fetch(client, src)
    assert r.status is FetchStatus.ERROR and "403" in r.error

async def test_timeout_maps_to_timeout_status(respx_mock):
    respx_mock.get(URL).mock(side_effect=httpx.ReadTimeout("slow"))
    assert (await fetch(client, src)).status is FetchStatus.TIMEOUT

async def test_5xx_is_retried_then_errors(respx_mock):
    route = respx_mock.get(URL).respond(503)
    assert (await fetch(client, src)).status is FetchStatus.ERROR
    assert route.call_count == 3

def test_cache_raw_writes_and_prune_removes_expired(tmp_path):
    p = cache_raw("acsc_alerts", b"<rss/>")
    assert p.read_bytes() == b"<rss/>"
    os.utime(p, (0, 0))
    assert prune_cache(ttl_days=7) == 1 and not p.exists()
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/test_http.py -v`
Expected: FAIL — `ModuleNotFoundError: worker.collectors.http`

- [ ] **Step 3: Implement**

`httpx.AsyncClient` based. Send `If-None-Match` / `If-Modified-Since` when validators are supplied. Retry 5xx and timeouts up to 3 attempts with exponential backoff; never retry 4xx. Map exceptions to `FetchStatus`, never propagate. Cap response size at 32 MiB. Cache bodies under `raw_cache_dir/<source_id>/<utc-date>/<sha256[:16]>.raw`.

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/unit/test_http.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat: HTTP layer with conditional requests and raw cache"
```

---

## Task 6: Lenient date parsing and feed collector

**Files:**
- Create: `worker/collectors/dates.py`, `worker/collectors/feed.py`
- Test: `tests/unit/test_dates.py`, `tests/unit/test_feed.py`
- Create fixtures: `tests/fixtures/feeds/{acsc_alerts,cisa_advisories,crowdstrike,securitybrief,arxiv,malformed,empty}.xml`

**Interfaces:**
- Consumes: `RawItem`, `SourceConfig`, `FetchResult`
- Produces: `parse_date(value: str | None) -> datetime | None` (always UTC-aware); `parse_feed(source: SourceConfig, body: bytes) -> list[RawItem]`

- [ ] **Step 1: Write the failing tests**

Covers **Review Focus #2** — the 2-digit-year and non-standard formats found live.

```python
@pytest.mark.parametrize("raw,expected", [
    ("Sun, 27 Sep 26 12:00:00 +0000", datetime(2026,9,27,12,0, tzinfo=UTC)),   # CISA 2-digit year
    ("Sep 28, 2026 00:00:00-0400",    datetime(2026,9,28,4,0,  tzinfo=UTC)),   # CrowdStrike
    ("Tue, 29 Sep 2026 21:30:00 +1300", datetime(2026,9,29,8,30, tzinfo=UTC)), # SecurityBrief NZ
    ("2026-09-29T11:57:00Z",          datetime(2026,9,29,11,57, tzinfo=UTC)),
    ("2026-09-29 11:57",              datetime(2026,9,29,11,57, tzinfo=UTC)),  # assume UTC
])
def test_parse_date_handles_real_world_formats(raw, expected):
    assert parse_date(raw) == expected

def test_two_digit_year_is_not_interpreted_as_1926():
    assert parse_date("Sun, 27 Sep 26 12:00:00 +0000").year == 2026

@pytest.mark.parametrize("bad", [None, "", "not a date", "Thu, 32 Xxx 2026"])
def test_parse_date_returns_none_rather_than_raising(bad):
    assert parse_date(bad) is None

def test_parsed_dates_are_always_timezone_aware():
    assert parse_date("2026-09-29 11:57").tzinfo is not None

def test_parse_feed_extracts_items_from_acsc():
    items = parse_feed(acsc_src, fixture_bytes("acsc_alerts.xml"))
    assert len(items) == 7 and all(i.url.startswith("https://") for i in items)

def test_parse_feed_prefers_guid_when_present():
    assert parse_feed(cisa_src, fixture_bytes("cisa_advisories.xml"))[0].guid

def test_parse_feed_tolerates_malformed_xml():
    assert parse_feed(src, fixture_bytes("malformed.xml")) == []

def test_parse_feed_on_empty_feed_returns_empty_list():
    assert parse_feed(src, fixture_bytes("empty.xml")) == []

def test_payload_hash_is_stable_across_calls():
    a, b = parse_feed(src, B)[0].payload_hash, parse_feed(src, B)[0].payload_hash
    assert a == b
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/test_dates.py tests/unit/test_feed.py -v`
Expected: FAIL — `ModuleNotFoundError: worker.collectors.dates`

- [ ] **Step 3: Implement**

`parse_date`: try `email.utils.parsedate_to_datetime`, then `datetime.fromisoformat`, then an explicit format list covering the fixtures above; normalise 2-digit years by pivoting on the current century; assume UTC when no offset is present; return `None` on failure and never raise.

`parse_feed`: `feedparser` based, tolerating `bozo` feeds by returning whatever entries parsed. Item identity prefers `id`/`guid`, falling back to `link`. `payload_hash` is `sha256` of the canonicalised entry.

Build the fixtures from the real responses captured during validation — trimmed, but with original date formats preserved verbatim.

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/unit/test_dates.py tests/unit/test_feed.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat: lenient date parsing and RSS/Atom collector"
```

---

## Task 7: JSON API collector

**Files:**
- Create: `worker/collectors/json_api.py`
- Test: `tests/unit/test_json_api.py`
- Create fixture: `tests/fixtures/kev_sample.json`

**Interfaces:**
- Consumes: `RawItem`, `SourceConfig`
- Produces: `parse_json_api(source: SourceConfig, body: bytes) -> list[RawItem]`, driven by the source's `parser` name via a registry `JSON_PARSERS: dict[str, Callable]`

- [ ] **Step 1: Write the failing tests**

```python
def test_kev_parser_extracts_vulnerabilities():
    items = parse_json_api(kev_src, fixture_bytes("kev_sample.json"))
    assert len(items) == 3 and all(i.title for i in items)

def test_kev_item_url_points_to_cisa_catalog():
    assert "cisa.gov" in parse_json_api(kev_src, fixture_bytes("kev_sample.json"))[0].url

def test_kev_guid_is_the_cve_id():
    assert parse_json_api(kev_src, fixture_bytes("kev_sample.json"))[0].guid == "CVE-2026-88772"

def test_unknown_parser_name_raises():
    with pytest.raises(KeyError, match="no JSON parser named"):
        parse_json_api(replace(kev_src, parser="nonexistent"), b"{}")

def test_invalid_json_returns_empty_list():
    assert parse_json_api(kev_src, b"{not json") == []

def test_missing_expected_key_returns_empty_list():
    assert parse_json_api(kev_src, b'{"unexpected": 1}') == []
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/test_json_api.py -v`
Expected: FAIL — `ModuleNotFoundError: worker.collectors.json_api`

- [ ] **Step 3: Implement**

`JSON_PARSERS` maps parser name → callable. Implement `kev` for Stage 1: read `vulnerabilities[]`, using `cveID` as `guid`, `"{vendorProject} {product}: {vulnerabilityName}"` as title, `shortDescription` as summary, `dateAdded` as published. Return `[]` on malformed JSON or a missing top-level key; raise `KeyError` only for an unregistered parser name (a configuration error, which should be loud).

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/unit/test_json_api.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat: JSON API collector with KEV parser"
```

---

## Task 8: Normalisation

**Files:**
- Create: `worker/pipeline/__init__.py`, `worker/pipeline/normalise.py`
- Test: `tests/unit/test_normalise.py`

**Interfaces:**
- Consumes: `RawItem`, `NormalisedItem`
- Produces: `normalise(item: RawItem, *, now: datetime) -> NormalisedItem`; `canonical_url(url: str) -> str`; `normalise_title(title: str) -> str`; `extract_cves(text: str) -> list[str]`; `tokenise(title: str) -> frozenset[str]`

- [ ] **Step 1: Write the failing tests**

Covers **Review Focus #3** — missing and future dates.

```python
@pytest.mark.parametrize("raw,expected", [
    ("https://x.com/a?utm_source=rss&utm_medium=feed", "https://x.com/a"),
    ("https://x.com/a/?fbclid=1", "https://x.com/a"),
    ("http://X.COM/A", "http://x.com/A"),        # host lowered, path preserved
    ("https://x.com/a#section", "https://x.com/a"),
])
def test_canonical_url_strips_tracking_and_normalises_host(raw, expected):
    assert canonical_url(raw) == expected

def test_normalise_title_is_case_and_punctuation_insensitive():
    assert normalise_title("Critical RCE in Acme!") == normalise_title("critical rce in acme")

def test_extract_cves_finds_all_and_dedupes_and_uppercases():
    assert extract_cves("cve-2026-1 and CVE-2026-1 and CVE-2025-12345") == ["CVE-2025-12345", "CVE-2026-1"]

def test_extract_cves_ignores_malformed_ids():
    assert extract_cves("CVE-26-1 CVE-2026 CVEX-2026-1") == []

def test_missing_published_falls_back_to_fetched_at():
    n = normalise(replace(item, published=None, fetched_at=NOW), now=NOW)
    assert n.published == NOW and n.published_is_estimated is True

def test_future_published_is_clamped_to_now():
    n = normalise(replace(item, published=NOW + timedelta(days=30)), now=NOW)
    assert n.published == NOW and n.published_is_estimated is True

def test_url_hash_matches_for_urls_differing_only_by_tracking_params():
    a = normalise(replace(item, url="https://x.com/a?utm_source=rss"), now=NOW)
    b = normalise(replace(item, url="https://x.com/a"), now=NOW)
    assert a.url_hash == b.url_hash
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/test_normalise.py -v`
Expected: FAIL — `ModuleNotFoundError: worker.pipeline.normalise`

- [ ] **Step 3: Implement**

Add `published_is_estimated: bool` to `NormalisedItem` (Task 2 model gains the field). `canonical_url` strips a fixed tracking-parameter set (`utm_*`, `fbclid`, `gclid`, `mc_cid`, `mc_eid`, `ref`), removes the fragment and any trailing slash, and lowercases scheme and host only. `normalise_title` casefolds, strips punctuation, and collapses whitespace. `extract_cves` uses `CVE-\d{4}-\d{4,7}` case-insensitively, returning a sorted unique uppercase list. `tokenise` drops stopwords and tokens shorter than 3 characters. Missing or future `published` falls back to `fetched_at` with `published_is_estimated = True`.

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/unit/test_normalise.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat: item normalisation with CVE extraction"
```

---

## Task 9: Deterministic event resolution

**Files:**
- Create: `worker/pipeline/resolve.py`, `worker/db/events.py`
- Test: `tests/unit/test_resolve.py`, `tests/integration/test_resolve_db.py`

**Interfaces:**
- Consumes: `NormalisedItem`, `Event`, `get_engine()`
- Produces: `Resolution(decision: Decision, event_id: str | None, method: str, score: float)`; `Decision` StrEnum (`NEW_EVENT`, `UPDATE_EXISTING`, `DUPLICATE`, `RELATED_BUT_DISTINCT`, `AMBIGUOUS`); `resolve(item, candidates: list[Event]) -> Resolution`; `find_candidates(conn, item) -> list[Event]`; `next_event_id(conn, year: int) -> str`

- [ ] **Step 1: Write the failing tests**

Covers **Review Focus #4** — recurring titles must not false-merge.

```python
def test_identical_url_hash_is_duplicate():
    r = resolve(item, [event_with_source(url_hash=item.url_hash)])
    assert (r.decision, r.method) == (Decision.DUPLICATE, "url_hash")

def test_matching_guid_is_duplicate():
    assert resolve(item, [event_with_source(guid=item.guid)]).method == "guid"

def test_same_normalised_title_within_window_updates_existing():
    r = resolve(item, [event(normalised_title=item.normalised_title, first_seen=item.published - timedelta(hours=6))])
    assert r.decision is Decision.UPDATE_EXISTING

def test_shared_cve_and_high_token_overlap_updates_existing():
    r = resolve(item_with_cve("CVE-2026-88772"), [event_with_cve("CVE-2026-88772", tokens=item.tokens)])
    assert r.decision is Decision.UPDATE_EXISTING and r.method == "cve+tokens"

def test_no_signal_creates_new_event():
    assert resolve(item, []).decision is Decision.NEW_EVENT

def test_recurring_title_in_a_different_period_is_a_new_event():
    """Patch Tuesday repeats monthly with a near-identical title."""
    old = event(normalised_title="microsoft patch tuesday september 2026",
                first_seen=datetime(2026, 9, 8, tzinfo=UTC))
    new = normalised_item(title="Microsoft Patch Tuesday October 2026",
                          published=datetime(2026, 10, 13, tzinfo=UTC))
    assert resolve(new, [old]).decision is Decision.NEW_EVENT

def test_title_similarity_alone_beyond_the_window_is_not_a_match():
    old = event(normalised_title=item.normalised_title, first_seen=item.published - timedelta(days=45))
    assert resolve(item, [old]).decision is Decision.NEW_EVENT

def test_borderline_similarity_is_ambiguous_not_a_guess():
    assert resolve(item, [event(token_overlap_ratio=0.55)]).decision is Decision.AMBIGUOUS

def test_next_event_id_is_sequential_and_zero_padded(pg_engine):
    assert next_event_id(conn, 2026) == "evt-2026-000001"
    insert_event(conn, "evt-2026-000001")
    assert next_event_id(conn, 2026) == "evt-2026-000002"
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/test_resolve.py -v`
Expected: FAIL — `ModuleNotFoundError: worker.pipeline.resolve`

- [ ] **Step 3: Implement**

`resolve` walks the ladder in cost order, stopping at the first confident answer: `url_hash` → `guid` → `canonical_url` → `title_hash` within a **14-day window** → shared CVE plus token overlap ≥ 0.5 → token overlap ≥ 0.75 with date proximity ≤ 72 h. Overlap in 0.45–0.75 with no other signal returns `AMBIGUOUS`, which Stage 1 treats as `NEW_EVENT` and flags for Stage 2 LLM adjudication. The date window is what stops recurring-title false merges, so it is mandatory on every title-only path.

`find_candidates` queries by `url_hash`, `guid`, `title_hash`, shared CVE, and `similarity(normalised_title, :t) > 0.4` using the pg_trgm GIN index, limited to events with `last_seen` inside 30 days.

`next_event_id` allocates from a per-year counter inside the caller's transaction.

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/unit/test_resolve.py tests/integration/test_resolve_db.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat: deterministic event resolution ladder"
```

---

## Task 10: Scoring engine

**Files:**
- Create: `config/scoring.yaml`, `worker/pipeline/score.py`
- Test: `tests/unit/test_score.py`

**Interfaces:**
- Consumes: `Event`, `EvidenceClass`
- Produces: `ScoringConfig.load(path) -> ScoringConfig`; `score_event(event, config, *, now) -> ScoredEvent` setting `risk.urgency`, `risk.confidence`, `risk.novelty`, `risk.prominence`; `independent_confirmations(event) -> int`; `freshness(event, config, now) -> float`

- [ ] **Step 1: Write the failing tests**

```python
def test_severity_base_weights_match_config():
    assert [base_weight(s, cfg) for s in (CRITICAL, HIGH, MEDIUM, LOW)] == [4, 3, 2, 1]

def test_unknown_severity_scores_between_low_and_medium():
    assert base_weight(LOW, cfg) < base_weight(UNKNOWN, cfg) < base_weight(MEDIUM, cfg)

def test_kev_listing_adds_a_bonus():
    assert score_event(kev_event, cfg, now=NOW).risk.urgency > score_event(plain_event, cfg, now=NOW).risk.urgency

def test_independent_confirmations_ignores_shared_lineage():
    e = event(sources=[src("vendor", lineage="L1", independent=True),
                       src("reuters", lineage="L1", independent=False),
                       src("thn",     lineage="L1", independent=False)])
    assert independent_confirmations(e) == 1

def test_independent_confirmations_counts_distinct_lineages():
    e = event(sources=[src("vendor", lineage="L1", independent=True),
                       src("acsc",   lineage="L2", independent=True)])
    assert independent_confirmations(e) == 2

@pytest.mark.parametrize("sev,hours", [(LOW,24),(MEDIUM,24),(HIGH,72),(CRITICAL,168)])
def test_freshness_halves_at_the_configured_half_life(sev, hours):
    e = event(severity=sev, last_material_update=NOW - timedelta(hours=hours))
    assert freshness(e, cfg, NOW) == pytest.approx(0.5, abs=0.01)

def test_decay_resets_on_material_update_not_on_last_seen():
    stale = event(last_material_update=NOW - timedelta(days=7), last_seen=NOW)
    fresh = event(last_material_update=NOW, last_seen=NOW)
    assert freshness(fresh, cfg, NOW) > freshness(stale, cfg, NOW)

def test_prominence_is_bounded_between_zero_and_one():
    assert 0.0 <= score_event(extreme_event, cfg, now=NOW).risk.prominence <= 1.0

def test_scoring_version_is_recorded_on_the_event():
    assert score_event(event(), cfg, now=NOW).scoring_version == SCORING_VERSION
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/test_score.py -v`
Expected: FAIL — `ModuleNotFoundError: worker.pipeline.score`

- [ ] **Step 3: Implement**

`config/scoring.yaml` holds `version`, `severity_weights` (including `unknown: 1.5`), `kev_bonus`, `half_life_hours` per severity, `evidence_class_weights`, and the prominence term weights. `freshness` uses exponential decay on `last_material_update` — never `last_seen`, which is the whole point of material-change gating. `independent_confirmations` counts distinct `lineage_id`s among sources marked independent. `prominence` combines severity, corroboration, AU relevance, novelty and freshness, clamped to `[0, 1]`.

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/unit/test_score.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat: versioned scoring with material-change-gated decay"
```

---

## Task 11: Source health and staleness detection

**Files:**
- Create: `worker/pipeline/health.py`, `worker/db/sources.py`
- Test: `tests/unit/test_health.py`, `tests/integration/test_health_db.py`

**Interfaces:**
- Consumes: `FetchResult`, `SourceConfig`, `SourceHealth`, `NormalisedItem`
- Produces: `HealthStatus` StrEnum (`OK`, `ERROR`, `TIMEOUT`, `EMPTY`, `STALE`, `DEGRADED`, `DISABLED`); `LifecycleState` StrEnum (`DISCOVERED`, `CANDIDATE`, `TESTING`, `VALIDATED`, `ACTIVE`, `DEGRADED`, `BROKEN`, `RETIRED`); `assess(source, fetch, items, *, now, history) -> SourceHealth` (adds `newest_item_age_days: float | None`, `items_fetched: int`, `duration_ms: int` to `SourceHealth`); `record_health(conn, health)`; `next_lifecycle_state(source, history) -> LifecycleState`

- [ ] **Step 1: Write the failing tests**

Covers **Review Focus #1** — the stale-but-200 feed, proven real.

```python
def test_successful_fetch_with_recent_items_is_ok():
    assert assess(src, ok_fetch, [item(published=NOW - timedelta(hours=2))], now=NOW, history=[]).status is HealthStatus.OK

def test_http_200_with_only_old_items_is_stale_not_ok():
    """The Google Security Blog case: feed moved, old feed still serves 200 and valid XML."""
    h = assess(src, ok_fetch, [item(published=NOW - timedelta(days=120))], now=NOW, history=[])
    assert h.status is HealthStatus.STALE and h.newest_item_age_days == 120

def test_staleness_threshold_comes_from_expected_frequency():
    daily = replace(src, expected_frequency="daily")
    assert assess(daily, ok_fetch, [item(published=NOW - timedelta(days=14))], now=NOW, history=[]).status is HealthStatus.STALE
    monthly = replace(src, expected_frequency="monthly")
    assert assess(monthly, ok_fetch, [item(published=NOW - timedelta(days=14))], now=NOW, history=[]).status is HealthStatus.OK

def test_zero_items_is_empty():
    assert assess(src, ok_fetch, [], now=NOW, history=[]).status is HealthStatus.EMPTY

def test_not_modified_preserves_previous_status():
    assert assess(src, not_modified_fetch, [], now=NOW, history=[ok_health]).status is HealthStatus.OK

def test_three_failures_warn_five_consecutive_degrade():
    assert next_lifecycle_state(src, [err]*3) is LifecycleState.ACTIVE      # warning only
    assert next_lifecycle_state(src, [err]*5) is LifecycleState.DEGRADED

def test_recovery_returns_a_degraded_source_to_testing_not_active():
    assert next_lifecycle_state(degraded_src, [ok_health]) is LifecycleState.TESTING

def test_health_records_duration_and_counts():
    h = assess(src, ok_fetch, [i1, i2], now=NOW, history=[])
    assert (h.items_fetched, h.duration_ms) == (2, ok_fetch.duration_ms)
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/test_health.py -v`
Expected: FAIL — `ModuleNotFoundError: worker.pipeline.health`

- [ ] **Step 3: Implement**

`assess` derives a staleness threshold from `expected_frequency` (`hourly` → 2 days, `daily` → 7, `weekly` → 30, `monthly` → 90) and returns `STALE` when the newest item exceeds it, **even on HTTP 200**. `NOT_MODIFIED` carries the previous status forward. Lifecycle transitions follow `PLAN.md` §4.2 BOUNCER: 3 failures warn, 5 consecutive degrade, and recovery goes to `TESTING` — never straight back to `ACTIVE`.

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/unit/test_health.py tests/integration/test_health_db.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat: source health with stale-feed detection"
```

---

## Task 12: Publisher — build, validate, secret-scan

**Files:**
- Create: `worker/publish/__init__.py`, `worker/publish/build.py`, `worker/publish/validate.py`
- Test: `tests/unit/test_validate.py`, `tests/integration/test_build.py`

**Interfaces:**
- Consumes: `Event`, `SourceHealth`, `RunSummary`, `schemas/*.json`
- Produces: `build_all(conn, out_dir, *, now) -> list[Path]`; `validate_payload(payload: dict, schema_name: str) -> None` raising `ValidationFailure`; `scan_for_secrets(payload: dict) -> list[str]`

- [ ] **Step 1: Write the failing tests**

Covers **Review Focus #5** — the publisher must fail closed.

```python
def test_valid_payload_passes():
    validate_payload(good_live_payload, "live")

def test_missing_required_field_raises():
    with pytest.raises(ValidationFailure, match="last_completed_collection"):
        validate_payload({k: v for k, v in good_live_payload.items() if k != "last_completed_collection"}, "live")

def test_bad_enum_value_raises():
    with pytest.raises(ValidationFailure):
        validate_payload(payload_with(severity="spicy"), "live")

@pytest.mark.parametrize("secret", [
    "sk-or-v1-0123456789abcdef0123456789abcdef",   # OpenRouter
    "tvly-abcdef0123456789abcdef0123",             # Tavily
    "ghp_0123456789abcdefghij0123456789abcdef",    # GitHub classic
    "github_pat_11ABCDEFG0123456789_abcdefghij",   # GitHub fine-grained
    "postgresql://user:hunter2@db:5432/cyber_intel",
])
def test_scan_detects_credential_shapes(secret):
    assert scan_for_secrets({"events": [{"summary": f"leaked {secret}"}]})

def test_scan_detects_env_values_present_in_output(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "9999:VERYSECRETVALUE")
    assert scan_for_secrets({"note": "9999:VERYSECRETVALUE"})

def test_scan_is_clean_on_legitimate_content():
    assert scan_for_secrets({"events": [{"summary": "CVE-2026-88772 exploited; patch available."}]}) == []

def test_build_writes_nothing_when_a_secret_is_found(tmp_path, conn, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-deadbeefdeadbeefdeadbeefdeadbeef")
    insert_event_with_summary(conn, "contains sk-or-v1-deadbeefdeadbeefdeadbeefdeadbeef")
    with pytest.raises(ValidationFailure, match="secret"):
        build_all(conn, tmp_path, now=NOW)
    assert list(tmp_path.iterdir()) == []          # fail closed: nothing written

def test_build_is_atomic_on_schema_failure(tmp_path, conn):
    (tmp_path / "live.json").write_text('{"previous": true}')
    with pytest.raises(ValidationFailure):
        build_all(conn, tmp_path, now=NOW)   # conn contains an invalid event
    assert json.loads((tmp_path / "live.json").read_text()) == {"previous": True}

def test_build_emits_expected_files(tmp_path, conn):
    names = {p.name for p in build_all(conn, tmp_path, now=NOW)}
    assert {"live.json","index.json","source-health.json","system-status.json"} <= names

def test_no_raw_article_body_is_published(tmp_path, conn):
    build_all(conn, tmp_path, now=NOW)
    assert all("raw_body" not in e for e in json.loads((tmp_path/"live.json").read_text())["events"])

def test_system_status_uses_last_completed_collection_not_live(tmp_path, conn):
    s = json.loads((tmp_path / "system-status.json").read_text())
    assert "last_completed_collection" in s and "live" not in json.dumps(s).lower()
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/test_validate.py -v`
Expected: FAIL — `ModuleNotFoundError: worker.publish.validate`

- [ ] **Step 3: Implement**

`scan_for_secrets` checks two ways: known credential regexes (OpenRouter, Tavily, GitHub classic and fine-grained, Telegram, Postgres URLs with a password, generic high-entropy `[A-Za-z0-9_\-]{32,}`), and the literal values of every populated secret in `Settings` — that second check is what catches a key we never anticipated.

`build_all` writes to a temporary directory, validates and scans every payload, and only then atomically moves files into place. Any failure leaves the previous output untouched. It emits `live.json` (events with `prominence > 0.05`, ordered by prominence), `index.json` (history index), `source-health.json`, `system-status.json` (with `last_completed_collection`), and `history/<YYYY-MM-DD>.json`.

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/unit/test_validate.py tests/integration/test_build.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat: publisher with schema validation and fail-closed secret scan"
```

---

## Task 13: Pipeline orchestrator, scheduler and CLI

**Files:**
- Create: `worker/pipeline/run.py`, `worker/scheduler.py`, `worker/main.py`, `worker/db/runs.py`
- Test: `tests/integration/test_run.py`

**Interfaces:**
- Consumes: everything from Tasks 4–12
- Produces: `async run_lane(lane: Lane, *, once: bool = False) -> RunSummary`; `RunSummary(run_id, lane, started_at, finished_at, sources_ok, sources_failed, sources_stale, items_fetched, new_events, updated_events, duplicates, archived_events, errors: list[str])`; `main()` CLI with `--lane {fast,normal,deep} --once`, `--migrate`, `--publish`

- [ ] **Step 1: Write the failing tests**

```python
async def test_run_lane_end_to_end_creates_events(pg_engine, respx_mock):
    mock_two_feeds(respx_mock)
    s = await run_lane(Lane.FAST, once=True)
    assert s.new_events > 0 and s.sources_ok == 2

async def test_second_identical_run_creates_no_new_events(pg_engine, respx_mock):
    mock_two_feeds(respx_mock)
    await run_lane(Lane.FAST, once=True)
    assert (await run_lane(Lane.FAST, once=True)).new_events == 0

async def test_one_failing_source_does_not_stop_the_others(pg_engine, respx_mock):
    respx_mock.get(URL_A).respond(500)
    respx_mock.get(URL_B).respond(200, content=fixture_bytes("acsc_alerts.xml"))
    s = await run_lane(Lane.FAST, once=True)
    assert s.sources_failed == 1 and s.sources_ok == 1 and s.items_fetched > 0

async def test_run_summary_is_persisted(pg_engine, respx_mock):
    s = await run_lane(Lane.FAST, once=True)
    assert load_run(pg_engine, s.run_id).lane == "fast"

async def test_sources_are_fetched_concurrently(pg_engine, respx_mock):
    """20 sources each delayed 100ms must finish well under serial time."""
    mock_n_slow_feeds(respx_mock, n=20, delay=0.1)
    t0 = time.monotonic(); await run_lane(Lane.FAST, once=True)
    assert time.monotonic() - t0 < 1.0

async def test_conditional_request_skips_unchanged_source(pg_engine, respx_mock):
    mock_feed_with_etag(respx_mock)
    await run_lane(Lane.FAST, once=True)
    s = await run_lane(Lane.FAST, once=True)      # server now answers 304
    assert s.items_fetched == 0 and s.sources_failed == 0
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/integration/test_run.py -v`
Expected: FAIL — `ModuleNotFoundError: worker.pipeline.run`

- [ ] **Step 3: Implement**

`run_lane`: load the registry, fetch the lane's sources concurrently with a semaphore (cap 10) using stored ETag/Last-Modified, parse, normalise, resolve, upsert, score, record health, and persist a `RunSummary`. Per-source exceptions are caught and recorded so one failure never aborts the run — this is the failure model from `PLAN.md` §11 made real.

`scheduler.py`: APScheduler `AsyncIOScheduler` with FAST at `*/15` and NORMAL at `0 */4`, `max_instances=1`, `coalesce=True`, timezone UTC.

`main.py`: argparse CLI wiring `--migrate`, `--lane`, `--once`, `--publish`, defaulting to running the scheduler.

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/integration/test_run.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat: pipeline orchestrator, scheduler and CLI"
```

---

## Task 14: Site — HUD shell and styling

**Files:**
- Create: `site/index.html`, `site/assets/hud.css`, `site/assets/hud.js`, `site/assets/fonts/` (Orbitron, Chakra Petch, JetBrains Mono woff2 + `OFL.txt`), `site/assets/vendor/`
- Test: `tests/unit/test_site_static.py`

**Interfaces:**
- Consumes: `data/live.json`, `data/system-status.json`, `data/source-health.json`
- Produces: `hud.js` exporting `loadData()`, `renderSections(data)`, `applyFilters(state)`, `renderPipeline(health)`, `renderIndex(data)`, `initFxToggle()`; CSS custom properties exactly as named in `PLAN.md` §8.1

- [ ] **Step 1: Write the failing tests**

Static assertions, so they run without a browser.

```python
def test_css_defines_every_palette_token():
    css = read("site/assets/hud.css")
    for t in ("--bg-void","--bg-panel","--bg-raised","--line","--line-strong","--text",
              "--text-dim","--text-muted","--cyan","--teal","--magenta",
              "--sev-critical","--sev-high","--sev-medium","--sev-low","--sev-info"):
        assert f"{t}:" in css.replace(" ", "")

def test_palette_hex_values_match_the_spec():
    css = read("site/assets/hud.css").replace(" ", "")
    assert "--sev-critical:#FF3B5C" in css and "--cyan:#00E5FF" in css

def test_every_keyframes_animation_is_gated_on_reduced_motion():
    css = read("site/assets/hud.css")
    for name in re.findall(r"@keyframes\s+([\w-]+)", css):
        assert re.search(rf"prefers-reduced-motion:\s*no-preference[^}}]*?animation[^;]*{name}", css, re.S), name

def test_site_never_says_live():
    for p in Path("site").rglob("*.html"):
        assert "live" not in p.read_text().lower().replace("live.json", "")

def test_index_declares_last_completed_collection():
    assert "LAST COMPLETED COLLECTION" in read("site/index.html")

def test_csp_meta_is_present_and_forbids_inline_script():
    html = read("site/index.html")
    assert 'http-equiv="Content-Security-Policy"' in html and "'unsafe-inline'" not in html

def test_no_external_origins_are_referenced():
    for p in list(Path("site").rglob("*.html")) + list(Path("site").rglob("*.js")):
        assert not re.search(r"https?://(?!localhost)", p.read_text()) or "vendor" in str(p)

def test_fonts_ship_with_their_licence():
    assert (Path("site/assets/fonts/OFL.txt")).exists()

def test_severity_is_conveyed_by_more_than_colour():
    assert "data-severity-shape" in read("site/assets/hud.js")
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/test_site_static.py -v`
Expected: FAIL — `FileNotFoundError: site/assets/hud.css`

- [ ] **Step 3: Implement**

`hud.css` (~15 KB, hand-written): palette and typography tokens; the chamfered `clip-path` HUD panel with bracket corners and tick ruler; severity tokens paired with shape glyphs; static scanline overlay with no flicker; `.fx-off` escape hatch; `content-visibility: auto` on long lists. Every `@keyframes` is referenced only inside `@media (prefers-reduced-motion: no-preference)`.

`index.html`: CSP meta, self-hosted font preloads, the `AUSTRALIA NOW` hero, the section list from `PLAN.md` §8.2, and a `LAST COMPLETED COLLECTION` status strip.

`hud.js`: ES module, no framework. Fetches the JSON, renders sections, and implements client-side tag filtering. Severity renders colour **plus** label **plus** `data-severity-shape` glyph **plus** bar count. `initFxToggle` persists the choice in `localStorage`. Times formatted with `Intl.DateTimeFormat('en-AU', {timeZone: 'Australia/Sydney'})`.

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/unit/test_site_static.py -v && python -m http.server 8000 --directory site`
Expected: PASS, and the page renders with real data from Task 13

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat: cyberpunk HUD shell, styling and rendering"
```

---

## Task 15: Site — radar, pipeline replay, world map, event detail, history

**Files:**
- Create: `site/event.html`, `site/history.html`, `site/assets/map.js`, `site/assets/vendor/countries-110m.json`, `site/assets/vendor/{d3-geo,topojson-client}.min.js`
- Modify: `site/assets/hud.js`, `site/assets/hud.css`
- Test: `tests/unit/test_site_visuals.py`

**Interfaces:**
- Consumes: `renderSections` from Task 14; `data/live.json`, `data/index.json`
- Produces: `map.js` exporting `renderMap(events, onCountryClick)`; `hud.js` gains `renderRadar(events)`, `renderPulseIndex(events)`, `renderEventDetail(event)`, `renderTimeline(event)`

- [ ] **Step 1: Write the failing tests**

```python
def test_world_atlas_is_vendored_and_small():
    p = Path("site/assets/vendor/countries-110m.json")
    assert p.exists() and p.stat().st_size < 200_000

def test_map_uses_iso_numeric_ids_for_filtering():
    assert 'data-n3' in read("site/assets/map.js")

def test_map_projection_is_centred_on_australia():
    assert "rotate([-150" in read("site/assets/map.js")

def test_map_has_a_keyboard_accessible_equivalent():
    assert "<select" in read("site/index.html") and "country" in read("site/index.html")

def test_pipeline_replay_is_labelled_as_a_replay():
    assert "COLLECTION REPLAY" in read("site/index.html")

def test_pipeline_stages_match_the_spec():
    js = read("site/assets/hud.js")
    for s in ("SOURCES","COLLECT","MATCH","VERIFY","ENRICH","CROSS-REF","SCORE","PUBLISH"):
        assert s in js

def test_ai_suggested_mitre_is_labelled_in_event_detail():
    assert "AI SUGGESTED" in read("site/event.html") or "AI SUGGESTED" in read("site/assets/hud.js")

def test_unknown_cvss_renders_as_unknown_not_zero():
    js = read("site/assets/hud.js")
    assert "unknown" in js.lower() and "cvss ?? 0" not in js.replace(" ", "")

def test_canvas_loop_pauses_when_hidden_and_offscreen():
    js = read("site/assets/hud.js")
    assert "visibilitychange" in js and "IntersectionObserver" in js

def test_ticker_is_pausable():
    assert "aria-hidden" in read("site/index.html") and "pause" in read("site/assets/hud.js").lower()
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/test_site_visuals.py -v`
Expected: FAIL — `FileNotFoundError: site/assets/map.js`

- [ ] **Step 3: Implement**

`map.js`: import `d3-geo` and `topojson-client` from the vendored files, `geoEqualEarth().rotate([-150, 0])` so Australia sits centre, one `<path data-n3>` per country, choropleth by event count, click and `<select>` both filtering. Only countries with events are focusable.

`hud.js` additions: the threat radar (blip ring by severity, angle by category, AU double-ringed); the CyberPulse Index gauge plus 24-hour EKG pulse line; the pipeline replay with `pathLength=100` travelling dash pulses and per-node counters and status LEDs; the canvas particle background obeying `visibilitychange`, `IntersectionObserver`, DPR ≤ 2 and 30 fps; the pausable ticker.

`event.html`: the full §5 field set, with **evidence visually separated from AI inference**, `AI SUGGESTED` labelling on MITRE, `unknown` rendered as `unknown` for absent CVSS/EPSS, plus timeline and related events.

`history.html`: previous/next day and jump-to-date driven by `index.json`.

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/unit/test_site_visuals.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat: radar, pipeline replay, world map, event detail and history"
```

---

## Task 16: Publish to GitHub Pages

**Files:**
- Create: `worker/publish/push.py`, `.github/workflows/pages.yml`
- Test: `tests/integration/test_push.py`

**Interfaces:**
- Consumes: `build_all` from Task 12, `Settings.github_token`, `Settings.github_repository`
- Produces: `push_data(data_dir: Path, *, branch: str = "data", dry_run: bool = False) -> PushResult`; `PushResult(pushed: bool, commit_sha: str | None, reason: str | None)`

- [ ] **Step 1: Write the failing tests**

```python
def test_push_is_skipped_when_data_is_unchanged(tmp_repo):
    push_data(tmp_repo / "data")
    r = push_data(tmp_repo / "data")
    assert r.pushed is False and r.reason == "no changes"

def test_push_creates_a_single_commit_orphan_branch(tmp_repo):
    push_data(tmp_repo / "data")
    write_new_event(tmp_repo / "data")
    push_data(tmp_repo / "data")
    assert git_rev_list_count(tmp_repo, "data") == 1      # history stays flat

def test_push_refuses_to_run_without_a_token(tmp_repo, monkeypatch):
    monkeypatch.delenv("CYBERPULSE_PUBLISH_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="no publish token"):
        push_data(tmp_repo / "data")

def test_push_never_touches_main(tmp_repo):
    before = git_rev_parse(tmp_repo, "main")
    push_data(tmp_repo / "data")
    assert git_rev_parse(tmp_repo, "main") == before

def test_token_is_absent_from_the_git_remote_config(tmp_repo):
    push_data(tmp_repo / "data")
    assert "github_pat" not in (tmp_repo / ".git" / "config").read_text()

def test_dry_run_reports_without_pushing(tmp_repo):
    assert push_data(tmp_repo / "data", dry_run=True).pushed is False
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/integration/test_push.py -v`
Expected: FAIL — `ModuleNotFoundError: worker.publish.push`

- [ ] **Step 3: Implement**

`push_data`: compare a content hash against the remote branch tip and return early when unchanged — no empty commits. Build the orphan branch in a temporary index and **force-push a single commit**, which keeps history flat and the repo well under Pages' 1 GB guidance. Pass credentials via an `http.extraheader` argument rather than embedding the token in the remote URL, so it never lands in `.git/config`.

`.github/workflows/pages.yml`: triggers on `push` to `main` and `data` plus `workflow_dispatch`; `concurrency: group: pages, cancel-in-progress: false`; permissions `contents: read, pages: write, id-token: write`; checks out `main` for the site and `data` into `./data`; then `actions/configure-pages`, `actions/upload-pages-artifact`, `actions/deploy-pages`. Pin action versions to the current majors (`upload-pages-artifact` v5, `deploy-pages` v5, `checkout` v6) and add a comment recording that they were verified on 2026-09-29.

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/integration/test_push.py -v`
Expected: PASS

- [ ] **Step 5: Commit and deploy**

```bash
git add -A && git commit -m "feat: orphan-branch publishing and Pages workflow"
git push origin main
docker compose run --rm worker python -m worker.publish.push
```

Confirm the site at `https://happycode0.github.io/CyberPulse-AI/`.

---

## Stage 1 exit criteria

- [ ] `docker compose up -d db && docker compose run --rm worker python -m worker.db.migrate` succeeds
- [ ] `run_lane(FAST)` collects real items from ACSC, CISA and KEV and creates canonical events
- [ ] A second identical run creates zero new events (deduplication demonstrably works)
- [ ] One deliberately broken source does not stop the run
- [ ] A feed serving HTTP 200 with months-old items is reported `stale`
- [ ] `build_all` refuses to write anything when a secret appears in the output
- [ ] The site renders real AU and global intelligence, with no "LIVE" claim anywhere
- [ ] The site is deployed and reachable on GitHub Pages
- [ ] `pytest -q` is green
- [ ] No secret appears in any tracked file: `git grep -nE "(sk-or-v1-|tvly-|ghp_|github_pat_)" || echo CLEAN`
