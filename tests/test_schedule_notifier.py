from datetime import UTC, datetime, timedelta

import httpx
import pytest

from job_monitor.config import Settings
from job_monitor.models import MatchedJob, MatchResult, ParsedJob, ProfileName, RawJob
from job_monitor.notifier import (
    TelegramNotifier,
    render_job_message,
    render_freshness,
    render_run_summary,
    source_age_days,
    split_message,
)
from job_monitor.pipeline import _qualifies_for_immediate_notification
from job_monitor.schedule import is_scheduled_window, local_run_key, scheduled_run_key


def test_et_schedule_handles_dst():
    assert is_scheduled_window(datetime(2026, 6, 18, 0, 0, tzinfo=UTC))
    assert is_scheduled_window(datetime(2026, 1, 18, 1, 0, tzinfo=UTC))
    assert is_scheduled_window(datetime(2026, 6, 18, 6, 0, tzinfo=UTC))


def test_run_key_uses_et_date():
    assert local_run_key(datetime(2026, 6, 18, 0, 0, tzinfo=UTC)) == "daily-2026-06-17"


def test_scheduled_run_key_handles_delayed_github_delivery():
    assert scheduled_run_key(datetime(2026, 6, 18, 0, 17, tzinfo=UTC)) == "daily-2026-06-17"
    assert scheduled_run_key(datetime(2026, 6, 18, 6, 6, tzinfo=UTC)) == "daily-2026-06-17"
    assert scheduled_run_key(datetime(2026, 6, 18, 23, 30, tzinfo=UTC)) is None
    assert scheduled_run_key(datetime(2026, 6, 19, 0, 30, tzinfo=UTC)) == "daily-2026-06-18"


def test_scheduled_run_key_skips_before_et_window():
    assert scheduled_run_key(datetime(2026, 1, 18, 0, 17, tzinfo=UTC)) is None
    assert scheduled_run_key(datetime(2026, 1, 18, 1, 17, tzinfo=UTC)) == "daily-2026-01-17"


def test_schedule_accepts_custom_timezone_hour_and_grace_period():
    within_window = datetime(2026, 6, 18, 1, 30, tzinfo=UTC)
    outside_window = datetime(2026, 6, 18, 3, 0, tzinfo=UTC)

    assert local_run_key(within_window, timezone="Asia/Taipei") == "daily-2026-06-18"
    assert (
        scheduled_run_key(
            within_window,
            timezone="Asia/Taipei",
            hour=9,
            grace_hours=2,
        )
        == "daily-2026-06-18"
    )
    assert not is_scheduled_window(
        outside_window,
        timezone="Asia/Taipei",
        hour=9,
        grace_hours=2,
    )


def test_message_split_respects_limit():
    chunks = split_message("line\n" * 100, limit=50)
    assert len(chunks) > 1
    assert all(len(chunk) <= 50 for chunk in chunks)


def test_run_summary_lists_matches_and_source_warnings():
    raw = RawJob(
        source_company="acme",
        external_job_id="1",
        title="Data Analyst",
        location_raw="Austin, TX",
        description_raw="SQL analytics",
        posted_at=datetime(2026, 6, 25, tzinfo=UTC),
        url="https://example.com/jobs/1",
    )
    job = ParsedJob(raw=raw)
    result = MatchResult(profile=ProfileName.TECH, score=0.82, eligible=True, tier="strong")
    matched = MatchedJob(
        company_name="Acme",
        job=job,
        result=result,
        first_seen_at=datetime(2026, 6, 26, tzinfo=UTC),
        is_new=True,
        changed=True,
    )

    summary = render_run_summary(
        run_key="daily-2026-06-26",
        stats={
            "sources_attempted": 2,
            "sources_succeeded": 1,
            "jobs_fetched": 10,
            "matches": 1,
            "immediate_candidates": 1,
        },
        errors=[{"company": "broken-source", "error": "timeout"}],
        matched_jobs=[matched],
        zero_job_sources=["Empty Verified Source"],
    )

    assert "Data Analyst" in summary
    assert "https://example.com/jobs/1" in summary
    assert "broken-source" in summary
    assert "Empty Verified Source" in summary
    assert "來源 1 天前｜新發布" in summary
    assert "Job Radar TW" in summary
    assert "職缺雷達" in summary
    assert "逐筆候選" in summary


