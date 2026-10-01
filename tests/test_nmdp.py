from copy import deepcopy
from pathlib import Path

import httpx
import pytest

from job_monitor.config import load_companies, load_preferences, load_profiles
from job_monitor.matching import match_job, parse_job
from job_monitor.models import AtsType, RemoteType
from job_monitor.pipeline import _potential_city_of_hope_candidate
from job_monitor.sources import OracleSource, SOURCE_CLASSES, SourceError, SourceRunner


COMPANY = next(c for c in load_companies(Path("config/companies.yml")) if c.slug == "nmdp")
IDS = ["1947", "2022", "2028", "2040", "2041", "2019"]


def item(job_id, **updates):
    return {
        "Id": job_id, "Title": "Clinical Research Project Coordinator",
        "PrimaryLocation": "Minneapolis, MN, United States",
        "PrimaryLocationCountry": "US", "WorkplaceTypeCode": "ORA_REMOTE",
        "WorkplaceType": "Remote", "PostedDate": "2026-09-30",
        "ShortDescriptionStr": "Coordinate clinical studies and study timelines.",
        "secondaryLocations": [], **updates,
    }


def page(ids, offset=0, limit=3, total=6):
    # Deliberately misleading wrapper metadata must not stop pagination.
    return {"count": 1, "hasMore": False, "offset": 0, "limit": 200, "items": [{
        "Offset": offset, "Limit": limit, "TotalJobsCount": total,
        "requisitionList": [item(i) for i in ids],
    }]}


def small_company():
    return COMPANY.model_copy(update={"ats_config": {**COMPANY.ats_config, "limit": 3}})


@pytest.mark.asyncio
async def test_inventory_runner_and_candidate_hydration():
    requests = []

    def handle(request):
        requests.append(request)
        assert "offset" not in request.url.params and "limit" not in request.url.params
        if request.url.path.endswith("recruitingCEJobRequisitionDetails"):
            assert request.url.params["finder"] == "ById;Id=2028,siteNumber=CX_2"
            return httpx.Response(200, json={"items": [{
                "Id": "2028", "ExternalDescriptionStr": "<p>Manage clinical studies.</p>",
                "ExternalQualificationsStr": "<p>Bachelor's degree required.</p>",
                "ExternalResponsibilitiesStr": "<p>Coordinate study timelines.</p>",
            }]})
        finder = request.url.params["finder"]
        assert finder.startswith("findReqs;siteNumber=CX_2,facetsList=NONE,limit=3,offset=")
        offset = int(finder.rsplit("=", 1)[1])
        assert offset in (0, 3)
        return httpx.Response(200, json=page(IDS[offset:offset + 3], offset))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        runner = SourceRunner(client)
        jobs, warnings = await runner.fetch_with_warnings(small_company())
        assert not warnings
        assert [j.stable_external_id for j in jobs] == IDS
        assert len(requests) == 2  # No detail I/O during inventory retrieval.
        candidate = jobs[2]
        result = match_job(
            parse_job(candidate), load_profiles(Path("config/profiles.yml"))["clinical-discovery"],
            load_preferences(Path("config/preferences.yml")),
        )
        assert _potential_city_of_hope_candidate([result])
        hydrated = await runner.hydrate_candidate(COMPANY, candidate)
        assert len(requests) == 3
        assert "Manage clinical studies." in hydrated.description_raw
        assert "Bachelor's degree required." in hydrated.description_raw
        assert "Coordinate study timelines." in hydrated.description_raw
        assert hydrated.content_hash == candidate.content_hash
        assert candidate.metadata["oracle"].get("detail_checked") is None
        assert await runner.hydrate_candidate(COMPANY, hydrated) is hydrated
        assert len(requests) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("failure, message", [
    ("repeated", "repeated requisition"),
    ("offset", "non-advancing"),
    ("empty", "unexpected empty"),
    ("partial", "page count"),
    ("total", "total changed"),
    ("metadata", "pagination metadata"),
    ("zero_limit", "non-advancing"),
])
async def test_pagination_failures_are_visible(failure, message):
    responses = [page(IDS[:3]), page(IDS[3:], 3)]
    second = responses[1]["items"][0]
    if failure == "repeated":
        second["requisitionList"] = [item(i) for i in IDS[:3]]
    elif failure == "offset":
        second["Offset"] = 0
    elif failure == "empty":
        second["requisitionList"] = []
    elif failure == "partial":
        second["requisitionList"].pop()
    elif failure == "total":
        second["TotalJobsCount"] = 7
    elif failure == "metadata":
        del second["Offset"]
    elif failure == "zero_limit":
        second["Limit"] = 0

    def handle(request):
        return httpx.Response(200, json=responses.pop(0))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(SourceError, match=message):
            await OracleSource(small_company(), client).fetch()
    assert not responses


@pytest.mark.asyncio
async def test_empty_inventory_is_valid():
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=page([], total=0))
    )) as client:
        assert await OracleSource(small_company(), client).fetch() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("detail", [
    {"Id": "9999", "ExternalDescriptionStr": "Wrong job"},
    {"Id": "2028", "ExternalDescriptionStr": ""},
])
async def test_invalid_detail_fails_visibly(detail):
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"items": [detail]})
    )) as client:
        source = OracleSource(COMPANY, client)
        with pytest.raises(SourceError):
            await source.hydrate(source._normalize(item("2028")))


def test_normalization_and_registry():
    assert COMPANY.enabled and COMPANY.source_verified
    assert COMPANY.profiles == ["clinical-discovery"]
    assert COMPANY.ats_type is AtsType.ORACLE
    assert SOURCE_CLASSES[COMPANY.ats_type] is OracleSource
    source = OracleSource(COMPANY, None)
    data = item(2028, secondaryLocations=[{"LocationName": "Boston, MA, United States"}])
    raw = source._normalize(data)
    assert raw.stable_external_id == "2028"
    assert raw.canonical_url == str(COMPANY.careers_url) + "/job/2028"
    assert raw.location_raw == "Minneapolis, MN, United States; Boston, MA, United States"
    assert raw.metadata["oracle"]["secondary_locations"] == data["secondaryLocations"]
    assert parse_job(raw).remote_type is RemoteType.REMOTE
    hybrid = source._normalize(item("1947", WorkplaceTypeCode="ORA_HYBRID", WorkplaceType="Hybrid"))
    assert parse_job(hybrid).remote_type is RemoteType.HYBRID
    assert raw.content_hash == source._normalize(data).content_hash
    changed = deepcopy(data)
    changed["secondaryLocations"] = []
    assert raw.content_hash != source._normalize(changed).content_hash
    assert source._normalize(item("1", Title="Financial Analyst")).title == "Financial Analyst"
    with pytest.raises(SourceError):
        source._normalize(item(None))
