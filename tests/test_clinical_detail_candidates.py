"""P1: title-only detail permission does not imply discovery or eligibility."""

import hashlib
import json
from pathlib import Path

import httpx
import pytest
from pydantic import HttpUrl

from job_monitor import pipeline
from job_monitor.config import (
    CandidateEligibilityConfig,
    SearchPreferences,
    Settings,
    load_companies,
    load_profiles,
)
from job_monitor.discovery import contextual_title_candidates
from job_monitor.matching import match_job, parse_job
from job_monitor.models import RawJob
from job_monitor.sources import SourceRunner
from job_monitor.storage import Storage

PROFILES = load_profiles(Path("config/profiles.yml"))
PROFILE = PROFILES["clinical-discovery"]
COMPANIES = {c.slug: c for c in load_companies(Path("config/companies.yml"))}
TITLES = [
    "Study Start-Up Specialist",
    "Clinical Data Coordinator",
    "Clinical Data Specialist",
    "Research Operations Specialist",
    "Clinical Trials Associate",
    "Site Start-Up Specialist",
    "TMF Specialist",
    "Clinical Implementation Specialist",
    "Implementation Specialist II",
]
ADAPTERS = [
    ("city-of-hope", "city_of_hope"),
    ("charter-research", "charter_research"),
    ("nmdp", "oracle"),
    ("avance-clinical", "jobadder_widget"),
    ("cognitive-research-corporation", "teamtailor"),
]
BODY = (
    "Coordinate clinical trial documentation. Maintain CTMS and trial master files. "
    "Track project milestones. Coordinate cross-functional teams. "
    "This role is fully remote and employees may reside in Florida."
)


def preferences():
    return SearchPreferences(
        excluded_seniorities=set(),
        candidate_eligibility=CandidateEligibilityConfig(
            residence_state="FL",
            held_professional_licenses=set(),
            commuting_policy={"onsite_states": ["FL"]},
        ),
    )


def raw(title, description="", location="Remote United States"):
    return RawJob(
        source_company="synthetic",
        external_job_id="1",
        title=title,
        description_raw=description,
        location_raw=location,
        url="https://example.test/1",
    )


@pytest.mark.parametrize("title", TITLES)
@pytest.mark.parametrize("description", ["", "Role overview: full-time opportunity."])
def test_p1_sparse_titles_can_request_details_without_becoming_matches(title, description):
    posting = raw(title, description)
    result = match_job(parse_job(posting), PROFILE, preferences())
    operations = match_job(parse_job(posting), PROFILES["transferable-operations"], preferences())
    assert not result.discovery_eligible
    assert pipeline._potential_city_of_hope_candidate(
        [result, operations], raw=posting, profile=PROFILE
    )
    assert not result.notification_eligible


@pytest.mark.parametrize(
    "title",
    [
        "Administrative Coordinator",
        "Software Engineer",
        "Lab Technician",
        "Research Operations Specialist - Manufacturing",
        "Study Start-Up Specialist - IRT",
        "Nurse",
    ],
)
def test_p1_unrelated_titles_do_not_expand_detail_selection(title):
    posting = raw(title)
    result = match_job(parse_job(posting), PROFILE, preferences())
    assert not pipeline._potential_city_of_hope_candidate([result], raw=posting, profile=PROFILE)


@pytest.mark.parametrize(
    "title", ["Clinical Data Coordinator", "Implementation Specialist II", "TMF Specialist"]
)
def test_p1_no_new_detail_permission_for_hard_location_exclusion_or_unassigned_profile(title):
    posting = raw(title, location="Boston, MA")
    result = match_job(parse_job(posting), PROFILE, preferences())
    operations = match_job(parse_job(posting), PROFILES["transferable-operations"], preferences())
    assert result.candidate_eligibility.hard_reasons
    assert not pipeline._potential_city_of_hope_candidate(
        [result, operations], raw=posting, profile=PROFILE
    )
    assert not pipeline._potential_city_of_hope_candidate([result], raw=posting)


