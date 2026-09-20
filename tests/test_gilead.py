"""Focused Gilead Workday configuration, scope, and discovery contracts."""

import json
import re
from pathlib import Path

import httpx
import pytest
import respx

from job_monitor.config import SearchPreferences, load_companies, load_profiles
from job_monitor.matching import match_job, parse_job
from job_monitor.models import RawJob
from job_monitor.sources import SOURCE_CLASSES, SourceError, WorkdayRequestController, WorkdaySource


COMPANY = next(
    company for company in load_companies(Path("config/companies.yml"))
    if company.slug == "gilead-sciences"
)
PROFILE = load_profiles(Path("config/profiles.yml"))["clinical-discovery"]
PATTERN = re.compile(COMPANY.ats_config["facet_patterns"]["locations"])


def test_gilead_uses_verified_official_workday_configuration():
    assert COMPANY.enabled and COMPANY.source_verified
    assert COMPANY.profiles == ["clinical-discovery"]
    assert SOURCE_CLASSES[COMPANY.ats_type] is WorkdaySource
    assert COMPANY.ats_config["endpoint"] == (
        "https://gilead.wd1.myworkdayjobs.com/wday/cxs/gilead/gileadcareers/jobs"
    )
    assert COMPANY.ats_config["site"] == "gilead.wd1.myworkdayjobs.com"
    assert COMPANY.ats_config["detail_base_url"] == (
        "https://gilead.wd1.myworkdayjobs.com/en-US/gileadcareers"
    )
    assert COMPANY.ats_config["detail_api_base"] == (
        "https://gilead.wd1.myworkdayjobs.com/wday/cxs/gilead/gileadcareers"
    )
    assert COMPANY.ats_config["limit"] == 20
    assert COMPANY.ats_config["validate_location_facets"] is True
    assert COMPANY.ats_config["location_facet_country"] == "United States of America"
    assert set(COMPANY.ats_config["facet_patterns"]) == {"locations"}


@pytest.mark.parametrize(
    "label",
    ["United States - California - Foster City", "United States - New Jersey - Parsippany",
     "US Field", "US Remote"],
)
def test_gilead_scope_pattern_accepts_us_location_labels(label):
    assert PATTERN.search(label)


@pytest.mark.parametrize(
    "label", ["Ireland - Dublin", "Canada - Toronto", "Remote - Europe", "2 Locations",
               "United States - All", "US"]
)
def test_gilead_scope_pattern_rejects_ambiguous_or_foreign_labels(label):
    assert not PATTERN.search(label)


def _facets():
    return [{
        "facetParameter": "locationMainGroup",
        "values": [{
            "facetParameter": "locations",
            "values": [
                {"descriptor": "United States - California - Foster City", "id": "us-foster"},
                {"descriptor": "US Remote", "id": "us-remote"},
                {"descriptor": "Ireland - Dublin", "id": "ie-dublin"},
            ],
        }],
    }]


