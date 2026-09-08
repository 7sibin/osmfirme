"""In-memory job registry.

Jobs do not survive a server restart; finished results do, because they are
written to the disk cache. Cancellation is cooperative: `cancel()` sets an
event and marks the job cancelled, but a blocking HTTP request already in
flight is abandoned rather than interrupted, and its thread may run to its own
timeout in the background.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from osm_businesses import Row

from webapp.contact_runner import ContactState
from webapp.enrich_runner import EnrichState

logger = logging.getLogger(__name__)

JobStatus = Literal["pending", "running", "done", "error", "cancelled"]
JobPhase = Literal["resolving", "querying", "parsing", "finished"]

TERMINAL: frozenset[str] = frozenset({"done", "error", "cancelled"})

GENERIC_ERROR = "Nesto je poslo naopako. Pokusaj ponovo."


@dataclass
class Job:
    job_id: str
    area_label: str
    status: JobStatus = "pending"
    phase: JobPhase | None = None
    message: str = ""
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None
    elements_found: int | None = None
    rows: list[Row] = field(default_factory=list)
    error: str | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event)
    enrich: EnrichState = field(default_factory=EnrichState)
    """What the website search has turned up for this job's rows so far."""
    enrich_cancel: threading.Event = field(default_factory=threading.Event)
    """Separate from `cancel_event`: the search is its own pass and is cancelled on its own."""
    contacts: ContactState = field(default_factory=ContactState)
    """What reading those sites turned up: addresses, numbers, socials."""
    contacts_cancel: threading.Event = field(default_factory=threading.Event)

    def set_phase(self, phase: JobPhase, message: str) -> None:
        self.phase = phase
        self.message = message

    def is_cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def elapsed_s(self) -> float:
        end = self.finished_at or datetime.now(timezone.utc)
        return (end - self.started_at).total_seconds()

    def to_status_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "status": self.status,
            "phase": self.phase,
            "message": self.message,
            "area_label": self.area_label,
            "started_at": self.started_at.isoformat(),
            "elapsed_s": round(self.elapsed_s(), 1),
            "elements_found": self.elements_found,
            "rows": len(self.rows) if self.status == "done" else None,
            "error": self.error,
            "enrich": self.enrich.progress.to_dict(),
            "contacts": self.contacts.progress.to_dict(),
        }


class JobRegistry:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._side_tasks: dict[str, list[asyncio.Task[None]]] = {}

    def create(self, area_label: str) -> Job:
        job = Job(job_id=uuid.uuid4().hex, area_label=area_label)
        self._jobs[job.job_id] = job
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def start(self, job: Job, work: Callable[[Job], Awaitable[None]]) -> None:
        if job.is_cancelled():
            # Cancelled between create() and start(): never touch the network at all.
            job.status = "cancelled"
            job.finished_at = datetime.now(timezone.utc)
            return
        job.status = "running"
        self._tasks[job.job_id] = asyncio.create_task(self._supervise(job, work))

    async def _supervise(self, job: Job, work: Callable[[Job], Awaitable[None]]) -> None:
        try:
            await work(job)
        except Exception as exc:  # noqa: BLE001 - the message is turned into a sentence
            logger.exception("job %s failed", job.job_id)
            if job.status != "cancelled":
                job.status = "error"
                job.error = str(exc) or GENERIC_ERROR
                job.message = job.error
        else:
            if job.status != "cancelled":
                job.status = "done"
                job.set_phase("finished", "Gotovo.")
        finally:
            job.finished_at = datetime.now(timezone.utc)
            self._tasks.pop(job.job_id, None)

    def start_side_task(self, job: Job, work: Awaitable[None]) -> None:
        """Run something alongside a finished job without touching its status.

        The website search is a second pass over a job that is already `done`.
        It reports through its own progress object, so a failure there must not
        turn a completed scrape into an errored one - `run_enrichment` records
        its own errors and never raises.
        """
        task = asyncio.create_task(self._supervise_side(job, work))
        self._side_tasks.setdefault(job.job_id, []).append(task)

    async def _supervise_side(self, job: Job, work: Awaitable[None]) -> None:
        try:
            await work
        except Exception:  # noqa: BLE001 - already reported through its own progress
            logger.exception("side task for job %s failed", job.job_id)
        finally:
            remaining = [
                task for task in self._side_tasks.get(job.job_id, []) if not task.done()
            ]
            if remaining:
                self._side_tasks[job.job_id] = remaining
            else:
                self._side_tasks.pop(job.job_id, None)

    async def wait_side(self, job_id: str) -> None:
        """Await the job's background passes. Used by the tests."""
        for task in list(self._side_tasks.get(job_id, [])):
            await task

    def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if job is None or job.status in TERMINAL:
            return False
        job.cancel_event.set()
        job.status = "cancelled"
        job.message = "Otkazano."
        return True

    async def wait(self, job_id: str) -> None:
        """Await the background task. Used by the tests; harmless elsewhere."""
        task = self._tasks.get(job_id)
        if task is not None:
            await task

    def prune(self, max_jobs: int = 20) -> None:
        """Drop the oldest finished jobs so a long session does not grow without bound."""
        finished = [job for job in self._jobs.values() if job.status in TERMINAL]
        finished.sort(key=lambda job: job.started_at)
        excess = len(finished) - max_jobs
        for job in finished[: max(0, excess)]:
            self._jobs.pop(job.job_id, None)
            self._side_tasks.pop(job.job_id, None)
