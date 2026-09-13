import json

from typer.testing import CliRunner

import job_monitor.cli as cli
from job_monitor.cli import app
from job_monitor.config import Settings
from job_monitor.models import CompanyConfig, MatchResult, RawJob
from job_monitor.pipeline import RunReport
from job_monitor.storage import MatchDecision, Storage


def test_cli_help_uses_product_brand():
    result = CliRunner().invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "Job Radar TW" in result.stdout


def test_suppress_notifications_requires_backfill():
    result = CliRunner().invoke(app, ["run", "--suppress-notifications"])

    assert result.exit_code != 0
    assert "--suppress-notifications requires --backfill" in result.stderr


def test_suppressed_backfill_does_not_require_telegram_secrets(monkeypatch):
    company = CompanyConfig(
        slug="acme",
        name="Acme",
        careers_url="https://example.com/jobs",
        ats_type="jsonld",
        industry="analytics",
        profiles=["tech"],
        source_verified=True,
    )
    settings = Settings(_env_file=None, database_url="sqlite:///unused.db")
    captured = {}

    async def fake_run_pipeline(*args, **kwargs):
        captured.update(kwargs)
        return RunReport(run_key="suppressed-cli-test")

    monkeypatch.setattr(cli, "_load", lambda selected=None: (settings, [company], {}, None, None))
    monkeypatch.setattr(cli, "run_pipeline", fake_run_pipeline)

    result = CliRunner().invoke(app, ["run", "--backfill", "--suppress-notifications"])

    assert result.exit_code == 0, result.output
    assert captured["backfill"] is True
    assert captured["suppress_notifications"] is True


def test_export_handoff_writes_stable_files(tmp_path, monkeypatch):
    database_url = f"sqlite:///{tmp_path / 'cli.db'}"
    db = Storage(database_url, create_schema=True)
    company_id = db.sync_company(
        CompanyConfig(
            slug="acme",
            name="Acme",
            careers_url="https://example.com/jobs",
            ats_type="jsonld",
            industry="tech",
            profiles=["tech"],
            source_verified=True,
        )
    )
    item = RawJob(
        source_company="acme",
        external_job_id="1",
        title="Data Analyst",
        location_raw="Phoenix, AZ",
        description_raw="SQL",
        url="https://example.com/jobs/1",
    )
    run_id = db.start_run("daily-2026-01-01")
    db.persist_job_decisions(
        company_id,
        run_id,
        item,
        db.plan_job(company_id, item),
        [
            MatchDecision(
                profile_version="1",
                result=MatchResult(profile="tech", score=0.9, eligible=True, tier="strong"),
            )
        ],
    )
    db.finish_run(run_id, {"sources_attempted": 1, "sources_succeeded": 1, "jobs_fetched": 1}, [])

    monkeypatch.setenv("DATABASE_URL", database_url)
    markdown = tmp_path / "handoff" / "latest.md"
    payload = tmp_path / "handoff" / "latest.json"
    args = ["export-handoff", "--out", str(markdown), "--json-out", str(payload)]

    first = CliRunner().invoke(app, args)
    assert first.exit_code == 0, first.output
    summary = json.loads(first.stdout)
    assert summary["jobs"] == 1
    assert summary["source_run"] == "daily-2026-01-01"
    assert "Data Analyst" in markdown.read_text(encoding="utf-8")

    before = (markdown.read_text(encoding="utf-8"), payload.read_text(encoding="utf-8"))
    second = CliRunner().invoke(app, args)
    assert second.exit_code == 0, second.output
    assert (markdown.read_text(encoding="utf-8"), payload.read_text(encoding="utf-8")) == before
