"""Narrow clinical support title variants; discovery is not candidate suitability."""

from pathlib import Path

import pytest

from job_monitor.config import CandidateEligibilityConfig, SearchPreferences, load_profiles
from job_monitor.matching import _transferable_pm_evidence, match_job, parse_job
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
    "Vendor Operations Coordinator",
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


PM_BODY = (
    "Support biotechnology projects in a GxP environment. Maintain project plans and timelines. "
    "Coordinate cross-functional teams and vendors. Track project action items and risks."
)


@pytest.mark.parametrize("title", [
    "Project Coordinator", "Project Specialist", "Associate Project Manager",
    "Scientific Project Coordinator", "Operations Project Coordinator",
    "Program Coordinator", "Study Coordinator",
])
def test_transferable_pm_requires_distinct_duties(title):
    matched = result(title, PM_BODY)
    assert matched.eligible
    assert any(
        reason == "transferable life-science PM: coordination_stakeholders, "
        "planning_schedule, tracking_governance"
        for reason in matched.reasons
    )


@pytest.mark.parametrize("title,body", [
    ("Project Specialist", "Support life sciences projects. Track milestones and project "
     "deliverables. Coordinate stakeholders and vendors. Prepare status reports and risk tracking."),
    ("Associate Project Manager", "Own pharmaceutical project scope, timelines and budgets. "
     "Coordinate stakeholders. Maintain project governance."),
    ("Scientific Project Coordinator", "Support laboratory operations. Develop project plans "
     "and milestones. Coordinate cross-functional teams. Maintain project documentation."),
    ("Project Coordinator", "Support GLP research. Maintain project documentation. "
     "Coordinate resources and project budgets. Prepare status reports."),
])
def test_transferable_pm_alternative_categories_and_ownership(title, body):
    assert result(title, body).eligible


@pytest.mark.parametrize("title", [
    "GxP Project Coordinator", "GxP Project Manager", "Validation Project Coordinator",
    "Validation Project Manager", "Scientific Project Manager",
])
def test_existing_explicit_regulated_titles_keep_title_only_behavior(title):
    assert result(title, "").eligible


@pytest.mark.parametrize("title,body", [
    ("Report Coordinator", "Prepare GLP reports and protocols/amendments using sponsor "
     "templates. Coordinate meetings and maintain regulated documentation."),
    ("Project Scientist", "Own scientific research and perform toxicology assays. " + PM_BODY),
    ("Senior Toxicology Study Director", "Serve as principal investigator for GLP studies. " + PM_BODY),
    ("Laboratory Technician", "Perform GMP laboratory testing and validation protocols."),
    ("Research Scientist", "Conduct experiments for biotechnology research projects."),
    ("QC Specialist", "Perform GMP quality testing and maintain validation documentation."),
    ("QA Analyst", "Review GLP protocols and regulatory quality documentation."),
    ("Clinical Research Associate", "Conduct site monitoring and monitor clinical trial sites."),
    ("Clinical Data Manager", "Manage clinical databases and data cleaning."),
    ("Administrative Project Coordinator", "Support biotechnology office calendars, "
     "meeting bookings and travel arrangements."),
    ("Project Coordinator", "Maintain project plans and timelines for software releases. "
     "Coordinate cross-functional teams. Track action items and risks."),
    ("Project Coordinator", "Support biotechnology operations. Maintain project plans, "
     "timelines, milestones, project schedules and project dependencies."),
    ("Project Specialist", "Support biotechnology projects. Maintain project plans and "
     "coordinate stakeholders."),
    ("Project Coordinator", "GxP biotechnology. Project plans, stakeholder coordination, "
     "status reporting, project budgets."),
    ("Project Coordinator", "Support biotechnology. No project plans are assigned. "
     "Coordinate stakeholders. Prepare status reports."),
    ("Project Coordinator", PM_BODY + " Perform laboratory testing."),
    ("Project Coordinator", PM_BODY + " Conduct site monitoring."),
    ("Senior Manager, Project Coordinator", PM_BODY),
    ("Associate Director, Project Coordinator", PM_BODY),
    ("Project Coordinator - Head of Research", PM_BODY),
    ("Project Coordinator", "Maintain project plans for construction validation. "
     "Coordinate stakeholders. Prepare status reports."),
])
def test_transferable_pm_false_friends(title, body):
    assert not result(title, body).eligible


