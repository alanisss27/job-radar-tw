"""Offline schedule/claim/delivery checks; only synthetic sources and in-memory storage."""

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import select, update
from typer.testing import CliRunner
import yaml

from job_monitor import cli, pipeline
from job_monitor.config import SearchPreferences, Settings
from job_monitor.models import CompanyConfig
from job_monitor.schedule import notification_delay, schedule_decision, scheduled_run_key
from job_monitor.storage import Storage, source_runs

ET = ZoneInfo("America/New_York")


@pytest.fixture(autouse=True)
def no_http(monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError("HTTP/Telegram is forbidden in schedule tests")

    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden)


@pytest.mark.parametrize("date", ["2026-10-21", "2026-03-08", "2026-11-01"])
@pytest.mark.parametrize(
    "time,allowed",
    [
        ("00:00", False),
        ("06:59", False),
        ("07:00", True),
        ("11:42", True),
        ("14:59", True),
        ("15:00", False),
    ],
)
def test_same_day_catch_up_boundaries(date, time, allowed):
    now = datetime.fromisoformat(f"{date}T{time}").replace(tzinfo=ET)
    key = scheduled_run_key(now.astimezone(UTC), grace_hours=8)
    assert key == (f"daily-{date}" if allowed else None)
    decision = schedule_decision(now, timezone="America/New_York", hour=7, grace_hours=8)
    assert decision["daily_key"] == f"daily-{date}"
    assert datetime.fromisoformat(decision["cutoff_exclusive"]) == now.replace(hour=15, minute=0)


def frozen_cli(monkeypatch, now):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz or ET)

    settings = Settings(
        _env_file=None,
        schedule_grace_hours=8,
        telegram_bot_token="synthetic",
        telegram_chat_id="synthetic",
    )
    monkeypatch.setattr(cli, "datetime", Clock)
    monkeypatch.setattr(cli, "_load", lambda selected=None: (settings, [], {}, None, None))


@pytest.mark.parametrize("hour,minute", [(15, 0), (6, 59), (0, 0)])
def test_outside_window_has_status_without_constructing_pipeline(
    monkeypatch, tmp_path, hour, minute
):
    frozen_cli(monkeypatch, datetime(2026, 10, 21, hour, minute, tzinfo=ET))

    def forbidden(*args, **kwargs):
        raise AssertionError("Outside-window invocation must not construct pipeline/storage")

    monkeypatch.setattr(cli, "run_pipeline", forbidden)
    monkeypatch.setattr(pipeline, "Storage", forbidden)
    monkeypatch.setattr(pipeline, "SourceRunner", forbidden)
    path = tmp_path / "status.json"
    result = CliRunner().invoke(cli.app, ["run", "--scheduled", "--status-file", str(path)])
    assert result.exit_code == 0, result.output
    status = json.loads(path.read_text())
    assert status["scan_outcome"] == "skipped_outside_window"
    assert status["schedule"]["daily_key"] == "daily-2026-10-21"
    assert status["sources_attempted"] == status["individual_notifications_sent"] == 0
    assert status["count_scope"] == "current_invocation"
    assert json.loads(result.stdout)["scan_outcome"] == "skipped_outside_window"


@pytest.mark.parametrize("hour,minute", [(11, 42), (14, 59)])
def test_cli_accepts_delayed_runs_with_same_daily_key(monkeypatch, tmp_path, hour, minute):
    frozen_cli(monkeypatch, datetime(2026, 10, 21, hour, minute, tzinfo=ET))

    async def fake(*args, **kwargs):
        assert kwargs["run_key"] == "daily-2026-10-21" and kwargs["scheduled"]
        report = kwargs["report"]
        report.sources_attempted = report.sources_succeeded = 2
        report.scan_outcome = "scan_completed"
        return report

    monkeypatch.setattr(cli, "run_pipeline", fake)
    path = tmp_path / "status.json"
    result = CliRunner().invoke(cli.app, ["run", "--scheduled", "--status-file", str(path)])
    assert result.exit_code == 0, result.output
    status = json.loads(path.read_text())
    assert status["scan_outcome"] == "scan_completed"
    assert status["schedule"]["decision_time"].startswith(f"2026-10-21T{hour:02}:{minute:02}")
    assert status["schedule"]["cutoff_exclusive"] == "2026-10-21T15:00:00-04:00"


