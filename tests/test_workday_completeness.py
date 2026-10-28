"""Strict coverage contract for Thermo Fisher's bounded Workday queries."""

import json
from pathlib import Path

import httpx
import pytest
import respx

from job_monitor.config import load_companies
from job_monitor.sources import SourceError, WorkdayRequestController, WorkdaySource


@pytest.fixture
def thermo():
    company = next(
        c for c in load_companies(Path("config/companies.yml")) if c.slug == "thermo-fisher-ppd"
    )
    assert company.ats_config["limit"] == 20
    assert company.ats_config["strict_pagination"] is True
    assert company.ats_config["location_facet_batch_size"] == 100
    # Listing coverage is independent of enrichment (covered in test_sources).
    company.ats_config.pop("detail_api_base")
    company.ats_config.pop("facet_patterns")
    company.ats_config.pop("location_facet_batch_size")
    return company


def postings(start, count):
    return [
        {"title": f"Job {i}", "externalPath": f"/job/US/R-{i}"} for i in range(start, start + count)
    ]


async def fetch(company):
    async with httpx.AsyncClient() as client:
        return await WorkdaySource(
            company, client, WorkdayRequestController(min_interval_seconds=0)
        ).fetch()


@respx.mock
async def test_twenty_record_pages_retain_initial_total_and_last_partial_page(thermo):
    requests = []

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        offset = body["offset"]
        return httpx.Response(
            200,
            json={
                "total": 45 if offset == 0 else 0,
                "jobPostings": postings(offset, min(20, 45 - offset)),
            },
        )

    respx.post(thermo.ats_config["endpoint"]).mock(side_effect=respond)
    assert len(await fetch(thermo)) == 45
    assert [r["offset"] for r in requests] == [0, 20, 40]
    assert all(r["limit"] == 20 for r in requests)


@pytest.mark.parametrize(
    "second,reason",
    [
        ({"total": 0, "jobPostings": []}, "short or empty"),
        ({"total": 0, "jobPostings": postings(20, 1)}, "short or empty"),
        ({"total": 0, "jobPostings": postings(0, 20)}, "duplicate or stalled"),
        ({"total": 0, "jobPostings": postings(19, 20)}, "duplicate or stalled"),
        ({"total": 41, "jobPostings": postings(20, 20)}, "total changed"),
        ({"total": 0, "jobPostings": [{"title": "Missing identity"}]}, "usable identity"),
        (400, "400 Bad Request"),
    ],
)
@respx.mock
async def test_incomplete_queries_fail_explicitly_without_returning_partial_jobs(
    thermo, second, reason
):
    route = respx.post(thermo.ats_config["endpoint"])
    route.side_effect = [
        httpx.Response(200, json={"total": 40, "jobPostings": postings(0, 20)}),
        httpx.Response(second) if isinstance(second, int) else httpx.Response(200, json=second),
    ]
    with pytest.raises(SourceError, match=f"incomplete coverage.*{reason}") as exc:
        await fetch(thermo)
    assert "offset=20" in str(exc.value)
    assert "reported_total=40" in str(exc.value)
    assert route.call_count == 2


@pytest.mark.parametrize(
    "payload",
    [
        {"jobPostings": []},
        {"total": -1, "jobPostings": []},
        {"total": True, "jobPostings": []},
        {"total": 1, "jobPostings": postings(0, 2)},
        {"total": 2, "jobPostings": postings(0, 1) * 2},
    ],
)
@respx.mock
async def test_invalid_initial_counts_and_duplicate_rows_are_incomplete(thermo, payload):
    respx.post(thermo.ats_config["endpoint"]).respond(200, json=payload)
    with pytest.raises(SourceError, match="incomplete coverage"):
        await fetch(thermo)


@respx.mock
async def test_empty_board_is_complete(thermo):
    respx.post(thermo.ats_config["endpoint"]).respond(200, json={"total": 0, "jobPostings": []})
    assert await fetch(thermo) == []


