"""Recall expansion and boundaries against unrelated support-role noise."""

from pathlib import Path

import pytest

from job_monitor.config import CandidateEligibilityConfig, SearchPreferences, load_profiles
from job_monitor.discovery import contextual_title_evidence
from job_monitor.matching import match_job, parse_job
from job_monitor.models import RawJob


PROFILE = load_profiles(Path("config/profiles.yml"))["clinical-discovery"]
BODY = "Support clinical research operations and maintain essential documents for study startup."


def match(title, body=BODY, preferences=None, profile=PROFILE):
    raw = RawJob(
        source_company="example",
        title=title,
        description_raw=body,
        location_raw="Remote United States",
        url="https://example.com/jobs/test",
    )
    return match_job(parse_job(raw), profile, preferences or SearchPreferences())


@pytest.mark.parametrize(
    "title", [title for terms in PROFILE.contextual_title_families.values() for title in terms]
)
def test_each_added_title_is_discoverable_with_work_context(title):
    result = match(title)
    assert result.eligible
    assert any("discovery title family:" in reason for reason in result.reasons)


@pytest.mark.parametrize(
    "title",
    [
        "Scientific Leadership Research Coordinator",
        "Proposals Development Associate II",
        "Clinical Data Coordinator",
        "Study StartUp Specialist",
        "Study Start-Up Associate",
        "Study Start–Up Coordinator (Contract)",
        "Regulatory Start-Up Specialist",
        "Clinical Start-Up Specialist - United States",
        "Start-Up Specialist",
    ],
)
def test_observed_titles_and_spelling_variants(title):
    assert match(title).eligible


@pytest.mark.parametrize(
    "title,body",
    [
        ("Research Coordinator", BODY),
        ("Research Operations Associate", "Schedule student interviews for education research."),
        ("Scientific Research Coordinator", "Coordinate physics research and university events."),
        ("Study Operations Associate", "Coordinate consumer surveys and market research."),
        (
            "Clinical Study Coordinator",
            "Schedule patient appointments and recruit clinic patients.",
        ),
        ("Clinical Regulatory Associate", "Own CMC submissions strategy and product labeling."),
        ("Study Regulatory Specialist", "Regulatory strategy for pharmaceutical products."),
        ("Startup Specialist", "Support venture capital startups and business incubators."),
        ("Development Operations Associate", "Support software releases for a biotechnology firm."),
        ("Development Project Coordinator", "Manage construction for a pharmaceutical office."),
        (
            "Proposal Coordinator",
            "Our company conducts clinical research. Prepare IT projects bids.",
        ),
        ("Proposal Associate", "Prepare government contracting proposals."),
        ("Proposals Development Associate II", "Prepare marketing and advertising proposals."),
        (
            "Proposal Development Coordinator",
            "Prepare sales proposals for pharmaceutical products.",
        ),
        ("Clinical Data Specialist", "Perform database engineering for clinical research."),
        ("Data Scientist", BODY),
        ("Data Analyst", BODY),
        ("Patient Service Coordinator", BODY),
        ("Administrative Coordinator", BODY),
        ("Senior Study Startup Manager", BODY),
        ("Study Startup Specialist - IRT", BODY),
        ("Clinical Development Associate Director", BODY),
        ("Research Operations Associate Manager", BODY),
        ("Clinical Data Coordinator - CMC", BODY),
    ],
)
def test_noise_does_not_enter_through_new_path(title, body):
    assert not contextual_title_evidence(title, body, PROFILE.contextual_title_families)
    assert not match(title, body).eligible


@pytest.mark.parametrize("title", ["Proposal Associate", "Research Operations Associate"])
def test_employer_boilerplate_is_not_work_context(title):
    assert not match(title, "Our company conducts clinical trials. Book office travel.").eligible


def test_clinical_services_proposals_can_use_commercial_language():
    assert match(
        "Proposal Associate",
        "Prepare sales proposals for clinical-development services at a CRO.",
    ).eligible


def test_preferred_experience_and_ordinary_software_skills_are_not_fit_gates():
    result = match(
        "Study Startup Specialist",
        BODY
        + " Previous startup experience preferred. Familiarity with office software preferred.",
    )
    assert result.eligible


def test_specialized_irt_requirement_is_not_rescued_by_new_title():
    assert not match(
        "Study Startup Specialist", BODY + " Established IRT experience is required."
    ).eligible
    assert match("Study Startup Specialist", BODY + " Familiarity with IRT preferred.").eligible


def test_clinical_software_proposals_are_not_clinical_development_services():
    assert not match(
        "Proposal Associate",
        "Prepare software proposals for clinical billing systems at a biotech firm.",
    ).eligible


def test_clinical_study_coordinator_requires_operations_exposure():
    assert match("Clinical Study Coordinator", BODY + " Recruit study participants.").eligible


def test_existing_titles_are_not_duplicated_and_other_profiles_are_unchanged():
    new_terms = [term for terms in PROFILE.contextual_title_families.values() for term in terms]
    assert len(new_terms) == len(set(new_terms))
    assert not set(new_terms) & set(PROFILE.title_terms)
    assert "clinical study associate" not in new_terms
    for name, profile in load_profiles(Path("config/profiles.yml")).items():
        if name != "clinical-discovery":
            assert not profile.contextual_title_families


@pytest.mark.parametrize(
    "title",
    [
        "Clinical Trial Associate",
        "Scientific Program Coordinator",
        "Clinical Project Manager",
        "Clinical Trial Assistant",
        "Clinical Study Associate",
    ],
)
def test_existing_discovery_results_are_preserved(title):
    body = "Support clinical study team updates. Maintain investigator files and update CTMS."
    old = PROFILE.model_copy(update={"contextual_title_families": {}})
    assert match(title, body).model_dump() == match(title, body, profile=old).model_dump()


def test_candidate_eligibility_still_blocks_notifications():
    preferences = SearchPreferences(
        candidate_eligibility=CandidateEligibilityConfig(
            residence_state="FL",
            held_professional_licenses=[],
        )
    )
    result = match("Clinical Data Coordinator", BODY + " Must reside in California.", preferences)
    assert result.discovery_eligible
    assert not result.notification_eligible


def test_scoring_weights_and_thresholds_are_preserved():
    assert PROFILE.weights == {"title": 0.70, "domain": 0.30}
    assert PROFILE.threshold == 0.70
    assert PROFILE.strong_threshold == 0.90
    result = match("Clinical Data Coordinator", "Support clinical research.")
    assert result.score == 0.8
    assert result.tier == "match"
