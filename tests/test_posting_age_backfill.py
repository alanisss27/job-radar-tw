from datetime import UTC, datetime

import pytest
from sqlalchemy import select, update

from job_monitor import pipeline
from job_monitor.config import Settings
from job_monitor.models import MatchResult
from job_monitor.storage import MatchDecision, Storage, jobs, source_runs
from test_candidate_eligibility import PROFILE, preferences, raw
from test_pipeline import FakeNotifier
from test_storage import company


@pytest.mark.asyncio
async def test_age_backfill_only_current_official_matches_once(tmp_path, monkeypatch):
    db = Storage(f"sqlite:///{tmp_path / 'age.db'}", create_schema=True)
    cfg = company().model_copy(update={"profiles": [PROFILE.name]})
    cid = db.sync_company(cfg)
    seed = db.claim_run("seed")
    postings = []
    ids = []
    for name in ["active", "closed", "missing", "unfit", "recent"]:
        job = raw(title="Clinical Project Coordinator", location="Remote United States")
        job = job.model_copy(update={
            "external_job_id": name,
            "posted_at": datetime(2020 if name != "recent" else 2026, 1, 1, tzinfo=UTC),
        })
        plan = db.plan_job(cid, job)
        db.persist_job_decisions(cid, seed.run_id, job, plan, [MatchDecision(
            profile_version=PROFILE.version,
            result=MatchResult(profile=PROFILE.name, score=1, tier="strong", eligible=name != "unfit"),
        )])
        postings.append(job)
        ids.append(plan.job_id)
    with db.engine.begin() as conn:
        conn.execute(update(jobs).values(first_seen_at=datetime(2026, 1, 2, tzinfo=UTC)))
        conn.execute(update(jobs).where(jobs.c.id == ids[1]).values(status="closed"))
    db.source_succeeded(cid, seed.run_id)
    db.finish_run(seed.run_id, {}, [])
    assert db.age_suppressed_job_ids(cid, 21, .82) == {ids[0], ids[2]}

    class Source:
        def __init__(self, *args):
            pass

        async def fetch(self, company):
            # Missing/closed historical records are not returned by the employer.
            return [postings[0], postings[3], postings[4]]

    notifier = FakeNotifier()
    monkeypatch.setattr(pipeline, "Storage", lambda *args, **kwargs: db)
    monkeypatch.setattr(pipeline, "SourceRunner", Source)
    monkeypatch.setattr(pipeline, "TelegramNotifier", lambda *args, **kwargs: notifier)
    waits = []

    async def sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr(pipeline.asyncio, "sleep", sleep)
    monkeypatch.setattr(pipeline, "notification_delay", lambda **kwargs: 123)
    settings = Settings(_env_file=None, database_url="sqlite:///unused",
                        telegram_bot_token="fake", telegram_chat_id="fake", llm_enabled=False)
    for index in range(2):
        report = await pipeline.run_pipeline(settings, [cfg], {PROFILE.name: PROFILE},
                                             preferences(), scheduled=True, run_key=f"test-{index}")
        assert not report.errors
        assert report.matches == (1 if index == 0 else 0)
        assert report.notifications == (1 if index == 0 else 0)
    assert waits == [123, 123]
    with db.engine.connect() as conn:
        assert conn.execute(select(source_runs.c.status).where(
            source_runs.c.run_key == f"posting-age-v1-{cid}"
        )).scalar_one() == "success"
        assert conn.execute(select(jobs.c.status).where(jobs.c.id == ids[1])).scalar_one() == "closed"
    db.engine.dispose()
