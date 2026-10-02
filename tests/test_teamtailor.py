from pathlib import Path

import httpx
import pytest
import respx

from job_monitor.active_status import ActiveStatus
from job_monitor.config import load_companies, load_preferences, load_profiles
from job_monitor.matching import match_job, parse_job
from job_monitor.models import AtsType, RemoteType
from job_monitor.pipeline import _potential_city_of_hope_candidate
from job_monitor.sources import SOURCE_CLASSES, SourceError, TeamtailorSource


COMPANY = next(
    company
    for company in load_companies(Path("config/companies.yml"))
    if company.slug == "cognitive-research-corporation"
)
LISTING = "https://careers.cogres.com/jobs"
REMOTE_URL = "https://careers.cogres.com/jobs/705280-senior-clinical-project-manager"


def listing() -> str:
    return """<html><ul>
      <li><a href="/jobs/705280-senior-clinical-project-manager">Senior Clinical Project Manager</a>
      <span>Clinical Operations · United States · Fully Remote</span></li>
      <li><a href="/jobs/705281-clinical-trial-associate">Clinical Trial Associate</a>
      <span>Clinical Operations · Tampa, FL · Onsite</span></li>
    </ul></html>"""


def detail(description: str, action: str = "") -> str:
    return f"""<main><h1>Senior Clinical Project Manager</h1>
      <div class="job-description">{description}</div>{action}</main>"""


@pytest.mark.asyncio
@respx.mock
async def test_teamtailor_inventory_is_listing_only_and_preserves_listing_fields():
    first = listing() + '<a rel="next" href="/jobs/page/2">Next</a>'
    page1 = respx.get(LISTING, params={}).respond(200, text=first)
    page2 = respx.get(f"{LISTING}/page/2").respond(
        200,
        text='<ul><li><a href="/jobs/705280-duplicate-slug">Duplicate title</a>'
        "<span>Wrong department · Wrong location · Onsite</span></li></ul>",
    )
    details = respx.get(REMOTE_URL).mock(return_value=httpx.Response(200, text=detail("ignored")))
    async with httpx.AsyncClient() as client:
        jobs = await TeamtailorSource(COMPANY, client).fetch()

    assert page1.called and page2.called
    assert not details.called
    assert len(jobs) == 2
    remote, onsite = jobs
    assert [job.external_job_id for job in jobs] == ["705280", "705281"]
    assert remote.stable_external_id == "705280"
    assert remote.posted_at is None
    assert remote.description_raw == ""
    assert str(remote.url) == REMOTE_URL
    assert remote.canonical_url == REMOTE_URL
    assert remote.location_raw == "United States; Fully Remote"
    assert remote.metadata["teamtailor"]["department"] == "Clinical Operations"
    assert remote.metadata["teamtailor"]["remote_status"] == "Fully Remote"
    assert remote.metadata["active_status"] == ActiveStatus.UNKNOWN.value
    assert parse_job(remote).remote_type is RemoteType.REMOTE
    assert parse_job(onsite).remote_type is RemoteType.ONSITE


@pytest.mark.asyncio
@respx.mock
async def test_teamtailor_terminal_page_without_next_is_normal_completion():
    first = respx.get(LISTING).respond(200, text=listing())
    async with httpx.AsyncClient() as client:
        jobs = await TeamtailorSource(COMPANY, client).fetch()
    assert first.called
    assert len(jobs) == 2


@pytest.mark.asyncio
@respx.mock
async def test_teamtailor_repeated_next_link_fails_visibly():
    page2 = f"{LISTING}?page=2"
    respx.get(LISTING).respond(200, text=f'<a href="{page2}" rel="next">Next</a>')
    respx.get(page2).respond(200, text=f'<a href="{LISTING}" rel="next">Next</a>')
    async with httpx.AsyncClient() as client:
        with pytest.raises(SourceError, match="pagination cycle"):
            await TeamtailorSource(COMPANY, client).fetch()