def test_cli_reports_pipeline_exception_without_masking_failure(monkeypatch, tmp_path):
    frozen_cli(monkeypatch, datetime(2026, 10, 21, 11, 42, tzinfo=ET))

    async def fail(*args, **kwargs):
        kwargs["report"].sources_attempted = 1
        raise RuntimeError("synthetic pipeline failure")

    monkeypatch.setattr(cli, "run_pipeline", fail)
    path = tmp_path / "status.json"
    result = CliRunner().invoke(cli.app, ["run", "--scheduled", "--status-file", str(path)])
    assert result.exit_code != 0
    assert isinstance(result.exception, RuntimeError)
    assert json.loads(path.read_text())["scan_outcome"] == "scan_failed"


@pytest.fixture
def offline_pipeline(monkeypatch):
    db = Storage("sqlite:///:memory:", create_schema=True)
    state = SimpleNamespace(db=db, calls=[], events=[], fail=set(), delivery_error=False)

    class Runner:
        def __init__(self, *args):
            state.events.append("source_runner")

        async def fetch_with_warnings(self, company):
            state.calls.append(company.slug)
            if company.slug in state.fail:
                raise RuntimeError("synthetic source failure")
            return [], []

    class Notifier:
        async def send(self, text):
            state.events.append("send")
            if state.delivery_error:
                raise RuntimeError("synthetic delivery failure")

    async def sleep(delay):
        state.events.append(("sleep", delay))

    monkeypatch.setattr(pipeline, "Storage", lambda *args, **kwargs: db)
    monkeypatch.setattr(pipeline, "SourceRunner", Runner)
    monkeypatch.setattr(pipeline, "TelegramNotifier", lambda *args: Notifier())
    monkeypatch.setattr(pipeline, "notification_delay", lambda **kwargs: 0)
    monkeypatch.setattr(pipeline.asyncio, "sleep", sleep)
    state.settings = Settings(
        _env_file=None,
        database_url="sqlite:///:memory:",
        telegram_bot_token="synthetic",
        telegram_chat_id="synthetic",
        llm_enabled=False,
        resume_path=None,
        resume_text=None,
    )
    state.companies = [
        CompanyConfig(
            slug=name,
            name=name,
            careers_url="https://example.test",
            ats_type="jsonld",
            industry="tech",
            profiles=["tech"],
        )
        for name in ("alpha", "beta")
    ]

    async def run():
        return await pipeline.run_pipeline(
            state.settings,
            state.companies,
            {},
            SearchPreferences(),
            run_key="daily-2026-10-21",
            scheduled=True,
        )

    state.run = run
    yield state
    db.engine.dispose()


@pytest.mark.asyncio
async def test_successful_daily_scan_blocks_rescan_and_delivery(offline_pipeline):
    state = offline_pipeline
    first = await state.run()
    assert first.scan_outcome == "scan_completed"
    previous = list(state.events)
    second = await state.run()
    assert second.scan_outcome == "skipped_daily_claim_unavailable"
    assert second.sources_attempted == second.notifications == 0
    assert state.events == previous and len(state.calls) == 2
    with state.db.engine.connect() as conn:
        row = conn.execute(select(source_runs)).mappings().one()
    assert row["status"] == "success"
    assert row["stats"]["scan_outcome"] == "scan_completed"


@pytest.mark.asyncio
async def test_fresh_running_claim_is_not_reported_as_success(offline_pipeline):
    state = offline_pipeline
    state.db.claim_run("daily-2026-10-21")
    report = await state.run()
    assert report.scan_outcome == "skipped_daily_claim_unavailable"
    assert state.events == state.calls == []


@pytest.mark.asyncio
async def test_stale_claim_can_retry_after_120_minutes(offline_pipeline):
    state = offline_pipeline
    claim = state.db.claim_run("daily-2026-10-21")
    with state.db.engine.begin() as conn:
        conn.execute(
            update(source_runs).values(started_at=datetime.now(UTC) - timedelta(minutes=121))
        )
    report = await state.run()
    assert report.scan_outcome == "scan_completed" and len(state.calls) == 2
    assert not state.db.finish_run(claim.run_id, {}, [])


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", [{"alpha"}, {"alpha", "beta"}])
async def test_partial_and_failed_scans_remain_retryable(offline_pipeline, failed):
    state = offline_pipeline
    state.fail = failed
    first = await state.run()
    expected = "scan_failed" if len(failed) == 2 else "scan_partial"
    assert first.scan_outcome == expected
    with state.db.engine.connect() as conn:
        row = conn.execute(select(source_runs)).mappings().one()
    assert row["status"] == ("failed" if len(failed) == 2 else "partial")
    assert row["stats"]["scan_outcome"] == expected
    state.fail = set()
    assert (await state.run()).scan_outcome == "scan_completed"
    assert len(state.calls) == 4


