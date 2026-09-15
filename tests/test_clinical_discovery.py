from datetime import UTC, datetime
from pathlib import Path

import pytest

from job_monitor import pipeline
from job_monitor.config import (
    Settings,
    load_candidate,
    load_companies,
    load_preferences,
    load_profiles,
)
from job_monitor.matching import match_job, parse_job
from job_monitor.models import RawJob
from job_monitor.storage import MatchDecision, Storage


PROFILES = load_profiles(Path("config/profiles.yml"))
PROFILE = PROFILES["clinical-discovery"]
PREFERENCES = load_preferences(Path("config/preferences.yml"))
COMPANIES = {company.slug: company for company in load_companies(Path("config/companies.yml"))}


def test_biotech_sources_are_clinical_discovery_only():
    expected = {
        "orca-bio": ("lever", {"site": "orcabiosystems"}),
        "praxis-precision-medicines": ("greenhouse", {"board_token": "praxisprecisionmedicines"}),
        "campfield-therapeutics": ("greenhouse", {"board_token": "campfieldtherapeuticsinc"}),
        "treeline-biosciences": ("greenhouse", {"board_token": "treelinebiosciences"}),
        "disc-medicine": ("greenhouse", {"board_token": "discmedicine"}),
        "spyre-therapeutics": ("greenhouse", {"board_token": "spyretherapeutics"}),
        "apogee-therapeutics": ("greenhouse", {"board_token": "apogeetherapeutics"}),
        "seaport-therapeutics": ("greenhouse", {"board_token": "seaporttherapeutics"}),
        "peptilogics": ("greenhouse", {"board_token": "peptilogics"}),
        "kailera": ("greenhouse", {"board_token": "kailera"}),
        "alkeus": ("greenhouse", {"board_token": "alkeus"}),
        "faeth-therapeutics": ("greenhouse", {"board_token": "faeththerapeutics"}),
        "iovance-biotherapeutics": ("greenhouse", {"board_token": "iovancebiotherapeutics"}),
        "relay-therapeutics": ("greenhouse", {"board_token": "relaytherapeutics"}),
        "legend-biotech": ("greenhouse", {"board_token": "legendcareers"}),
        "city-therapeutics": ("greenhouse", {"board_token": "citytherapeutics"}),
        "dianthus-therapeutics": ("greenhouse", {"board_token": "dianthustherapeutics"}),
        "nurix-therapeutics": ("greenhouse", {"board_token": "nurix"}),
        "lightship": ("lever", {"site": "lightship"}),
        "pliant-therapeutics": ("greenhouse", {"board_token": "plianttherapeuticsinc"}),
        "precision-for-medicine": ("greenhouse", {"board_token": "pfm"}),
        "iterative-health": ("greenhouse", {"board_token": "iterativehealth"}),
        "maze-therapeutics": ("greenhouse", {"board_token": "mazetherapeutics"}),
        "oruka-therapeutics": ("greenhouse", {"board_token": "oruka"}),
        "janux-therapeutics": ("lever", {"site": "januxrx"}),
        "anteris-technologies": ("greenhouse", {"board_token": "anteristech"}),
        "immunome": ("greenhouse", {"board_token": "immunomeinc"}),
        "alimentiv": ("lever", {"site": "alimentiv-2"}),
        "scholar-rock": ("lever", {"site": "scholarrock"}),
        "mineralys-therapeutics": ("greenhouse", {"board_token": "mineralystherapeutics"}),
        "kura-oncology": ("greenhouse", {"board_token": "kuraoncology"}),
        "tango-therapeutics": ("greenhouse", {"board_token": "tangotherapeutics"}),
        "kymera-therapeutics": ("greenhouse", {"board_token": "KymeraTherapeutics"}),
        "cullinan-therapeutics": ("lever", {"site": "cullinanoncology"}),
        "endpoint-clinical": ("lever", {"site": "endpointclinical"}),
        "protrials-research": ("lever", {"site": "protrials"}),
        "kincell-bio": ("greenhouse", {"board_token": "kincellbio"}),
        "vaxcyte": ("greenhouse", {"board_token": "vaxcyte"}),
        "alumis": ("greenhouse", {"board_token": "alumis"}),
        "revolution-medicines": ("greenhouse", {"board_token": "revolutionmedicines"}),
        "wep-clinical": ("lever", {"site": "wepclinical"}),
        "lakefront-biotherapeutics": ("greenhouse", {"board_token": "lakefrontbiotherapeuticsinc"}),
        "eikon-therapeutics": ("greenhouse", {"board_token": "eikontherapeutics"}),
        "artbio": ("lever", {"site": "artbio"}),
        "clinchoice": ("greenhouse", {"board_token": "clinchoice"}),
        "dispatch-bio": ("greenhouse", {"board_token": "dispatchbio"}),
        "genscript": ("greenhouse", {"board_token": "genscript"}),
        "heartflow": ("greenhouse", {"board_token": "heartflowinc"}),
        "syner-g": ("greenhouse", {"board_token": "synerg"}),
        "natera": ("greenhouse", {"board_token": "natera"}),
        "psi-cro": ("smartrecruiters", {"company_identifier": "PSICRO"}),
        "agc-biologics": (
            "workday",
            {
                "endpoint": "https://agcbio.wd5.myworkdayjobs.com/wday/cxs/agcbio/agcbio_careers/jobs",
                "site": "agcbio.wd5.myworkdayjobs.com",
                "detail_base_url": "https://agcbio.wd5.myworkdayjobs.com/en-US/agcbio_careers",
                "detail_api_base": "https://agcbio.wd5.myworkdayjobs.com/wday/cxs/agcbio/agcbio_careers",
            },
        ),
    }
    for slug, (ats_type, ats_config) in expected.items():
        company = COMPANIES[slug]
        assert company.enabled
        assert company.source_verified
        assert company.profiles == ["clinical-discovery"]
        assert company.ats_type.value == ats_type
        assert company.ats_config == ats_config

    assert COMPANIES["komodo-health"].profiles == ["clinical-discovery"]
    assert not COMPANIES["abzena"].enabled
    assert not COMPANIES["arcellx"].enabled
    assert not COMPANIES["databricks"].enabled
    assert not COMPANIES["nvidia"].enabled
    assert (
        sum(
            company.enabled
            and company.ats_type.value == "workday"
            and company.ats_config.get("site") == "agcbio.wd5.myworkdayjobs.com"
            for company in COMPANIES.values()
        )
        == 1
    )


