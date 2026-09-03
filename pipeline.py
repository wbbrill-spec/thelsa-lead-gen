"""Discovery pipeline runner — the single place a discovery run is executed.

Used by the dashboard ("Run Discovery"), the /cron/run endpoint and the
worker. Responsibilities:

  * run in the background (the dashboard request returns immediately);
  * refuse to start while another run is genuinely in progress (lock);
  * record stage + heartbeat as the run progresses, so a run killed by a
    deploy / restart can be recognised and marked FAILED by ``reap_stale_runs``;
  * mark the run FAILED with a clear ``error_message`` on ANY failure —
    a run is never "COMPLETED" with 0 leads because an API silently broke;
  * write a human-readable ``summary`` line-by-line for the Runs page.
"""
from __future__ import annotations

import threading
import traceback
from datetime import datetime, timedelta, timezone

import config
from db import get_db
from models import DiscoveryRun


class RunAlreadyInProgress(RuntimeError):
    def __init__(self, run: DiscoveryRun):
        self.run_id = run.id
        self.stage = run.stage
        super().__init__(f"Run #{run.id} is already in progress (stage: {run.stage or 'starting'})")


_start_lock = threading.Lock()
_current = threading.local()  # .run_id of the run executing on this thread


def heartbeat() -> None:
    """Cheap progress ping — call inside long per-candidate loops so a slow
    stage is not mistaken for a dead run by ``reap_stale_runs``."""
    run_id = getattr(_current, "run_id", None)
    if not run_id:
        return
    try:
        with get_db() as db:
            run = db.query(DiscoveryRun).filter_by(id=run_id, status=DiscoveryRun.STATUS_RUNNING).first()
            if run:
                run.heartbeat_at = _now()
    except Exception as exc:  # never let a heartbeat kill the run
        print(f"[PIPELINE] heartbeat failed: {exc}")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; Postgres hands back aware ones."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


# ── Public API ────────────────────────────────────────────────────────────────

def reap_stale_runs() -> int:
    """Mark RUNNING runs with a stale heartbeat as FAILED. Returns count."""
    cutoff = _now() - timedelta(minutes=config.RUN_STALE_MINUTES)
    reaped = 0
    with get_db() as db:
        stale = (
            db.query(DiscoveryRun)
            .filter(DiscoveryRun.status == DiscoveryRun.STATUS_RUNNING)
            .all()
        )
        for run in stale:
            last = _aware(run.heartbeat_at or run.started_at)
            if last is None or last < cutoff:
                run.status = DiscoveryRun.STATUS_FAILED
                run.completed_at = _now()
                run.error_message = (
                    f"Run died without finishing (no progress since "
                    f"{last.strftime('%Y-%m-%d %H:%M UTC') if last else 'start'} — "
                    f"probably a deploy or restart while it was at stage '{run.stage or 'starting'}')."
                )
                run.summary = (run.summary or "") + "\n[reaper] marked FAILED: no heartbeat"
                reaped += 1
    if reaped:
        print(f"[PIPELINE] reaped {reaped} stale run(s)")
    return reaped


def active_run() -> DiscoveryRun | None:
    """Return the run currently in progress (fresh heartbeat), if any."""
    reap_stale_runs()
    with get_db() as db:
        return (
            db.query(DiscoveryRun)
            .filter(DiscoveryRun.status == DiscoveryRun.STATUS_RUNNING)
            .order_by(DiscoveryRun.started_at.desc())
            .first()
        )


def start_run(user_id: int | None, triggered_by: str, background: bool = True) -> int:
    """Create a DiscoveryRun and execute it (in a thread by default).

    Raises RunAlreadyInProgress if another run has a fresh heartbeat.
    Returns the new run id.
    """
    with _start_lock:
        current = active_run()
        if current:
            raise RunAlreadyInProgress(current)
        with get_db() as db:
            run = DiscoveryRun(
                run_by_user_id=user_id,
                status=DiscoveryRun.STATUS_RUNNING,
                stage="queued",
                heartbeat_at=_now(),
                triggered_by=triggered_by,
                summary=f"Run started by {triggered_by}",
            )
            db.add(run)
            db.flush()
            run_id = run.id

    if background:
        # Non-daemon so a graceful shutdown waits for the run instead of
        # killing it silently mid-stage.
        t = threading.Thread(target=execute_run, args=(run_id, user_id),
                             name=f"discovery-run-{run_id}", daemon=False)
        t.start()
    else:
        execute_run(run_id, user_id)
    return run_id


