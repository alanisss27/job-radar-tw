"""Focused Otsuka Workday configuration and bounded source contract tests."""

import json
from pathlib import Path

import httpx
import pytest
import respx

from job_monitor.config import load_companies
from job_monitor.sources import SOURCE_CLASSES, WorkdayRequestController, WorkdaySource


COMPANY = next(
    company for company in load_companies(Path("config/companies.yml"))
    if company.slug == "otsuka-pharmaceutical"
)
HOST = "vhr-otsuka.wd1.myworkdayjobs.com"
API = f"https://{HOST}/wday/cxs/vhr_otsuka/External"


def test_otsuka_uses_existing_workday_configuration():
    assert COMPANY.enabled and COMPANY.source_verified
    assert COMPANY.profiles == ["clinical-discovery"]
    assert SOURCE_CLASSES[COMPANY.ats_type] is WorkdaySource
    assert COMPANY.ats_config["endpoint"] == f"{API}/jobs"
    assert COMPANY.ats_config["site"] == HOST
    assert COMPANY.ats_config["detail_base_url"] == f"https://{HOST}/en-US/External"
    assert COMPANY.ats_config["detail_api_base"] == API
    assert COMPANY.ats_config["limit"] == 20
    assert COMPANY.ats_config["location_facet_parameter"] == "locationCountry"
    assert COMPANY.ats_config["validate_location_facets"] is True
    assert COMPANY.ats_config["location_facet_country"] == "United States of America"


@pytest.mark.asyncio
@respx.mock
async def test_otsuka_listing_identity_detail_and_remote_metadata():
    listing = respx.post(f"{API}/jobs").mock(side_effect=[
        httpx.Response(200, json={
            "facets": [{
                "facetParameter": "locationCountry",
                "values": [{
                    "id": "bc33aa3152ec42d4995f4791a106ed09",
                    "descriptor": "United States of America",
                }],
            }],
        }),
        httpx.Response(200, json={
            "total": 1,
            "jobPostings": [{
                "title": "Manager, Regulatory Operations",
                "externalPath": "/job/Remote/Manager-Regulatory-Operations_R12900",
                "locationsText": "Remote",
                "remoteType": "Remote",
            }],
        }),
    ])
    path = "/job/Remote/Manager-Regulatory-Operations_R12900"
    respx.get(f"{API}{path}").respond(
        200,
        json={
            "jobPostingInfo": {
                "location": "Remote",
                "additionalLocations": ["Princeton, New Jersey, United States of America"],
                "country": {"descriptor": "United States of America"},
                "remoteType": "Remote",
                "jobDescription": "Coordinate regulated clinical operations deliverables.",
            }
        },
    )
    async with httpx.AsyncClient() as client:
        jobs = await WorkdaySource(
            COMPANY, client, WorkdayRequestController(min_interval_seconds=0)
        ).fetch()
    assert listing.call_count == 2
    assert jobs[0].external_job_id == path
    assert str(jobs[0].url) == COMPANY.ats_config["detail_base_url"] + path
    assert jobs[0].metadata["eligibility"]["work_arrangement"] == "Remote"
    assert jobs[0].metadata["workday_locations"]["additional"] == [
        "Princeton, New Jersey, United States of America"
    ]
    assert json.loads(listing.calls[1].request.content)["appliedFacets"] == {
        "locationCountry": ["bc33aa3152ec42d4995f4791a106ed09"]
    }