def raw(title, description="", location="Remote"):
    return RawJob(
        source_company="komodo-health",
        external_job_id="clinical-test",
        title=title,
        location_raw=location,
        description_raw=description,
        url="https://example.com/jobs/clinical-test",
    )


def match(title, description="", location="Remote"):
    return match_job(
        parse_job(raw(title, description, location)),
        PROFILE,
        PREFERENCES,
    )


@pytest.mark.parametrize(
    "title",
    [
        "Associate Clinical Project Manager",
        "Clinical Project Manager",
        "Associate Project Manager — Clinical",
        "Associate Project Manager - Clinical",
        "Associate Project Manager — Life Sciences",
        "Associate Project Manager - Life Sciences",
        "Clinical Project Coordinator",
        "Clinical Operations Project Coordinator",
        "Clinical Operations Project Manager",
        "Clinical Program Coordinator",
        "Clinical Trial Project Coordinator",
        "Clinical Operations Associate",
        "Clinical Operations Specialist",
        "Clinical Trial Associate",
        "Clinical Operations Project Specialist",
        "GxP Project Coordinator",
        "GxP Project Manager",
        "Validation Project Coordinator",
        "Validation Project Manager",
        "Scientific Project Manager",
        "Scientific Program Coordinator",
        "GMP Operations Manager",
        "GxP Operations Manager",
        "GMP/GxP Operations Manager",
        "GMP Project Manager",
        "GMP Project Coordinator",
        "GMP/GxP Project Manager",
        "GMP/GxP Project Coordinator",
        "Quality Operations Project Manager",
        "Technical Operations Project Manager",
    ],
)
def test_requested_titles_qualify_without_description(title):
    assert match(title).eligible


