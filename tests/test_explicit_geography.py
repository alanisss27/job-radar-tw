"""Synthetic geography and rendering regressions; no live requests or private data."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select

from job_monitor.config import (
    CandidateEligibilityConfig,
    SearchPreferences,
    load_profiles,
    Settings,
    ProfileConfig,
)
from job_monitor.eligibility import assess_candidate
from job_monitor.matching import match_job, parse_job
from job_monitor.models import RawJob, MatchResult, EligibilityAssessment
from job_monitor.notifier import render_job_message
from job_monitor.pipeline import _qualifies_for_immediate_notification
from job_monitor.storage import Storage, MatchDecision, notification_outbox
from job_monitor.config import CompanyConfig

PROFILES = load_profiles(Path("config/profiles.yml"))
BODY = "Coordinate clinical study activities and clinical trial execution under the protocol."


def preferences():
    return SearchPreferences(
        location_terms=["Tampa, FL"],
        excluded_seniorities=set(),
        candidate_eligibility=CandidateEligibilityConfig(
            residence_state="FL",
            held_professional_licenses=set(),
            commuting_policy={"onsite_states": ["FL"]},
        ),
    )


def raw(location, body="", title="Clinical Research Coordinator"):
    return RawJob(
        source_company="synthetic",
        external_job_id="1",
        title=title,
        location_raw=location,
        description_raw=BODY + " " + body,
        url="https://example.test/1",
    )


@pytest.mark.parametrize(
    "location,status",
    [
        ("India", "unsuitable"),
        ("Sydney, Australia", "unsuitable"),
        ("India; Australia", "unsuitable"),
        ("Toronto, Canada", "unsuitable"),
        ("Melbourne", "review_needed"),
        ("Sydney", "review_needed"),
        ("IN", "unsuitable"),  # Existing US Indiana code, never interpreted as India.
        ("Unknown City", "review_needed"),
        ("Paris, France", "review_needed"),  # Outside the deliberately bounded vocabulary.
        ("Tampa, FL", "eligible"),
        ("Melbourne, FL, United States", "review_needed"),
        ("Boston, MA", "unsuitable"),
        ("India; Tampa, FL", "review_needed"),
        ("India / Unknown City", "review_needed"),
        ("United States or India", "review_needed"),
    ],
)
def test_physical_geography(location, status):
    result = assess_candidate(raw(location), preferences())
    assert result.status == status
    if status == "unsuitable":
        assert any(r.startswith("onsite_geography_incompatible") for r in result.hard_reasons)


@pytest.mark.parametrize(
    "body",
    [
        "You must reside in India.",
        "Candidates are required to be based in Australia.",
        "Australia only.",
        "Remote: Australia only.",
    ],
)
@pytest.mark.parametrize("location", ["Remote", "Remote United States"])
def test_mandatory_foreign_remote_residence(body, location):
    result = assess_candidate(raw(location, body), preferences())
    assert result.status == "unsuitable"
    assert result.hard_reasons[0].startswith("remote_residence_incompatible")


@pytest.mark.parametrize(
    "body",
    [
        "Our headquarters are in India.",
        "Support customers in Australia.",
        "Occasional travel to India is required.",
        "Employees may reside in Florida. Our headquarters are in Australia.",
        "Applicants must reside in India or the United States.",
        "Applicants must reside in Florida or Australia.",
        "You are not required to be based in India.",
        "You need not reside in Australia.",
        "Our customers must reside in India.",
        "You must be located in India for customer visits.",
    ],
)
def test_us_remote_incidental_or_compatible_foreign_context(body):
    assert assess_candidate(raw("Remote United States", body), preferences()).status == "eligible"


@pytest.mark.parametrize(
    "location,status",
    [
        ("Remote India", "unsuitable"),
        ("Remote Sydney, Australia", "unsuitable"),
        ("Remote United States / India", "eligible"),
        ("Remote India / Unknown", "review_needed"),
        ("Remote IN", "unsuitable"),
    ],
)
def test_remote_location_scope(location, status):
    assert assess_candidate(raw(location), preferences()).status == status


def test_us_state_restrictions_and_contradictory_scope_remain_safe():
    assert (
        assess_candidate(
            raw("Remote India", "Employees may reside in Florida."), preferences()
        ).status
        == "review_needed"
    )
    assert (
        assess_candidate(
            raw(
                "Remote India", "Applicants must reside in India. US applicants are also eligible."
            ),
            preferences(),
        ).status
        == "review_needed"
    )
    assert (
        assess_candidate(
            raw("Tampa, FL", "Our customers must work onsite in India."), preferences()
        ).status
        == "eligible"
    )
    assert (
        assess_candidate(
            raw(
                "Remote United States",
                "Applicants must reside in India. US applicants are also eligible.",
            ),
            preferences(),
        ).status
        == "review_needed"
    )
    assert (
        assess_candidate(
            raw("Remote United States", "Employees must reside in California only."), preferences()
        ).status
        == "unsuitable"
    )
    assert (
        assess_candidate(
            raw("Remote India", "This remote role is available in the United States."),
            preferences(),
        ).status
        == "review_needed"
    )
    assert (
        assess_candidate(
            raw("Tampa, FL", "You must work onsite in Sydney, Australia."), preferences()
        ).status
        == "unsuitable"
    )
    assert (
        assess_candidate(
            raw("Tampa, FL", "You must attend annual meetings at our office in India."),
            preferences(),
        ).status
        == "eligible"
    )


@pytest.mark.parametrize("profile", ["clinical-discovery", "other"])
@pytest.mark.parametrize("status", ["eligible", "review_needed", "unsuitable", None])
def test_render_assessment_status_and_escape_reasons(profile, status):
    assessment = (
        None
        if status is None
        else EligibilityAssessment(
            status=status,
            review_reasons=["commute_unresolved: <unverified>"]
            if status == "review_needed"
            else [],
            hard_reasons=["onsite_geography_incompatible"] if status == "unsuitable" else [],
        )
    )
    result = MatchResult(
        profile=profile,
        score=0.9,
        tier="strong",
        eligible=status in (None, "eligible"),
        candidate_eligibility=assessment,
    )
    message = render_job_message(
        "Synthetic", parse_job(raw("Unknown City")), result, datetime.now(UTC)
    )
    if status == "review_needed":
        assert "review needed; NOT confirmed eligible" in message
        assert "commute_unresolved: &lt;unverified&gt;" in message
        assert "通過設定條件" not in message
    elif status == "eligible":
        assert "Eligibility: confirmed by configured checks" in message
    elif status == "unsuitable":
        assert "Eligibility: ineligible" in message
    else:
        assert "Eligibility:" not in message
        if profile == "clinical-discovery":
            assert "通過設定條件" in message


@pytest.mark.parametrize("batched", [False, True])
@pytest.mark.parametrize(
    "location,body", [("India", ""), ("Remote United States", "You must reside in India.")]
)
def test_hard_exclusions_cannot_queue_or_qualify_for_alert(location, body, batched):
    posting = raw(location, body)
    parsed = parse_job(posting)
    result = match_job(parsed, PROFILES["clinical-discovery"], preferences())
    assert result.candidate_eligibility.status == "unsuitable"
    assert not result.notification_eligible and not result.needs_eligibility_review
    assert not _qualifies_for_immediate_notification(
        parsed, result, datetime.now(UTC), Settings(_env_file=None), is_new=True
    )
    db = Storage("sqlite:///:memory:", create_schema=True)
    try:
        company = CompanyConfig(
            slug="synthetic",
            name="Synthetic",
            careers_url="https://example.test",
            ats_type="jsonld",
            industry="clinical",
            profiles=["clinical-discovery"],
        )
        cid = db.sync_company(company)
        rid = db.start_run("synthetic")
        plan = db.plan_job(cid, posting)
        decisions = [MatchDecision("1", result, "must never queue")]
        if batched:
            db.persist_job_decisions_batch(cid, rid, [(posting, plan, decisions)])
        else:
            db.persist_job_decisions(cid, rid, posting, plan, decisions)
        with db.engine.connect() as conn:
            assert not conn.execute(select(notification_outbox)).all()
    finally:
        db.engine.dispose()


def test_p3_review_guard_survives_geographic_confirmation_across_profiles():
    posting = raw(
        "Remote United States",
        "Maintain trial master files and clinical study documentation. Employees may reside in Florida.",
        title="TMF Specialist",
    )
    overlap = ProfileConfig(
        name="overlap",
        weights={"title": 1},
        title_terms=["tmf specialist"],
        domain_terms=[],
        skills=[],
        allow_other_job_family=True,
    )
    for profile in [PROFILES["clinical-discovery"], overlap]:
        result = match_job(parse_job(posting), profile, preferences())
        assert result.discovery_eligible and result.needs_eligibility_review
        assert not result.eligible and not result.notification_eligible
