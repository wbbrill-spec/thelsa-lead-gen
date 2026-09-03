# Thelsa Lead Gen (Web App)

Flask web app for Thelsa's cross-border corporate lead generation, deployed on Render (service `thelsa-lead-gen`, ID `srv-d85nkdjrjlhs73a4uj30`).

This is one of **two** automations for this project — the other is the scheduled-task pipeline driven by `pipeline_skill.md` in the Cowork project folder ("Cross-border Corp Lead Gen - Thelsa"), which is simpler and prompt-only. This app is the full multi-user dashboard version with a database and Gmail OAuth per user.

## Pipeline modules (`modules/`)

Run roughly in this order:

1. **mod01_discovery** — web search for cross-border expansion signals (Mexican companies opening US operations, US companies expanding into Mexico).
2. **mod02_deduplication** — filters out companies already tracked.
3. **mod03_scorer** — scores leads for fit with Thelsa's moving/relocation services.
4. **mod04_segmentation** — segments/categorizes qualified leads.
5. **mod05_enricher** — uses Claude to find a named contact (person + email) at the company or its subsidiaries.
6. **mod06_scheduler** — orchestrates scheduled runs.
7. **mod07_drafter** — uses Claude to draft bilingual (EN/ES) outreach emails.
8. **mod08_search** — supporting search utilities.
9. **mod09_query_rotator** — rotates/varies search queries across runs.
10. **mod10_reply_detector** — detects replies to outreach emails.

## Data model (`db.py`)

`Company`, `Contact`, `Lead`, `EmailDraft`, `User`, `DiscoveryRun`, `LeadStatusHistory`.

## Other key files

- `app.py` — Flask app / dashboard routes
- `pipeline.py` — background discovery runner, run lock, stale-run reaper, run summary
- `modules/llm.py` — shared Claude client (`CLAUDE_MODEL`), tolerant JSON parsing
- `web_auth.py` — Gmail OAuth per user
- `config.py`, `render.yaml`, `Procfile`, `requirements.txt` — deployment config
- `query_bank.json` — search query bank
- `templates/` — dashboard HTML

## Recent fixes

**2026-06-12** — `mod05_enricher.py` and `mod07_drafter.py` were calling a deprecated Claude model string, causing enrichment and drafting calls to fail. Fixed:
- Both updated to `model="claude-sonnet-4-6"`.
- `mod05_enricher._web_search_contact()` prompt rewritten to accept any named individual at the company or its subsidiaries as a usable contact, with retry logic if the first pass returns an empty result.

Deployed via commit `9ae663d` ("Fix deprecated model + improve contact extraction reliability"), verified live on Render.

**2026-06-17** — `mod01_discovery.py`, `mod03_scorer.py`, and `mod04_segmentation.py` were also using the deprecated model. `mod01` is the critical failure: when its Claude call fails, it silently returns `[]`, and the entire pipeline produces zero leads. Fixed:
- All three updated to `model="claude-sonnet-4-6"` (4 call sites total).

Deployed via commit `c12e1c1` ("Fix deprecated model in mod01/mod03/mod04 — restore lead discovery"), verified live on Render.

**2026-06-18** — Four additional bugs found causing discovery to still produce zero leads after the model fix:
1. `mod08_search.py`: `recency_days` parameter was silently ignored; `tbs=qdr:m` was hardcoded. Fixed: map `recency_days ≤7 → qdr:w`, `≤30 → qdr:m`, `else → qdr:y`.
2. `mod01_discovery.py`: `_extract_candidates()` only sent the first 20 of up to 80 results to Claude. Fixed: URL-deduplicate, send up to 50 unique results, increase `max_tokens` 2000 → 4000.
3. `mod01_discovery.py`: Extraction prompt too strict; nearshoring/acquisition/subsidiary signals were excluded. Fixed: loosened prompt criteria.
4. `mod09_query_rotator.py`: Query rotation never worked because stored records lacked an `"id"` key. Fixed: `get_queries_for_run()` now returns `list[tuple[str, str]]` (id, query_string) pairs.

Deployed via commit `5bbb10f` ("Fix discovery pipeline: extend search window to 1yr, send 50 results to Claude, fix query rotation"). Verified: 9 new leads generated (Nestlé, Bulkmatic, AutoZone, Amazon, Panduit, Technimark, Deacero, PepsiCo, Coca-Cola).