@pytest.mark.parametrize(
    "title",
    [
        "Clinical Data Specialist",
        "Laboratory Operations Specialist",
        "Quality Operations Specialist",
        "Validation Specialist",
        "Scientific Operations Associate",
        "Operational Excellence Project Manager",
    ],
)
def test_responsibilities_qualify_without_pm_title(title):
    parsed = parse_job(
        raw(
            title,
            "Coordinate projects across teams. Track project milestones for clinical operations.",
        )
    )
    result = match_job(parsed, PROFILE, PREFERENCES)

    assert result.eligible
    assert result.score >= PROFILE.threshold
    assert any(
        reason.startswith("responsibilities:") and "coordinate projects" in reason
        for reason in result.reasons
    )


@pytest.mark.parametrize(
    "title",
    [
        "Associate Clinical SAS Programmer/Junior Statistician",
        "Clinical Statistical Programmer",
        "Clinical Biostatistician",
        "Clinical Programmer",
    ],
)
def test_statistics_and_sas_titles_are_excluded_from_responsibility_fallback(title):
    result = match(
        title,
        "Manage projects and track project milestones for clinical operations.",
    )

    assert not result.eligible
    assert result.score == 0
    assert result.filtered_reason == "discovery_responsibility_evidence"


def test_one_responsibility_hit_is_not_enough():
    result = match(
        "Clinical Data Specialist",
        "Coordinate projects for clinical operations.",
    )
    assert not result.eligible
    assert result.filtered_reason == "discovery_responsibility_evidence"


def test_responsibility_evidence_requires_domain():
    result = match(
        "Technical Operations Specialist",
        "Coordinate projects and track project milestones for a general platform.",
    )
    assert not result.eligible
    assert result.filtered_reason == "discovery_responsibility_evidence"


def test_responsibility_evidence_requires_title_anchor():
    result = match(
        "Research Scientist",
        "Coordinate projects and track project milestones for clinical operations.",
    )
    assert not result.eligible
    assert result.filtered_reason == "discovery_responsibility_evidence"


def test_generic_project_manager_with_life_sciences_evidence_qualifies():
    result = match(
        "Project Manager",
        "Coordinate project plans and track project budgets for GMP biotechnology operations.",
    )

    assert result.eligible
    assert result.score >= PROFILE.threshold


@pytest.mark.parametrize(
    "title,description",
    [
        (
            "Project Manager",
            "Coordinate project plans for GMP biotechnology operations.",
        ),
        (
            "Project Manager",
            "Coordinate project plans and track project budgets for a general platform.",
        ),
        (
            "IT Project Manager",
            "Coordinate project plans and track project budgets for software infrastructure.",
        ),
        (
            "Marketing Project Manager",
            "Coordinate project plans and track project budgets for commercial campaigns.",
        ),
    ],
)
def test_generic_project_manager_requires_life_sciences_support(title, description):
    result = match(title, description)

    assert not result.eligible
    assert result.score == 0
    assert result.filtered_reason == "discovery_responsibility_evidence"


def test_bare_generic_project_manager_does_not_qualify():
    result = match("Project Manager")

    assert not result.eligible
    assert result.score < PROFILE.threshold


def test_operational_excellence_requires_responsibility_evidence():
    title = "Operational Excellence Project Manager"

    assert not match(title, "Improve routine operational efficiency.").eligible
    assert match(
        title,
        "Own project milestones and project deliverables for clinical operations.",
    ).eligible


@pytest.mark.parametrize(
    "title",
    [
        "Nonclinical Project Manager",
        "Preclinical Project Manager",
        "Trial Associateship",
        "Operations Specialistship",
    ],
)
def test_title_phrases_do_not_match_inside_longer_words(title):
    assert not match(title).eligible
    assert not match(
        title,
        "Coordinate projects and track project milestones for a general platform.",
    ).eligible


