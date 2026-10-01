from pathlib import Path
from urllib.parse import parse_qs
from uuid import UUID

import httpx
import pytest

from job_monitor.config import load_companies
from job_monitor.matching import parse_job
from job_monitor.models import AtsType, RemoteType
from job_monitor.sources import DynamicsAtsSource, SOURCE_CLASSES, SourceError, SourceRunner


COMPANY = next(c for c in load_companies(Path("config/companies.yml")) if c.slug == "kps-life")
ENDPOINT = COMPANY.ats_config["listing_endpoint"]
FORM = COMPANY.ats_config["form_id"]


def posting(number=1, **updates):
    job_id = str(UUID(int=number))
    return {
        "Id": job_id,
        "name": "Clinical Trial Associate",
        "description": "<p>Coordinate clinical studies and maintain the TMF.</p>"
        "<p>Bachelor's degree required. Must reside on the East Coast.</p>",
        "JobUrl": f"https://portal.dynamicsats.com/JobListing/Details/{FORM}/{job_id}",
        "dcrs_category": "Clinical Operations",
        "dcrs_type": "On Assignment",
        "dcrs_location": "Remote USA (Central/East Coast)",
        "dcrs_city": "Remote",
        "dcrs_state": "N/A",
        "dcrs_country": "United States",
        **updates,
    }


def inventory(items):
    return {"Data": items, "Total": len(items), "Errors": None, "ErrorMessage": None}


@pytest.mark.asyncio
async def test_complete_inventory_normalization_and_no_detail_requests():
    # More than the board's default 10-row display page: all rows must be retained.
    items = [posting(i) for i in range(1, 13)]
    items[-1] = posting(
        12,
        name="Finance Manager",
        dcrs_country="Mexico",
        dcrs_location="Mexico",
        dcrs_city="n/a",
        dcrs_state="n/a",
    )
    requests = []

    def handle(request):
        requests.append(request)
        assert request.method == "POST" and str(request.url) == ENDPOINT
        assert parse_qs(request.content.decode()) == {"formId": [FORM]}
        return httpx.Response(200, json=inventory(items))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        runner = SourceRunner(client)
        jobs, warnings = await runner.fetch_with_warnings(COMPANY)
        assert not warnings
        assert len(jobs) == len({j.stable_external_id for j in jobs}) == 12
        raw = jobs[0]
        assert raw.stable_external_id == items[0]["Id"]
        assert raw.source_company == "kps-life"
        assert raw.canonical_url == items[0]["JobUrl"]
        assert raw.posted_at is None
        assert raw.location_raw == "Remote USA (Central/East Coast); Remote; United States"
        assert "N/A" not in raw.location_raw
        assert parse_job(raw).remote_type is RemoteType.REMOTE
        assert "Must reside on the East Coast." in raw.description_raw
        assert "<p>" not in raw.description_raw
        assert raw.metadata["dynamics_ats"]["dcrs_type"] == "On Assignment"
        assert jobs[-1].title == "Finance Manager" and jobs[-1].location_raw == "Mexico"
        assert await runner.hydrate_candidate(COMPANY, raw) is raw
        assert len(requests) == 1  # Full descriptions are already in the inventory.
        same = (await runner.fetch(COMPANY))[0]
        assert raw.content_hash == same.content_hash
        items[0]["description"] += " Additional qualification."
        changed = (await runner.fetch(COMPANY))[0]
        assert raw.content_hash != changed.content_hash


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload, message",
    [
        ({"Data": [], "Total": 1}, "incomplete inventory"),
        ({"Data": [posting()], "Total": 2}, "incomplete inventory"),
        (inventory([posting(), posting()]), "repeated posting"),
        ({"Data": [], "Total": "0"}, "missing inventory"),
        ({"Data": [], "Total": 0, "Errors": "Unavailable"}, "unsuccessful"),
        ({"Data": [], "Total": 0, "ErrorMessage": "Invalid form"}, "unsuccessful"),
        (inventory([posting(Id=None)]), "invalid posting ID"),
        (inventory([posting(JobUrl=None)]), "detail URL"),
        (inventory([posting(JobUrl="https://example.com/job")]), "detail URL"),
        (inventory([posting(description="")]), "missing title or full description"),
        (inventory([posting(description="<p></p>")]), "empty title or full description"),
        (inventory([posting(dcrs_country={})]), "malformed location"),
        (inventory([None]), "malformed posting"),
    ],
)
async def test_incomplete_or_malformed_inventory_fails_visibly(payload, message):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    ) as client:
        with pytest.raises(SourceError, match=message):
            await DynamicsAtsSource(COMPANY, client).fetch()


@pytest.mark.asyncio
async def test_explicit_empty_inventory_is_valid():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=inventory([])))
    ) as client:
        assert await DynamicsAtsSource(COMPANY, client).fetch() == []


def test_registry():
    assert COMPANY.enabled and COMPANY.source_verified
    assert COMPANY.ats_type is AtsType.DYNAMICS_ATS
    assert COMPANY.profiles == ["clinical-discovery"]
    assert SOURCE_CLASSES[COMPANY.ats_type] is DynamicsAtsSource