def execute_run(run_id: int, user_id: int | None) -> None:
    """Run every pipeline stage for ``run_id`` and record the outcome."""
    from modules import mod03_scorer, mod05_enricher, mod08_search
    from modules.mod01_discovery import run_discovery
    from modules.mod02_deduplication import deduplicate
    from modules.mod03_scorer import score_candidates
    from modules.mod04_segmentation import segment_and_detect_rmc
    from modules.mod05_enricher import enrich_contacts

    _current.run_id = run_id
    log = _RunLog(run_id)
    log.stage("discovery", f"model={config.CLAUDE_MODEL}")

    # Attribute leads to *someone* — the worker/cron have no session user.
    if user_id is None:
        user_id = _system_user_id()
        if user_id is None:
            log.fail("No active user found to attribute leads to")
            return

    # Reset per-run stats on the modules
    for k in mod05_enricher.STATS:
        mod05_enricher.STATS[k] = 0
    search_calls_before = mod08_search.STATS["calls"]

    try:
        acct = mod08_search.account_status()
        if acct:
            left = acct.get("total_searches_left", acct.get("plan_searches_left"))
            log.note(f"SerpAPI: {left} searches left this month "
                     f"({acct.get('this_month_usage', '?')}/{acct.get('searches_per_month', '?')} used"
                     f"{', ' + str(acct.get('plan_name')) if acct.get('plan_name') else ''})"
                     if "error" not in acct else f"SerpAPI account check failed: {acct['error']}")
            if left is not None and int(left) < config.SEARCH_MIN_LEFT:
                raise mod08_search.SearchError(
                    f"SerpAPI quota nearly exhausted ({left} searches left; a run needs ~{config.SEARCH_MIN_LEFT}). "
                    f"Upgrade the plan at serpapi.com or wait for the monthly reset.")

        candidates = run_discovery(run_id=run_id)
        log.stage("dedup", f"{len(candidates)} candidate companies from "
                           f"{mod08_search.STATS['calls'] - search_calls_before} searches")

        net_new = deduplicate(candidates, run_id=run_id)
        log.stage("scoring", f"{len(net_new)} net-new after dedup "
                             f"({len(candidates) - len(net_new)} already known)")

        qualified = score_candidates(net_new, run_id=run_id)
        errs = mod03_scorer.STATS["errors"]
        log.stage("segmentation", f"{len(qualified)} qualified (score ≥ 7)"
                                  + (f", {errs} skipped on API error — will retry next run" if errs else ""))

        segmented = segment_and_detect_rmc(qualified)
        large = sum(1 for s in segmented if s.size_tier == "LARGE_CORP")
        rmc = sum(1 for s in segmented if s.rmc_detected)
        log.stage("enrichment", f"{large} large-corp, {rmc} with RMC")

        lead_ids = enrich_contacts(segmented, run_id=run_id, generated_by_user_id=user_id)
        st = mod05_enricher.STATS
        log.stage("finishing", f"{len(lead_ids)} leads created — contacts: "
                               f"{st['zoominfo_hits']} ZoomInfo, {st['web_hits']} web, {st['empty']} none"
                               + (f"; ZoomInfo HTTP errors: {st['zoominfo_http_errors']}"
                                  if st["zoominfo_http_errors"] else ""))

        if segmented and not lead_ids:
            log.fail(f"{len(segmented)} qualified companies but no leads were written — check DB errors in the log")
            return

        with get_db() as db:
            run = db.query(DiscoveryRun).filter_by(id=run_id).first()
            if run:
                run.leads_created = len(lead_ids)
                run.contacts_found = st["zoominfo_hits"] + st["web_hits"]
                run.contacts_zoominfo = st["zoominfo_hits"]
        log.complete(f"done: {len(lead_ids)} new lead(s)")

    except Exception as exc:  # noqa: BLE001 — everything must land in the run record
        print(f"[PIPELINE] run {run_id} FAILED at stage {log.current_stage}: {exc}")
        traceback.print_exc()
        try:
            log.fail(f"{type(exc).__name__} at stage '{log.current_stage}': {exc}")
        except Exception as exc2:  # DB itself is down — leave it to the reaper
            print(f"[PIPELINE] could not record failure for run {run_id}: {exc2}")
    finally:
        _current.run_id = None


def last_run_status() -> dict:
    """Small dict used by the dashboard banner and the morning alert."""
    reap_stale_runs()
    with get_db() as db:
        run = db.query(DiscoveryRun).order_by(DiscoveryRun.started_at.desc()).first()
        if not run:
            return {"exists": False}
        return {
            "exists": True,
            "id": run.id,
            "status": run.status,
            "stage": run.stage,
            "started_at": run.started_at,
            "completed_at": run.completed_at,
            "leads_created": run.leads_created or 0,
            "leads_qualified": run.leads_qualified or 0,
            "companies_discovered": run.companies_discovered or 0,
            "error_message": run.error_message,
            "triggered_by": run.triggered_by,
        }


# ── Internals ─────────────────────────────────────────────────────────────────

def _system_user_id() -> int | None:
    from models import User
    with get_db() as db:
        u = (
            db.query(User).filter(User.email_gmail == "wbbrill@gmail.com").first()
            or db.query(User).filter_by(is_active=True).first()
        )
        return u.id if u else None


class _RunLog:
    """Writes stage / heartbeat / summary lines to the DiscoveryRun row."""

    def __init__(self, run_id: int):
        self.run_id = run_id
        self.current_stage = "starting"

    def _append(self, line: str, **fields):
        stamp = _now().strftime("%H:%M:%S")
        with get_db() as db:
            run = db.query(DiscoveryRun).filter_by(id=self.run_id).first()
            if not run:
                return
            if run.status != DiscoveryRun.STATUS_RUNNING:
                # Reaped (or otherwise finalised) while we were still going —
                # record the line but never overwrite the final status.
                fields = {k: v for k, v in fields.items() if k not in ("status", "completed_at", "error_message")}
                line = f"(after finalised) {line}"
            run.heartbeat_at = _now()
            run.summary = f"{run.summary or ''}\n[{stamp}] {line}".strip()
            for k, v in fields.items():
                setattr(run, k, v)
        print(f"[PIPELINE] run {self.run_id}: {line}")

    def note(self, line: str):
        self._append(line)

    def stage(self, name: str, note: str = ""):
        self.current_stage = name
        self._append(f"→ {name}" + (f" — {note}" if note else ""), stage=name)

    def complete(self, note: str):
        self._append(note, status=DiscoveryRun.STATUS_COMPLETED,
                     stage="completed", completed_at=_now())

    def fail(self, message: str):
        self._append(f"FAILED: {message}", status=DiscoveryRun.STATUS_FAILED,
                     completed_at=_now(), error_message=message[:2000])