def test_responsibility_phrases_do_not_match_inside_longer_words():
    assert not match("Clinical Data Specialist", "Supercoordinate projects.").eligible


def test_matching_is_case_insensitive_and_accepts_title_suffixes():
    assert match("ASSOCIATE CLINICAL PROJECT MANAGER (CONTRACT)").eligible
    assert match(
        "Clinical Data Specialist",
        "COORDINATE PROJECTS and track PROJECT MILESTONES for clinical operations.",
    ).eligible


def test_domain_words_alone_do_not_qualify():
    result = match(
        "Laboratory Technician",
        "Clinical pharmaceutical biotechnology laboratory GMP GxP validation.",
    )

    assert not result.eligible
    assert result.score == 0.30
    assert result.filtered_reason is None


@pytest.mark.parametrize(
    "requirement",
    [
        "Must be a U.S. citizen.",
        "An active security clearance is required.",
    ],
)
def test_citizenship_and_clearance_still_override_discovery(requirement):
    result = match("Clinical Project Manager", requirement)

    assert not result.eligible
    assert result.filtered_reason == "citizenship_or_clearance"


@pytest.mark.parametrize("location", ["Boston, MA", "Basel", ""])
def test_location_remains_broad(location):
    assert match("Clinical Project Manager", location=location).eligible


@pytest.mark.parametrize(
    "title",
    [
        "Junior Clinical Project Manager",
        "Senior Clinical Project Manager",
        "Director, Clinical Project Manager",
    ],
)
def test_seniority_is_not_a_discovery_exclusion(title):
    assert match(title).eligible


def test_candidate_requirements_do_not_penalize_without_candidate():
    title = "Clinical Project Manager"
    result = match(
        title,
        "20 years of experience required. PhD required. Manage a team.",
    )

    assert result.eligible
    assert result.score == match(title).score
    assert result.bucket == "target"


def test_legacy_profile_defaults_and_family_rejection_are_preserved():
    for name in ("healthcare", "semiconductor", "tech"):
        assert not PROFILES[name].allow_other_job_family
        assert PROFILES[name].responsibility_terms == []

    parsed = parse_job(raw("Lead Software Engineer - Data Platform", "Coordinate projects."))
    result = match_job(parsed, PROFILES["tech"], PREFERENCES)

    assert not result.eligible
    assert result.filtered_reason == "job_family"


@pytest.mark.parametrize(
    "title",
    [
        "IT Analyst",
        "Analytics Consultant",
        "Analytics Consulting",
        "Analytics Engineer",
        "Data Scientist",
        "Software Engineer",
        "Software Developer",
        "AI Product Manager",
        "Product Manager",
        "Bench Scientist",
        "Research Scientist",
        "QA Analyst",
        "QC Specialist",
    ],
)
def test_unrelated_titles_do_not_qualify_from_project_language(title):
    result = match(
        title,
        "Coordinate projects and track project milestones for clinical operations.",
    )
    assert not result.eligible
    assert result.filtered_reason == "discovery_responsibility_evidence"


