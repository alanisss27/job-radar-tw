from pathlib import Path

import httpx
import pytest
import respx

from job_monitor.active_status import ActiveStatus
from job_monitor.config import load_companies
from job_monitor.models import AtsType, RawJob
from job_monitor.sources import CharterResearchSource, SOURCE_CLASSES, SourceRunner


COMPANY = next(
    item for item in load_companies(Path("config/companies.yml"))
    if item.slug == "charter-research"
)
LISTING = "https://www.charterresearch.com/open-roles/"


def row(job_id: str, title: str, location: str) -> str:
    return f"""<tr><td style="border:0"></td><td><a href="/careers/job/{job_id}">{title}</a></td>
      <td class="notranslate">{location}</td></tr>"""


@pytest.mark.asyncio
@respx.mock
async def test_charter_listing_retrieves_and_deduplicates_without_detail_fetch():
    detail_url = "https://www.charterresearch.com/careers/job/637997130"
    listing = respx.get(LISTING).respond(
        200,
        text=("<table>" + row("637997130", "Regulatory Associate", "Orlando")
              + row("621235456", "Clinical Research Coordinator", "Orlando")
              + row("637997130", "Regulatory Associate", "Orlando") + "</table>"),
    )
    detail = respx.get(detail_url).mock(return_value=httpx.Response(200))

    async with httpx.AsyncClient() as client:
        jobs = await CharterResearchSource(COMPANY, client).fetch()

    assert listing.called and not detail.called
    assert len(jobs) == 2
    role = next(job for job in jobs if job.external_job_id == "637997130")
    assert role.title == "Regulatory Associate"
    assert role.location_raw == "Orlando"
    assert role.canonical_url == detail_url
    assert role.content_hash == role.metadata["charter_research"]["listing_hash"]


@pytest.mark.asyncio
@respx.mock
async def test_charter_candidate_hydration_uses_official_detail_and_apply_evidence():
    detail_url = "https://www.charterresearch.com/careers/job/621235456"
    respx.get(detail_url).respond(
        200,
        text="""<main><h1>Clinical Research Coordinator</h1>
        <div class="job-description">Coordinate clinical trials and study visits.</div>
        <a href="/application/621235456">Apply</a></main>""",
    )
    raw = RawJob(
        source_company=COMPANY.slug,
        external_job_id="621235456",
        title="Clinical Research Coordinator",
        location_raw="Orlando",
        url=detail_url,
        metadata={"charter_research": {"detail_url": detail_url, "listing_hash": "listing-hash"}},
    )
    async with httpx.AsyncClient() as client:
        hydrated = await SourceRunner(client).hydrate_candidate(COMPANY, raw)

    assert hydrated.description_raw == "Coordinate clinical trials and study visits."
    assert hydrated.metadata["active_status"] == ActiveStatus.ACTIVE.value
    assert hydrated.content_hash == raw.content_hash


@pytest.mark.asyncio
@respx.mock
async def test_charter_unavailable_candidate_detail_is_inactive():
    detail_url = "https://www.charterresearch.com/careers/job/621235456"
    respx.get(detail_url).respond(410)
    raw = RawJob(
        source_company=COMPANY.slug,
        external_job_id="621235456",
        title="Clinical Research Coordinator",
        url=detail_url,
        metadata={"charter_research": {"detail_url": detail_url, "listing_hash": "listing-hash"}},
    )
    async with httpx.AsyncClient() as client:
        hydrated = await CharterResearchSource(COMPANY, client).hydrate(raw)
    assert hydrated.metadata["active_status"] == ActiveStatus.INACTIVE.value


def test_charter_config_and_source_registration():
    assert COMPANY.ats_type is AtsType.CHARTER_RESEARCH
    assert COMPANY.enabled and COMPANY.source_verified
    assert COMPANY.profiles == ["clinical-discovery"]
    assert SOURCE_CLASSES[AtsType.CHARTER_RESEARCH] is CharterResearchSource
