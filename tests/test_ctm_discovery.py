"""Evidence-gated clinical trial manager discovery coverage."""

from pathlib import Path

import pytest

from job_monitor.config import SearchPreferences, load_profiles
from job_monitor.matching import match_job, parse_job
from job_monitor.models import RawJob


PROFILE = load_profiles(Path("config/profiles.yml"))["clinical-discovery"]


def result(title: str, description: str):
    raw = RawJob(
        source_company="example",
        external_job_id=title,
        title=title,
        location_raw="Remote United States",
        description_raw=description,
        url="https://example.com/jobs/ctm",
    )
    return match_job(parse_job(raw), PROFILE, SearchPreferences())


MODERNA_ASSOCIATE_BODY = (
    "Support clinical trial planning and execution. Manage study timelines and trial "
    "deliverables. Coordinate vendors and trial activities. Report study status and "
    "coordinate TMF documentation under GCP."
)
MODERNA_CTM_BODY = (
    "Lead clinical trial planning and execution. Manage study timelines and trial "
    "deliverables. Coordinate vendors and stakeholders. Report study status and "
    "maintain TMF documentation under GCP."
)


@pytest.mark.parametrize(
    "title",
    [
        "Associate Clinical Trial Manager, Clinical Operations, Infectious Disease",
        "Associate Clinical Trials Manager, Clinical Operations",
    ],
)
def test_associate_ctm_variants_use_path_a_evidence_gate(title):
    matched = result(title, MODERNA_ASSOCIATE_BODY)
    assert matched.eligible
    assert matched.score >= PROFILE.threshold
    assert any(reason.startswith("clinical trial manager:") for reason in matched.reasons)


@pytest.mark.parametrize(
    "title",
    [
        "Clinical Trial Manager, Clinical Operations, Infectious Disease",
        "Clinical Trials Manager, Early Phase",
    ],
)
def test_ctm_variants_use_path_a_evidence_gate(title):
    matched = result(title, MODERNA_CTM_BODY)
    assert matched.eligible
    assert matched.score >= PROFILE.threshold


@pytest.mark.parametrize(
    "title",
    [
        "Senior Clinical Trial Manager",
        "Sr. Clinical Trial Manager",
        "Lead Clinical Trial Manager",
        "Principal Clinical Trial Manager",
        "Director, Clinical Trial Management",
        "Associate Director, Clinical Trial Management",
        "Head of Clinical Trial Management",
        "VP, Clinical Trial Management",
        "Vice President, Clinical Trial Management",
        "Executive Clinical Trial Manager",
    ],
)
def test_senior_and_leadership_ctm_variants_remain_excluded(title):
    matched = result(title, MODERNA_CTM_BODY)
    assert not matched.eligible
    assert matched.score < PROFILE.threshold


@pytest.mark.parametrize(
    "body",
    [
        "Clinical trial management includes timelines, stakeholder coordination and status reporting.",
        "Coordinate clinical trial vendors and sites.",
        "Monitor clinical trial sites and conduct site visits. Prepare monitoring reports.",
        "Manage software trial timelines and vendors.",
    ],
)
def test_ctm_requires_two_affirmative_clinical_trial_categories(body):
    matched = result("Clinical Trial Manager", body)
    assert not matched.eligible
    assert matched.score < PROFILE.threshold


def test_ctm_monitoring_language_can_coexist_with_independent_pm_evidence():
    matched = result(
        "Clinical Trial Manager",
        MODERNA_CTM_BODY + " Review monitoring reports and site visit findings.",
    )
    assert matched.eligible


def test_gilead_early_phase_ctm_boundary_remains_below_threshold():
    matched = result(
        "Clinical Trials Manager, Early Phase",
        "Lead study execution, sites, vendors, and protocols.",
    )
    assert not matched.eligible
    assert matched.score < PROFILE.threshold


def test_bms_senior_clinical_trial_management_associate_uses_no_ctm_route():
    matched = result(
        "Senior Clinical Trial Management Associate",
        "Support study vendors, TMF tracking, study timelines, and Study Lead coordination.",
    )
    assert not matched.eligible
    assert matched.score < PROFILE.threshold


def test_ctm_thresholds_remain_unchanged():
    assert PROFILE.threshold == 0.70
    assert PROFILE.strong_threshold == 0.90