def test_discovery_configuration_and_company_scope():
    assert PREFERENCES.location_terms == []
    assert PREFERENCES.include_remote
    assert PREFERENCES.excluded_seniorities == set()
    assert PREFERENCES.exclude_citizenship_required
    assert PREFERENCES.exclude_clearance_required
    assert load_candidate(Path("config/candidate.yml")) is None
    assert COMPANIES["komodo-health"].profiles == ["clinical-discovery"]
    assert not COMPANIES["databricks"].enabled
    assert not COMPANIES["nvidia"].enabled

    assigned = {slug for slug, company in COMPANIES.items() if PROFILE.name in company.profiles}
    assert assigned == {
        "komodo-health",
        "orca-bio",
        "praxis-precision-medicines",
        "campfield-therapeutics",
        "treeline-biosciences",
        "disc-medicine",
        "spyre-therapeutics",
        "apogee-therapeutics",
        "seaport-therapeutics",
        "peptilogics",
        "kailera",
        "alkeus",
        "faeth-therapeutics",
        "iovance-biotherapeutics",
        "relay-therapeutics",
        "legend-biotech",
        "city-therapeutics",
        "dianthus-therapeutics",
        "nurix-therapeutics",
        "lightship",
        "pliant-therapeutics",
        "precision-for-medicine",
        "iterative-health",
        "maze-therapeutics",
        "oruka-therapeutics",
        "janux-therapeutics",
        "anteris-technologies",
        "immunome",
        "alimentiv",
        "scholar-rock",
        "mineralys-therapeutics",
        "kura-oncology",
        "tango-therapeutics",
        "arcellx",
        "kymera-therapeutics",
        "cullinan-therapeutics",
        "endpoint-clinical",
        "protrials-research",
        "kincell-bio",
        "vaxcyte",
        "alumis",
        "revolution-medicines",
        "wep-clinical",
        "lakefront-biotherapeutics",
        "eikon-therapeutics",
        "artbio",
        "clinchoice",
        "dispatch-bio",
        "abzena",
        "genscript",
        "heartflow",
        "syner-g",
        "natera",
        "agc-biologics",
        "fortrea",
        "parexel",
        "worldwide-clinical-trials",
        "psi-cro",
    }
    assert COMPANIES["nvidia"].ats_config["search_texts"] == [
        "data",
        "analytics",
        "business intelligence",
    ]

    for company in COMPANIES.values():
        assert set(company.profiles) <= set(PROFILES)


def test_fortrea_workday_source_contract_is_enabled():
    company = COMPANIES["fortrea"]
    assert company.name == "Fortrea"
    assert company.enabled
    assert company.source_verified
    assert company.profiles == ["clinical-discovery"]
    assert company.ats_type.value == "workday"
    assert company.ats_config == {
        "endpoint": "https://fortrea.wd1.myworkdayjobs.com/wday/cxs/fortrea/Fortrea/jobs",
        "site": "fortrea.wd1.myworkdayjobs.com",
        "detail_base_url": "https://fortrea.wd1.myworkdayjobs.com/en-US/Fortrea",
        "detail_api_base": "https://fortrea.wd1.myworkdayjobs.com/wday/cxs/fortrea/Fortrea",
        "applied_facets": {"locationCountry": ["bc33aa3152ec42d4995f4791a106ed09"]},
    }


@pytest.mark.asyncio
async def test_company_routing_limits_generic_associate_pm_discovery(monkeypatch):
    class FakeSourceRunner:
        def __init__(self, client, max_concurrency):
            pass

        async def fetch(self, company):
            return [
                raw("Associate Project Manager").model_copy(update={"source_company": company.slug})
            ]

    monkeypatch.setattr(pipeline, "SourceRunner", FakeSourceRunner)
    settings = Settings(
        _env_file=None,
        database_url=None,
        telegram_bot_token=None,
        telegram_chat_id=None,
        llm_enabled=False,
        resume_path=None,
        resume_text=None,
        visa_sponsorship_required=False,
    )

    for slug, expected in (
        ("komodo-health", set()),
        ("databricks", set()),
        ("nvidia", set()),
    ):
        report = await pipeline.run_pipeline(
            settings,
            [COMPANIES[slug]],
            PROFILES,
            PREFERENCES,
            candidate=None,
            dry_run=True,
            run_key=f"routing-{slug}",
        )

        assert report.errors == []
        assert {item.result.profile for item in report.dry_run_matches} == expected