@pytest.mark.asyncio
@respx.mock
async def test_teamtailor_candidate_gate_hydrates_only_potential_candidate():
    page = """<ul>
      <li><a href="/jobs/705280-senior-clinical-project-manager">Senior Clinical Project Manager</a>
      <span>Clinical Operations · United States · Fully Remote</span></li>
      <li><a href="/jobs/705281-accounts-payable-specialist">Accounts Payable Specialist</a>
      <span>Administration · Tampa, FL · Onsite</span></li>
    </ul>"""
    respx.get(LISTING).respond(200, text=page)
    candidate_detail = respx.get(REMOTE_URL).respond(
        200,
        text=detail(
            "Coordinate clinical studies and timelines.",
            '<button>Apply for this job</button><div id="application-form">'
            "Loading application form</div>",
        ),
    )
    non_candidate_detail = respx.get(
        "https://careers.cogres.com/jobs/705281-accounts-payable-specialist"
    ).mock(return_value=httpx.Response(200, text=detail("Should not be requested")))
    profiles = load_profiles(Path("config/profiles.yml"))
    preferences = load_preferences(Path("config/preferences.yml"))
    async with httpx.AsyncClient() as client:
        source = TeamtailorSource(COMPANY, client)
        jobs = await source.fetch()
        assert not candidate_detail.called and not non_candidate_detail.called

        candidate = next(job for job in jobs if job.external_job_id == "705280")
        candidate_result = match_job(
            parse_job(candidate), profiles["clinical-discovery"], preferences
        )
        assert _potential_city_of_hope_candidate([candidate_result])
        listing_hash = candidate.content_hash
        candidate = await source.hydrate(candidate)

        non_candidate = next(job for job in jobs if job.external_job_id == "705281")
        non_candidate_result = match_job(
            parse_job(non_candidate), profiles["clinical-discovery"], preferences
        )
        assert not _potential_city_of_hope_candidate([non_candidate_result])

    assert candidate_detail.called
    assert not non_candidate_detail.called
    assert "Coordinate clinical studies" in candidate.description_raw
    assert candidate.external_job_id == "705280"
    assert candidate.stable_external_id == "705280"
    assert candidate.canonical_url == REMOTE_URL
    assert candidate.content_hash == listing_hash
    assert candidate.metadata["active_status"] == ActiveStatus.ACTIVE.value
    assert candidate.metadata["active_status_page_checked"] is True
    assert candidate.metadata["active_status_evidence"]["apply_action"]["form_affordance"]
    assert candidate.metadata["teamtailor"]["detail_checked"] is True
    assert "Coordinate clinical studies" not in non_candidate.description_raw


@pytest.mark.asyncio
@respx.mock
async def test_teamtailor_hydration_preserves_unknown_and_disabled_apply_behavior():
    page = """<ul>
      <li><a href="/jobs/1-role">Role</a><span>Science · US · Fully Remote</span></li>
      <li><a href="/jobs/2-role">Role 2</a><span>Science · US · Remote</span></li>
    </ul>"""
    respx.get(LISTING).respond(200, text=page)
    respx.get("https://careers.cogres.com/jobs/1-role").respond(
        200, text="<h1>Role</h1><div class='job-description'>Description only</div>"
    )
    respx.get("https://careers.cogres.com/jobs/2-role").respond(
        200, text="<h1>Role 2</h1><button disabled>Apply for this job</button>"
    )
    async with httpx.AsyncClient() as client:
        source = TeamtailorSource(COMPANY, client)
        jobs = await source.fetch()
        first, second = [await source.hydrate(job) for job in jobs]
    assert first.metadata["active_status"] == ActiveStatus.UNKNOWN.value
    assert second.metadata["active_status"] == ActiveStatus.INACTIVE.value
    assert first.metadata["active_status_page_checked"] is True
    assert first.metadata["teamtailor"]["detail_checked"] is True


def test_teamtailor_config_is_strict_and_registered():
    assert COMPANY.ats_type is AtsType.TEAMTAILOR
    assert COMPANY.profiles == ["clinical-discovery"]
    assert SOURCE_CLASSES[AtsType.TEAMTAILOR] is TeamtailorSource