def test_job_message_includes_freshness():
    raw = RawJob(
        source_company="acme",
        external_job_id="1",
        title="Data Analyst",
        location_raw="Austin, TX",
        description_raw="SQL analytics",
        posted_at=datetime(2026, 6, 25, tzinfo=UTC),
        url="https://example.com/jobs/1",
    )
    job = ParsedJob(raw=raw)
    result = MatchResult(profile=ProfileName.TECH, score=0.84, eligible=True, tier="strong")

    message = render_job_message("Acme", job, result, datetime(2026, 6, 26, tzinfo=UTC))

    assert "新鮮度" in message
    assert "2026-06-25" in message
    assert (
        "\u9996\u6b21\u767c\u73fe\uff1a2026-06-26\uff1b\u4f86\u6e90\u65e5\u671f\uff1a2026-06-25"
        in message
    )
    assert "?????" not in message
    assert "?????" not in message
    assert source_age_days(raw.posted_at, datetime(2026, 6, 26, tzinfo=UTC)) == 1


def test_immediate_notification_gate_requires_fresh_new_strong_match():
    settings = Settings(
        immediate_notification_min_score=0.82,
        immediate_notification_max_source_age_days=14,
    )
    raw = RawJob(
        source_company="acme",
        external_job_id="1",
        title="Senior Data Analyst",
        location_raw="Austin, TX",
        description_raw="SQL analytics",
        posted_at=datetime(2026, 6, 20, tzinfo=UTC),
        url="https://example.com/jobs/1",
    )
    job = ParsedJob(raw=raw)
    result = MatchResult(profile=ProfileName.TECH, score=0.84, eligible=True, tier="strong")

    assert _qualifies_for_immediate_notification(
        job,
        result,
        datetime(2026, 6, 26, tzinfo=UTC),
        settings,
        is_new=True,
    )
    assert not _qualifies_for_immediate_notification(
        job,
        result,
        datetime(2026, 6, 26, tzinfo=UTC),
        settings,
        is_new=False,
    )
    assert not _qualifies_for_immediate_notification(
        job,
        result,
        datetime(2026, 7, 20, tzinfo=UTC),
        settings,
        is_new=True,
    )


def test_backfill_gate_allows_old_existing_strong_match_above_threshold():
    settings = Settings(
        immediate_notification_min_score=0.82,
        immediate_notification_max_source_age_days=14,
    )
    raw = RawJob(
        source_company="acme",
        external_job_id="1",
        title="Data Analyst",
        location_raw="Austin, TX",
        description_raw="SQL analytics",
        posted_at=datetime(2026, 6, 20, tzinfo=UTC),
        url="https://example.com/jobs/1",
    )
    result = MatchResult(profile="custom", score=0.9, eligible=True, tier="strong")

    assert _qualifies_for_immediate_notification(
        ParsedJob(raw=raw),
        result,
        datetime(2026, 6, 26, tzinfo=UTC),
        settings,
        is_new=False,
        backfill=True,
    )


