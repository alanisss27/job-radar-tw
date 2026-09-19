"""Narrow clinical support title variants; discovery is not candidate suitability."""

from pathlib import Path

import pytest

from job_monitor.config import CandidateEligibilityConfig, SearchPreferences, load_profiles
from job_monitor.matching import match_job, parse_job
from job_monitor.models import RawJob


PROFILES = load_profiles(Path("config/profiles.yml"))
TITLES = [
    "Clinical Study Associate",
    "Clinical Study Specialist",
    "Clinical Project Associate",
    "Clinical Project Support Specialist",
    "Project Support Specialist",
    "Clinical Trial Assistant",
    "Clinical Trials Assistant",
]
BODY = "Support clinical study team updates. Maintain investigator files and update CTMS."


def result(title, body=BODY, preferences=None, profile="clinical-discovery"):
    job = RawJob(
        source_company="example", title=title, description_raw=body,
        location_raw="Remote United States", url="https://example.com/job/support",
    )
    return match_job(parse_job(job), PROFILES[profile], preferences or SearchPreferences())


@pytest.mark.parametrize("title", TITLES)
def test_transition_titles_require_multiple_clinical_support_duties(title):
    matched = result(title)
    assert matched.eligible
    assert any("clinical study support:" in reason for reason in matched.reasons)
    assert not any(f.code == "established_ownership_requirement" for f in matched.review_flags)


@pytest.mark.parametrize("title", TITLES)
@pytest.mark.parametrize("body", [
    "",
    "Our company conducts clinical trials. Assist with general office filing and calendars.",
    "Maintain TMF documentation for clinical studies.",
    "CTMS, investigator files, clinical study team updates.",
    "Support marketing projects. Maintain vendor invoices and software release schedules.",
    "Maintain investigator files and update CTMS. Our company conducts clinical trials.",
    "Coordinate clinical study meetings and investigator meetings.",
])
def test_transition_titles_alone_or_incidental_keywords_do_not_qualify(title, body):
    assert not result(title, body).eligible


@pytest.mark.parametrize("title", [
    "Associate", "Specialist", "Assistant", "Study Coordinator", "Project Support",
    "Study Management Associate", "Feasibility Specialist", "Site Intelligence Associate",
    "Development Operations Associate", "Vendor Operations Coordinator",
    "Clinical Study Associate Manager", "Associate Clinical Study Manager",
    "Clinical Study Associate Director", "Clinical Study Specialist - Study Lead",
    "Clinical Project Associate - CRA Monitoring", "Clinical Trial Assistant Manager",
    "Clinical Research Associate", "Clinical Data Manager", "Clinical Team Lead",
])
def test_other_titles_do_not_inherit_transition_path(title):
    assert not result(title).eligible


@pytest.mark.parametrize("title", [
    "Senior Clinical Study Specialist", "Sr. Clinical Study Associate",
    "Clinical Trials Assistant - FSP", "Clinical Project Associate (Contract)",
])
def test_support_title_qualifiers(title):
    assert result(title).eligible


def test_study_reporting_and_meeting_coordination_are_supported():
    matched = result(
        "Clinical Study Specialist",
        "Organizes and delivers reports and metrics to the clinical study lead. "
        "Coordinates study meetings, agendas and minutes.",
    )
    assert matched.eligible
    assert any("clinical study support:" in reason for reason in matched.reasons)


def test_support_scope_does_not_hide_explicit_ownership_requirement():
    matched = result(
        "Clinical Study Associate",
        BODY + " Required qualifications: 2-5 years of direct project management experience.",
    )
    assert matched.eligible
    assert any(f.code == "established_ownership_requirement" for f in matched.review_flags)


@pytest.mark.parametrize("body,status", [
    (BODY, "eligible"),
    (BODY + " Active RN license required.", "unsuitable"),
    (BODY + " Must reside in California.", "unsuitable"),
])
def test_transition_discovery_preserves_candidate_constraints(body, status):
    preferences = SearchPreferences(candidate_eligibility=CandidateEligibilityConfig(
        residence_state="Florida", held_professional_licenses=[],
        commuting_policy={"onsite_states": ["FL"]},
    ))
    matched = result("Clinical Trial Assistant", body, preferences)
    assert matched.discovery_eligible
    assert matched.candidate_eligibility.status == status
    assert matched.notification_eligible is (status == "eligible")


def test_transition_path_is_clinical_profile_only():
    assert not result("Clinical Study Specialist", profile="tech").eligible


def test_lower_confidence_family_keeps_existing_responsibility_path():
    matched = result(
        "Clinical Feasibility Specialist",
        "Coordinate projects and track project timelines for clinical research.",
    )
    assert matched.eligible
    assert not any("clinical study support:" in reason for reason in matched.reasons)


def test_transition_discovery_can_require_candidate_review():
    preferences = SearchPreferences(candidate_eligibility=CandidateEligibilityConfig(
        held_professional_licenses=[],
    ))
    matched = result("Clinical Study Associate", BODY + " Must reside in Florida.", preferences)
    assert matched.discovery_eligible
    assert matched.candidate_eligibility.status == "review_needed"
    assert not matched.notification_eligible