**2026-06-18** — `app.py` was calling `deduplicate()` and `score_candidates()` without `run_id`, so discovery run counters (`companies_discovered`, `leads_qualified`, `leads_disqualified`) were always 0 even when leads were generated. Fixed: pass `run_id=run_id` to both calls. Deployed via commit `0ae2dec`.

**2026-09-02 — Reliability overhaul (Fable takeover).** Root causes of "it often breaks down" and their fixes:
1. **Discovery ran inside the web request** (5–20 min) → Render's proxy timed out, users re-clicked, runs overlapped, and a deploy mid-run left the run `RUNNING` forever. Now `pipeline.py` is the single runner: `POST /pipeline/run` starts a background thread and redirects to the new **`/runs`** page; a second click while a run is in progress is refused (lock on fresh heartbeat); `reap_stale_runs()` marks runs with no heartbeat for `RUN_STALE_MINUTES` (45) as FAILED at boot and on every Runs page load. Each run records `stage`, `heartbeat_at`, `triggered_by`, `leads_created`, `contacts_found`, `contacts_zoominfo` and a line-by-line `summary` (columns auto-added by `models._migrate_added_columns`).
2. **Failures looked like success.** `mod01` returned `[]` on any Claude error, `mod08` never logged SerpAPI 401/429, and an empty `SEARCH_API_KEY` silently used mock data — every one produced a `COMPLETED` run with 0 leads. Now: `mod01` raises `DiscoveryError`, `mod08` raises `SearchError` on 401/403/429 (bad key / quota) and refuses mock data in production, and the run is marked `FAILED` with the exact reason. The morning alert email says **FAILED** in the subject when the last run failed.
3. **Claude call fragility.** New `modules/llm.py`: one client, model from `CLAUDE_MODEL` env var (default `claude-sonnet-4-6` — change it in Render without a deploy), `timeout=90s`, `max_retries=5`, tolerant JSON extraction (fences/preamble OK), truncation (`stop_reason=max_tokens`) raises instead of parsing half an array; `mod01` budget 4000→8000 tokens. A scoring API error now **skips** the company instead of writing it to `companies` as disqualified forever. `mod07._generate_email` raises instead of returning a `[Email generation failed]` body (which used to become a real Outlook draft).
4. **Double daily runs.** `scheduler.main()` no longer runs a cycle on every boot/deploy (only if nothing ran in the last 20 h); within a process `/cron/run` cycles are serialised by `CYCLE_LOCK`, and across processes the DB-backed run lock in `pipeline.start_run` refuses a second discovery. Keep ONE daily trigger (the Render Cron Job hitting `/cron/run`); `python scheduler.py --once` forces one cycle by hand.
5. **Microsoft Graph**: token cached (was one token request per Graph call → AAD throttling), 429/5xx retried with `Retry-After`; a failed Sent-folder listing now raises instead of returning a partial list that flagged real leads `NO_OUTREACH`.
6. **Config validation**: `config.config_warnings()` logged at boot, shown on `/runs` and in `/health`.

**2026-09-03 — ZoomInfo enrichment rewritten + SerpAPI quota guard.**
- Root cause of "0 leads for a week every month": the SerpAPI monthly quota runs out ~3½ weeks in (runs Jul 25–Aug 3 and Aug 30–Sep 2 all returned 0 with HTTP 429 "Your account has run out of searches", which the old code swallowed). Now the run checks `https://serpapi.com/account` first, logs "N searches left", refuses to start below `SEARCH_MIN_LEFT` (15), and the Runs page shows the remaining quota.
- ZoomInfo: the old enrich call sent only `companyName + jobTitle`, which is not a valid person identifier for `/contacts/enrich`, so it never matched. New `modules/zoominfo.py` does the documented two-step flow: `POST /gtm/data/v1/contacts/search` (JSON:API envelope `{"data":{"type":"ContactSearch","attributes":{companyWebsite|companyName, jobTitleList, requiredFieldsList:["email"]}}}`, sorted by `-contactAccuracyScore`) → `POST /gtm/data/v1/contacts/enrich` by `personId`. The enrich envelope `type` is not documented publicly, so the client tries a short list of envelopes on HTTP 400 and remembers the one that works. `/admin/zoominfo-test?token=CRON_TOKEN&company=…&domain=…` runs the flow once and returns every raw request/response.

## Notes

- Never reference insurance products/coverage anywhere in Thelsa-facing content — Thelsa is a moving/relocation company.
- No git push credentials are configured for this local clone; deploys are done via GitHub's web "Upload files" UI, which triggers Render auto-deploy.
