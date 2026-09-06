from __future__ import annotations

import asyncio

import pytest

from webapp.jobs import Job, JobRegistry


def test_new_job_is_pending():
    job = JobRegistry().create("Nis")
    assert job.status == "pending"
    assert job.phase is None
    assert job.area_label == "Nis"


def test_get_returns_the_job_and_none_for_unknown_ids():
    registry = JobRegistry()
    job = registry.create("Nis")
    assert registry.get(job.job_id) is job
    assert registry.get("nope") is None


def test_status_dict_has_the_documented_shape():
    job = JobRegistry().create("Nis")
    status = job.to_status_dict()
    assert set(status) == {
        "job_id", "status", "phase", "message", "started_at", "elapsed_s",
        "elements_found", "rows", "error", "area_label",
    }
    assert status["rows"] is None


@pytest.mark.asyncio
async def test_successful_run_ends_done():
    registry = JobRegistry()
    job = registry.create("Nis")

    async def work(job: Job) -> None:
        job.set_phase("querying", "radim")

    registry.start(job, work)
    await registry.wait(job.job_id)

    assert job.status == "done"
    assert job.phase == "finished"
    assert job.finished_at is not None


@pytest.mark.asyncio
async def test_failing_run_ends_error_with_a_message():
    registry = JobRegistry()
    job = registry.create("Nis")

    async def work(job: Job) -> None:
        raise RuntimeError("boom")

    registry.start(job, work)
    await registry.wait(job.job_id)

    assert job.status == "error"
    assert job.error == "boom"
    assert job.message == "boom"


@pytest.mark.asyncio
async def test_cancel_marks_the_job_cancelled_and_sets_the_event():
    registry = JobRegistry()
    job = registry.create("Nis")
    started = asyncio.Event()

    async def work(job: Job) -> None:
        started.set()
        while not job.is_cancelled():
            await asyncio.sleep(0.01)

    registry.start(job, work)
    await started.wait()
    assert registry.cancel(job.job_id) is True
    await registry.wait(job.job_id)

    assert job.status == "cancelled"
    assert job.cancel_event.is_set()


def test_cancel_of_an_unknown_job_returns_false():
    assert JobRegistry().cancel("nope") is False


@pytest.mark.asyncio
async def test_a_cancelled_job_does_not_get_overwritten_by_a_late_success():
    registry = JobRegistry()
    job = registry.create("Nis")

    async def work(job: Job) -> None:
        # to_thread, not a bare .wait(): a blocking wait would freeze the event loop.
        await asyncio.to_thread(job.cancel_event.wait)

    registry.start(job, work)
    registry.cancel(job.job_id)
    await registry.wait(job.job_id)

    assert job.status == "cancelled"


@pytest.mark.asyncio
async def test_a_job_cancelled_before_it_starts_never_runs():
    registry = JobRegistry()
    job = registry.create("Nis")
    job.cancel_event.set()
    ran = False

    async def work(job: Job) -> None:
        nonlocal ran
        ran = True

    registry.start(job, work)
    await registry.wait(job.job_id)

    assert ran is False
    assert job.status == "cancelled"


def test_prune_keeps_only_the_newest_jobs():
    registry = JobRegistry()
    jobs = [registry.create(f"job-{index}") for index in range(5)]
    for job in jobs:
        job.status = "done"

    registry.prune(max_jobs=2)

    assert registry.get(jobs[0].job_id) is None
    assert registry.get(jobs[-1].job_id) is not None


def test_prune_never_drops_a_running_job():
    registry = JobRegistry()
    running = registry.create("running")
    running.status = "running"
    for index in range(5):
        registry.create(f"done-{index}").status = "done"

    registry.prune(max_jobs=1)

    assert registry.get(running.job_id) is not None
