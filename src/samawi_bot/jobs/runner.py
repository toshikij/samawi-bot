"""Runs a job with history and alerting. Every job's outcome lands in `job_runs`; a failure emails."""

from __future__ import annotations

import logging
import traceback
from collections.abc import Callable
from typing import Any

from ..alerts import safe_send
from ..app import App
from ..db import session_scope
from ..db.models import JobRun

log = logging.getLogger("samawi.jobs")

JobFn = Callable[[App], dict[str, Any]]


class JobSkipped(Exception):
    """Raised by a job that decided not to run (e.g. kill switch). Recorded, not alerted."""


def run_job(app: App, name: str, fn: JobFn) -> JobRun:
    with session_scope(app.db) as s:
        run = JobRun(job=name, started_at=app.now(), status="running", summary={})
        s.add(run)
        s.flush()
        run_id = run.id
    status, summary, error = "ok", {}, None
    try:
        summary = fn(app) or {}
    except JobSkipped as exc:
        status, summary = "skipped", {"reason": str(exc)}
    except Exception as exc:
        status, error = "failed", f"{type(exc).__name__}: {exc}"
        log.exception("job %s failed", name)
        safe_send(app.mailer, f"FAIL {name}", f"{error}\n\n{traceback.format_exc()[-4000:]}")
    with session_scope(app.db) as s:
        row = s.get(JobRun, run_id)
        assert row is not None
        row.finished_at, row.status, row.summary, row.error = app.now(), status, _jsonable(summary), error
        s.flush()
        s.expunge(row)
        return row


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)