def test_p1_candidate_terms_do_not_change_contextual_matching():
    assert contextual_title_candidates(
        "Clinical Data Coordinator II", PROFILE.contextual_title_families
    )
    assert not contextual_title_candidates(
        "Clinical Database Engineer", PROFILE.contextual_title_families
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("slug,metadata_key", ADAPTERS)
@pytest.mark.parametrize("title", TITLES)
@pytest.mark.parametrize("description", ["", "Role overview."])
async def test_p1_real_adapter_detail_budget_and_unchanged_inventory_skip(
    monkeypatch,
    slug,
    metadata_key,
    title,
    description,
):
    company = COMPANIES[slug]
    db = Storage("sqlite:///:memory:", create_schema=True)
    calls = []
    detail_url = str(company.careers_url).rstrip("/") + "/synthetic-detail/1"
    posting = raw(title, description).model_copy(
        update={
            "source_company": company.slug,
            "url": HttpUrl(detail_url),
            "metadata": {
                metadata_key: {
                    "detail_url": detail_url,
                    "listing_hash": hashlib.sha256(title.encode()).hexdigest(),
                }
            },
        }
    )
    noise = [
        raw(name).model_copy(
            update={
                "source_company": company.slug,
                "external_job_id": str(i),
                "metadata": {
                    metadata_key: {"detail_url": detail_url + "/noise", "listing_hash": str(i)}
                },
            }
        )
        for i, name in enumerate(["Payroll Administrator", "Software Engineer", "Receptionist"], 10)
    ]

    def handle(request):
        calls.append(request)
        if metadata_key == "oracle":
            assert request.url.path.endswith("recruitingCEJobRequisitionDetails")
            assert "Id=1," in request.url.params["finder"]
            return httpx.Response(
                200, json={"items": [{"Id": "1", "ExternalDescriptionStr": BODY}]}
            )
        if metadata_key == "jobadder_widget":
            assert str(request.url).startswith(company.ats_config["detail_endpoint"])
            assert request.url.params["jobID"] == "1"
            html = f'<div class="ja-job-details"><h1 class="title">{title}</h1><div class="description">{BODY}</div></div>'
            return httpx.Response(200, text="radar(" + json.dumps(html) + ");")
        assert str(request.url) == detail_url
        return httpx.Response(
            200,
            text=f'<main><h1>{title}</h1><div class="job-description">{BODY}</div><a href="/apply/1">Apply</a></main>',
        )

    original_client = httpx.AsyncClient

    def client(*args, **kwargs):
        return original_client(*args, transport=httpx.MockTransport(handle), **kwargs)

    class Runner(SourceRunner):
        async def fetch_with_warnings(self, company):
            # Inventory never invokes hydrate; normal adapter inventory contracts
            # are additionally exercised by their existing pagination suites.
            return [p.model_copy(deep=True) for p in [posting, *noise]], []

    monkeypatch.setattr(pipeline.httpx, "AsyncClient", client)
    monkeypatch.setattr(pipeline, "SourceRunner", Runner)
    monkeypatch.setattr(pipeline, "Storage", lambda *args, **kwargs: db)
    settings = Settings(
        _env_file=None,
        database_url="sqlite:///:memory:",
        llm_enabled=False,
        resume_path=None,
        resume_text=None,
    )
    try:
        report = await pipeline.run_pipeline(
            settings,
            [company],
            PROFILES,
            preferences(),
            run_key="synthetic-first",
            suppress_notifications=True,
        )
        assert not report.errors
        assert len(calls) == 1  # One selected candidate, zero irrelevant details.
        assert report.diagnostics["discovery_outcomes"]["passed"] >= 1
        if title in {
            "TMF Specialist",
            "Clinical Implementation Specialist",
            "Implementation Specialist II",
        }:
            assert report.matches == 0 and report.eligibility_reviews == 1
        await pipeline.run_pipeline(
            settings,
            [company],
            PROFILES,
            preferences(),
            run_key="synthetic-second",
            suppress_notifications=True,
        )
        assert len(calls) == 1  # Listing fingerprints still skip unchanged jobs.
    finally:
        db.engine.dispose()
