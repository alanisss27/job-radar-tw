"""Focused bounded AdventHealth Workday configuration tests."""

import json
from pathlib import Path

import httpx
import pytest
import respx

from job_monitor.config import load_companies
from job_monitor.sources import SOURCE_CLASSES, WorkdayRequestController, WorkdaySource


COMPANY = next(
    company for company in load_companies(Path("config/companies.yml"))
    if company.slug == "adventhealth"
)
HOST = "adventhealth.wd12.myworkdayjobs.com"
API = f"https://{HOST}/wday/cxs/adventhealth/AH_External_Career_Site"


def test_adventhealth_uses_bounded_workday_facets():
    assert COMPANY.enabled and COMPANY.source_verified
    assert COMPANY.profiles == ["clinical-discovery"]
    assert SOURCE_CLASSES[COMPANY.ats_type] is WorkdaySource
    assert COMPANY.ats_config["endpoint"] == f"{API}/jobs"
    assert COMPANY.ats_config["site"] == HOST
    assert COMPANY.ats_config["detail_base_url"] == (
        f"https://{HOST}/en-US/AH_External_Career_Site"
    )
    assert COMPANY.ats_config["detail_api_base"] == API
    assert COMPANY.ats_config["limit"] == 20
    facets = COMPANY.ats_config["applied_facets"]
    assert set(facets) == {"jobFamilyGroup", "jobFamily"}
    assert len(facets["jobFamilyGroup"]) == 2
    assert len(facets["jobFamily"]) == 4


@pytest.mark.asyncio
@respx.mock
async def test_adventhealth_paginates_bounded_faceted_inventory_and_preserves_locations():
    endpoint = f"{API}/jobs"
    first_path = "/job/ADVENTHEALTH-TAMPA/Risk-Manager_R-test"
    second_path = "/job/Remote/Clinical-Project-Coordinator_R-test"
    cfg = COMPANY.model_copy(deep=True)
    cfg.ats_config["limit"] = 2
    listing = respx.post(endpoint).mock(side_effect=[
        httpx.Response(200, json={
            "total": 3,
            "jobPostings": [
                {"title": "Risk Manager", "externalPath": first_path},
                {"title": "Clinical Project Coordinator", "externalPath": second_path},
            ],
        }),
        httpx.Response(200, json={"total": 3, "jobPostings": [
            {"title": "Quality Operations Specialist", "externalPath": "/job/Remote/Quality-Operations_R-test"},
        ]}),
    ])
    respx.get(f"{API}{first_path}").respond(
        200,
        json={
            "jobPostingInfo": {
                "location": "Tampa, Florida",
                "additionalLocations": ["Orlando, Florida"],
                "country": {"descriptor": "United States of America"},
                "remoteType": "On-site",
                "jobDescription": "Coordinate regulated quality operations.",
            }
        },
    )
    respx.get(f"{API}{second_path}").respond(
        200,
        json={
            "jobPostingInfo": {
                "location": "Remote",
                "additionalLocations": [],
                "country": {"descriptor": "United States of America"},
                "remoteType": "Fully Remote",
                "jobDescription": "Coordinate clinical operations projects.",
            }
        },
    )
    respx.get(f"{API}/job/Remote/Quality-Operations_R-test").respond(
        200,
        json={"jobPostingInfo": {
            "location": "Remote",
            "additionalLocations": [],
            "country": {"descriptor": "United States of America"},
            "remoteType": "Fully Remote",
            "jobDescription": "Support quality operations projects.",
        }},
    )
    async with httpx.AsyncClient() as client:
        jobs = await WorkdaySource(
            cfg, client, WorkdayRequestController(min_interval_seconds=0)
        ).fetch()
    assert listing.call_count == 2
    assert {job.external_job_id for job in jobs} == {
        first_path, second_path, "/job/Remote/Quality-Operations_R-test"
    }
    florida = next(job for job in jobs if job.external_job_id == first_path)
    remote = next(job for job in jobs if job.external_job_id == second_path)
    assert florida.location_raw == "Tampa, Florida; Orlando, Florida; United States of America"
    assert florida.metadata["eligibility"]["work_arrangement"] == "On-site"
    assert remote.metadata["eligibility"]["work_arrangement"] == "Fully Remote"
    assert json.loads(listing.calls[0].request.content)["appliedFacets"] == cfg.ats_config[
        "applied_facets"
    ]
