"""In-process background jobs for tools too slow for one MCP round-trip.

claude.ai's connector transport abandons a tool call that runs too long, but the
work itself is legitimately slow (a batch of illustrations ~60s, a preview render
of a long book 30s+). So a slow tool hands its work to :func:`run_with_soft_timeout`:
if the work finishes inside the soft timeout the tool answers inline (the model
never learns a job existed), otherwise it returns a job id and the model polls
:func:`get` / :func:`wait`.

Job state lives in this process only: it does not survive a restart, and finished
jobs are dropped after ``JOB_TTL_S``. Pruning is lazy — done inside the public
functions — so importing this module starts no threads and the executor itself is
built on first use (cheap import, nothing inherited across a fork).

Note for callers: the pool's threads are joined at interpreter exit, so a runaway
job delays container shutdown. Every ``fn`` submitted here must carry its own
timeout (subprocess timeout, request timeout, …) rather than relying on the caller
giving up.
"""

from __future__ import annotations

import logging
import math
import os
import re
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import wait as _futures_wait
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


class JobError(Exception):
    """Bad job id or bad polling argument — never the job's own failure.

    A job that raised is reported through the snapshot dict (``status="error"``),
    or re-raised as its original exception by :func:`run_with_soft_timeout`.
    """


def _env_num(name: str, default: float, minimum: float, cast, maximum: float | None = None) -> Any:
    """Read a numeric env var, clamping to [minimum, maximum]; junk falls back to default."""
    raw = os.getenv(name, "")
    if not raw.strip():
        return cast(default)
    try:
        value = cast(raw)
    except (TypeError, ValueError, OverflowError):
        logger.warning("%s=%r is not a number; using %s", name, raw, default)
        return cast(default)
    if not math.isfinite(value):
        logger.warning("%s=%r is not a finite number; using %s", name, raw, default)
        return cast(default)
    if value < minimum:
        logger.warning("%s=%s is below the minimum %s; using %s", name, value, minimum, minimum)
        return cast(minimum)
    if maximum is not None and value > maximum:
        logger.warning("%s=%s is above the maximum %s; using %s", name, value, maximum, maximum)
        return cast(maximum)
    return value


MAX_WORKERS: int = _env_num("JOB_MAX_WORKERS", 4, 1, int)
# Ceiling on any single blocking wait. Past ~9.2e9s the underlying lock raises a bare
# OverflowError, and long before that a wait would pin an MCP request thread with no
# way for the model to recover; capping lets it poll again instead.
MAX_WAIT_S: float = 600.0
# Wall-clock budget a tool may spend waiting before it hands the caller a job id.
SOFT_TIMEOUT_S: float = _env_num("MCP_SOFT_TIMEOUT_S", 90.0, 0.0, float, MAX_WAIT_S)
# How long a finished job's result stays pollable.
JOB_TTL_S: float = _env_num("JOB_TTL_S", 3600.0, 1.0, float)

_lock = threading.RLock()  # re-entrant: a fast job's done-callback fires inside submit()
_jobs: dict[str, _Job] = {}
_executor: ThreadPoolExecutor | None = None


