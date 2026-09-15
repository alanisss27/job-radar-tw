"""Posting-only review regression fixtures; no live sources or services."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from job_monitor import pipeline
from job_monitor.config import Settings
from job_monitor.matching import _clinical_review_flags, match_job, parse_job
from job_monitor.models import CandidateProfile, MatchedJob, MatchResult
from job_monitor.notifier import render_job_message, render_run_summary
from job_monitor.storage import MatchDecision, Storage, match_results, notification_outbox
from test_clinical_discovery import PREFERENCES, PROFILE, raw
from test_storage import company


KYMERA = (
    "<h3>Skills and experience you’ll bring:</h3><ul><li>Bachelor of Science in "
    "Life Sciences and 3+ years (CTM)/5+ years (Sr. CTM) as a project/clinical "
    "trial manager in biotech/pharmaceutical industry.</li></ul>"
    "Clinical biotechnology operations."
)
AGC = (
    "Required qualifications:\n2–5 years of direct project management experience.\n"
    "Responsibilities:\nLead project delivery with scope, timelines and budget accountability.\n"
    "Coordinate project plans and technology transfer for GMP biotechnology and life sciences."
)


@pytest.mark.parametrize(
    "title,body,codes",
    [
        (
            "Clinical Trial Associate or Senior Clinical Trial Associate",
            KYMERA,
            ["title_body_level_mismatch", "established_ownership_requirement"],
        ),
        ("Project Manager", AGC, ["established_ownership_requirement"]),
    ],
)
def test_examples_are_advisory_only(title, body, codes):
    job = parse_job(raw(title, body))
    result = match_job(job, PROFILE, PREFERENCES)
    # Identical configuration with another name exercises the untouched calculation.
    baseline = match_job(job, PROFILE.model_copy(update={"name": "control"}), PREFERENCES)
    assert result.model_dump(exclude={"profile", "review_flags"}) == baseline.model_dump(
        exclude={"profile", "review_flags"}
    )
    assert result.score == 1 and result.eligible and result.tier == "strong"
    assert [flag.code for flag in result.review_flags] == codes
    assert baseline.review_flags == []
    assert "review_flags" not in baseline.model_dump(mode="json")
    if title.startswith("Clinical Trial Associate"):
        assert "5+ years (Sr. CTM)" in result.review_flags[0].evidence[0]
    settings = Settings(_env_file=None)
    assert pipeline._qualifies_for_immediate_notification(
        job, result, datetime.now(UTC), settings, is_new=True
    ) == pipeline._qualifies_for_immediate_notification(
        job, baseline, datetime.now(UTC), settings, is_new=True
    )


@pytest.mark.parametrize(
    "body",
    [
        "Support project managers with scope, timelines and budgets.",
        "Assist with project delivery as primary client contact.",
        "Participate in trial start-up through close-out.",
        "3+ years as a project manager preferred.",
        "No direct project management experience required.",
        "Preferred qualifications:\n3+ years as a project manager.\nDirect PM experience required.",
        "Required: 3+ years supporting a clinical trial manager.",
        "Required: 3+ years working with a project manager.",
        "Required: 3+ years reporting to a clinical trial manager.",
        "Track budget, vendor, site, CRA and project plans.",
        "Lead meetings about project plans.",
        "Lead weekly meetings to discuss project scope, timelines and budget.",
        "You will not be responsible for project scope, timelines or budget.",
        "Preferred experience includes:<br>3 years as a project manager.",
        "You must work as a project manager.",
        "Knowledge of project ownership covering scope, timelines and budgets.",
        "Preferred qualifications & experience:\n3 years as a project manager.",
    ],
)
def test_non_requirements_do_not_trigger(body):
    assert _clinical_review_flags("Senior CTA", body) == []


@pytest.mark.parametrize(
    "body",
    [
        "Own project scope, timelines and budget.",
        "Responsible for project delivery as primary client contact.",
        "Lead trial activities from start-up through study close-out.",
        "Lead project activities from initiation to project closeout.",
    ],
)
def test_accountability_requires_substantive_ownership(body):
    assert [f.code for f in _clinical_review_flags("Coordinator", body)] == [
        "established_ownership_requirement"
    ]


def test_managerial_title_and_preferred_section_boundaries():
    flags = _clinical_review_flags("Associate Director", KYMERA)
    assert [f.code for f in flags] == ["established_ownership_requirement"]
    body = "<h3>Preferred qualifications</h3><li>5 years as a project manager</li>"
    body += "<h3>Required qualifications</h3><li>3 years as a clinical trial manager</li>"
    flags = _clinical_review_flags("Senior CTA", body)
    assert len(flags) == 2
    assert flags[0].evidence == ["3 years as a clinical trial manager"]


@pytest.mark.parametrize("heading", ["Required qualifications", "Qualifications"])
def test_preferred_introduction_resets_at_required_heading(heading):
    body = "Preferred experience includes:<br>3 years as a project manager."
    body += f"<h3>{heading}</h3><li>5 years as a clinical trial manager</li>"
    flags = _clinical_review_flags("Senior CTA", body)
    assert [flag.code for flag in flags] == [
        "title_body_level_mismatch",
        "established_ownership_requirement",
    ]
    assert all(flag.evidence == ["5 years as a clinical trial manager"] for flag in flags)


def test_evidence_is_normalized_deduplicated_and_bounded():
    clause = "Required: 3 years as a project manager " + " relevant  experience " * 40
    body = "\n".join([clause] * 4 + ["Required: 5 years as a clinical trial manager"] * 3)
    flags = _clinical_review_flags("Clinical Trial Associate", body)
    for flag in flags:
        assert len(flag.evidence) == 2
        assert all(len(e) <= 240 and e == " ".join(e.split()) for e in flag.evidence)


def test_flags_do_not_compare_candidate_capabilities():
    job = parse_job(raw("Senior Clinical Trial Associate", KYMERA))
    expected = match_job(job, PROFILE, PREFERENCES).review_flags
    for years in (10, 20):
        candidate = CandidateProfile(years_experience=years, current_level="senior")
        assert match_job(job, PROFILE, PREFERENCES, candidate=candidate).review_flags == expected


@pytest.mark.parametrize("bucket", ["target", "stretch"])
@pytest.mark.parametrize("flagged", [False, True])
def test_discovery_messages_are_neutral_and_escape_evidence(bucket, flagged):
    job = parse_job(raw("Clinical Trial Associate", KYMERA if flagged else "clinical"))
    result = match_job(job, PROFILE, PREFERENCES)
    result.bucket = bucket
    result.gaps = ["Existing rule <gap>"]
    if flagged:
        result.review_flags[0].evidence = ["Required PM experience <verified posting>"]
    now = datetime.now(UTC)
    message = render_job_message("Example", job, result, now)
    assert "探索相關度" in message and "Career-ops" in message
    assert "待確認要求：" in message and "自動檢查提示：Existing rule &lt;gap&gt;" in message
    assert "地點篩選：通過設定條件；非地理適配評分" in message
    for forbidden in ("強烈推薦", "無明顯缺口", "location: 100%", "seniority:"):
        assert forbidden not in message
    if flagged:
        assert "&lt;verified posting&gt;" in message
    else:
        assert "不代表符合全部資格" in message
    summary = render_run_summary(
        run_key="test",
        stats={},
        errors=[],
        zero_job_sources=[],
        matched_jobs=[
            MatchedJob(
                company_name="Example",
                job=job,
                result=result,
                first_seen_at=now,
                is_new=True,
                changed=False,
            )
        ],
    )
    assert "探索相關度" in summary and "非候選人適配" in summary
    assert "待確認：" in summary and "探索／配對結果" in summary
    assert "verified posting" not in summary


def test_old_results_and_empty_gaps():
    old = dict(profile="clinical-discovery", score=1, eligible=True, tier="strong")
    result = MatchResult.model_validate(old)
    assert result.review_flags == []
    message = render_job_message("Example", parse_job(raw("CTA")), result, datetime.now(UTC))
    assert "未產生其他規則提示；候選人資格尚未核實" in message
    result.profile = "tech"
    legacy = render_job_message("Example", parse_job(raw("CTA")), result, datetime.now(UTC))
    assert "強烈推薦" in legacy and "無明顯缺口" in legacy
    assert "探索相關度" not in legacy and "待確認要求" not in legacy


@pytest.mark.asyncio
@pytest.mark.parametrize("batched", [False, True])
async def test_suppressed_backfill_persists_flags_and_invalidates_old_claim(
    tmp_path, monkeypatch, batched
):
    db = Storage(f"sqlite:///{tmp_path / 'review.db'}", create_schema=True)
    co = company().model_copy(update={"profiles": ["clinical-discovery"]})
    posting = raw("Clinical Trial Associate", KYMERA).model_copy(update={"source_company": co.slug})
    cid = db.sync_company(co)
    rid = db.start_run("initial")
    result = match_job(parse_job(posting), PROFILE, PREFERENCES)
    old_details = result.model_dump(exclude={"review_flags"})
    old = MatchResult.model_validate(old_details)
    db.persist_job_decisions(
        cid,
        rid,
        posting,
        db.plan_job(cid, posting),
        [MatchDecision(PROFILE.version, old, "old notification")],
    )
    claim = db.claim_pending_notifications(rid, 1)[0]

    class Runner:
        def __init__(self, *args):
            pass

        async def fetch(self, company):
            return [posting]

    def forbidden(*args, **kwargs):
        raise AssertionError("No network or notifications allowed")

    monkeypatch.setattr(pipeline, "Storage", lambda *args, **kwargs: db)
    monkeypatch.setattr(pipeline, "SourceRunner", Runner)
    monkeypatch.setattr(pipeline, "_supports_batch_persistence", lambda storage: batched)
    monkeypatch.setattr(pipeline, "TelegramNotifier", forbidden)
    monkeypatch.setattr(pipeline.httpx.AsyncClient, "send", forbidden)
    report = await pipeline.run_pipeline(
        Settings(
            _env_file=None,
            database_url="sqlite://",
            llm_enabled=False,
            resume_path=None,
            resume_text=None,
            telegram_bot_token="fake",
            telegram_chat_id="fake",
        ),
        [co],
        {"clinical-discovery": PROFILE},
        PREFERENCES,
        backfill=True,
        suppress_notifications=True,
        run_key="review-backfill",
    )
    assert report.errors == [] and report.notifications == 0 and report.matches == 1
    with db.engine.connect() as conn:
        details = conn.execute(select(match_results.c.details)).scalar_one()
        assert MatchResult.model_validate(details).review_flags == result.review_flags
        assert conn.execute(select(notification_outbox)).all() == []
    assert not db.notification_claim_is_valid(rid, claim["id"], claim["claim_token"])
    db.engine.dispose()
