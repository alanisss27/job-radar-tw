import json

import pytest
from sqlalchemy import select

from job_monitor import pipeline
from job_monitor.config import ProfileConfig, SearchPreferences, Settings
from job_monitor.diagnostics import DecisionDiagnostics, render_diagnostics
from job_monitor.models import CompanyConfig, EligibilityAssessment, MatchResult, RawJob
from job_monitor.notifier import render_run_summary
from job_monitor.storage import Storage, source_runs


def decision(**kwargs):
    return MatchResult(
        profile="clinical-discovery", score=0.8, eligible=False, tier="match", **kwargs
    )


def test_unique_jobs_and_overlapping_profile_reason_counts_are_separate():
    d = DecisionDiagnostics()
    d.observe_job("acme", "1")
    d.observe_job("acme", "1")
    d.observe_job("acme", "unchanged")
    assessment = EligibilityAssessment(
        status="unsuitable",
        hard_reasons=[
            "required_license_not_held (RN): private evidence",
            "required_license_not_held (RN): other evidence",
            "onsite_geography_incompatible: private site",
        ],
        review_reasons=["commute_unresolved: private location"],
    )
    for _ in range(2):
        d.observe("acme", "1", decision(discovery_eligible=True, candidate_eligibility=assessment))
    snap = d.snapshot("run-1", {"jobs_new": 1, "jobs_changed": 0, "matches": 0}, complete=True)
    assert snap["unique_jobs_observed"] == 2
    assert snap["unique_jobs_evaluated"] == 1
    assert snap["profile_decisions"] == 2
    assert snap["discovery_outcomes"] == {"passed": 2}
    assert snap["candidate_hard_reasons"] == {
        "required_license_not_held": 2,
        "onsite_geography_incompatible": 2,
    }
    assert snap["candidate_review_reasons"] == {"commute_unresolved": 2}
    assert snap["final_matches"] == snap["unique_jobs_matched"] == 0
    assert "private" not in json.dumps(snap)


def test_missing_assessment_and_source_coverage_are_unknown_not_hard_exclusions():
    d = DecisionDiagnostics()
    d.observe("acme", "1", decision(filtered_reason="discovery_responsibility_evidence"))
    d.observe(
        "acme",
        "2",
        decision(
            discovery_eligible=True,
            candidate_eligibility=EligibilityAssessment(
                status="review_needed", review_reasons=["remote_residence_unknown: sensitive"]
            ),
        ),
    )
    d.observe_source_warning(
        {"reason": "location validation failed", "title": "sensitive", "company": "Otsuka"}
    )
    snap = d.snapshot("run", {}, complete=False)
    assert snap["inventory"] == {"new": None, "changed": None}
    assert snap["final_matches"] is None
    assert snap["candidate_statuses"] == {"unknown": 1, "review_needed": 1}
    assert snap["candidate_hard_reasons"] == {}
    assert snap["source_stage"] == {
        "total_excluded": None,
        "observed_warnings": {"workday_location_validation_failed": 1},
    }
    assert "matches=unknown" in render_diagnostics(snap)
    assert "Source exclusion total: unknown" in render_diagnostics(snap)
    assert render_diagnostics(None) == "Decision diagnostics: unknown"


def test_reason_vocabulary_is_bounded():
    d = DecisionDiagnostics()
    for i in range(100):
        d.observe_source_warning({"reason": f"untrusted {i}"})
        d._increment(d.hard, f"new_code_{i}: secret")
    snap = d.snapshot("run", {}, complete=True)
    assert len(snap["candidate_hard_reasons"]) <= 65
    assert sum(snap["candidate_hard_reasons"].values()) == 100
    assert snap["source_stage"]["observed_warnings"] == {"unclassified_warning": 100}


