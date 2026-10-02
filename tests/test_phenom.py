from pathlib import Path

import httpx
import json
import pytest
import respx

from job_monitor.config import load_companies
from job_monitor.models import AtsType
from job_monitor.sources import PhenomSource, SOURCE_CLASSES, SourceError


COMPANY = next(
    company for company in load_companies(Path("config/companies.yml")) if company.slug == "merck"
)
LISTING = "https://jobs.merck.com/us/en/search-results/"
LILLY = next(
    company
    for company in load_companies(Path("config/companies.yml"))
    if company.slug == "eli-lilly"
)
LILLY_LISTING = "https://careers.lilly.com/us/en/search-results/"


def item(seq: str = "MERCUSR1ENUS", job_id: str = "R1", **values):
    return {
        "jobSeqNo": seq,
        "jobId": job_id,
        "reqId": job_id,
        "title": "Clinical Research Associate",
        "postedDate": "2026-09-20T00:00:00.000+0000",
        "dateCreated": "2026-09-19T12:00:00.000+0000",
        "category": "Clinical",
        "type": "Full time",
        "descriptionTeaser": "Coordinate clinical research activities.",
        "cityStateCountry": "Rahway, New Jersey, United States",
        "location": "Rahway, New Jersey, United States",
        "multi_location": ["Rahway, NJ, USA"],
        **values,
    }


def page(items, total, next_offset=None, listing=LISTING):
    next_link = (
        f'<link rel="next" href="{listing}?from={next_offset}&amp;s=1">'
        if next_offset is not None
        else ""
    )
    payload = {
        "eagerLoadRefineSearch": {
            "status": 200,
            "hits": len(items),
            "totalHits": total,
            "data": {"jobs": items},
        }
    }
    return f"<html><head>{next_link}</head><script>var phApp={{}}; phApp.ddo = {json.dumps(payload)};</script></html>"


@pytest.mark.asyncio
@respx.mock
async def test_phenom_inventory_paginates_parses_structured_jobs_without_details():
    first = item(
        multi_location=["Rahway, NJ, USA", "West Point, PA, USA"],
        multi_location_array=[
            {"location": "Rahway, NJ, USA"},
            {"location": "West Point, PA, USA"},
        ],
    )
    page1 = respx.get(LISTING + "?s=1").respond(
        200, text=page([first, item("MERCUSR2ENUS", "R2")], 3, 10)
    )
    page2 = respx.get(LISTING + "?from=10&s=1").respond(
        200, text=page([item("MERCUSR3ENUS", "R3")], 4)
    )
    async with httpx.AsyncClient() as client:
        jobs = await PhenomSource(COMPANY, client).fetch()

    assert page1.called and page2.called
    assert len(respx.calls) == 2  # inventory makes no job-detail requests
    assert jobs[0].external_job_id == "MERCUSR1ENUS"
    assert jobs[0].title == "Clinical Research Associate"
    assert jobs[0].posted_at.isoformat().startswith("2026-09-20")
    assert jobs[0].description_raw == "Coordinate clinical research activities."
    assert jobs[0].location_raw == "Rahway, NJ, USA; West Point, PA, USA"
    assert str(jobs[0].url) == "https://jobs.merck.com/us/en/job/R1/clinical-research-associate"
    assert jobs[0].metadata["phenom"]["job_id"] == "R1"
    assert jobs[0].metadata["phenom"]["req_id"] == "R1"
    assert jobs[0].metadata["phenom"]["total_hits"] == 3
    assert jobs[0].metadata["phenom"]["category"] == "Clinical"
    assert jobs[0].metadata["phenom"]["type"] == "Full time"


