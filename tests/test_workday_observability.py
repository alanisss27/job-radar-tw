import logging
from datetime import UTC, datetime
from email.utils import format_datetime

import httpx
import pytest
import respx
from sqlalchemy import select, update

from job_monitor import pipeline
from job_monitor.config import SearchPreferences, Settings
from job_monitor.models import CompanyConfig, RawJob
from job_monitor.notifier import render_run_summary
from job_monitor.sources import WorkdayRequestController, WorkdayRequestError
from job_monitor.storage import Storage, companies, jobs, source_runs


@pytest.mark.parametrize("count", [0, 1, 8, 25])
def test_summary_error_total_and_displayed_count(count):
    summary = render_run_summary(
        run_key="test",
        stats={},
        errors=[{"company": f"source-{i}", "error": "<HTTP 429>"} for i in range(count)],
        matched_jobs=[],
        zero_job_sources=[],
    )
    if count:
        assert f"共 {count} 筆，目前列出 {min(count, 8)} 筆" in summary
        assert summary.count("&lt;HTTP 429&gt;") == min(count, 8)
        assert "source-8:" not in summary
    else:
        assert "來源錯誤" not in summary


@pytest.mark.parametrize(
    "value,expected",
    [
        ("2", 2),
        ("999", 999),
        ("-1", 0),
        ("invalid", None),
        ("nan", None),
        ("inf", None),
        (None, None),
    ],
)
def test_retry_after_parsing(value, expected):
    headers = {} if value is None else {"Retry-After": value}
    assert WorkdayRequestController._retry_after(httpx.Response(429, headers=headers)) == expected


@pytest.mark.parametrize("seconds", [-10, 20, 60])
def test_retry_after_http_date_is_not_capped(monkeypatch, seconds):
    monkeypatch.setattr("job_monitor.sources.time.time", lambda: 1000)
    date = format_datetime(datetime.fromtimestamp(1000 + seconds, UTC), usegmt=True)
    response = httpx.Response(429, headers={"Retry-After": date})
    assert WorkdayRequestController._retry_after(response) == max(0, seconds)