@pytest.mark.parametrize(
    ("result", "posted_at", "backfill"),
    [
        (
            MatchResult(profile="custom", score=0.81, eligible=True, tier="strong"),
            datetime(2026, 6, 20, tzinfo=UTC),
            True,
        ),
        (
            MatchResult(profile="custom", score=0.9, eligible=True, tier="match"),
            datetime(2026, 6, 20, tzinfo=UTC),
            True,
        ),
        (
            MatchResult(
                profile="custom",
                score=0.95,
                eligible=True,
                tier="strong",
                bucket="stretch",
            ),
            datetime(2026, 6, 20, tzinfo=UTC),
            True,
        ),
        (
            MatchResult(profile="custom", score=0.9, eligible=True, tier="strong"),
            datetime(2025, 1, 1, tzinfo=UTC),
            True,
        ),
        (
            MatchResult(profile="custom", score=0.9, eligible=True, tier="strong"),
            datetime(2026, 6, 20, tzinfo=UTC),
            False,
        ),
    ],
)
def test_backfill_gate_keeps_score_tier_age_and_newness_gates(
    result,
    posted_at,
    backfill,
):
    settings = Settings(
        immediate_notification_min_score=0.82,
        immediate_notification_max_source_age_days=14,
    )
    raw = RawJob(
        source_company="acme",
        external_job_id="1",
        title="Data Analyst",
        location_raw="Austin, TX",
        description_raw="SQL analytics",
        posted_at=posted_at,
        url="https://example.com/jobs/1",
    )

    assert not _qualifies_for_immediate_notification(
        ParsedJob(raw=raw),
        result,
        datetime(2026, 6, 26, tzinfo=UTC),
        settings,
        is_new=False,
        backfill=backfill,
    )


@pytest.mark.asyncio
async def test_telegram_http_error_does_not_expose_bot_token():
    token = "123456:super-secret-token"
    requests = 0

    def telegram_error(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(401, request=request, json={"ok": False})

    transport = httpx.MockTransport(telegram_error)
    async with httpx.AsyncClient(transport=transport) as client:
        notifier = TelegramNotifier(token, "chat-id", client)

        with pytest.raises(RuntimeError) as error:
            await notifier.send("hello")

    assert requests == 3
    assert token not in str(error.value)


@pytest.mark.parametrize(
    ("age", "label"),
    [
        (1, "\u65b0\u767c\u5e03"),
        (5, "\u8fd1\u671f"),
        (12, "\u4e00\u822c"),
        (24, "\u8f03\u65e9"),
        (75, "\u8f03\u820a"),
    ],
)
def test_freshness_labels_show_source_age_and_discovery_status(age, label):
    first_seen = datetime(2026, 6, 26, tzinfo=UTC)
    posted = first_seen - timedelta(days=age)
    rendered = render_freshness(posted, first_seen, is_new=True)
    assert f"\u4f86\u6e90 {age} \u5929\u524d\uff5c{label}" in rendered
    assert ("\uff08\u672c\u6b21\u65b0\u767c\u73fe\uff09" in rendered) is (age > 3)


def test_freshness_handles_unknown_date_and_content_change():
    first_seen = datetime(2026, 6, 26, tzinfo=UTC)
    assert (
        "\u4f86\u6e90\u65e5\u671f\u672a\u77e5\uff5c\u672c\u6b21\u65b0\u767c\u73fe"
        in render_freshness(None, first_seen, is_new=True)
    )
    posted = first_seen - timedelta(days=5)
    assert (
        "\u4f86\u6e90 5 \u5929\u524d\uff5c\u8fd1\u671f\uff5c\u5167\u5bb9\u66f4\u65b0"
        in render_freshness(posted, first_seen, changed=True)
    )


def test_summary_removes_generic_new_marker_and_shows_update_status():
    raw = RawJob(
        source_company="acme",
        external_job_id="old-1",
        title="Data Analyst",
        location_raw="Austin, TX",
        description_raw="SQL analytics",
        posted_at=datetime(2026, 6, 1, tzinfo=UTC),
        url="https://example.com/jobs/old-1",
    )
    item = MatchedJob(
        company_name="Acme",
        job=ParsedJob(raw=raw),
        result=MatchResult(profile=ProfileName.TECH, score=0.82, eligible=True, tier="strong"),
        first_seen_at=datetime(2026, 6, 26, tzinfo=UTC),
        is_new=True,
        changed=False,
    )
    summary = render_run_summary(
        run_key="daily-2026-06-26", stats={}, errors=[], matched_jobs=[item], zero_job_sources=[]
    )
    assert "NEW " not in summary
    assert (
        "\u4f86\u6e90 25 \u5929\u524d\uff5c\u8f03\u65e9\uff08\u672c\u6b21\u65b0\u767c\u73fe\uff09"
        in summary
    )