@pytest.mark.parametrize("payload", [{"total": 1}, [], None])
@respx.mock
async def test_invalid_response_shape_reports_incomplete_coverage(thermo, payload):
    respx.post(thermo.ats_config["endpoint"]).respond(200, json=payload)
    with pytest.raises(SourceError, match="incomplete coverage"):
        await fetch(thermo)


@respx.mock
async def test_invalid_json_reports_incomplete_coverage(thermo):
    respx.post(thermo.ats_config["endpoint"]).respond(200, text="not json")
    with pytest.raises(SourceError, match="incomplete coverage.*not JSON"):
        await fetch(thermo)


async def test_strict_source_refuses_oversized_page_limit(thermo):
    thermo.ats_config["limit"] = 100
    with pytest.raises(SourceError, match="limit between 1 and 20"):
        await fetch(thermo)


@respx.mock
async def test_incomplete_source_is_reported_as_failed_scan(thermo):
    from job_monitor.config import SearchPreferences, Settings
    from job_monitor.pipeline import run_pipeline

    respx.post(thermo.ats_config["endpoint"]).respond(200, json={"total": 3001, "jobPostings": []})
    report = await run_pipeline(
        Settings(
            _env_file=None,
            database_url=None,
            telegram_bot_token=None,
            telegram_chat_id=None,
            llm_enabled=False,
        ),
        [thermo],
        {},
        SearchPreferences(),
        dry_run=True,
        run_key="incomplete-workday-test",
    )
    assert report.sources_attempted == 1
    assert report.sources_succeeded == 0
    assert report.scan_outcome == "scan_failed"
    assert report.jobs_fetched == 0
    assert "incomplete coverage" in report.errors[0]["error"]
    assert report.notifications == 0


@pytest.mark.parametrize("truncated", [False, True])
@respx.mock
async def test_result_window_is_measured_and_never_silently_truncated(thermo, truncated):
    offsets = []

    def respond(request):
        offset = json.loads(request.content)["offset"]
        offsets.append(offset)
        count = 0 if truncated and offset == 2000 else min(20, 2021 - offset)
        return httpx.Response(
            200, json={"total": 2021 if offset == 0 else 0, "jobPostings": postings(offset, count)}
        )

    respx.post(thermo.ats_config["endpoint"]).mock(side_effect=respond)
    if truncated:
        with pytest.raises(SourceError, match="incomplete coverage.*short or empty"):
            await fetch(thermo)
        assert offsets[-1] == 2000
    else:
        assert len(await fetch(thermo)) == 2021
        assert offsets[-1] == 2020


@respx.mock
async def test_location_batches_preserve_scope_and_union_overlapping_jobs(thermo):
    thermo.ats_config.update(
        {
            "facet_patterns": {"locations": ", USA$"},
            "location_facet_batch_size": 100,
        }
    )
    batches = []

    def respond(request):
        body = json.loads(request.content)
        assert body["searchText"] == ""
        if not body["appliedFacets"]:
            return httpx.Response(
                200,
                json={
                    "facets": [
                        {
                            "facetParameter": "locations",
                            "values": [
                                {"id": str(i), "descriptor": f"City {i}, USA"} for i in range(201)
                            ]
                            + [{"id": "foreign", "descriptor": "City, Canada"}],
                        }
                    ]
                },
            )
        ids = body["appliedFacets"]["locations"]
        batches.append(ids)
        assert body["limit"] == 20 and body["offset"] == 0
        return httpx.Response(
            200, json={"total": 2, "jobPostings": postings(0, 1) + postings(int(ids[0]) + 1, 1)}
        )

    respx.post(thermo.ats_config["endpoint"]).mock(side_effect=respond)
    assert len(await fetch(thermo)) == 4
    assert [len(b) for b in batches] == [100, 100, 1]
    assert [i for b in batches for i in b] == [str(i) for i in range(201)]