@pytest.mark.parametrize("retry_after", [None, "invalid", "nan", "2", "31"])
async def test_429_diagnostics_are_safe_and_measure_actual_wait(monkeypatch, caplog, retry_after):
    clock = [100.0]
    sleeps = []
    calls = []

    async def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds + 0.125

    monkeypatch.setattr("job_monitor.sources.asyncio.sleep", sleep)
    monkeypatch.setattr("job_monitor.sources.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("job_monitor.sources.random.uniform", lambda *_: 0.1)

    def handler(request):
        calls.append(request)
        headers = {"x-request-id": "safe-id:123", "Set-Cookie": "secret-cookie"}
        if retry_after is not None:
            headers["Retry-After"] = retry_after
        return httpx.Response(429 if len(calls) == 1 else 200, headers=headers, text="secret-body")

    caplog.set_level(logging.WARNING, logger="job_monitor.sources")
    controller = WorkdayRequestController(min_interval_seconds=0)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        if retry_after == "31":
            with pytest.raises(WorkdayRequestError, match="Retry-After 31s exceeds wait budget"):
                await controller.request(client, "GET", "https://example.test/private?token=secret")
            assert len(calls) == 1
            assert not sleeps
            assert "action=stop_retry_after_budget" in caplog.text
            assert "waited_seconds=0.000" in caplog.text
        elif retry_after != "2":
            with pytest.raises(WorkdayRequestError, match="HTTP 429") as exc:
                await controller.request(client, "GET", "https://example.test/private?token=secret")
            assert exc.value.status_code == 429
            assert len(calls) == 1
            assert not sleeps
            assert "action=stop_429_without_retry_after" in caplog.text
            assert "retry_after_seconds=missing_or_invalid" in caplog.text
            assert "requested_wait_seconds=0.000 waited_seconds=0.000" in caplog.text
        else:
            await controller.request(client, "GET", "https://example.test/private?token=secret")
            assert len(calls) == 2
            assert sleeps == [2.0]
            assert "waited_seconds=2.125" in caplog.text
    assert "attempt=1/3" in caplog.text
    assert "request_id=x-request-id:safe-id:123" in caplog.text
    assert "retry_after_seconds=" in caplog.text
    assert "secret" not in caplog.text
    assert "/private" not in caplog.text


async def test_persistent_429_logs_final_attempt_and_rejects_unsafe_id(monkeypatch, caplog):
    async def sleep(_):
        pass

    monkeypatch.setattr("job_monitor.sources.asyncio.sleep", sleep)
    caplog.set_level(logging.WARNING, logger="job_monitor.sources")
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            429, headers={"x-request-id": "unsafe\nforged-log", "Retry-After": "0"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(WorkdayRequestError, match="HTTP 429"):
            await WorkdayRequestController(min_interval_seconds=0).request(
                client, "POST", "https://example.test/jobs"
            )
    assert len(calls) == 3
    assert "attempt=3/3" in caplog.text
    assert "action=stop_attempts" in caplog.text
    assert "request_id=unavailable" in caplog.text
    assert "forged-log" not in caplog.text


@pytest.mark.parametrize(
    "detail,validate,retry_after",
    [
        (False, False, "60"),
        (False, False, None),
        (True, False, "60"),
        (True, True, "60"),
        (True, False, None),
        (True, True, "invalid"),
        (True, False, "0"),
        (True, True, "0"),
    ],
)
@respx.mock
async def test_429_source_failure_preserves_inventory(
    tmp_path, monkeypatch, detail, validate, retry_after
):
    company = CompanyConfig(
        slug="acme",
        name="Acme",
        careers_url="https://example.test/jobs",
        ats_type="workday",
        industry="tech",
        profiles=["tech"],
        source_verified=True,
        ats_config={
            "endpoint": "https://example.test/jobs",
            "site": "acme",
            "detail_base_url": "https://example.test/careers",
            "detail_api_base": "https://example.test/details",
            "validate_location_facets": validate,
            "facet_patterns": {"locations": "US"} if validate else {},
        },
    )
    db = Storage(f"sqlite:///{tmp_path / 'inventory.db'}", create_schema=True)
    company_id = db.sync_company(company)
    seed = db.start_run("seed")
    raw = RawJob(
        source_company="acme",
        external_job_id="old",
        title="Existing job",
        location_raw="US",
        description_raw="Existing",
        url="https://example.test/job/old",
    )
    db.upsert_job(company_id, seed, raw)
    db.finish_run(seed, {}, [])
    # One more missing scan would close this job if failure protection regressed.
    with db.engine.begin() as conn:
        conn.execute(update(jobs).values(missing_count=1))
    monkeypatch.setattr(pipeline, "Storage", lambda *a, **kw: db)

    def unexpected_mark_missing(*args):
        pytest.fail("incomplete Workday inventory must skip mark_missing")

    monkeypatch.setattr(db, "mark_missing", unexpected_mark_missing)
    headers = {} if retry_after is None else {"Retry-After": retry_after}
    listing = respx.post(company.ats_config["endpoint"])
    if detail:
        listing.respond(
            200,
            json={
                "total": 2,
                "facets": [
                    {"facetParameter": "locations", "values": [{"id": "us", "descriptor": "US"}]}
                ],
                "jobPostings": [
                    {"externalPath": "/job/new", "title": "New job", "locationsText": "US"},
                    {"externalPath": "/job/later", "title": "Later job", "locationsText": "US"},
                ],
            },
        )
        failed = respx.get("https://example.test/details/job/new").respond(429, headers=headers)
    else:
        failed = listing.respond(429, headers=headers)

    report = await pipeline.run_pipeline(
        Settings(
            _env_file=None,
            database_url="sqlite:///unused",
            telegram_bot_token=None,
            telegram_chat_id=None,
            llm_enabled=False,
        ),
        [company],
        {},
        SearchPreferences(),
        run_key="429-test",
    )
    assert failed.call_count == (3 if retry_after == "0" else 1)
    assert report.sources_succeeded == 0
    assert report.scan_outcome == "scan_failed"
    assert report.jobs_fetched == report.jobs_closed == 0
    assert "HTTP 429" in report.errors[0]["error"]
    with db.engine.connect() as conn:
        row = conn.execute(select(jobs.c.status, jobs.c.missing_count)).one()
        assert tuple(row) == ("active", 1)
        assert conn.execute(select(companies.c.consecutive_failures)).scalar_one() == 1
        run = (
            conn.execute(select(source_runs).where(source_runs.c.run_key == "429-test"))
            .mappings()
            .one()
        )
        assert run["status"] == "failed"
    db.engine.dispose()
