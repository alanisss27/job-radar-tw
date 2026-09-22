"""Configuration and bounded-contract tests for the 2026 Workday additions."""

import json
from pathlib import Path

import httpx
import pytest
import respx

from job_monitor.config import load_companies
from job_monitor.sources import SOURCE_CLASSES, WorkdayRequestController, WorkdaySource


COMPANIES = {
    company.slug: company
    for company in load_companies(Path("config/companies.yml"))
    if company.slug in {"biogen", "johnson-johnson", "genentech", "mass-general-brigham"}
}


EXPECTED = {
    "biogen": (
        "https://biibhr.wd3.myworkdayjobs.com/wday/cxs/biibhr/external/jobs",
        "biibhr.wd3.myworkdayjobs.com",
        "https://biibhr.wd3.myworkdayjobs.com/wday/cxs/biibhr/external",
    ),
    "johnson-johnson": (
        "https://jj.wd5.myworkdayjobs.com/wday/cxs/jj/JJ/jobs",
        "jj.wd5.myworkdayjobs.com",
        "https://jj.wd5.myworkdayjobs.com/wday/cxs/jj/JJ",
    ),
    "genentech": (
        "https://roche.wd3.myworkdayjobs.com/wday/cxs/roche/ROG-A2O-GENE/jobs",
        "roche.wd3.myworkdayjobs.com",
        "https://roche.wd3.myworkdayjobs.com/wday/cxs/roche/ROG-A2O-GENE",
    ),
    "mass-general-brigham": (
        "https://massgeneralbrigham.wd1.myworkdayjobs.com/wday/cxs/massgeneralbrigham/MGBExternal/jobs",
        "massgeneralbrigham.wd1.myworkdayjobs.com",
        "https://massgeneralbrigham.wd1.myworkdayjobs.com/wday/cxs/massgeneralbrigham/MGBExternal",
    ),
}


@pytest.mark.parametrize("slug", EXPECTED)
def test_workday_identity_and_configuration(slug):
    company = COMPANIES[slug]
    endpoint, site, detail_api = EXPECTED[slug]
    assert company.enabled and company.source_verified
    assert company.profiles == ["clinical-discovery"]
    assert SOURCE_CLASSES[company.ats_type] is WorkdaySource
    assert company.ats_config["endpoint"] == endpoint
    assert company.ats_config["site"] == site
    assert company.ats_config["detail_api_base"] == detail_api
    assert company.ats_config["detail_base_url"].startswith(f"https://{site}/en-US/")
    assert company.ats_config["limit"] == 20


def test_bounded_mgb_job_family_scope():
    facets = COMPANIES["mass-general-brigham"].ats_config["applied_facets"]
    assert set(facets) == {"jobFamily"}
    assert len(facets["jobFamily"]) == 2


@pytest.mark.asyncio
@respx.mock
async def test_genentech_listing_identity_and_detail_enrichment():
    company = COMPANIES["genentech"]
    endpoint = company.ats_config["endpoint"]
    api = company.ats_config["detail_api_base"]
    path = "/job/South-San-Francisco/Data-Strategy-Specialist_R-test"
    listing = respx.post(endpoint).respond(
        200,
        json={
            "total": 1,
            "jobPostings": [{
                "title": "Clinical Operations Project Specialist",
                "externalPath": path,
                "locationsText": "South San Francisco",
                "bulletFields": [],
            }],
        },
    )
    respx.get(api + path).respond(
        200,
        json={
            "jobPostingInfo": {
                "location": "South San Francisco",
                "additionalLocations": [],
                "country": {"descriptor": "United States of America"},
                "remoteType": "Hybrid",
                "jobDescription": "Coordinate regulated clinical operations projects.",
            }
        },
    )
    async with httpx.AsyncClient() as client:
        jobs = await WorkdaySource(
            company, client, WorkdayRequestController(min_interval_seconds=0)
        ).fetch()
    assert listing.call_count == 1
    assert jobs[0].external_job_id == path
    assert str(jobs[0].url) == company.ats_config["detail_base_url"] + path
    assert jobs[0].description_raw == "Coordinate regulated clinical operations projects."
    assert jobs[0].metadata["eligibility"]["work_arrangement"] == "Hybrid"
    assert json.loads(listing.calls[0].request.content)["appliedFacets"] == company.ats_config[
        "applied_facets"
    ]