@pytest.mark.asyncio
@respx.mock
async def test_phenom_empty_terminal_and_out_of_range_page_is_valid():
    respx.get(LISTING + "?s=1").respond(200, text=page([item()], 1, 10))
    respx.get(LISTING + "?from=10&s=1").respond(200, text=page([], 1))
    async with httpx.AsyncClient() as client:
        jobs = await PhenomSource(COMPANY, client).fetch()
    assert len(jobs) == 1


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize(
    ("next_offset", "second"),
    [
        (0, None),
        (10, [item("MERCUSR1ENUS", "R-duplicate")]),
    ],
)
async def test_phenom_stalled_and_duplicate_pagination_fail(next_offset, second):
    respx.get(LISTING + "?s=1").respond(
        200, text=page([item(f"MERCUSR{i}ENUS", f"R{i}") for i in range(10)], 20, next_offset)
    )
    if second is not None:
        respx.get(LISTING + "?from=10&s=1").respond(200, text=page(second, 20))
    async with httpx.AsyncClient() as client:
        with pytest.raises(SourceError):
            await PhenomSource(COMPANY, client).fetch()


@pytest.mark.asyncio
@respx.mock
async def test_phenom_accepts_changing_totals_and_short_final_page():
    respx.get(LISTING + "?s=1").respond(
        200, text=page([item(f"MERCUSR{i}ENUS", f"R{i}") for i in range(10)], 11, 10)
    )
    respx.get(LISTING + "?from=10&s=1").respond(200, text=page([item("MERCUSR11ENUS", "R11")], 12))
    async with httpx.AsyncClient() as client:
        jobs = await PhenomSource(COMPANY, client).fetch()
    assert len(jobs) == 11


@pytest.mark.asyncio
@respx.mock
async def test_phenom_candidate_hydration_fetches_full_description_and_arrangement():
    listing = item()
    respx.get(LISTING + "?s=1").respond(200, text=page([listing], 1))
    detail_url = "https://jobs.merck.com/us/en/job/R1/clinical-research-associate"
    detail = respx.get(detail_url).respond(
        200,
        text=(
            '<script>var phApp={}; phApp.ddo = {"jobDetail":{"data":{"job":'
            '{"jobDescription":"<p>Full clinical research role description.</p>"}}}};</script>'
            "<main>Flexible Work Arrangements: Hybrid</main>"
        ),
    )
    async with httpx.AsyncClient() as client:
        source = PhenomSource(COMPANY, client)
        jobs = await source.fetch()
        assert not detail.called
        hydrated = await source.hydrate(jobs[0])
    assert detail.called
    assert hydrated.description_raw == "Full clinical research role description."
    assert hydrated.metadata["phenom"]["work_arrangement"] == "Hybrid"
    assert hydrated.metadata["eligibility"]["work_arrangement"] == "Hybrid"


def test_phenom_is_registered_and_enabled():
    assert COMPANY.enabled is True
    assert COMPANY.ats_type is AtsType.PHENOM
    assert SOURCE_CLASSES[AtsType.PHENOM] is PhenomSource


@pytest.mark.asyncio
@respx.mock
async def test_lilly_phenom_payload_and_offset_pagination_need_no_inventory_details():
    first = item(
        "LILLYUS1ENUS", "R-1001", title="Research Scientist", location="Indianapolis, Indiana"
    )
    first_route = respx.get(LILLY_LISTING + "?s=1").respond(
        200, text=page([first], 11, 10, LILLY_LISTING)
    )
    next_route = respx.get(LILLY_LISTING + "?from=10&s=1").respond(
        200, text=page([item("LILLYUS2ENUS", "R-1002")], 11, listing=LILLY_LISTING)
    )
    async with httpx.AsyncClient() as client:
        jobs = await PhenomSource(LILLY, client).fetch()

    assert first_route.called and next_route.called
    assert len(respx.calls) == 2
    assert [job.external_job_id for job in jobs] == ["LILLYUS1ENUS", "LILLYUS2ENUS"]
    assert str(jobs[0].url) == ("https://careers.lilly.com/us/en/job/R-1001/research-scientist")
    assert jobs[0].metadata["phenom"]["job_id"] == "R-1001"