def test_transferable_pm_profile_independence_and_thresholds():
    assert not result("Project Coordinator", PM_BODY, profile="tech").eligible
    assert PROFILES["clinical-discovery"].threshold == 0.70
    assert PROFILES["clinical-discovery"].strong_threshold == 0.90


def test_transferable_pm_reports_all_five_categories_once():
    matched = result(
        "Project Coordinator",
        PM_BODY * 2 + " Maintain project documentation and change control. "
        "Support resource coordination and project financial tracking.",
    )
    assert "transferable life-science PM: coordination_stakeholders, deliverables_control, " \
        "planning_schedule, resource_financial, tracking_governance" in matched.reasons


@pytest.mark.parametrize("domain", [
    "biotech", "pharma", "biopharma", "CRO", "regulated research", "GLP",
])
def test_transferable_pm_domain_variants(domain):
    assert result("Project Coordinator", PM_BODY.replace(
        "biotechnology projects in a GxP environment", domain + " projects",
    )).eligible


@pytest.mark.parametrize("domain", [
    "corporate", "software", "construction", "marketing", "engineering",
    "regulatory", "validation", "scientific",
])
def test_transferable_pm_requires_credible_domain(domain):
    assert not result("Project Coordinator", PM_BODY.replace(
        "biotechnology projects in a GxP environment", domain + " projects",
    )).eligible


@pytest.mark.parametrize("suffix", [
    "Software", "Construction", "Marketing", "Engineering", "Statistical Programming", "QC",
])
def test_transferable_pm_excludes_unrelated_title_context(suffix):
    assert not result("Project Coordinator - " + suffix, PM_BODY).eligible


@pytest.mark.parametrize("boilerplate", [
    "We are a biotechnology company developing innovative therapies.",
    "Our company develops biotechnology projects and supports GMP operations.",
    "Develop biotechnology projects at our company with a global team.",
    "Biotechnology. GMP. Scientific operations. Regulated research.",
])
def test_transferable_pm_rejects_unrelated_work_with_domain_boilerplate(boilerplate):
    body = (
        "Coordinate software releases. Maintain software project timelines. "
        "Coordinate IT stakeholders and cross-functional teams. "
        "Prepare software project status reports. " + boilerplate
    )
    assert not _transferable_pm_evidence("Project Coordinator", body)
    assert not result("Project Coordinator", body).eligible


@pytest.mark.parametrize("body", [
    "Biotechnology project management includes timelines, stakeholder coordination "
    "and status reporting.",
    "Support biotechnology projects. Management of timelines, stakeholder coordination "
    "and status reporting.",
    "Support biotechnology projects. Tracking timelines, stakeholder coordination and "
    "status reporting is an important concept.",
    "Support biotechnology projects. Reporting timelines, stakeholder coordination and "
    "status reporting involves several skills.",
    "Support biotechnology projects. Coordination, planning, tracking and reporting.",
])
def test_transferable_pm_rejects_noun_only_responsibilities(body):
    assert not _transferable_pm_evidence("Project Coordinator", body)
    assert not result("Project Coordinator", body).eligible


@pytest.mark.parametrize("action", ["Manage", "Manages", "Managed", "Managing"])
def test_transferable_pm_affirmative_action_inflections(action):
    body = (
        f"{action} biotech development projects. Develops and maintains project timelines. "
        "Coordinates cross-functional scientific stakeholders. "
        "Tracks actions/risks and reports project status."
    )
    assert _transferable_pm_evidence("Project Coordinator", body) == {
        "planning_schedule", "coordination_stakeholders", "tracking_governance",
    }
    assert result("Project Coordinator", body).eligible


@pytest.mark.parametrize("context", [
    "Coordinate pharma development projects.",
    "Manage GMP validation projects.",
    "Support GxP operations projects.",
    "Coordinate laboratory operations projects.",
    "Coordinate scientific operations projects.",
    "Manage regulated research deliverables.",
    "Support clinical quality operations projects.",
])
def test_transferable_pm_work_context_can_ground_separate_duties(context):
    body = context + (
        " Develop and maintain project timelines. Coordinate cross-functional teams. "
        "Track risks and report project status."
    )
    assert _transferable_pm_evidence("Project Coordinator", body)
    assert result("Project Coordinator", body).eligible
