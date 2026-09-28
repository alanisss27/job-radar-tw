from datetime import UTC, datetime

import httpx
import pytest

from job_monitor.active_status import (
    ActiveStatus,
    manual_verification_label,
    notification_status_allows,
    structured_status,
    verify_active_status,
    verify_if_actionable,
)
from job_monitor.config import Settings
from job_monitor.models import MatchedJob, MatchResult, ParsedJob, ProfileName, RawJob
from job_monitor.notifier import render_run_summary
from job_monitor.pipeline import _qualifies_for_immediate_notification


def raw(*, metadata=None, posted_at=None):
    return RawJob(
        source_company="acme",
        title="Clinical Project Coordinator",
        description_raw="Coordinate clinical study timelines.",
        posted_at=posted_at,
        url="https://careers.example.test/jobs/123",
        metadata=metadata or {},
    )


def test_workday_can_apply_false_is_inactive():
    assert structured_status({"active_status_evidence": {"canApply": False}}) is ActiveStatus.INACTIVE


@pytest.mark.parametrize(
    ("evidence", "expected"),
    [
        (
            {"posted": True, "canApply": True, "apply_url": "https://apply.example.test/123"},
            ActiveStatus.ACTIVE,
        ),
        ({"posted": True, "canApply": True, "apply_url": None}, ActiveStatus.UNKNOWN),
        ({"posted": True, "canApply": True}, ActiveStatus.UNKNOWN),
        ({"active_status": "active", "posted": True, "canApply": True}, ActiveStatus.UNKNOWN),
        ({"posted": True, "canApply": False}, ActiveStatus.INACTIVE),
        ({"active_status": "active", "posted": False}, ActiveStatus.INACTIVE),
        (
            {"posted": False, "canApply": True, "apply_url": "https://apply.example.test/123"},
            ActiveStatus.INACTIVE,
        ),
        ({"posted": True}, ActiveStatus.UNKNOWN),
    ],
)
def test_workday_status_requires_current_apply_evidence(evidence, expected):
    metadata = {
        "workday": {"externalPath": "/job/123"},
        "active_status_evidence": evidence,
    }
    assert structured_status(metadata) is expected


@pytest.mark.parametrize(
    "evidence",
    [
        {"eightfold": {"application_url": "https://apply.example.test/123"}},
        {"active_status_evidence": {"apply_url": "/apply/123"}},
    ],
)
def test_reliable_apply_evidence_is_active(evidence):
    assert structured_status(evidence) is ActiveStatus.ACTIVE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("<html><h1>Clinical Project Coordinator</h1><p>Job description</p></html>", ActiveStatus.UNKNOWN),
        ("<html><p>This job posting is no longer active.</p></html>", ActiveStatus.INACTIVE),
        ("<html><button>Apply now</button></html>", ActiveStatus.ACTIVE),
        ("<html><button disabled>Apply</button></html>", ActiveStatus.INACTIVE),
    ],
)
async def test_official_page_evidence(body, expected):
    async def handler(request):
        return httpx.Response(200, text=body)

    job = raw()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await verify_active_status(job, client, {}) is expected
    assert job.metadata["active_status"] == expected.value


@pytest.mark.asyncio
async def test_workday_unknown_can_still_become_inactive_on_explicit_closure():
    async def handler(request):
        return httpx.Response(200, text="<p>This job posting is no longer active.</p>")

    job = raw(metadata={
        "workday": {"externalPath": "/job/123"},
        "active_status_evidence": {"posted": True, "canApply": True},
    })
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await verify_active_status(job, client, {}) is ActiveStatus.INACTIVE


def test_old_posting_age_is_informational_when_apply_evidence_exists():
    job = raw(
        metadata={"active_status_evidence": {"apply_url": "https://apply.example.test/123"}},
        posted_at=datetime(2020, 1, 1, tzinfo=UTC),
    )
    result = MatchResult(profile=ProfileName.HEALTHCARE, score=.95, eligible=True, tier="strong")
    assert _qualifies_for_immediate_notification(
        ParsedJob(raw=job), result, datetime(2026, 1, 1, tzinfo=UTC), Settings(), is_new=True
    )
    assert structured_status(job.metadata) is ActiveStatus.ACTIVE


def test_unknown_retains_candidate_with_manual_verification_label():
    job_status = structured_status({
        "workday": {},
        "active_status_evidence": {"posted": True, "canApply": True},
    })
    assert job_status is ActiveStatus.UNKNOWN
    assert notification_status_allows(job_status)
    assert manual_verification_label(ActiveStatus.UNKNOWN) == (
        "Active status not confirmed — manual verification needed"
    )


def test_explicitly_inactive_is_suppressed_from_notification():
    assert not notification_status_allows(ActiveStatus.INACTIVE)


def test_summary_labels_unknown_and_hides_inactive_postings():
    active_unknown = raw(metadata={"active_status": "unknown"})
    inactive = raw(metadata={"active_status": "inactive"}).model_copy(
        update={"title": "Closed Project Coordinator"}
    )
    result = MatchResult(profile=ProfileName.HEALTHCARE, score=.9, eligible=True, tier="strong")
    matches = [
        MatchedJob(company_name="Acme", job=ParsedJob(raw=job), result=result,
                   first_seen_at=datetime.now(UTC), is_new=True, changed=False)
        for job in (active_unknown, inactive)
    ]
    summary = render_run_summary(
        run_key="daily-test", stats={}, errors=[], matched_jobs=matches, zero_job_sources=[]
    )
    assert "Active status not confirmed — manual verification needed" in summary
    assert "Closed Project Coordinator" not in summary


@pytest.mark.asyncio
async def test_status_verification_is_skipped_before_actionable_gate(monkeypatch):
    called = False

    async def verify(*args):
        nonlocal called
        called = True
        return ActiveStatus.ACTIVE

    monkeypatch.setattr("job_monitor.active_status.verify_active_status", verify)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200))) as client:
        result = await verify_if_actionable(False, raw(), client, {})
    assert result is None
    assert not called
