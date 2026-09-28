from pathlib import Path

import httpx
import pytest
import respx

from job_monitor.active_status import ActiveStatus, verify_active_status
from job_monitor.config import load_companies
from job_monitor.matching import parse_job
from job_monitor.models import AtsType, CompanyConfig, RemoteType
from job_monitor.sources import SOURCE_CLASSES, SuccessFactorsSource


COMPANY = next(
    company
    for company in load_companies(Path("config/companies.yml"))
    if company.slug == "bausch-lomb"
)
SEARCH = str(COMPANY.ats_config["search_endpoint"])


def listing(title: str, job_id: str, location: str, date: str) -> str:
    return (
        "<table><thead><tr><th>Title</th><th>Location</th><th>Date</th></tr></thead>"
        f"<tbody><tr><td><a href='/job/{title.replace(' ', '-')}/{job_id}/'>{title}</a></td>"
        f"<td>{location}</td><td>{date}</td></tr></tbody></table>"
    )


@pytest.mark.asyncio
@respx.mock
async def test_bausch_successfactors_pagination_normalization_and_status():
    company = COMPANY.model_copy(deep=True)
    company.ats_config["page_size"] = 2
    first = listing("Clinical Trial Associate", "1365623757", "USA - Remote, US", "Sep 28, 2026")
    second = listing("Research Operations Coordinator", "1365623758", "US-NY-Rochester, US", "Sep 27, 2026")
    page1 = respx.get(SEARCH, params__contains={"startrow": "0"}).mock(
        return_value=httpx.Response(200, text=first + second)
    )
    page2 = respx.get(SEARCH, params__contains={"startrow": "2"}).mock(
        return_value=httpx.Response(200, text="<html><body>No results</body></html>")
    )
    detail1 = respx.get("https://careers.bauschlomb.com/job/Clinical-Trial-Associate/1365623757/").respond(
        200,
        text="<h1>Clinical Trial Associate</h1><div id='jobDescription'>Study operations</div>"
        "<a href='/talentcommunity/apply/1365623757/?locale=en_US'>Apply now »</a>",
    )
    detail2 = respx.get("https://careers.bauschlomb.com/job/Research-Operations-Coordinator/1365623758/").respond(
        200,
        text="<h1>Research Operations Coordinator</h1><div id='jobDescription'>Research operations</div>"
        "<a href='#'>Apply now</a>",
    )
    async with httpx.AsyncClient() as client:
        jobs = await SuccessFactorsSource(company, client).fetch()
        assert await verify_active_status(jobs[0], client, {}) is ActiveStatus.ACTIVE
        assert await verify_active_status(jobs[1], client, {}) is ActiveStatus.UNKNOWN
    assert page1.call_count == 1 and page2.call_count == 1
    assert detail1.call_count == detail2.call_count == 1
    assert [job.external_job_id for job in jobs] == ["1365623757", "1365623758"]
    assert jobs[0].stable_external_id == "1365623757"
    assert jobs[0].posted_at is not None
    assert jobs[0].metadata["successfactors"]["apply_url"].endswith("1365623757/?locale=en_US")
    assert parse_job(jobs[0]).remote_type is RemoteType.REMOTE
    assert jobs[1].location_raw == "US-NY-Rochester, US"
    assert jobs[1].metadata["active_status"] == "unknown"


@pytest.mark.asyncio
@respx.mock
async def test_successfactors_explicit_closed_and_missing_detail_are_conservative():
    company = COMPANY.model_copy(deep=True)
    company.ats_config["page_size"] = 1
    page1 = listing("Clinical Trial Associate", "111", "USA - Remote, US", "Sep 28, 2026")
    page2 = listing("Study Operations Specialist", "222", "US-FL-Tampa, US", "Sep 28, 2026")
    respx.get(SEARCH).mock(side_effect=[
        httpx.Response(200, text=page1),
        httpx.Response(200, text=page2),
        httpx.Response(200, text="<html>No jobs</html>"),
    ])
    respx.get("https://careers.bauschlomb.com/job/Clinical-Trial-Associate/111/").respond(
        200,
        text="<h1>Clinical Trial Associate</h1><p>The job posting is no longer active.</p>"
        "<a href='/talentcommunity/apply/111/'>Apply now</a>",
    )
    respx.get("https://careers.bauschlomb.com/job/Study-Operations-Specialist/222/").respond(404)
    async with httpx.AsyncClient() as client:
        jobs = await SuccessFactorsSource(company, client).fetch()
    assert len(jobs) == 1
    assert jobs[0].metadata["active_status"] == "inactive"


def test_successfactors_config_is_strict_and_registered():
    assert COMPANY.ats_type is AtsType.SUCCESSFACTORS
    assert COMPANY.profiles == ["clinical-discovery"]
    assert SOURCE_CLASSES[AtsType.SUCCESSFACTORS] is SuccessFactorsSource
    with pytest.raises(ValueError, match="search_endpoint"):
        CompanyConfig(
            slug="invalid-successfactors",
            name="Invalid",
            careers_url="https://careers.example.com/search",
            ats_type=AtsType.SUCCESSFACTORS,
            industry="biotech",
            profiles=["clinical-discovery"],
        )
