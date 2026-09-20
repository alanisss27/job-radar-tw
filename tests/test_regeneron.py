"""Official Regeneron location scoping and unchanged discovery contracts."""

import json
import re
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
from job_monitor.sources import SOURCE_CLASSES, SourceError, WorkdaySource


COMPANY = next(c for c in load_companies(Path("config/companies.yml")) if c.slug == "regeneron")
PROFILE = load_profiles(Path("config/profiles.yml"))["clinical-discovery"]
PATTERN = re.compile(COMPANY.ats_config["facet_patterns"]["locations"])
BODY = "Support clinical study team updates. Maintain investigator files and update CTMS."


def test_official_broad_configuration():
    assert COMPANY.enabled and COMPANY.source_verified
    assert COMPANY.profiles == ["clinical-discovery"]
    assert SOURCE_CLASSES[COMPANY.ats_type] is WorkdaySource
    assert COMPANY.ats_config["endpoint"] == (
        "https://regeneron.wd1.myworkdayjobs.com/wday/cxs/regeneron/Careers/jobs"
    )
    assert COMPANY.ats_config["validate_location_facets"] is True
    assert "applied_facets" not in COMPANY.ats_config
    assert "search_texts" not in COMPANY.ats_config
    assert PROFILE.threshold == 0.70
    assert PROFILE.responsibility_min_hits == 2


@pytest.mark.parametrize("label", [
    "TARRYTOWN", "SLEEPY HOLLOW", "Armonk", "Warren", "Cambridge", "Hawthorne",
    "Los Angeles", "Seattle", "Saratoga Springs", "Washington DC", "RENSSELAER",
    "RENSS - GLOBAL VIEW", "RENSS - BLD17 Filling", "RENSS - MENANDS", "RENSS - SUNY",
    "RENSS - TECH VALLEY", "RENSS - TEMPEL LN", "Tampa, FL", "Albany, NY",
    "Remote - United States", "Remote - Florida", "Remote - North Dakota", "Honolulu, HI",
])
def test_validated_campuses_and_nationwide_state_labels(label):
    assert PATTERN.search(label)


@pytest.mark.parametrize("label", [
    "Dublin", "Limerick", "Uxbridge1", "Hyderabad", "Toronto", "Tokyo", "Amsterdam3",
    "Remote - Ireland", "Remote - Germany", "Remote - United Kingdom", "Remote - Italy",
    "Unknown Campus", "Cambridge, United Kingdom", "Tampa, FL, Canada", "2 Locations",
])
def test_foreign_and_unknown_labels_are_not_scope_evidence(label):
    assert not PATTERN.search(label)


def facets():
    return [{"facetParameter": "locationMainGroup", "values": [{
        "facetParameter": "locations", "values": [
            {"descriptor": "TARRYTOWN", "id": "us-campus-new-id"},
            {"descriptor": "Remote - United States", "id": "us-remote"},
            {"descriptor": "Dublin", "id": "foreign"},
        ],
    }]}]


