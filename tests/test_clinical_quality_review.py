"""P3: clinical quality/TMF support is review-only across overlapping profiles."""

import json
from pathlib import Path

import pytest
from sqlalchemy import select

from job_monitor.config import (
    CandidateEligibilityConfig,
    ProfileConfig,
    SearchPreferences,
    Settings,
    load_profiles,
)
from job_monitor.diagnostics import DecisionDiagnostics
from job_monitor.matching import match_job, parse_job
from job_monitor.models import CompanyConfig, RawJob
from job_monitor.pipeline import _qualifies_for_immediate_notification
from job_monitor.storage import MatchDecision, Storage, notification_outbox

PROFILE = load_profiles(Path("config/profiles.yml"))["clinical-discovery"]
CASES = json.loads(Path("tests/fixtures/clinical_support.json").read_text())["p3"]
BODY = "Maintain trial master files and clinical study documentation."
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


def raw(title="TMF Specialist", body=BODY + " " + REMOTE, location="Remote United States"):
    return RawJob(
        source_company="synthetic",
        external_job_id="1",
        title=title,
        description_raw=body,
        location_raw=location,
        url="https://example.test/1",
    )


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["title"] + "-" + str(c["pass"]))
def test_p3_synthetic_quality_fixtures(case):
    result = match_job(
        parse_job(raw(case["title"], case["description"] + " " + REMOTE)), PROFILE, preferences()
    )
    assert result.discovery_eligible is case["pass"]
    assert not result.eligible and not result.notification_eligible
    if case["pass"]:
        assert result.needs_eligibility_review
        assert result.candidate_eligibility.status == "review_needed"


@pytest.mark.parametrize(
    "title",
    [
        "TMF Coordinator",
        "eTMF Document Specialist",
        "Trial Master File Specialist II",
        "GCP Quality Assurance Specialist",
        "Clinical Quality Coordinator",
        "TMF Specialist – Remote",
    ],
)
def test_p3_coordinator_specialist_variants(title):
    result = match_job(parse_job(raw(title)), PROFILE, preferences())
    assert result.discovery_eligible and result.needs_eligibility_review


@pytest.mark.parametrize(
    "body,location,reason",
    [
        (BODY + " This role is onsite.", "Boston, MA", "onsite_geography_incompatible"),
        (
            BODY + " Remote work is only available in California.",
            "Remote United States",
            "remote_residence_incompatible",
        ),
        (
            BODY + " An active RN license is required. " + REMOTE,
            "Remote United States",
            "required_license_not_held",
        ),
    ],
)
def test_p3_hard_eligibility_is_never_replaced_by_review(body, location, reason):
    result = match_job(parse_job(raw(body=body, location=location)), PROFILE, preferences())
    assert result.candidate_eligibility.status == "unsuitable"
    assert any(r.startswith(reason) for r in result.candidate_eligibility.hard_reasons)
    assert not result.needs_eligibility_review and not result.notification_eligible


def test_p3_missing_private_facts_or_context_never_confirms_eligibility():
    for prefs in [preferences(), SearchPreferences(excluded_seniorities=set())]:
        result = match_job(parse_job(raw()), PROFILE, prefs)
        assert result.needs_eligibility_review and not result.notification_eligible
    result = match_job(parse_job(raw(body="")), PROFILE, preferences())
    assert result.filtered_reason == "clinical_quality_context_missing"
    assert result.candidate_eligibility.status == "review_needed"
    assert not result.discovery_eligible and not result.notification_eligible


def test_p3_negative_or_preferred_credentials_do_not_become_hard_requirements():
    for text in [
        "No certification required.",
        "CQA certification preferred.",
        "Preferred qualifications:\nCQA certification.",
    ]:
        result = match_job(
            parse_job(raw(body=BODY + " " + text + " " + REMOTE)), PROFILE, preferences()
        )
        assert result.discovery_eligible and result.needs_eligibility_review


@pytest.mark.parametrize(
    "title",
    [
        "Clinical Quality Manager",
        "Manufacturing QC Specialist",
        "Laboratory QC Coordinator",
        "Administrative Coordinator",
    ],
)
def test_p3_does_not_add_unrelated_or_senior_title_coverage(title):
    from job_monitor.clinical_support import quality_support_title

    assert not quality_support_title(title)


@pytest.mark.parametrize(
    "title",
    ["Senior TMF Specialist", "Clinical Quality Specialist III", "GCP Quality Specialist - Lead"],
)
def test_p3_senior_variants_cannot_leak_through_generic_clinical_matching(title):
    result = match_job(
        parse_job(raw(title, BODY + " Manage projects and project timelines. " + REMOTE)),
        PROFILE,
        preferences(),
    )
    assert result.filtered_reason == "clinical_quality_seniority"
    assert not result.discovery_eligible and not result.notification_eligible


@pytest.mark.parametrize("batched", [False, True])
def test_p3_overlap_cannot_enqueue_individual_alerts_or_bypass_review(batched):
    generic = ProfileConfig(
        name="overlap",
        weights={"title": 1},
        title_terms=["tmf specialist"],
        domain_terms=[],
        skills=[],
        allow_other_job_family=True,
    )
    posting = raw()
    parsed = parse_job(posting)
    results = [match_job(parsed, p, preferences()) for p in [PROFILE, generic]]
    assert all(r.discovery_eligible and not r.notification_eligible for r in results)
    db = Storage("sqlite:///:memory:", create_schema=True)
    company = CompanyConfig(
        slug="synthetic",
        name="Synthetic",
        careers_url="https://example.test",
        ats_type="jsonld",
        industry="clinical",
        profiles=["clinical-discovery"],
    )
    company_id = db.sync_company(company)
    run_id = db.start_run("synthetic")
    plan = db.plan_job(company_id, posting)
    decisions = [MatchDecision("1", r, "must not queue") for r in results]
    if batched:
        db.persist_job_decisions_batch(company_id, run_id, [(posting, plan, decisions)])
    else:
        db.persist_job_decisions(company_id, run_id, posting, plan, decisions)
    with db.engine.connect() as conn:
        assert not conn.execute(select(notification_outbox)).all()
    assert not any(
        _qualifies_for_immediate_notification(
            parsed, r, plan.first_seen_at, Settings(_env_file=None), is_new=True
        )
        for r in results
    )
    diagnostics = DecisionDiagnostics()
    diagnostics.observe("synthetic", "1", results[0])
    snapshot = diagnostics.snapshot(run_id, {}, complete=True)
    assert snapshot["candidate_review_reasons"]["clinical_quality_qualifications_unverified"] == 1
    db.engine.dispose()
