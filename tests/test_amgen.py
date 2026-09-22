"""Amgen configuration and source-to-discovery contracts; no live network."""

import json
from pathlib import Path

import httpx
import pytest
import respx

from job_monitor.config import (
    CandidateEligibilityConfig,
    SearchPreferences,
    load_companies,
    load_profiles,
)
from job_monitor.matching import match_job, parse_job
from job_monitor.models import RawJob
from job_monitor.sources import SOURCE_CLASSES, WorkdaySource


COMPANY = next(c for c in load_companies(Path("config/companies.yml")) if c.slug == "amgen")
PROFILE = load_profiles(Path("config/profiles.yml"))["clinical-discovery"]
FACETS = {"LocationCountry": ["bc33aa3152ec42d4995f4791a106ed09"]}


def test_amgen_official_broad_source_configuration():
    assert COMPANY.enabled and COMPANY.source_verified
    assert str(COMPANY.careers_url) == "https://careers.amgen.com/"
    assert COMPANY.profiles == ["clinical-discovery"]
    assert SOURCE_CLASSES[COMPANY.ats_type] is WorkdaySource
    assert COMPANY.ats_config == {
        "endpoint": "https://amgen.wd1.myworkdayjobs.com/wday/cxs/amgen/Careers/jobs",
        "site": "amgen.wd1.myworkdayjobs.com",
        "detail_base_url": "https://amgen.wd1.myworkdayjobs.com/en-US/Careers",
        "detail_api_base": "https://amgen.wd1.myworkdayjobs.com/wday/cxs/amgen/Careers",
        "limit": 20,
        "applied_facets": FACETS,
    }


@pytest.mark.asyncio
@respx.mock
async def test_amgen_pagination_retains_unfiltered_jobs_and_eligibility_evidence():
    cfg = COMPANY.model_copy(deep=True)
    cfg.ats_config["limit"] = 1
    titles = ["Clinical Project Manager", "Sales Representative"]

    def listing(request):
        body = json.loads(request.content)
        assert body["appliedFacets"] == FACETS
        assert body["searchText"] == ""
        index = body["offset"]
        return httpx.Response(200, json={"total": 2, "jobPostings": [{
            "title": titles[index], "externalPath": f"/job/US/R-{index}",
            "locationsText": "2 Locations", "bulletFields": [f"R-{index}"],
        }]})

    route = respx.post(cfg.ats_config["endpoint"]).mock(side_effect=listing)
    for index in range(2):
        respx.get(cfg.ats_config["detail_api_base"] + f"/job/US/R-{index}").respond(
            200, json={"jobPostingInfo": {
                "jobDescription": "<p>Coordinate clinical trial execution.</p>"
                                  "<p>Active RN license required.</p>",
                "location": "United States - Remote",
                "additionalLocations": ["US - Florida - Tampa"],
                "country": {"descriptor": "United States of America"},
                "remoteType": "Remote",
                "applicantLocationRequirements": {"name": "Florida"},
            }},
        )
    async with httpx.AsyncClient() as client:
        jobs = await WorkdaySource(cfg, client).fetch()
    assert [json.loads(c.request.content)["offset"] for c in route.calls] == [0, 1]
    assert [j.title for j in jobs] == titles
    assert len({j.external_job_id for j in jobs}) == 2
    for job in jobs:
        assert "Coordinate clinical trial execution." in job.description_raw
        assert "<p>" not in job.description_raw
        assert "United States - Remote" in job.location_raw
        assert "US - Florida - Tampa" in job.location_raw
        assert str(job.url).startswith(cfg.ats_config["detail_base_url"] + "/job/")
        evidence = job.metadata["eligibility"]
        assert evidence["work_arrangement"] == "Remote"
        assert evidence["applicant_locations"] == ["Florida"]
        assert "Active RN license required." in evidence["requirements"]


@pytest.mark.parametrize("title,body,expected", [
    ("Clinical Project Manager", "Coordinate clinical trial execution.", True),
    ("Scientific Project Manager", "Manage scientific research project plans.", True),
    ("GxP Project Coordinator", "Coordinate GMP validation project plans.", True),
    ("Project Manager", "Manage software releases and sales forecasts.", False),
    ("Scientist, Pathology", "Perform laboratory experiments and analyze tissue samples.", False),
    ("Sales Representative", "Our company supports clinical trials and scientific research.", False),
])
def test_amgen_uses_existing_discovery_rules(title, body, expected):
    job = RawJob(source_company="amgen", title=title, description_raw=body,
                 location_raw="United States - Remote",
                 url="https://amgen.wd1.myworkdayjobs.com/en-US/Careers/job/US/R-test")
    result = match_job(parse_job(job), PROFILE, SearchPreferences())
    assert result.eligible is expected


@pytest.mark.parametrize("location,body,arrangement,status", [
    ("United States - Remote", "", "Remote", "eligible"),
    ("United States - Remote", "Must reside in California.", "Remote", "unsuitable"),
    ("Tampa, FL", "Onsite three days per week.", "Onsite", "eligible"),
    ("Thousand Oaks, CA", "Onsite three days per week.", "Onsite", "unsuitable"),
    # State tokens in ATS labels are resolved even when the city/state order is nonstandard.
    ("US - Florida - Tampa", "Onsite three days per week.", "Onsite", "review_needed"),
    ("US - California - Thousand Oaks", "Onsite three days per week.", "Onsite", "unsuitable"),
    ("United States", "", "", "review_needed"),
    ("United States - Remote", "Active RN license required.", "Remote", "unsuitable"),
])
def test_amgen_preserves_candidate_tristate_and_florida_policy(location, body, arrangement, status):
    job = RawJob(
        source_company="amgen", title="Clinical Project Manager", location_raw=location,
        description_raw="Coordinate clinical trial execution. " + body,
        url="https://amgen.wd1.myworkdayjobs.com/en-US/Careers/job/US/R-test",
        metadata={"eligibility": {"work_arrangement": arrangement}} if arrangement else {},
    )
    prefs = SearchPreferences(
        location_terms=["Tampa, FL"],
        candidate_eligibility=CandidateEligibilityConfig(
            residence_state="Florida", held_professional_licenses=[],
            commuting_policy={"onsite_states": ["FL"]},
        ),
    )
    result = match_job(parse_job(job), PROFILE, prefs)
    assert result.discovery_eligible
    assert result.candidate_eligibility.status == status
    assert result.notification_eligible is (status == "eligible")