@dataclass(slots=True)
class _Job:
    job_id: str
    label: str
    future: Future
    started: float
    finished: float | None = field(default=None)


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return (
        datetime.fromtimestamp(ts, timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _clean_label(label: str) -> str:
    """Labels are echoed back to the model and into logs — keep them one short line."""
    if not label:
        return ""
    return re.sub(r"\s+", " ", str(label).replace("\x00", "")).strip()[:120]


def _executor_locked() -> ThreadPoolExecutor:
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="job")
        logger.info("Job executor started with %d worker(s)", MAX_WORKERS)
    return _executor


def _prune_locked() -> None:
    cutoff = time.time() - JOB_TTL_S
    stale = [jid for jid, j in _jobs.items() if j.finished is not None and j.finished < cutoff]
    for jid in stale:
        del _jobs[jid]
    if stale:
        logger.info("Dropped %d job(s) older than %ds", len(stale), int(JOB_TTL_S))


def _on_done(job: _Job, fut: Future) -> None:
    with _lock:
        job.finished = time.time()
    elapsed = job.finished - job.started
    name = job.label or job.job_id
    if fut.cancelled():
        logger.warning("Job %s (%s) cancelled after %.1fs", job.job_id, name, elapsed)
        return
    exc = fut.exception()
    if exc is not None:
        logger.warning(
            "Job %s (%s) failed after %.1fs: %s: %s",
            job.job_id, name, elapsed, type(exc).__name__, exc,
        )
    else:
        logger.info("Job %s (%s) finished in %.1fs", job.job_id, name, elapsed)


def _submit_job(fn: Callable[[], Any], label: str) -> _Job:
    if not callable(fn):
        raise JobError(
            f"submit() needs a zero-argument callable, got {type(fn).__name__}. "
            "Wrap the work in a lambda or functools.partial, e.g. "
            "submit(lambda: render(html), label='preview')."
        )
    label = _clean_label(label)
    with _lock:
        _prune_locked()
        while True:
            job_id = uuid.uuid4().hex[:12]
            if job_id not in _jobs:
                break
        # Submit under the lock so no caller can observe a half-registered job.
        # A job that finishes instantly just blocks its own done-callback until
        # this block exits; the callback never waits on anything we hold.
        future = _executor_locked().submit(fn)
        job = _Job(job_id=job_id, label=label, future=future, started=time.time())
        _jobs[job_id] = job
        future.add_done_callback(lambda f, j=job: _on_done(j, f))
    logger.info("Job %s submitted (%s)", job_id, label or "unlabelled")
    return job


def _snapshot(job: _Job, *, include_result: bool) -> dict[str, Any]:
    fut = job.future
    status = "running"
    result: Any = None
    error: str | None = None
    if fut.done():
        if fut.cancelled():
            status, error = "error", "job was cancelled"
        else:
            exc = fut.exception()
            if exc is not None:
                status = "error"
                error = f"{type(exc).__name__}: {exc}"
            else:
                status = "done"
                result = fut.result()

    with _lock:
        finished = job.finished
    # The done-callback lands a moment after the future flips to done; treat that
    # window as "finished now" so status and timestamps never disagree.
    end = finished if finished is not None else time.time()

    snap: dict[str, Any] = {"job_id": job.job_id, "label": job.label, "status": status}
    if include_result:
        snap["result"] = result
    snap["error"] = error
    snap["started"] = _iso(job.started)
    snap["finished"] = _iso(end) if status != "running" else None
    snap["elapsed_s"] = round(end - job.started, 2)
    return snap


def _recent_locked(limit: int) -> list[dict[str, Any]]:
    jobs = sorted(_jobs.values(), key=lambda j: j.started, reverse=True)[:limit]
    return [_snapshot(j, include_result=False) for j in jobs]


def _lookup(job_id: str) -> _Job:
    key = job_id.strip() if isinstance(job_id, str) else ""
    if "\x00" in key:
        key = ""
    with _lock:
        _prune_locked()
        job = _jobs.get(key)
        if job is not None:
            return job
        known = _recent_locked(10)
    if known:
        listing = "Job ids currently on this server: " + ", ".join(
            f"{d['job_id']} ({d['status']}"
            + (f", {d['label']}" if d["label"] else "")
            + ")"
            for d in known
        )
    else:
        listing = "No jobs exist on this server right now."
    raise JobError(
        f"Unknown job_id {job_id!r}. Job ids are 12 hex characters, live only inside "
        f"this server process (lost on restart), and finished jobs are dropped "
        f"{int(JOB_TTL_S)}s after they end. {listing}. "
        "If the job you wanted is gone, re-run the tool that created it."
    )


def _coerce_timeout(value: Any, arg: str, default: float) -> float:
    try:
        seconds = float(value)
    except (TypeError, ValueError, OverflowError):
        raise JobError(
            f"{arg} must be a number of seconds (got {value!r}); "
            f"omit it to use the server default of {default}s."
        ) from None
    if not math.isfinite(seconds):
        raise JobError(
            f"{arg} must be a finite number of seconds between 0 and {MAX_WAIT_S} "
            f"(got {value!r}); omit it to use the server default of {default}s."
        )
    if seconds > MAX_WAIT_S:
        # Silently capping beats blocking forever: the job keeps running and the
        # caller gets a "running" snapshot it can poll again.
        logger.warning("%s=%s exceeds the %ss ceiling; waiting %ss", arg, seconds, MAX_WAIT_S, MAX_WAIT_S)
        return MAX_WAIT_S
    return max(0.0, seconds)


def submit(fn: Callable[[], Any], label: str = "") -> str:
    """Run ``fn()`` on the shared pool; return a 12-hex job id to poll with :func:`get`."""
    return _submit_job(fn, label).job_id


def run_with_soft_timeout(
    fn: Callable[[], Any],
    soft_timeout: float | None = None,
    label: str = "",
) -> tuple[bool, str, Any]:
    """Run ``fn()``, waiting up to ``soft_timeout`` seconds for it to finish.

    Returns ``(finished, job_id, result)``. When ``finished`` is False the job is
    still running in the background and ``result`` is None — hand the caller the
    job id. When it is True the job's return value is ready; a job that raised
    inside the window re-raises its original exception here, so a fast failure
    reaches the model as an error instead of a job id it would poll pointlessly.

    ``soft_timeout=None`` uses ``MCP_SOFT_TIMEOUT_S`` (default 90s); 0 means never
    wait, so everything but an already-finished job goes to the background.
    """
    limit = SOFT_TIMEOUT_S if soft_timeout is None else _coerce_timeout(
        soft_timeout, "soft_timeout", SOFT_TIMEOUT_S
    )
    job = _submit_job(fn, label)
    done, _ = _futures_wait([job.future], timeout=limit)
    if not done:
        logger.info(
            "Job %s (%s) still running after %.0fs soft timeout; backgrounding",
            job.job_id, job.label or "unlabelled", limit,
        )
        return False, job.job_id, None
    return True, job.job_id, job.future.result()  # re-raises the worker's exception


def get(job_id: str) -> dict[str, Any]:
    """Snapshot a job: job_id, label, status, result, error, started, finished, elapsed_s.

    ``status`` is "running" | "done" | "error"; ``result`` is only populated when
    status is "done", ``error`` only when it is "error". Raises :class:`JobError`
    for an unknown or expired id.
    """
    return _snapshot(_lookup(job_id), include_result=True)


def wait(job_id: str, timeout: float = 60) -> dict[str, Any]:
    """Block up to ``timeout`` seconds for a job, then return the same dict as :func:`get`.

    A job that failed comes back as ``status="error"`` — this does not raise the
    job's own exception; a still-running job comes back as ``status="running"``.
    ``timeout`` is capped at ``MAX_WAIT_S``; poll again if the job is still running.
    """
    limit = _coerce_timeout(timeout, "timeout", 60.0)
    job = _lookup(job_id)
    if limit and not job.future.done():
        _futures_wait([job.future], timeout=limit)
    return _snapshot(job, include_result=True)


def recent(limit: int = 20) -> list[dict[str, Any]]:
    """Newest-first job snapshots without ``result`` — safe to dump into a tool reply."""
    try:
        n = max(1, int(limit))
    except (TypeError, ValueError):
        raise JobError(f"limit must be a positive integer (got {limit!r}).") from None
    with _lock:
        _prune_locked()
        return _recent_locked(n)
