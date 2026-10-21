"""Synthetic, offline fixtures: no claims about an actual Vetcove posting."""

import json
from pathlib import Path

import pytest
from sqlalchemy import select

from job_monitor import pipeline
from job_monitor.config import (
    CandidateEligibilityConfig,
    ProfileConfig,
    SearchPreferences,
    Settings,
    load_profiles,
)
from job_monitor.diagnostics import DecisionDiagnostics
from job_monitor.matching import match_job, parse_job
from job_monitor.models import CompanyConfig, MatchResult, RawJob
from job_monitor.notifier import render_run_summary
from job_monitor.storage import MatchDecision, Storage, notification_outbox
from job_monitor.transferable_operations import PROFILE, operations_evidence

FIXTURE = json.loads((Path(__file__).parent / "fixtures/transferable_operations.json").read_text())
PROFILE_CONFIG = load_profiles(Path("config/profiles.yml"))[PROFILE]
DUTIES = "Track project milestones. Coordinate cross-functional teams."
REMOTE = "This role is fully remote and employees may reside in Florida."


def preferences():
    return SearchPreferences(
        excluded_seniorities=set(),
        candidate_eligibility=CandidateEligibilityConfig(
            residence_state="FL",
            held_professional_licenses=set(),
            commuting_policy={"onsite_states": ["FL"]},
        ),
    )


def raw(title="Project Coordinator", body=DUTIES + " " + REMOTE, location="Remote"):
    return RawJob(
        source_company="acme",
        external_job_id="1",
        title=title,
        description_raw=body,
        location_raw=location,
        url="https://example.test/1",
    )


@pytest.mark.parametrize("case", FIXTURE["cases"], ids=lambda case: case["name"])
def test_synthetic_fixtures(case):
    result = match_job(
        parse_job(raw(case["title"], case["description"])), PROFILE_CONFIG, preferences()
    )
    assert result.filtered_reason == case["rejection"]
    assert not result.notification_eligible
    if case["rejection"] is None:
        assert result.discovery_eligible
        assert result.needs_eligibility_review
        assert result.candidate_eligibility.status == "review_needed"
        assert "operations_qualifications_unverified" in result.eligibility_reasons[-1]


@pytest.mark.parametrize(
    "title",
    [
        "Product Operations Associate",
        "Business Operations Specialist",
        "Project Coordinator",
        "Program Operations Coordinator",
        "Implementation Coordinator",
        "Operations Associate",
        "Onboarding Specialist",
        "Junior Product Operations Analyst",
    ],
)
def test_bounded_titles_require_two_distinct_affirmative_duties(title):
    assert operations_evidence(title, DUTIES).rejection is None
    assert (
        operations_evidence(title, "Track project milestones and action items.").rejection
        == "operations_duties_insufficient"
    )


@pytest.mark.parametrize(
    "body",
    [
        "",
        "Project tracking and cross-functional coordination.",
        "Experience tracking project milestones. Experience coordinating cross-functional teams.",
        "You will not track project milestones. No cross-functional coordination is required.",
        "Preferred qualifications: experience with project tracking and cross-functional teams.",
        "Our company tracks project milestones and coordinates cross-functional teams.",
    ],
)
def test_title_keywords_requirements_negations_and_boilerplate_are_not_duties(body):
    result = match_job(parse_job(raw(body=body)), PROFILE_CONFIG, preferences())
    assert result.filtered_reason == "operations_duties_insufficient"
    assert not result.discovery_eligible
    assert not result.notification_eligible
    assert result.candidate_eligibility.status == "review_needed"


def test_associate_product_manager_requires_explicit_transferable_acceptance():
    result = operations_evidence("Associate Product Manager", DUTIES)
    assert result.rejection == "operations_transferability_unverified"
    assert (
        operations_evidence(
            "Associate Product Manager", DUTIES + " Operations experience is not accepted."
        ).rejection
        == "operations_transferability_unverified"
    )


@pytest.mark.parametrize(
    "requirement",
    [
        "Own the product roadmap.",
        "Lead engineering delivery.",
        "Salesforce administration is required.",
        "Must meet sales quotas.",
        "An engineering degree is required.",
        "PMP certification is mandatory.",
        "Required qualifications:\nEpic implementation experience.",
    ],
)
def test_specialized_requirements_cannot_be_rescued_by_operations_duties(requirement):
    assert (
        operations_evidence("Implementation Specialist", DUTIES + "\n" + requirement).rejection
        == "operations_specialized_requirement"
    )


def test_preferred_credentials_are_not_hard_exclusions():
    assert (
        operations_evidence(
            "Project Coordinator", DUTIES + " PMP certification is preferred."
        ).rejection
        is None
    )


@pytest.mark.parametrize(
    "location,body,hard_code",
    [
        ("Boston, MA", DUTIES + " This role is onsite.", "onsite_geography_incompatible"),
        (
            "Remote",
            DUTIES + " This role is fully remote. Applicants must reside in California.",
            "remote_residence_incompatible",
        ),
        (
            "Remote",
            DUTIES + " " + REMOTE + " An active RN license is required.",
            "required_license_not_held",
        ),
    ],
)
def test_existing_geography_and_license_gates_remain_hard(location, body, hard_code):
    result = match_job(parse_job(raw(body=body, location=location)), PROFILE_CONFIG, preferences())
    assert result.discovery_eligible
    assert result.candidate_eligibility.status == "unsuitable"
    assert any(reason.startswith(hard_code) for reason in result.candidate_eligibility.hard_reasons)
    assert not result.needs_eligibility_review
    assert not result.notification_eligible