@pytest.mark.asyncio
@respx.mock
async def test_gilead_resolves_nested_us_facets_deduplicates_and_enriches_details():
    cfg = COMPANY.model_copy(deep=True)
    cfg.ats_config["limit"] = 2
    endpoint = cfg.ats_config["endpoint"]
    detail_base = cfg.ats_config["detail_api_base"]

    def listing(request):
        body = json.loads(request.content)
        if not body["appliedFacets"]:
            return httpx.Response(200, json={"facets": _facets()})
        assert body["appliedFacets"] == {"locations": ["us-foster", "us-remote"]}
        if body["offset"] == 0:
            postings = [
                {"externalPath": "/job/R-good", "title": "Clinical Program Manager",
                 "locationsText": "United States - California - Foster City"},
                {"externalPath": "/job/R-mixed", "title": "Clinical Trials Manager",
                 "locationsText": "2 Locations"},
            ]
        else:
            postings = [
                {"externalPath": "/job/R-mixed", "title": "Clinical Trials Manager",
                 "locationsText": "2 Locations"},
                {"externalPath": "/job/R-remote", "title": "Clinical Operations Intern",
                 "locationsText": "US Remote"},
            ]
        return httpx.Response(200, json={"total": 3, "jobPostings": postings})

    listing_route = respx.post(endpoint).mock(side_effect=listing)
    respx.get(detail_base + "/job/R-good").respond(200, json={"jobPostingInfo": {
        "location": "United States - California - Foster City",
        "additionalLocations": ["United States - California - Santa Monica"],
        "country": {"descriptor": "United States of America"},
        "jobDescription": "<p>Hybrid-Eligible clinical program leadership.</p>",
    }})
    respx.get(detail_base + "/job/R-mixed").respond(200, json={"jobPostingInfo": {
        "location": "Ireland - Dublin",
        "additionalLocations": ["United States - California - Foster City"],
        "country": {"descriptor": "Ireland"},
        "jobRequisitionLocation": {"country": {"alpha2Code": "IE"}},
        "jobDescription": "<p>Coordinate global study operations.</p>",
    }})
    respx.get(detail_base + "/job/R-remote").respond(200, json={"jobPostingInfo": {
        "location": "US Remote",
        "additionalLocations": [],
        "country": {"descriptor": "United States of America"},
        "remoteType": "Remote",
        "jobDescription": "<p>Remote-Eligible study operations support.</p>",
    }})

    async with httpx.AsyncClient() as client:
        jobs = await WorkdaySource(
            cfg, client, WorkdayRequestController(min_interval_seconds=0)
        ).fetch()

    assert listing_route.call_count == 3  # facet discovery plus two pages
    page_requests = [json.loads(call.request.content) for call in listing_route.calls[1:]]
    assert all(request["limit"] == 2 for request in page_requests)
    assert all(request["appliedFacets"] == {"locations": ["us-foster", "us-remote"]}
               for request in page_requests)
    assert [job.external_job_id for job in jobs] == ["/job/R-good", "/job/R-mixed", "/job/R-remote"]
    assert "Hybrid-Eligible" in jobs[0].description_raw
    assert "Remote-Eligible" in jobs[2].description_raw
    mixed = jobs[1]
    evidence = mixed.metadata["workday_locations"]
    assert evidence["primary"] == "Ireland - Dublin"
    assert evidence["additional"] == ["United States - California - Foster City"]
    assert evidence["scoped_additional"] == ["United States - California - Foster City"]
    assert "Ireland - Dublin" in mixed.location_raw
    assert "United States - California - Foster City, United States of America" in mixed.location_raw


@pytest.mark.asyncio
@respx.mock
async def test_gilead_missing_or_unsafe_location_facets_fail_closed():
    endpoint = COMPANY.ats_config["endpoint"]
    route = respx.post(endpoint).respond(200, json={"facets": [{
        "facetParameter": "locationMainGroup",
        "values": [{"facetParameter": "locations", "values": [
            {"descriptor": "Ireland - Dublin", "id": "ie"},
        ]}],
    }]})
    async with httpx.AsyncClient() as client:
        with pytest.raises(SourceError, match="no IDs"):
            await WorkdaySource(COMPANY, client).fetch()
    assert len(route.calls) == 1


@pytest.mark.parametrize(
    ("title", "description"),
    [
        ("Clinical Program Manager", "Own clinical program timelines, budgets, and vendors."),
        ("Clinical Trials Manager, Early Phase", "Lead study execution, sites, vendors, and protocols."),
        ("Director, Clinical Operations", "Lead global clinical operations and study teams."),
    ],
)
def test_current_gilead_senior_roles_remain_below_existing_discovery_threshold(title, description):
    raw = RawJob(
        source_company="gilead-sciences", external_job_id=title, title=title,
        location_raw="United States - California - Foster City",
        description_raw=description,
        url="https://gilead.wd1.myworkdayjobs.com/gileadcareers/job/example",
    )
    result = match_job(parse_job(raw), PROFILE, SearchPreferences())
    assert not result.eligible
    assert result.score < PROFILE.threshold


@pytest.mark.parametrize(
    "title", ["Clinical Project Coordinator", "Clinical Operations Associate", "Clinical Trial Associate"]
)
def test_existing_transition_families_remain_unchanged(title):
    raw = RawJob(
        source_company="gilead-sciences", external_job_id=title, title=title,
        location_raw="US Remote",
        description_raw="Coordinate clinical study timelines, risks, budgets, and deliverables.",
        url="https://gilead.wd1.myworkdayjobs.com/gileadcareers/job/example",
    )
    assert match_job(parse_job(raw), PROFILE, SearchPreferences()).eligible
