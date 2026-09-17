"""Candidate constraints use synthetic fixtures, mocked transport and local SQLite only."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
import httpx
import respx
from pydantic import ValidationError

from job_monitor import pipeline
from job_monitor.config import (
    CandidateEligibilityConfig,
    SearchPreferences,
    Settings,
    load_profiles,
)
from job_monitor.eligibility import (
    assess_candidate,
    attendance_frequency,
    candidate_rejections,
    license_rejections,
    states_in,
    work_arrangement,
)
from job_monitor.matching import match_job, parse_job
from job_monitor.models import MatchedJob, RawJob, RemoteType
from job_monitor.notifier import render_run_summary
from job_monitor.sources import _eligibility_metadata
from job_monitor.sources import GreenhouseSource
from job_monitor.storage import Storage
from job_monitor.storage import MatchDecision, notification_outbox
from sqlalchemy import select
from test_pipeline import FakeNotifier, FakeStorage
from test_storage import company

PROFILE = load_profiles(Path("config/profiles.yml"))["clinical-discovery"]
BODY = "Coordinate clinical study activities and clinical trial execution under the protocol. Pharmaceutical scientific research."


def preferences(**changes):
    return SearchPreferences(
        location_terms=["Tampa, FL"],
        excluded_seniorities=set(),
        candidate_eligibility=CandidateEligibilityConfig(
            **{
                "residence_state": "Florida",
                "held_professional_licenses": [],
                "commuting_policy": {"onsite_states": ["FL"]},
                **changes,
            }
        ),
    )


def raw(title="Clinical Research Coordinator", location="Remote United States", body="", **kwargs):
    return RawJob(
        source_company="acme",
        external_job_id=title + location,
        title=title,
        location_raw=location,
        description_raw=BODY + "\n" + body,
        url="https://example.com/jobs/eligibility",
        **kwargs,
    )


@pytest.mark.parametrize(
    "title,location,body,license_required",
    [
        (
            "Clinical Research Coordinator I - RN",
            "Dayton, OH",
            "Active Registered Nurse (RN) license required.",
            True,
        ),
        (
            "Clinical Research Coordinator II - Per Diem - 1-2 days/week",
            "Oxford, Mississippi",
            "Conduct protocol-required patient visits. Maintain supplies onsite.",
            False,
        ),
        (
            "Clinical Research Coordinator II - Per Diem - 1-2 days/week",
            "Jackson, TN",
            "Conduct protocol-required patient visits.",
            False,
        ),
        (
            "Clinical Research Coordinator II",
            "Homewood, AL",
            "Conduct protocol-required patient visits.",
            False,
        ),
        (
            "Senior Clinical Research Coordinator",
            "Flowood, MS",
            "Perform blood draws. Maintain study equipment onsite.",
            False,
        ),
    ],
)
def test_reported_crc_examples(title, location, body, license_required):
    job = raw(title, location, body)
    result = match_job(parse_job(job), PROFILE, preferences())
    assert result.score == 1  # Discovery coverage and score remain intact.
    assert not result.eligible
    assert result.candidate_eligibility.status == "unsuitable"
    assert any("onsite_geography_incompatible" in reason for reason in result.eligibility_reasons)
    assert (
        any("required_license" in reason for reason in result.eligibility_reasons)
        == license_required
    )
    assert work_arrangement(job)[0] == RemoteType.ONSITE


@pytest.mark.parametrize(
    "credential",
    [
        "RN",
        "R.N.",
        "registered nurse",
        "LPN",
        "L.P.N.",
        "licensed practical nurse",
        "LVN",
        "L.V.N.",
        "licensed vocational nurse",
    ],
)
def test_required_license_body_and_title(credential):
    for job in [
        raw(body=f"Active {credential} license required."),
        raw(title=f"Clinical Research Coordinator - {credential}"),
    ]:
        assert license_rejections(job, set())
        assert license_rejections(job, None)
        assert not match_job(parse_job(job), PROFILE, preferences()).eligible


@pytest.mark.parametrize(
    "body",
    [
        "RN license preferred.",
        "RN license not required.",
        "Work with registered nurses and licensed staff.",
        "Collaborate with RN staff. Active clinical collaboration required.",
        "Preferred Qualifications:\nActive RN license.\nResponsibilities:\nCoordinate clinical trials.",
        "CCRC or CCRP certification preferred.",
    ],
)
def test_optional_and_incidental_licenses_do_not_reject(body):
    assert match_job(parse_job(raw(body=body)), PROFILE, preferences()).eligible


def test_required_after_preferred_and_html():
    for body in [
        "RN preferred but LPN required.",
        "RN required and LPN preferred.",
        "&lt;h3&gt;Required qualifications&lt;/h3&gt;&lt;li&gt;Active RN license required.&lt;/li&gt;",
        "Preferred Qualifications:\nRN license.\nRequirements:\nLPN license required.",
    ]:
        assert license_rejections(raw(body=body), set())


def test_license_alternatives_and_holder():
    job = raw(body="Active RN or LPN license required.")
    assert match_job(
        parse_job(job), PROFILE, preferences(held_professional_licenses={"LPN"})
    ).eligible
    assert license_rejections(raw(body="RN and LPN licenses required."), {"RN"})
    assert (
        "alternative_unverified"
        in license_rejections(raw(body="RN license or equivalent experience required."), set())[0]
    )
    assert match_job(
        parse_job(raw(title="Clinical Research Coordinator - RN")),
        PROFILE,
        preferences(held_professional_licenses={"Registered Nurse"}),
    ).eligible


@pytest.mark.parametrize(
    "location,body,allowed",
    [
        ("Remote United States", "", True),
        ("Remote Florida", "", True),
        ("Remote CA", "", False),
        ("Remote United States", "Candidates must reside in Florida or Georgia.", True),
        ("Remote United States", "Candidates must reside in California or New York.", False),
        ("Remote United States", "Eligible states: CA, NY.", False),
        ("Remote United States", "Candidates must not reside in Florida.", False),
        ("Remote United States", "This role is not available in California.", True),
        ("Remote United States", "Remote work is available except in Florida.", False),
        ("Remote United States", "Candidates must reside in selected states.", False),
        ("Remote United States; Remote California; Remote New York", "", True),
        ("Remote", "", False),
        ("Remote Canada", "", False),
        ("Durham, NC", "This role is fully remote within the United States.", True),
        ("Durham, NC", "Home-based in the United States.", True),
        ("Home-based, US", "", True),
        ("Remote United States", "This role requires on-site attendance two days per week.", False),
        ("Tampa, FL", "Our company has remote teams throughout the United States.", True),
        ("Tampa, Florida", "This role is hybrid.", True),
        ("Tampa, FL", "This role is on-site in Miami, FL.", False),
        ("Miami, FL", "This role is hybrid.", False),
        ("", "", False),
    ],
)
def test_geography(location, body, allowed):
    result = match_job(parse_job(raw(location=location, body=body)), PROFILE, preferences())
    assert result.eligible == allowed, result.eligibility_reasons


def test_physical_per_diem_and_remote_disabled():
    job = raw(location="Jackson, TN", body="Per diem: must attend the clinic 1-2 days/week.")
    assert work_arrangement(job)[0] == RemoteType.ONSITE
    assert candidate_rejections(job, preferences())
    prefs = preferences().model_copy(update={"include_remote": False})
    assert candidate_rejections(raw(), prefs)


def test_no_statewide_commuting_and_no_substrings():
    for terms in [["Florida"], ["FL"], ["Tampa"], ["Tampa, FL"]]:
        prefs = preferences().model_copy(update={"location_terms": terms})
        assert candidate_rejections(raw(location="New Tampa, FL"), prefs)


def test_state_screening_is_not_commute_approval():
    for city in ["Cocoa", "Rockledge", "Melbourne", "Titusville", "Orlando", "Miami"]:
        assessment = assess_candidate(raw(location=f"{city}, Florida"), preferences())
        assert assessment.status == "review_needed"
        assert assessment.review_visible
    assert assess_candidate(raw(location="Tampa, FL"), preferences()).status == "eligible"
    assert assess_candidate(raw(location="Jackson, TN"), preferences()).status == "unsuitable"
    # Configurable policies, not hardcoded states or residence-derived exclusions.
    prefs = preferences(commuting_policy={"onsite_states": ["TN"]})
    assert assess_candidate(raw(location="Jackson, TN"), prefs).status == "review_needed"
    assert assess_candidate(raw(location="Tampa, FL"), prefs).status == "unsuitable"
    assert (
        assess_candidate(raw(location="Jackson, TN"), preferences(commuting_policy={})).status
        == "review_needed"
    )


def test_structured_requirements_and_remote_scope():
    metadata = _eligibility_metadata(
        {
            "requiredLicenses": ["RN"],
            "jobLocationType": "TELECOMMUTE",
            "applicantLocationRequirements": [{"name": "Florida"}],
        }
    )
    job = raw(location="", metadata=metadata)
    assert not match_job(parse_job(job), PROFILE, preferences()).eligible
    assert match_job(
        parse_job(job), PROFILE, preferences(held_professional_licenses={"RN"})
    ).eligible
    changed = job.model_copy(
        update={"metadata": _eligibility_metadata({"requiredLicenses": ["LPN"]})}
    )
    assert changed.content_hash != job.content_hash
    foreign = raw(
        metadata=_eligibility_metadata({"applicantLocationRequirements": {"name": "Canada"}})
    )
    assert candidate_rejections(foreign, preferences())
    unknown = raw(metadata={"eligibility": {"required_licenses": ["RN"]}})
    assert candidate_rejections(unknown, preferences(held_professional_licenses=None))


def test_state_names_and_invalid_config():
    assert states_in("West Virginia and North Carolina") == {"WV", "NC"}
    assert states_in("work in or near a clinic") == set()
    for fields in [
        {"residence_state": "invalid"},
        {"held_professional_licenses": ["BSN"]},
        {"commuting_policy": {"onsite_states": ["invalid"]}},
        {"commuting_policy": {"limited_attendance_states": ["invalid"]}},
    ]:
        with pytest.raises(ValidationError):
            CandidateEligibilityConfig(**fields)


def test_both_notification_helpers_reject_high_score():
    job = parse_job(raw(title="Clinical Research Coordinator - RN"))
    result = match_job(job, PROFILE, preferences())
    now = datetime.now(UTC)
    assert result.score == 1 and not result.eligible
    assert not pipeline._qualifies_for_immediate_notification(
        job, result, now, Settings(_env_file=None), is_new=True
    )
    message = render_run_summary(
        run_key="test",
        stats={},
        errors=[],
        matched_jobs=[MatchedJob("Acme", job, result, now, True, True)],
        zero_job_sources=[],
    )
    assert job.raw.title not in message


@pytest.mark.asyncio
@pytest.mark.parametrize("queued_review", [False, True])
async def test_pipeline_gates_daily_and_immediate_with_mocked_notifier(monkeypatch, queued_review):
    postings = [
        raw(title="Clinical Research Coordinator - RN", location="Dayton, OH"),
        raw(title="Clinical Project Coordinator", location="Remote United States"),
        raw(title="Clinical Research Coordinator Review", location="Orlando, FL"),
    ]
    storage, notifier = FakeStorage(), FakeNotifier()
    if queued_review:
        storage.outbox.append(
            {
                "id": "old-review",
                "job_id": postings[2].external_job_id,
                "profile": PROFILE.name,
                "version_hash": postings[2].content_hash,
                "score": 1.0,
                "message": "OLD REVIEW MUST NOT SEND",
            }
        )
    storage.notification_job = lambda job_id, content_hash: next(
        (
            job
            for job in postings
            if job.external_job_id == job_id and job.content_hash == content_hash
        ),
        None,
    )

    class Source:
        def __init__(self, *args):
            pass

        async def fetch(self, company):
            return postings

    monkeypatch.setattr(pipeline, "Storage", lambda *args, **kwargs: storage)
    monkeypatch.setattr(pipeline, "SourceRunner", Source)
    monkeypatch.setattr(pipeline, "TelegramNotifier", lambda *args, **kwargs: notifier)
    employer = company().model_copy(update={"profiles": [PROFILE.name]})
    settings = Settings(
        _env_file=None,
        database_url="sqlite:///unused",
        telegram_bot_token="fake",
        telegram_chat_id="fake",
        llm_enabled=False,
    )
    report = await pipeline.run_pipeline(
        settings,
        [employer],
        {PROFILE.name: PROFILE},
        preferences(),
        backfill=True,
        run_key="isolated-test",
    )
    assert not report.errors
    assert report.matches == 1 and report.notifications == 1
    assert len(notifier.messages) == 2
    assert report.eligibility_reviews == 1
    assert "Clinical Research Coordinator Review" not in notifier.messages[0]
    assert "Clinical Research Coordinator Review" in notifier.messages[1]
    assert "Commute/eligibility review needed" in notifier.messages[1]
    assert all("OLD REVIEW MUST NOT SEND" not in text for text in notifier.messages)
    assert all("Clinical Research Coordinator - RN" not in text for text in notifier.messages)
    assert all("Clinical Project Coordinator" in text for text in notifier.messages)
    assert any(not result.eligible and result.eligibility_reasons for _, result in storage.matches)


def test_queued_version_reader(tmp_path):
    db = Storage(f"sqlite:///{tmp_path / 'eligibility.db'}", create_schema=True)
    try:
        cid = db.sync_company(company())
        rid = db.start_run("local-test")
        job = raw(metadata={"eligibility": {"required_licenses": ["RN"]}})
        saved = db.persist_job_decisions(cid, rid, job, db.plan_job(cid, job), [])
        loaded = db.notification_job(saved.job_id, job.content_hash)
        assert loaded == job
        assert db.notification_job(saved.job_id, "missing") is None
        assert candidate_rejections(loaded, preferences())
    finally:
        db.engine.dispose()


def test_legacy_queue_gate_does_not_starve_valid_remote_alert(tmp_path):
    db = Storage(f"sqlite:///{tmp_path / 'queue.db'}", create_schema=True)
    try:
        employer = company().model_copy(update={"profiles": [PROFILE.name]})
        cid = db.sync_company(employer)
        rid = db.start_run("local-queue-test")
        postings = [
            raw(title="Clinical Research Coordinator - RN", location="Dayton, OH"),
            raw(title="Clinical Research Coordinator Review", location="Orlando, FL"),
            raw(title="Clinical Project Coordinator", location="Remote United States"),
        ]
        for job in postings:
            old = match_job(parse_job(job), PROFILE, SearchPreferences(excluded_seniorities=set()))
            db.persist_job_decisions(
                cid,
                rid,
                job,
                db.plan_job(cid, job),
                [MatchDecision(PROFILE.version, old, job.title)],
            )
        queued = db.claim_pending_notifications(
            rid, 1, eligibility_check=lambda job: candidate_rejections(job, preferences())
        )
        assert len(queued) == 1
        assert queued[0]["message"] == "Clinical Project Coordinator"
        with db.engine.connect() as conn:
            blocked = (
                conn.execute(
                    select(notification_outbox).where(
                        notification_outbox.c.message == postings[0].title
                    )
                )
                .mappings()
                .one()
            )
        assert "required_license_not_held" in blocked["last_error"]
        assert "onsite_geography_incompatible" in blocked["last_error"]
        assert blocked["claim_token"] is None
        with db.engine.connect() as conn:
            held = (
                conn.execute(
                    select(notification_outbox).where(
                        notification_outbox.c.message == postings[1].title
                    )
                )
                .mappings()
                .one()
            )
        assert "commute_not_verified" in held["last_error"]
        assert held["claim_token"] is None
    finally:
        db.engine.dispose()


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize("required,eligible", [(True, False), (False, True)])
async def test_source_preserves_credential_sections(required, eligible):
    body = (
        "<h3>Preferred Qualifications</h3><p>Active RN license.</p>"
        + ("<h3>Required Qualifications</h3><p>Active LPN license.</p>" if required else "")
        + "<h3>Responsibilities</h3><p>Coordinate clinical trial execution.</p>"
    )
    respx.get("https://boards-api.greenhouse.io/v1/boards/example/jobs?content=true").mock(
        return_value=httpx.Response(
            200,
            json={
                "jobs": [
                    {
                        "id": 1,
                        "title": "Clinical Research Coordinator",
                        "content": body,
                        "location": {"name": "Remote US"},
                        "absolute_url": "https://example.com/1",
                    }
                ]
            },
        )
    )
    employer = company().model_copy(
        update={"ats_type": "greenhouse", "ats_config": {"board_token": "example"}}
    )
    async with httpx.AsyncClient() as client:
        posting = (await GreenhouseSource(employer, client).fetch())[0]
    assert match_job(parse_job(posting), PROFILE, preferences()).eligible == eligible


@pytest.mark.parametrize(
    "body,kind,days,category",
    [
        ("This role requires 1 day/week onsite.", "weekly", 1, "limited"),
        ("This role requires two onsite days per week.", "weekly", 2, "limited"),
        ("This role requires 3 days/week onsite.", "weekly", 3, "frequent"),
        ("This role requires 1-3 onsite days/week.", "weekly", 3, "frequent"),
        ("This role requires 1\u20132 onsite days/week.", "weekly", 2, "limited"),
        ("This role requires monthly onsite attendance.", "monthly", None, "limited"),
        ("This role requires occasional onsite attendance.", "occasional", None, "limited"),
        ("This role is hybrid.", "unknown", None, "unknown"),
        ("This role offers 3 remote days per week.", "unknown", None, "unknown"),
        ("3 remote days per week and 2 onsite days per week.", "weekly", 2, "limited"),
        ("This role requires daily onsite attendance.", "unknown", None, "frequent"),
    ],
)
def test_attendance_frequency(body, kind, days, category):
    evidence = attendance_frequency(raw(location="Orlando, FL", body=body))
    assert (evidence.kind, evidence.max_days_per_week, evidence.category) == (kind, days, category)
    assert not evidence.conflicting


def test_conflicting_attendance_and_license_aggregation():
    job = raw(
        location="Tampa, FL", body="This role is hybrid. 1 day/week onsite. 3 days/week onsite."
    )
    assessment = assess_candidate(job, preferences())
    assert assessment.status == "review_needed"
    assert assessment.attendance.conflicting
    licensed = assess_candidate(
        job.model_copy(update={"title": "Clinical Research Coordinator RN"}), preferences()
    )
    assert licensed.status == "unsuitable"
    assert licensed.hard_reasons and licensed.review_reasons
    assert not licensed.review_visible
    distant = assess_candidate(job.model_copy(update={"location_raw": "Dayton, OH"}), preferences())
    assert distant.status == "unsuitable"


def test_limited_attendance_policy_is_more_permissive():
    prefs = preferences(
        commuting_policy={"onsite_states": ["FL"], "limited_attendance_states": ["GA"]}
    )
    for schedule in [
        "1 day/week onsite",
        "2 days/week onsite",
        "occasional onsite",
        "monthly onsite",
        "",
    ]:
        assessment = assess_candidate(
            raw(location="Savannah, GA", body=f"This role is hybrid. {schedule}"), prefs
        )
        assert assessment.status == "review_needed"
        assert assessment.review_visible
    for schedule in ["3 days/week onsite", "daily onsite"]:
        assessment = assess_candidate(
            raw(location="Savannah, GA", body=f"This role is hybrid. {schedule}"), prefs
        )
        assert assessment.status == "unsuitable"
    # Permission for occasional attendance does not make a faraway site remote.
    assert (
        assess_candidate(
            raw(location="Dayton, OH", body="This role is hybrid. Monthly onsite."), prefs
        ).status
        == "unsuitable"
    )


def test_physical_per_diem_frequency():
    assessment = assess_candidate(
        raw(
            title="Clinical Research Coordinator Per Diem 1-2 days/week",
            location="Oxford, MS",
            body="Conduct patient visits.",
        ),
        preferences(),
    )
    assert assessment.attendance.physical_per_diem
    assert assessment.attendance.max_days_per_week == 2
    assert assessment.work_arrangement == RemoteType.ONSITE
    assert assessment.status == "unsuitable"


@pytest.mark.parametrize(
    "location,expected",
    [
        ("Remote United States", "eligible"),
        ("Remote Florida", "eligible"),
        ("Remote California", "unsuitable"),
        ("Orlando, Florida", "review_needed"),
        ("Cocoa, FL", "review_needed"),
        ("Melbourne, FL, United States", "review_needed"),
        ("Rockledge, Florida, USA", "review_needed"),
        ("Titusville, FL 32780", "review_needed"),
        ("", "review_needed"),
        ("32780", "review_needed"),
        ("FL", "review_needed"),
        ("Unknown location", "review_needed"),
        ("Orlando, FL; Dayton, OH", "review_needed"),
        ("Oxford, MS | Jackson, TN", "unsuitable"),
    ],
)
def test_explicit_statuses(location, expected):
    result = match_job(parse_job(raw(location=location)), PROFILE, preferences())
    assert result.candidate_eligibility.status == expected
    assert result.discovery_eligible and result.score == 1 and result.tier == "strong"
    assert result.eligible == (expected == "eligible")


def test_mandatory_site_overrides_local_listing():
    result = assess_candidate(
        raw(location="Tampa, FL", body="This role is on-site in Jackson, Tennessee."), preferences()
    )
    assert result.status == "unsuitable"
    assert any(
        loc.source == "mandatory_attendance_text" and loc.states == ["TN"]
        for loc in result.locations
    )


def test_remote_hard_failure_overrides_unknown_restriction():
    result = assess_candidate(
        raw(
            body="Candidates must reside in selected states. Candidates must reside in California."
        ),
        preferences(),
    )
    assert result.status == "unsuitable"


def test_review_summary_is_capped_separate_and_does_not_leak_unsuitable():
    now = datetime.now(UTC)
    postings = [
        raw(title="Clinical Project Coordinator", location="Remote United States"),
        raw(title="Clinical Research Coordinator Review One", location="Orlando, FL"),
        raw(title="Clinical Research Coordinator Review Two", location="Cocoa, FL"),
        raw(title="Clinical Research Coordinator Unlocatable", location=""),
        raw(title="Clinical Research Coordinator RN", location="Dayton, OH"),
    ]
    matches = []
    for posting in postings:
        job = parse_job(posting)
        result = match_job(job, PROFILE, preferences())
        matches.append(MatchedJob("Acme", job, result, now, True, True))
        assert (
            pipeline._qualifies_for_immediate_notification(
                job, result, now, Settings(_env_file=None), is_new=True
            )
            == result.notification_eligible
        )
    message = render_run_summary(
        run_key="test",
        stats={},
        errors=[],
        matched_jobs=matches,
        zero_job_sources=[],
        max_reviews=1,
    )
    normal, review = message.split("Commute/eligibility review needed")
    assert postings[0].title in normal
    assert postings[1].title not in normal and postings[1].title in review
    assert postings[2].title not in message and "1 additional review records" in message
    assert postings[3].title not in message and "Unresolved eligibility/location: 1" in message
    assert postings[4].title not in message


def test_low_discovery_relevance_does_not_enter_review_section():
    job = parse_job(raw(title="Restaurant Chef", location="Orlando, FL", body=""))
    result = match_job(job, PROFILE, preferences())
    assert result.candidate_eligibility.status == "review_needed"
    assert not result.discovery_eligible and not result.needs_eligibility_review


def test_national_remote_does_not_evaluate_commuting(monkeypatch):
    import job_monitor.eligibility as eligibility

    def forbidden(*args):
        pytest.fail("national remote must bypass physical geography")

    monkeypatch.setattr(eligibility, "_physical_assessment", forbidden)
    assert assess_candidate(raw(), preferences()).status == "eligible"


@pytest.mark.asyncio
async def test_batch_pipeline_retains_review_assessments(tmp_path, monkeypatch):
    from job_monitor.storage import match_results

    db = Storage(f"sqlite:///{tmp_path / 'tristate.db'}", create_schema=True)
    notifier = FakeNotifier()
    postings = [
        raw(title="Clinical Project Coordinator", location="Remote United States"),
        raw(title="Clinical Research Coordinator Review", location="Cocoa, FL"),
        raw(title="Clinical Research Coordinator Missing", location=""),
        raw(title="Clinical Research Coordinator RN", location="Dayton, OH"),
    ]

    class Source:
        def __init__(self, *args):
            pass

        async def fetch(self, company):
            return postings

    monkeypatch.setattr(pipeline, "Storage", lambda *args, **kwargs: db)
    monkeypatch.setattr(pipeline, "SourceRunner", Source)
    monkeypatch.setattr(pipeline, "TelegramNotifier", lambda *args, **kwargs: notifier)
    try:
        report = await pipeline.run_pipeline(
            Settings(
                _env_file=None,
                database_url="sqlite:///unused",
                telegram_bot_token="fake",
                telegram_chat_id="fake",
                llm_enabled=False,
            ),
            [company().model_copy(update={"profiles": [PROFILE.name]})],
            {PROFILE.name: PROFILE},
            preferences(),
            backfill=True,
            run_key="isolated-tristate",
        )
        assert not report.errors
        assert report.matches == 1 and report.eligibility_reviews == 2
        assert report.notifications == 1 and len(notifier.messages) == 2
        assert "Clinical Research Coordinator Review" in notifier.messages[-1]
        assert "Unresolved eligibility/location: 1" in notifier.messages[-1]
        assert all("Clinical Research Coordinator RN" not in text for text in notifier.messages)
        with db.engine.connect() as conn:
            rows = conn.execute(select(match_results)).mappings().all()
        assert len(rows) == 4
        assert sum(row["eligible"] for row in rows) == 1
    finally:
        db.engine.dispose()


def test_unknown_license_alternative_is_review_not_hard_rejection():
    result = assess_candidate(
        raw(body="RN license or equivalent experience required."), preferences()
    )
    assert result.status == "review_needed"
    assert result.review_reasons and not result.hard_reasons