def test_citizenship_and_missing_candidate_evidence_are_not_bypassed():
    result = match_job(
        parse_job(raw(body=DUTIES + " Must be a U.S. citizen.")), PROFILE_CONFIG, preferences()
    )
    assert result.filtered_reason == "citizenship_or_clearance"
    result = match_job(
        parse_job(raw()), PROFILE_CONFIG, SearchPreferences(excluded_seniorities=set())
    )
    assert result.discovery_eligible and result.needs_eligibility_review
    assert not result.notification_eligible


@pytest.mark.parametrize("batched", [False, True])
def test_overlapping_profiles_cannot_create_duplicate_alerts(batched):
    db = Storage("sqlite:///:memory:", create_schema=True)
    company = CompanyConfig(
        slug="acme",
        name="Acme",
        careers_url="https://example.test",
        ats_type="jsonld",
        industry="tech",
        profiles=["tech", PROFILE],
    )
    company_id = db.sync_company(company)
    run_id = db.start_run("synthetic")
    posting = raw()
    plan = db.plan_job(company_id, posting)
    operations = match_job(parse_job(posting), PROFILE_CONFIG, preferences())
    legacy = MatchResult(profile="tech", score=0.95, tier="strong", eligible=True)
    decisions = [
        MatchDecision("1", legacy, "legacy synthetic alert"),
        MatchDecision("1", operations, "must not queue"),
    ]
    if batched:
        db.persist_job_decisions_batch(company_id, run_id, [(posting, plan, decisions)])
    else:
        db.persist_job_decisions(company_id, run_id, posting, plan, decisions)
    with db.engine.connect() as conn:
        queued = conn.execute(select(notification_outbox)).mappings().all()
    assert len(queued) == 1 and queued[0]["profile"] == "tech"
    assert not pipeline._qualifies_for_immediate_notification(
        parse_job(posting), operations, plan.first_seen_at, Settings(), is_new=True
    )
    db.engine.dispose()


@pytest.mark.asyncio
async def test_supplemental_profile_reuses_inventory_and_diagnostics(monkeypatch):
    db = Storage("sqlite:///:memory:", create_schema=True)
    monkeypatch.setattr(pipeline, "Storage", lambda *args, **kwargs: db)

    class Runner:
        def __init__(self, *args):
            pass

        async def fetch_with_warnings(self, company):
            return [raw()], []

    monkeypatch.setattr(pipeline, "SourceRunner", Runner)
    legacy = ProfileConfig(
        name="legacy",
        weights={"title": 1},
        title_terms=["project coordinator"],
        domain_terms=[],
        skills=[],
        allow_other_job_family=True,
    )
    company = CompanyConfig(
        slug="acme",
        name="Acme",
        careers_url="https://example.test",
        ats_type="jsonld",
        industry="tech",
        profiles=["legacy"],
    )
    report = await pipeline.run_pipeline(
        Settings(database_url="sqlite:///:memory:", telegram_bot_token=None, llm_enabled=False),
        [company],
        {"legacy": legacy, PROFILE: PROFILE_CONFIG},
        preferences(),
        run_key="synthetic-profile",
        suppress_notifications=True,
    )
    assert report.diagnostics["unique_jobs_evaluated"] == 1
    assert report.diagnostics["profile_decisions"] == 2
    assert report.diagnostics["profile_discovery_outcomes"][PROFILE + "__passed"] == 1
    assert (
        report.diagnostics["candidate_review_reasons"]["operations_qualifications_unverified"] == 1
    )
    assert report.matches == 1 and report.eligibility_reviews == 1
    assert report.notifications == 0
    summary = render_run_summary(
        run_key=report.run_key,
        stats=report.stats(),
        errors=[],
        matched_jobs=report.matched_jobs,
        zero_job_sources=[],
    )
    assert summary.count('href="https://example.test/1"') == 1
    db.engine.dispose()


def test_negative_operations_reasons_reach_phase_one_diagnostics():
    result = match_job(
        parse_job(raw("Senior Software Product Manager")), PROFILE_CONFIG, preferences()
    )
    d = DecisionDiagnostics()
    d.observe("acme", "1", result)
    snap = d.snapshot("synthetic", {"matches": 0}, complete=True)
    assert snap["discovery_reasons"] == {"operations_seniority": 1}
    assert snap["profile_discovery_outcomes"] == {PROFILE + "__filtered": 1}


@pytest.mark.parametrize(
    "title",
    [
        "Associate, Product Operations",
        "Product Operations Specialist I",
        "Implementation Coordinator (Remote)",
    ],
)
def test_bounded_entry_title_variants(title):
    assert operations_evidence(title, DUTIES).rejection is None


def test_listing_missing_duties_can_reach_existing_candidate_hydration():
    result = match_job(parse_job(raw(body="")), PROFILE_CONFIG, preferences())
    assert result.filtered_reason == "operations_duties_insufficient"
    assert any(
        reason.startswith("operations_duties_insufficient:")
        for reason in result.candidate_eligibility.review_reasons
    )
    assert pipeline._potential_city_of_hope_candidate([result])
    unrelated = match_job(
        parse_job(raw(title="Software Engineer", body="")), PROFILE_CONFIG, preferences()
    )
    assert not pipeline._potential_city_of_hope_candidate([unrelated])
    assert not any(
        reason.startswith("operations_qualifications_unverified")
        for reason in unrelated.eligibility_reasons
    )


def test_missing_degree_evidence_is_review_not_confirmed_qualification():
    result = match_job(
        parse_job(raw(body=DUTIES + " " + REMOTE + " A master's degree is required.")),
        PROFILE_CONFIG,
        preferences(),
    )
    assert result.discovery_eligible
    assert result.needs_eligibility_review
    assert not result.notification_eligible
