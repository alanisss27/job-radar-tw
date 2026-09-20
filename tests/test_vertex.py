"""Focused Vertex Workday configuration, ingestion, and discovery tests."""

import json
from pathlib import Path

import httpx
import pytest
import respx

from job_monitor.config import SearchPreferences, load_companies, load_profiles
from job_monitor.matching import match_job, parse_job
from job_monitor.models import RawJob
from job_monitor.sources import SOURCE_CLASSES, WorkdayRequestController, WorkdaySource


COMPANY = next(
    company for company in load_companies(Path("config/companies.yml"))
    if company.slug == "vertex-pharmaceuticals"
)
PROFILE = load_profiles(Path("config/profiles.yml"))["clinical-discovery"]


def test_vertex_uses_verified_official_workday_configuration():
    assert COMPANY.enabled and COMPANY.source_verified
    assert COMPANY.profiles == ["clinical-discovery"]
    assert SOURCE_CLASSES[COMPANY.ats_type] is WorkdaySource
    assert COMPANY.ats_config == {
        "endpoint": "https://vrtx.wd501.myworkdayjobs.com/wday/cxs/vrtx/Vertex_Careers/jobs",
        "site": "vrtx.wd501.myworkdayjobs.com",
        "detail_base_url": "https://vrtx.wd501.myworkdayjobs.com/en-US/Vertex_Careers",
        "detail_api_base": "https://vrtx.wd501.myworkdayjobs.com/wday/cxs/vrtx/Vertex_Careers",
        "limit": 20,
        "applied_facets": {
            "locationCountry": ["bc33aa3152ec42d4995f4791a106ed09"],
        },
    }


@pytest.mark.asyncio
@respx.mock
async def test_vertex_paginates_limit_twenty_and_enriches_details():
    cfg = COMPANY.model_copy(deep=True)
    cfg.ats_config["limit"] = 2
    endpoint = cfg.ats_config["endpoint"]
    detail_base = cfg.ats_config["detail_api_base"]
    listings = respx.post(endpoint).mock(side_effect=[
        httpx.Response(200, json={
            "total": 3,
            "jobPostings": [
                {"externalPath": "/job/1", "title": "Clinical Study Quality Lead",
                 "locationsText": "Boston, MA"},
                {"externalPath": "/job/2", "title": "GMP Validation Associate Director",
                 "locationsText": "Boston, MA"},
            ],
        }),
        # Vertex may report total=0 after the first page while still returning jobs.
        httpx.Response(200, json={
            "total": 0,
            "jobPostings": [
                {"externalPath": "/job/3", "title": "Clinical Operations Manager",
                 "locationsText": "Boston, MA"},
            ],
        }),
    ])
    for path, detail in {
        "/job/1": {
            "location": "Boston, MA",
            "additionalLocations": ["Seattle, WA"],
            "country": {"descriptor": "United States of America"},
            "jobDescription": "<p>Hybrid-Eligible clinical study quality oversight.</p>",
        },
        "/job/2": {
            "location": "Boston, MA",
            "additionalLocations": [],
            "country": {"descriptor": "United States of America"},
            "jobDescription": "<p>Remote-Eligible GMP validation project.</p>",
        },
        "/job/3": {
            "location": "Boston, MA",
            "additionalLocations": [],
            "country": {"descriptor": "United States of America"},
            "jobDescription": "<p>Study operations and project timelines.</p>",
        },
    }.items():
        respx.get(detail_base + path).respond(200, json={"jobPostingInfo": detail})

    async with httpx.AsyncClient() as client:
        jobs = await WorkdaySource(
            cfg, client, WorkdayRequestController(min_interval_seconds=0)
        ).fetch()

    assert listings.call_count == 2
    requests = [json.loads(call.request.content) for call in listings.calls]
    assert all(request["limit"] == 2 for request in requests)
    assert all(
        request["appliedFacets"] == {
            "locationCountry": ["bc33aa3152ec42d4995f4791a106ed09"]
        }
        for request in requests
    )
    assert len(jobs) == 3
    assert all("Boston, MA" in job.location_raw for job in jobs)
    assert "Seattle, WA" in jobs[0].location_raw
    assert "Hybrid-Eligible" in jobs[0].description_raw
    assert "Remote-Eligible" in jobs[1].description_raw
    assert all(str(job.url).startswith(cfg.ats_config["detail_base_url"]) for job in jobs)


@pytest.mark.parametrize(
    ("title", "description"),
    [
        (
            "Clinical Monitoring Associate Director",
            "Global Clinical Operations, study oversight, GCP compliance, and clinical site management.",
        ),
        (
            "Clinical Development, Medical Director- Neurology",
            "Medical lead for clinical trials, protocols, and cross-functional study execution teams.",
        ),
        (
            "Senior Director, GCP Quality",
            "GCP operational quality, quality risk management, and inspection readiness.",
        ),
    ],
)
def test_vertex_senior_and_false_friend_roles_stay_below_discovery_threshold(
    title, description
):
    raw = RawJob(
        source_company="vertex-pharmaceuticals",
        external_job_id=title,
        title=title,
        location_raw="Boston, MA",
        description_raw=description,
        url="https://vrtx.wd501.myworkdayjobs.com/Vertex_Careers/job/example",
    )
    result = match_job(parse_job(raw), PROFILE, SearchPreferences())
    assert not result.eligible
    assert result.score < PROFILE.threshold


@pytest.mark.parametrize(
    "title",
    [
        "Clinical Project Coordinator",
        "Clinical Operations Associate",
        "Clinical Trial Associate",
        "Associate Project Manager - Clinical",
        "GxP Project Manager",
    ],
)
def test_existing_transition_families_remain_unchanged(title):
    raw = RawJob(
        source_company="vertex-pharmaceuticals",
        external_job_id=title,
        title=title,
        location_raw="Remote",
        description_raw="Coordinate clinical study timelines, risks, budgets, and project deliverables.",
        url="https://vrtx.wd501.myworkdayjobs.com/Vertex_Careers/job/example",
    )
    result = match_job(parse_job(raw), PROFILE, SearchPreferences())
    assert result.eligible