def test_ordinary_discovery_match_reaches_handoff(tmp_path):
    posting = raw(
        "Quality Operations Specialist",
        "Coordinate projects and track project milestones for clinical operations.",
    )
    result = match_job(parse_job(posting), PROFILE, PREFERENCES)

    assert result.eligible
    assert result.score >= PROFILE.threshold
    assert result.tier == "match"
    assert not pipeline._qualifies_for_immediate_notification(
        parse_job(posting),
        result,
        datetime.now(UTC),
        Settings(_env_file=None, llm_enabled=False, resume_path=None, resume_text=None),
        is_new=True,
    )

    db = Storage(f"sqlite:///{tmp_path / 'discovery.db'}", create_schema=True)
    try:
        company_id = db.sync_company(COMPANIES["komodo-health"])
        run_id = db.start_run("clinical-handoff")
        assert run_id is not None
        db.persist_job_decisions(
            company_id,
            run_id,
            posting,
            db.plan_job(company_id, posting),
            [MatchDecision(profile_version=PROFILE.version, result=result)],
        )
        db.finish_run(
            run_id,
            {"sources_attempted": 1, "sources_succeeded": 1, "jobs_fetched": 1},
            [],
        )

        rows = db.list_handoff_jobs()
        assert len(rows) == 1
        assert rows[0]["profile"] == PROFILE.name
        assert rows[0]["tier"] == "match"
        assert any(
            reason.startswith("responsibilities:") and "coordinate projects" in reason
            for reason in rows[0]["reasons"]
        )
        assert "description_raw" not in rows[0]
    finally:
        db.engine.dispose()


@pytest.mark.parametrize(
    "title, description, expected",
    [
        (
            "Bilingual Research Associate (English and Mandarin)",
            "Recruit study participants and conduct protocol-specific study visits. Maintain study logs and assure source documents and CRFs are complete. Fluent Mandarin preferred.",
            True,
        ),
        (
            "Clinical Research Coordinator",
            "Coordinate clinical study activities across investigators and project managers. Lead planning, execution, and closeout of Phase I trials under the study protocol.",
            True,
        ),
        (
            "Project Specialist",
            "Maintain project management plans and track project action items for clinical research deliverables. Support CTMS setup and study initiation.",
            True,
        ),
        (
            "Research Associate",
            "Recruit study participants and conduct screening visits. Maintain study logs and source documents.",
            True,
        ),
        (
            "Bilingual Enrollment Coordinator",
            "Screen clinical trial participants according to the study protocol and complete enrollment documentation. Korean fluent.",
            True,
        ),
        (
            "Research Associate",
            "Recruit employees for hiring. Maintain source documents for the company.",
            False,
        ),
        (
            "Research Associate",
            "Screen customers for marketing studies. Our clinical company supports trials.",
            False,
        ),
        (
            "Research Associate",
            "Screen specimens for assays. CRFs are used elsewhere in the company.",
            False,
        ),
        ("Research Associate", "Recruit study participants for a clinical trial.", False),
        ("Research Associate", "Maintain source documents and CRFs for studies.", False),
        (
            "Enrollment Coordinator",
            "Process university admissions and enrollment inquiries.",
            False,
        ),
        ("Enrollment Coordinator", "Sell customer enrollment packages and manage accounts.", False),
        (
            "IT Project Specialist",
            "Maintain project plans and track action items for software releases. Support IT system deployment.",
            False,
        ),
        (
            "Project Specialist",
            "Maintain project plans and action items. Our company supports clinical research and life sciences.",
            False,
        ),
        (
            "Research Associate",
            "Work in a preclinical laboratory conducting animal studies and assays. Maintain source documents.",
            False,
        ),
    ],
)
def test_bounded_clinical_discovery_paths(title, description, expected):
    assert match(title, description).eligible is expected


def test_new_discovery_paths_preserve_profile_independence():
    posting = parse_job(
        raw(
            "Project Specialist",
            "Maintain project plans for clinical study deliverables and support CTMS study initiation.",
        )
    )
    assert not match_job(posting, PROFILES["tech"], PREFERENCES).eligible


def test_support_language_does_not_create_ownership_flag():
    result = match(
        "Project Specialist",
        "Support project plans and participate in CTMS study initiation for clinical research.",
    )
    assert result.eligible
    assert not any(flag.code == "established_ownership_requirement" for flag in result.review_flags)
