"""P2: synthetic aliases use JD evidence; existing title routes stay intact."""

import json
from pathlib import Path

import pytest

from job_monitor.config import CandidateEligibilityConfig, SearchPreferences, load_profiles
from job_monitor.matching import match_job, parse_job
from job_monitor.models import RawJob
from job_monitor.transferable_operations import PROFILE, operations_evidence

PROFILES = load_profiles(Path("config/profiles.yml"))
CASES = json.loads(Path("tests/fixtures/clinical_support.json").read_text())["p2"]
REMOTE = "This role is fully remote and employees may reside in Florida."
DUTIES = "Track project milestones. Coordinate cross-functional teams."


def preferences():
    return SearchPreferences(
        excluded_seniorities=set(),
        candidate_eligibility=CandidateEligibilityConfig(
            residence_state="FL",
            held_professional_licenses=set(),
            commuting_policy={"onsite_states": ["FL"]},
        ),
    )


def job(title, description):
    return parse_job(
        RawJob(
            source_company="synthetic",
            external_job_id="1",
            title=title,
            description_raw=description,
            location_raw="Remote United States",
            url="https://example.test/1",
        )
    )


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["title"] + "-" + str(c["pass"]))
def test_p2_clinical_alias_fixtures(case):
    result = match_job(
        job(case["title"], case["description"] + " " + REMOTE),
        PROFILES["clinical-discovery"],
        preferences(),
    )
    assert result.discovery_eligible is case["pass"]
    if not case["pass"]:
        assert not result.notification_eligible


@pytest.mark.parametrize(
    "title",
    [
        "Clinical Implementation Specialist",
        "Clinical Implementation Coordinator",
        "Clinical Implementation Specialist II",
        "Implementation Specialist II",
        "Implementation Specialist 2 (Remote)",
    ],
)
def test_p2_new_implementation_variants_remain_review_only(title):
    body = DUTIES + " Support clinical trial operations. " + REMOTE
    parsed = job(title, body)
    result = match_job(parsed, PROFILES[PROFILE], preferences())
    assert result.discovery_eligible and result.needs_eligibility_review
    assert not result.notification_eligible
    # Do not let the clinical responsibility fallback bypass the review profile.
    assert not match_job(
        parsed, PROFILES["clinical-discovery"], preferences()
    ).notification_eligible


@pytest.mark.parametrize(
    "body",
    [
        DUTIES,
        "Our company supports clinical trials. " + DUTIES,
        "Implement software platforms. " + DUTIES,
        "Build API integrations. Support clinical trials. " + DUTIES,
    ],
)
def test_p2_new_implementation_variants_require_clinical_work_and_no_specialist_duties(body):
    result = match_job(
        job("Implementation Specialist II", body + " " + REMOTE), PROFILES[PROFILE], preferences()
    )
    assert not result.discovery_eligible and not result.notification_eligible


def test_p2_does_not_expand_other_generic_or_senior_titles():
    for title in [
        "Implementation Manager",
        "Senior Implementation Specialist II",
        "Implementation Specialist III",
        "Clinical Software Implementation Specialist",
        "Clinical Implementations Director",
    ]:
        assert operations_evidence(title, DUTIES).rejection is not None
    # Existing generic implementation discovery is not retroactively narrowed.
    assert operations_evidence("Implementation Specialist", DUTIES).rejection is None
