"""Wave 1 config and adapter contracts; descriptions below are synthetic.

Endpoint/field shapes, IDs, location labels and restriction types were verified
against the public sources on 2026-09-24. No network or production writes here.
"""

import json
from datetime import UTC, datetime
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
from job_monitor.eligibility import assess_candidate
from job_monitor.matching import match_job, parse_job
from job_monitor.sources import SOURCE_CLASSES, GreenhouseSource, WorkdaySource
from job_monitor.storage import JobIndexRow, Storage

COMPANIES = {c.slug: c for c in load_companies(Path("config/companies.yml"))}
SMPA = COMPANIES["sumitomo-pharma-america"]
BEACON = COMPANIES["beacon-biosignals"]
PROFILE = load_profiles(Path("config/profiles.yml"))["clinical-discovery"]
FLORIDA = SearchPreferences(
    candidate_eligibility=CandidateEligibilityConfig(
        residence_state="FL",
        held_professional_licenses=[],
        commuting_policy={"onsite_states": ["FL"]},
    )
)
BODY = "Support clinical research operations and maintain essential study documents."


def assert_repeat_is_unchanged(raw):
    """Exercise the persistence planner without opening any database."""
    now = datetime.now(UTC)
    first = Storage._plan_from_row(raw, None, now)
    index = JobIndexRow(first.job_id, raw.content_hash, first.first_seen_at)
    repeated = raw.model_copy(update={"fetched_at": datetime.now(UTC)})
    assert repeated.stable_external_id == raw.stable_external_id
    assert repeated.content_hash == raw.content_hash
    plan = Storage._plan_from_row(repeated, index, now)
    assert not plan.is_new and not plan.changed
    assert plan.job_id == first.job_id


@pytest.mark.parametrize(
    "company,adapter",
    [
        (SMPA, WorkdaySource),
        (BEACON, GreenhouseSource),
    ],
)
def test_wave1_uses_existing_adapter_and_only_clinical_discovery(company, adapter):
    assert company.enabled and company.source_verified
    assert company.profiles == ["clinical-discovery"]
    assert SOURCE_CLASSES[company.ats_type] is adapter
    assert sum(c.ats_config == company.ats_config for c in COMPANIES.values()) == 1


def test_canonical_wave1_source_identifiers():
    assert SMPA.ats_config == {
        "endpoint": "https://sumitomopharma.wd5.myworkdayjobs.com/wday/cxs/sumitomopharma/SMPA/jobs",
        "site": "sumitomopharma.wd5.myworkdayjobs.com",
        "detail_base_url": "https://sumitomopharma.wd5.myworkdayjobs.com/en-US/SMPA",
        "detail_api_base": "https://sumitomopharma.wd5.myworkdayjobs.com/wday/cxs/sumitomopharma/SMPA",
        "limit": 20,
    }
    assert BEACON.ats_config == {"board_token": "beaconbiosignals"}


@pytest.mark.asyncio
@respx.mock
async def test_smpa_pagination_details_remote_identity_and_discovery():
    company = SMPA.model_copy(deep=True)
    company.ats_config["limit"] = 1
    path = "/job/US-Remote/Associate-Clinical-Project-Manager_R01505"
    item = {
        "title": "Associate Clinical Project Manager",
        "externalPath": path,
        "locationsText": "US-Remote",
        "bulletFields": ["R01505"],
    }
    route = respx.post(company.ats_config["endpoint"]).mock(
        side_effect=[
            httpx.Response(200, json={"total": 2, "jobPostings": [item]}),
            httpx.Response(200, json={"total": 2, "jobPostings": [item]}),
        ]
    )
    detail = respx.get(company.ats_config["detail_api_base"] + path).respond(
        200,
        json={
            "jobPostingInfo": {
                "title": item["title"],
                "location": "US-Remote",
                "country": {"descriptor": "United States of America"},
                "jobRequisitionLocation": {"country": {"alpha2Code": "US"}},
                "jobDescription": "<p>" + BODY + "</p>",
                "canApply": True,
                "posted": True,
            }
        },
    )
    async with httpx.AsyncClient() as client:
        jobs = await WorkdaySource(company, client).fetch()
    assert len(jobs) == 1 and detail.call_count == 1
    assert [json.loads(c.request.content)["offset"] for c in route.calls] == [0, 1]
    raw = jobs[0]
    assert raw.source_company == company.slug
    assert raw.external_job_id == path
    assert raw.metadata["workday"]["bulletFields"] == ["R01505"]
    assert raw.location_raw == "US-Remote; United States of America; US"
    assert raw.description_raw == BODY
    assert str(raw.url) == company.ats_config["detail_base_url"] + path
    assert match_job(parse_job(raw), PROFILE, FLORIDA).discovery_eligible
    assert assess_candidate(raw, FLORIDA).work_arrangement.value == "remote"
    assert_repeat_is_unchanged(raw)
    attended = raw.model_copy(
        update={
            "description_raw": BODY + " This role requires periodic onsite meetings at the office."
        }
    )
    assert not match_job(parse_job(attended), PROFILE, FLORIDA).notification_eligible


@pytest.mark.asyncio
@respx.mock
async def test_beacon_full_content_restrictions_identity_and_discovery():
    restriction = "This role is fully remote in the Pacific or Mountain time zones in the U.S."
    # Greenhouse's live content is HTML-escaped; office labels also survive.
    content = (
        "&lt;p&gt;"
        + BODY
        + " "
        + restriction
        + " Remote office hubs are located in Boston, New York, and Paris.&lt;/p&gt;"
    )
    item = {
        "id": 4418079009,
        "title": "Clinical Study Operations Associate",
        "location": {"name": "Remote"},
        "content": content,
        "absolute_url": "https://job-boards.greenhouse.io/beaconbiosignals/jobs/4418079009",
        "updated_at": "2026-09-24T00:00:00Z",
    }
    route = respx.get(
        "https://boards-api.greenhouse.io/v1/boards/beaconbiosignals/jobs?content=true"
    ).respond(200, json={"jobs": [item, item]})
    async with httpx.AsyncClient() as client:
        jobs = await GreenhouseSource(BEACON, client).fetch()
    assert route.call_count == 1
    raw = jobs[0]
    assert raw.source_company == "beacon-biosignals"
    assert raw.title == item["title"] and raw.location_raw == "Remote"
    assert raw.external_job_id == "4418079009"
    assert str(raw.url) == item["absolute_url"]
    assert restriction in raw.description_raw
    assert jobs[1].stable_external_id == raw.stable_external_id
    assert jobs[1].content_hash == raw.content_hash
    assert_repeat_is_unchanged(raw)
    result = match_job(parse_job(raw), PROFILE, FLORIDA)
    assert result.discovery_eligible
    # Existing residence parsing blocks this complete posting for Florida; this
    # is not a claim that it implements general time-zone eligibility.
    assert not result.notification_eligible
    assert result.candidate_eligibility.work_arrangement.value == "remote"
    restricted = raw.model_copy(
        update={"description_raw": BODY + " This role is remote; must reside in California."}
    )
    assert assess_candidate(restricted, FLORIDA).status == "unsuitable"
