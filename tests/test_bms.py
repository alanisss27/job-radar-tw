"""Focused BMS Eightfold / PCS-X source and discovery contracts."""

from pathlib import Path

import httpx
import pytest
import respx

from job_monitor.config import SearchPreferences, load_companies, load_profiles
from job_monitor.matching import match_job, parse_job
from job_monitor.models import RawJob
from job_monitor.sources import EightfoldSource, SOURCE_CLASSES


COMPANY = next(
    company for company in load_companies(Path("config/companies.yml"))
    if company.slug == "bristol-myers-squibb"
)
PROFILE = load_profiles(Path("config/profiles.yml"))["clinical-discovery"]


def test_bms_uses_verified_eightfold_configuration():
    assert COMPANY.enabled and COMPANY.source_verified
    assert COMPANY.profiles == ["clinical-discovery"]
    assert SOURCE_CLASSES[COMPANY.ats_type] is EightfoldSource
    assert COMPANY.ats_config == {
        "search_endpoint": "https://jobs.bms.com/api/pcsx/search",
        "detail_endpoint": "https://jobs.bms.com/api/pcsx/position_details",
        "domain": "bms.com",
        "location": "United States",
        "limit": 10,
        "public_job_url_template": "https://jobs.bms.com/careers/job/{position_id}",
    }


def _detail(
    position_id, requisition, title, locations, description, *, work="onsite", flexibility=None
):
    return {
        "id": position_id,
        "displayJobId": requisition,
        "atsJobId": requisition,
        "name": title,
        "locations": locations,
        "standardizedLocations": locations,
        "workLocationOption": work,
        "locationFlexibility": flexibility,
        "postedTs": 1789689600,
        "jobDescription": description,
        "positionUserActions": {
            "applyAction": {
                "applyUrl": f"https://bristolmyerssquibb.wd5.myworkdayjobs.com/BMS/job/{requisition}/apply"
            }
        },
    }


@pytest.mark.asyncio
@respx.mock
async def test_bms_paginates_at_effective_ten_deduplicates_and_enriches():
    cfg = COMPANY.model_copy(deep=True)
    cfg.ats_config["limit"] = 20  # The public API caps this at 10.
    search = cfg.ats_config["search_endpoint"]
    details = cfg.ats_config["detail_endpoint"]
    pages = [
        [
            {"id": 101, "name": "Senior Clinical Trial Management Associate",
             "locations": ["Remote - United States - US"], "workLocationOption": "remote_local"},
            {"id": 102, "name": "Global Trial Lead", "locations": [
                "Princeton - NJ - US", "Warsaw - PL"], "workLocationOption": "onsite"},
        ],
        [
            {"id": 102, "name": "Global Trial Lead", "locations": [
                "Princeton - NJ - US", "Warsaw - PL"]},
            {"id": 103, "name": "Clinical Research Associate", "locations": ["Ohio - US"]},
        ],
    ]

    def search_response(request):
        params = dict(request.url.params)
        assert params["domain"] == "bms.com"
        assert params["location"] == "United States"
        assert params["num"] == "10"
        return httpx.Response(200, json={"status": 200, "data": {
            "count": 3, "positions": pages[0 if params["start"] == "0" else 1],
        }})

    route = respx.get(search).mock(side_effect=search_response)
    respx.get(details, params={"domain": "bms.com", "position_id": "101"}).respond(
        200, json={"status": 200, "data": _detail(
            101, "R1603280", "Senior Clinical Trial Management Associate",
            ["Remote - United States - US"],
            "<p>Support study vendors, TMF tracking, study timelines, and Study Lead coordination.</p>",
            work="remote_local", flexibility="remote",
        )},
    )
    respx.get(details, params={"domain": "bms.com", "position_id": "102"}).respond(
        200, json={"status": 200, "data": _detail(
            102, "R1606342", "Global Trial Lead",
            ["Princeton - NJ - US", "Warsaw - PL"],
            "<p>Own end-to-end study delivery, budgets, timelines, and vendors.</p>",
        )},
    )
    respx.get(details, params={"domain": "bms.com", "position_id": "103"}).respond(
        200, json={"status": 200, "data": _detail(
            103, "R1606243", "Clinical Research Associate", ["Ohio - US"],
            "<p>Monitor clinical trial sites and conduct site visits.</p>",
        )},
    )

    async with httpx.AsyncClient() as client:
        jobs = await EightfoldSource(cfg, client).fetch()

    assert route.call_count == 2
    assert [job.external_job_id for job in jobs] == ["101", "102", "103"]
    assert jobs[0].title == "Senior Clinical Trial Management Associate"
    assert jobs[0].metadata["eightfold"]["requisition_id"] == "R1603280"
    assert jobs[0].metadata["eightfold"]["position_id"] == 101
    assert jobs[0].metadata["eightfold"]["work_location_option"] == "remote_local"
    assert jobs[0].metadata["eightfold"]["location_flexibility"] == "remote"
    assert "TMF tracking" in jobs[0].description_raw
    assert str(jobs[0].url) == "https://jobs.bms.com/careers/job/101"
    assert jobs[0].metadata["eightfold"]["application_url"].endswith("/R1603280/apply")
    assert jobs[1].location_raw == "Princeton - NJ - US; Warsaw - PL"
    assert jobs[1].metadata["eightfold"]["locations"] == ["Princeton - NJ - US", "Warsaw - PL"]


@pytest.mark.asyncio
@respx.mock
async def test_bms_listing_failure_is_source_fatal():
    route = respx.get(COMPANY.ats_config["search_endpoint"]).respond(503)
    async with httpx.AsyncClient() as client:
        with pytest.raises(httpx.HTTPStatusError):
            await EightfoldSource(COMPANY, client).fetch()
    assert route.call_count == 3


@pytest.mark.asyncio
@respx.mock
async def test_bms_malformed_detail_is_excluded_and_reported():
    search = respx.get(COMPANY.ats_config["search_endpoint"]).respond(
        200, json={"data": {"count": 1, "positions": [
            {"id": 101, "name": "Clinical Trial Management Associate",
             "locations": ["Remote - United States - US"]},
        ]}},
    )
    detail = respx.get(COMPANY.ats_config["detail_endpoint"]).respond(
        200, json={"data": {"id": 101, "name": "Clinical Trial Management Associate"}},
    )
    async with httpx.AsyncClient() as client:
        source = EightfoldSource(COMPANY, client)
        jobs = await source.fetch()
    assert not jobs
    assert len(source.warnings) == 1
    assert source.warnings[0]["title"] == "Clinical Trial Management Associate"
    assert "ValueError" in source.warnings[0]["reason"]
    assert search.call_count == 1
    assert detail.call_count == 1


@pytest.mark.parametrize(
    ("title", "body"),
    [
        ("Senior Clinical Trial Management Associate",
         "Support study vendors, TMF tracking, study timelines, and Study Lead coordination."),
        ("Global Trial Lead",
         "Own end-to-end study delivery, budgets, timelines, and vendors."),
        ("Clinical Research Associate",
         "Monitor clinical trial sites and conduct site visits."),
    ],
)
def test_bms_representative_roles_keep_existing_discovery_behavior(title, body):
    raw = RawJob(
        source_company="bristol-myers-squibb",
        external_job_id=title,
        title=title,
        location_raw="Remote - United States - US",
        description_raw=body,
        url="https://jobs.bms.com/careers/job/example",
    )
    result = match_job(parse_job(raw), PROFILE, SearchPreferences())
    assert not result.eligible
    assert result.score < PROFILE.threshold