def test_matches_count_profiles_while_unique_matches_count_jobs():
    d = DecisionDiagnostics()
    for profile in ("tech", "healthcare"):
        d.observe(
            "acme", "1", MatchResult(profile=profile, score=0.9, eligible=True, tier="strong")
        )
    d.observe(
        "acme", "2", MatchResult(profile="tech", score=0.2, eligible=False, tier="below_threshold")
    )
    snap = d.snapshot("run", {"matches": 2}, complete=True)
    assert snap["final_matches"] == 2
    assert snap["unique_jobs_matched"] == 1
    assert snap["discovery_outcomes"] == {"passed": 2, "below_threshold": 1}
    assert snap["discovery_reasons"] == {"below_threshold": 1}


@pytest.mark.asyncio
async def test_zero_match_run_persists_diagnostics_and_renders_summary(monkeypatch):
    db = Storage("sqlite:///:memory:", create_schema=True)
    monkeypatch.setattr(pipeline, "Storage", lambda *args, **kwargs: db)

    class Runner:
        def __init__(self, *args):
            pass

        async def fetch_with_warnings(self, company):
            return [
                RawJob(
                    source_company="acme",
                    external_job_id="1",
                    title="Unrelated role",
                    location_raw="Remote",
                    description_raw="Unrelated work",
                    url="https://example.test/1",
                )
            ], [{"reason": "location validation failed", "company": "Otsuka"}]

    monkeypatch.setattr(pipeline, "SourceRunner", Runner)
    monkeypatch.setattr(
        pipeline,
        "match_job",
        lambda *args, **kwargs: decision(
            discovery_eligible=False, filtered_reason="discovery_responsibility_evidence"
        ),
    )
    company = CompanyConfig(
        slug="acme",
        name="Acme",
        careers_url="https://example.test",
        ats_type="jsonld",
        industry="tech",
        profiles=["clinical-discovery"],
    )
    report = await pipeline.run_pipeline(
        Settings(
            database_url="sqlite:///:memory:",
            telegram_bot_token=None,
            resume_path=None,
            candidate_config="config/nonexistent.yml",
            llm_enabled=False,
        ),
        [company],
        {
            "clinical-discovery": ProfileConfig(
                name="clinical-discovery",
                weights={"title": 1},
                title_terms=[],
                domain_terms=[],
                skills=[],
            )
        },
        SearchPreferences(),
        run_key="diagnostic-test",
        suppress_notifications=True,
    )
    with db.engine.connect() as conn:
        row = conn.execute(select(source_runs)).mappings().one()
    snap = row["stats"]["decision_diagnostics"]
    assert snap["run_id"] == row["id"]
    assert snap["started_at"] and snap["recorded_at"]
    assert snap["complete"] is True
    assert snap["inventory"] == {"new": 1, "changed": 0}
    assert snap["unique_jobs_evaluated"] == snap["profile_decisions"] == 1
    assert snap["final_matches"] == report.matches == 0
    assert snap["source_stage"]["observed_warnings"]["workday_location_validation_failed"] == 1
    summary = render_run_summary(
        run_key=report.run_key,
        stats=report.stats(),
        errors=[],
        matched_jobs=[],
        zero_job_sources=[],
    )
    assert "1 unique jobs / 1 profile decisions; matches=0" in summary
    assert "discovery_responsibility_evidence=1" in summary
    assert "Reason counts overlap" in summary

    second = await pipeline.run_pipeline(
        Settings(
            database_url="sqlite:///:memory:",
            telegram_bot_token=None,
            resume_path=None,
            llm_enabled=False,
        ),
        [company],
        {
            "clinical-discovery": ProfileConfig(
                name="clinical-discovery",
                weights={"title": 1},
                title_terms=[],
                domain_terms=[],
                skills=[],
            )
        },
        SearchPreferences(),
        run_key="diagnostic-unchanged",
        suppress_notifications=True,
    )
    assert second.diagnostics["inventory"] == {"new": 0, "changed": 0}
    assert second.diagnostics["unique_jobs_observed"] == 1
    assert (
        second.diagnostics["unique_jobs_evaluated"] == second.diagnostics["profile_decisions"] == 0
    )