@pytest.mark.asyncio
async def test_delivery_failure_does_not_relabel_completed_scan(offline_pipeline):
    state = offline_pipeline
    state.delivery_error = True
    report = await state.run()
    assert report.scan_outcome == "scan_completed"
    assert report.errors and report.errors[0]["company"] == "telegram"
    with state.db.engine.connect() as conn:
        assert conn.execute(select(source_runs.c.status)).scalar_one() == "partial"


@pytest.mark.asyncio
async def test_processing_crash_preserves_partial_outcome(offline_pipeline, monkeypatch):
    state = offline_pipeline
    original = state.db.source_succeeded

    def crash_on_beta(company_id, run_id):
        if len(state.calls) == 2 and state.events.count("persisted") == 1:
            raise RuntimeError("synthetic processing crash")
        state.events.append("persisted")
        return original(company_id, run_id)

    monkeypatch.setattr(state.db, "source_succeeded", crash_on_beta)
    report = pipeline.RunReport(run_key="daily-2026-10-21")
    with pytest.raises(RuntimeError, match="synthetic processing crash"):
        await pipeline.run_pipeline(
            state.settings,
            state.companies,
            {},
            SearchPreferences(),
            run_key=report.run_key,
            scheduled=True,
            report=report,
        )
    assert report.scan_outcome == "scan_partial" and report.sources_succeeded == 1
    with state.db.engine.connect() as conn:
        row = conn.execute(select(source_runs)).mappings().one()
    assert row["stats"]["scan_outcome"] == "scan_partial"
    assert row["status"] == "partial"


@pytest.mark.asyncio
@pytest.mark.parametrize("time,delay", [("07:30", 5400), ("11:42", 0), ("14:59", 0)])
async def test_scheduled_delivery_waits_only_before_nine(
    offline_pipeline, monkeypatch, time, delay
):
    now = datetime.fromisoformat("2026-10-21T" + time).replace(tzinfo=ET)
    monkeypatch.setattr(
        pipeline, "notification_delay", lambda **kwargs: notification_delay(now, **kwargs)
    )
    original = offline_pipeline.db.claim_pending_notifications

    def claim(*args, **kwargs):
        offline_pipeline.events.append("claim_outbox")
        return original(*args, **kwargs)

    monkeypatch.setattr(offline_pipeline.db, "claim_pending_notifications", claim)
    await offline_pipeline.run()
    assert offline_pipeline.events[-3:] == [("sleep", delay), "claim_outbox", "send"]


def test_workflow_policy_and_summary_execution(tmp_path, monkeypatch):
    workflow = yaml.safe_load(Path(".github/workflows/monitor.yml").read_text(encoding="utf-8"))
    assert workflow[True]["schedule"] == [
        {"cron": "30 7,8,9 * * *", "timezone": "America/New_York"}
    ]
    steps = workflow["jobs"]["monitor"]["steps"]
    monitor = next(s for s in steps if s.get("name") == "Run scheduled monitor")
    assert monitor["env"]["SCHEDULE_GRACE_HOURS"] == "8"
    assert '--status-file "${RUNNER_TEMP}/job-radar-scan-status.json"' in monitor["run"]
    step = next(s for s in steps if s.get("name") == "Summarize scheduled scan outcome")
    assert step["if"] == "always() && github.event_name == 'schedule'"
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    code = step["run"].split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    exec(compile(code, "workflow-summary", "exec"), {})
    assert "unknown" in summary.read_text()
    report = pipeline.RunReport(
        run_key="daily-test", scan_outcome="skipped_daily_claim_unavailable"
    )
    (tmp_path / "job-radar-scan-status.json").write_text(json.dumps(report.scan_status()))
    exec(compile(code, "workflow-summary", "exec"), {})
    assert "skipped_daily_claim_unavailable" in summary.read_text()