@pytest.mark.asyncio
@respx.mock
async def test_scoped_pagination_deduplication_and_mixed_primary_locations():
    cfg = COMPANY.model_copy(deep=True)
    cfg.ats_config["limit"] = 2
    titles = ["Clinical Study Associate", "Clinical Study Specialist", "Sales Representative"]

    def listing(request):
        body = json.loads(request.content)
        assert body["searchText"] == ""
        if not body["appliedFacets"]:
            return httpx.Response(200, json={"facets": facets()})
        assert body["appliedFacets"] == {"locations": ["us-campus-new-id", "us-remote"]}
        indices = [0, 1] if body["offset"] == 0 else [1, 2]
        return httpx.Response(200, json={"total": 4, "jobPostings": [{
            "externalPath": f"/job/test/R-{i}", "title": titles[i],
            "locationsText": "2 Locations", "bulletFields": [f"R-{i}"],
        } for i in indices]})

    route = respx.post(cfg.ats_config["endpoint"]).mock(side_effect=listing)
    for i in range(3):
        respx.get(cfg.ats_config["detail_api_base"] + f"/job/test/R-{i}").respond(
            200, json={"jobPostingInfo": {
                "location": "Dublin" if i == 1 else "TARRYTOWN",
                "additionalLocations": ["TARRYTOWN"] if i == 1 else ["Warren"],
                "country": {"descriptor": "Ireland" if i == 1 else "United States of America"},
                "jobRequisitionLocation": {"country": {"alpha2Code": "IE" if i == 1 else "US"}},
                "jobDescription": f"<p>{BODY}</p>",
            }},
        )
    async with httpx.AsyncClient() as client:
        jobs = await WorkdaySource(cfg, client).fetch()
    assert len(jobs) == 3
    assert len(route.calls) == 3
    assert [j.title for j in jobs] == titles  # Ingestion includes non-discovery roles.
    mixed = jobs[1]
    assert mixed.location_raw == "Dublin; Ireland; IE; TARRYTOWN, United States of America"
    evidence = mixed.metadata["workday_locations"]
    assert evidence["primary"] == "Dublin"
    assert evidence["primary_country"] == {"descriptor": "Ireland"}
    assert evidence["additional"] == ["TARRYTOWN"]
    assert evidence["listing"] == "2 Locations"
    assert evidence["scoped_additional"] == ["TARRYTOWN"]
    assert evidence["facet_country"] == "United States of America"
    assert evidence["requisition_location"]["country"]["alpha2Code"] == "IE"
    assert mixed.description_raw == BODY
    assert match_job(parse_job(jobs[0]), PROFILE, SearchPreferences()).eligible
    assert not match_job(parse_job(jobs[2]), PROFILE, SearchPreferences()).eligible
    preferences = SearchPreferences(candidate_eligibility=CandidateEligibilityConfig())
    result = match_job(parse_job(mixed), PROFILE, preferences)
    assert result.discovery_eligible
    assert result.candidate_eligibility.status == "review_needed"
    assert not result.notification_eligible


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize("detail,status", [
    ({"location": "Dublin", "country": {"descriptor": "United States of America"}}, 200),
    ({"location": "2 Locations"}, 200),
    ({"location": "TARRYTOWN", "additionalLocations": "Dublin"}, 200),
    ({"location": "Cambridge", "country": {"descriptor": "United Kingdom"}}, 200),
    ({}, 200),
    ({}, 503),
])
async def test_ignored_scope_missing_or_failed_details_fail_closed(detail, status):
    cfg = COMPANY.ats_config
    respx.post(cfg["endpoint"]).mock(side_effect=[
        httpx.Response(200, json={"facets": facets()}),
        httpx.Response(200, json={"total": 1, "jobPostings": [{
            "externalPath": "/job/R-1", "title": "Clinical Study Specialist",
            "locationsText": "TARRYTOWN",  # Listing/country alone cannot prove scope.
        }]}),
    ])
    respx.get(cfg["detail_api_base"] + "/job/R-1").respond(
        status, json={"jobPostingInfo": detail},
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(SourceError):
            await WorkdaySource(COMPANY, client).fetch()


@pytest.mark.asyncio
@respx.mock
async def test_disappearing_us_facets_never_fall_back_to_global():
    route = respx.post(COMPANY.ats_config["endpoint"]).respond(200, json={"facets": [{
        "facetParameter": "locations", "values": [{"descriptor": "Dublin", "id": "ie"}],
    }]})
    async with httpx.AsyncClient() as client:
        with pytest.raises(SourceError, match="no IDs"):
            await WorkdaySource(COMPANY, client).fetch()
    assert len(route.calls) == 1


@pytest.mark.parametrize("title,body,expected", [
    ("Clinical Study Specialist", BODY, True),
    ("Clinical Study Associate", BODY, True),
    ("Clinical Study Specialist", "Our company conducts clinical trials.", False),
    ("Clinical Study Specialist", "Maintain investigator files and update CTMS.", False),
    ("Clinical Study Associate Manager", BODY, False),
    ("Clinical Project Manager", "Coordinate clinical trial execution.", True),
    ("Sales Representative", BODY, False),
])
def test_existing_evidence_gates_without_company_exceptions(title, body, expected):
    raw = RawJob(source_company="regeneron", title=title, description_raw=body,
                 location_raw="TARRYTOWN; United States of America; Warren",
                 url="https://regeneron.wd1.myworkdayjobs.com/en-US/Careers/job/test")
    result = match_job(parse_job(raw), PROFILE, SearchPreferences())
    assert result.eligible is expected


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["facet_patterns", "detail_api_base"])
async def test_location_validation_requires_configuration(missing):
    cfg = COMPANY.model_copy(deep=True)
    del cfg.ats_config[missing]
    async with httpx.AsyncClient() as client:
        with pytest.raises(SourceError, match="requires"):
            await WorkdaySource(cfg, client).fetch()


@pytest.mark.parametrize("location,body,status", [
    ("Remote - United States; United States of America; US", BODY, "eligible"),
    ("Remote - United States; United States of America; US",
     BODY + " Must reside in California.", "unsuitable"),
    ("Uxbridge1; United Kingdom; GB; Armonk, United States of America; "
     "Warren, United States of America", BODY + " This role is hybrid.", "review_needed"),
])
def test_scoped_availability_does_not_override_candidate_policy(location, body, status):
    raw = RawJob(source_company="regeneron", title="Clinical Study Specialist",
                 description_raw=body, location_raw=location,
                 url="https://regeneron.wd1.myworkdayjobs.com/en-US/Careers/job/test")
    preferences = SearchPreferences(candidate_eligibility=CandidateEligibilityConfig(
        residence_state="Florida", held_professional_licenses=[],
        commuting_policy={"onsite_states": ["FL"]},
    ))
    result = match_job(parse_job(raw), PROFILE, preferences)
    assert result.discovery_eligible
    assert result.candidate_eligibility.status == status
    assert result.notification_eligible is (status == "eligible")
