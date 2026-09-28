from pathlib import Path

import httpx
import pytest
import respx

from job_monitor.active_status import ActiveStatus
from job_monitor.config import load_companies
from job_monitor.matching import parse_job
from job_monitor.models import AtsType, RemoteType
from job_monitor.sources import SOURCE_CLASSES, TeamtailorSource


COMPANY = next(
    company
    for company in load_companies(Path("config/companies.yml"))
    if company.slug == "cognitive-research-corporation"
)
LISTING = "https://careers.cogres.com/jobs"


def listing() -> str:
    return """<html><ul>
      <li><a href="/jobs/705280-senior-clinical-project-manager">Senior Clinical Project Manager</a>
      <span>Clinical Operations · United States · Fully Remote</span></li>
      <li><a href="/jobs/705281-clinical-trial-associate">Clinical Trial Associate</a>
      <span>Clinical Operations · Tampa, FL · Onsite</span></li>
    </ul></html>"""


@pytest.mark.asyncio
@respx.mock
async def test_teamtailor_listing_and_details_normalize_conservatively():
    respx.get(LISTING).respond(200, text=listing())
    respx.get("https://careers.cogres.com/jobs/705280-senior-clinical-project-manager").respond(
        200,
        text="""<main><h1>Senior Clinical Project Manager</h1>
          <div class="job-description">Coordinate clinical studies.</div>
          <button>Apply for this job</button><div id="application-form">Loading application form</div>
        </main>""",
    )
    respx.get("https://careers.cogres.com/jobs/705281-clinical-trial-associate").respond(
        200,
        text="""<main><h1>Clinical Trial Associate</h1><div class="job-description">Support trials.</div>
          <button>Apply for this job</button><div id="application-form">Loading application form</div>
        </main>""",
    )
    async with httpx.AsyncClient() as client:
        jobs = await TeamtailorSource(COMPANY, client).fetch()

    assert len(jobs) == 2
    remote, onsite = jobs
    assert [job.external_job_id for job in jobs] == ["705280", "705281"]
    assert remote.stable_external_id == "705280"
    assert remote.posted_at is None
    assert "Coordinate clinical studies" in remote.description_raw
    assert str(remote.url).endswith("705280-senior-clinical-project-manager")
    assert remote.metadata["teamtailor"]["department"] == "Clinical Operations"
    assert remote.metadata["active_status"] == ActiveStatus.ACTIVE.value
    assert onsite.metadata["active_status"] == ActiveStatus.ACTIVE.value
    assert parse_job(remote).remote_type is RemoteType.REMOTE
    assert parse_job(onsite).remote_type is RemoteType.ONSITE


@pytest.mark.asyncio
@respx.mock
async def test_teamtailor_missing_apply_is_unknown_and_disabled_is_inactive():
    page = """<ul><li><a href="/jobs/1-role">Role</a><span>Science · US · Fully Remote</span></li>
      <li><a href="/jobs/2-role">Role 2</a><span>Science · US · Remote</span></li></ul>"""
    respx.get(LISTING).respond(200, text=page)
    respx.get("https://careers.cogres.com/jobs/1-role").respond(
        200, text="<h1>Role</h1><div class='job-description'>Description only</div>"
    )
    respx.get("https://careers.cogres.com/jobs/2-role").respond(
        200, text="<h1>Role 2</h1><button disabled>Apply for this job</button>"
    )
    async with httpx.AsyncClient() as client:
        jobs = await TeamtailorSource(COMPANY, client).fetch()
    assert jobs[0].metadata["active_status"] == ActiveStatus.UNKNOWN.value
    assert jobs[1].metadata["active_status"] == ActiveStatus.INACTIVE.value


def test_teamtailor_config_is_strict_and_registered():
    assert COMPANY.ats_type is AtsType.TEAMTAILOR
    assert COMPANY.profiles == ["clinical-discovery"]
    assert SOURCE_CLASSES[AtsType.TEAMTAILOR] is TeamtailorSource
