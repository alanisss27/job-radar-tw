"""Alexion's published TalentBrew AJAX contract and complete-inventory boundary."""

import json
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import select, update

from job_monitor import pipeline
from job_monitor.config import SearchPreferences, Settings, load_companies
from job_monitor.models import RawJob
from job_monitor.sources import SourceError, TalentBrewSource
from job_monitor.storage import Storage, jobs

COMPANY = next(
    c
    for c in load_companies(Path("config/companies.yml"))
    if c.slug == "alexion-astrazeneca-rare-disease"
)
POST = "https://careers.astrazeneca.com/search-jobs/resultspost"


def module(total, page=1):
    attrs = {
        "id": "search-results",
        "data-total-results": total,
        "data-total-job-results": total,
        "data-total-pages": (total + 14) // 15,
        "data-current-page": page,
        "data-records-per-page": 15,
        "data-organization-ids": "7684",
        "data-distance": 50,
        "data-sort-criteria": 0,
        "data-sort-direction": 0,
        "data-search-type": 5,
        "data-results-type": 0,
        "data-search-results-module-name": "Search Results",
        "data-ajax-post-url": "/search-jobs/resultspost",
    }
    return "<section " + " ".join(f'{k}="{v}"' for k, v in attrs.items()) + ">"


def filters(total, checked=True):
    return (
        '<section id="search-filters" data-radius-unit-type="0" '
        'data-search-filters-module-name="Search Filters">'
        '<input data-field-name="custom_fields.jobPostingSite" data-id="Alexion" '
        f'data-count="{total}" data-display="Alexion" data-facet-type="5" '
        + ('checked="checked"' if checked else "")
        + "></section>"
    )


def landing(total):
    return module(total) + "</section>" + filters(total, False)


def card(posting_id):
    return (
        '<li><a class="left-link" '
        f'data-job-id="{posting_id}" href="/job/boston/coordinator/43991/{posting_id}">'
        '<div class="brand-logo-wrapper alexion-brand"></div>'
        '<h2>Clinical Project Coordinator</h2><span class="job-location">Boston, MA</span>'
        "</a></li>"
    )


def envelope(total, ids, page=1):
    return {
        "results": module(total, page)
        + '<section id="search-results-list"><ul>'
        + "".join(card(i) for i in ids)
        + "</ul></section></section>",
        "filters": filters(total),
        "hasJobs": total > 0,
        "hasContent": False,
    }


@pytest.mark.asyncio
@respx.mock
async def test_verified_brand_inventory_paginates_before_any_detail_fetch():
    respx.get(str(COMPANY.careers_url)).respond(200, text=landing(16))
    route = respx.post(POST).mock(
        side_effect=[
            httpx.Response(200, json=envelope(16, range(100000, 100015))),
            httpx.Response(200, json=envelope(16, [100015], 2)),
        ]
    )
    async with httpx.AsyncClient() as client:
        rows = await TalentBrewSource(COMPANY, client)._alexion_inventory()
    assert len(rows) == 16
    assert list(rows)[-1] == "100015"
    assert rows["100000"]["title"] == "Clinical Project Coordinator"
    assert rows["100000"]["location"] == "Boston, MA"
    assert len(respx.calls) == 3
    bodies = [json.loads(c.request.content) for c in route.calls]
    assert [b["CurrentPage"] for b in bodies] == [1, 2]
    assert all(b["OrganizationIds"] == "7684" and b["RecordsPerPage"] == 15 for b in bodies)
    assert bodies[0]["FacetFilters"] == [
        {
            "ID": "Alexion",
            "FacetType": 5,
            "Count": 16,
            "Display": "Alexion",
            "IsApplied": True,
            "FieldName": "custom_fields.jobPostingSite",
        }
    ]


@pytest.mark.asyncio
@respx.mock
async def test_fetch_retains_detail_requisition_identity_and_unknown_fallback():
    respx.get(str(COMPANY.careers_url)).respond(200, text=landing(2))
    respx.post(POST).respond(200, json=envelope(2, [100001, 100002]))
    respx.get("https://careers.astrazeneca.com/job/boston/coordinator/43991/100001").respond(
        200,
        text="<main><h1>Clinical Project Coordinator</h1>Requisition ID: R-300001 "
        '<span class="job-location">Boston, MA</span><a href="/apply">Apply Now</a></main>',
    )
    respx.get("https://careers.astrazeneca.com/job/boston/coordinator/43991/100002").respond(410)
    async with httpx.AsyncClient() as client:
        jobs = await TalentBrewSource(COMPANY, client).fetch()
    assert [j.external_job_id for j in jobs] == ["R-300001", "100002"]
    assert jobs[1].metadata["active_status"] == "unknown"


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize(
    "fault",
    [
        "old-empty-envelope",
        "not-json",
        "not-object",
        "missing-filters",
        "wrong-page",
        "changed-total",
        "wrong-size",
        "wrong-org",
        "wrong-pages",
        "unselected-brand",
        "foreign-brand",
        "no-logo",
        "no-identity",
        "foreign-url",
        "short-page",
        "extra-country-filter",
        "missing-list",
        "missing-total",
        "false-hasJobs",
        "unexpected-region",
    ],
)
async def test_unverified_response_never_returns_partial_inventory(fault):
    respx.get(str(COMPANY.careers_url)).respond(200, text=landing(1))
    payload = envelope(1, [100001])
    replacements = {
        "wrong-page": ('data-current-page="1"', 'data-current-page="2"'),
        "changed-total": ('data-total-results="1"', 'data-total-results="2"'),
        "wrong-size": ('data-records-per-page="15"', 'data-records-per-page="1"'),
        "wrong-org": ('data-organization-ids="7684"', 'data-organization-ids="other"'),
        "wrong-pages": ('data-total-pages="1"', 'data-total-pages="2"'),
        "foreign-brand": ("alexion-brand", "az-brand"),
        "no-logo": ("brand-logo-wrapper", "other"),
        "no-identity": ('data-job-id="100001"', ""),
        "foreign-url": ('href="/job/', 'href="https://other.test/job/'),
        "short-page": (card(100001), ""),
        "missing-list": ('id="search-results-list"', 'id="other"'),
        "missing-total": ('data-total-results="1"', ""),
        "unexpected-region": ('id="search-results"', 'id="search-results" data-location="FL"'),
    }
    if fault in replacements:
        payload["results"] = payload["results"].replace(*replacements[fault])
    elif fault == "old-empty-envelope":
        payload.update(results="", filters="")
    elif fault == "missing-filters":
        del payload["filters"]
    elif fault == "unselected-brand":
        payload["filters"] = filters(1, False)
    elif fault == "extra-country-filter":
        payload["filters"] += '<input data-id="US" checked>'
    elif fault == "false-hasJobs":
        payload["hasJobs"] = False
    elif fault == "not-object":
        payload = []
    route = respx.post(POST)
    if fault == "not-json":
        route.respond(200, text="not-json")
    else:
        route.respond(200, json=payload)
    async with httpx.AsyncClient() as client:
        with pytest.raises(SourceError, match="incomplete inventory"):
            await TalentBrewSource(COMPANY, client).fetch()
    assert len(respx.calls) == 2  # no detail requests before proven coverage


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize("changed_total", [False, True])
async def test_later_page_failure_discards_first_page(changed_total):
    respx.get(str(COMPANY.careers_url)).respond(200, text=landing(16))
    second = envelope(15 if changed_total else 16, [100000], 2)
    respx.post(POST).mock(
        side_effect=[
            httpx.Response(200, json=envelope(16, range(100000, 100015))),
            httpx.Response(200, json=second),
        ]
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(SourceError, match="incomplete inventory"):
            await TalentBrewSource(COMPANY, client).fetch()
    assert len(respx.calls) == 3


@pytest.mark.asyncio
@respx.mock
async def test_zero_inventory_requires_explicit_scoped_zero_metadata():
    respx.get(str(COMPANY.careers_url)).respond(200, text=landing(0))
    respx.post(POST).respond(200, json=envelope(0, []))
    async with httpx.AsyncClient() as client:
        assert await TalentBrewSource(COMPANY, client).fetch() == []


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize("fault", ["budget", "foreign-endpoint", "keyword", "missing-brand"])
async def test_bootstrap_refuses_unverified_or_unbounded_search(fault):
    html = landing(1501 if fault == "budget" else 1)
    if fault == "foreign-endpoint":
        html = html.replace(
            "/search-jobs/resultspost", "https://other.test/search-jobs/resultspost"
        )
    elif fault == "keyword":
        html = html.replace('id="search-results"', 'id="search-results" data-keywords="narrow"')
    elif fault == "missing-brand":
        html = html.replace('data-id="Alexion"', 'data-id="Other"')
    respx.get(str(COMPANY.careers_url)).respond(200, text=html)
    async with httpx.AsyncClient() as client:
        with pytest.raises(SourceError, match="incomplete inventory"):
            await TalentBrewSource(COMPANY, client).fetch()
    assert len(respx.calls) == 1


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize("partial", [False, True])
async def test_failed_alexion_inventory_never_ages_existing_jobs_toward_closure(
    monkeypatch, partial
):
    db = Storage("sqlite:///:memory:", create_schema=True)
    company_id = db.sync_company(COMPANY)
    seed = db.start_run("seed")
    raw = RawJob(
        source_company=COMPANY.slug,
        external_job_id="existing-job",
        title="Clinical Coordinator",
        location_raw="United States",
        url="https://example.test/job/existing-job",
    )
    db.upsert_job(company_id, seed, raw)
    db.finish_run(seed, {}, [])
    with db.engine.begin() as conn:
        conn.execute(update(jobs).values(missing_count=1))
    monkeypatch.setattr(pipeline, "Storage", lambda *a, **kw: db)
    respx.get(str(COMPANY.careers_url)).respond(
        200, text=landing(16) if partial else "<main></main>"
    )
    if partial:

        def respond(request):
            page = json.loads(request.content)["CurrentPage"]
            payload = (
                envelope(16, range(100000, 100015)) if page == 1 else envelope(15, [100015], 2)
            )
            return httpx.Response(200, json=payload)

        respx.post(POST).mock(side_effect=respond)
    settings = Settings(
        _env_file=None,
        database_url="sqlite:///:memory:",
        telegram_bot_token=None,
        telegram_chat_id=None,
        llm_enabled=False,
    )
    for attempt in range(3):
        report = await pipeline.run_pipeline(
            settings, [COMPANY], {}, SearchPreferences(), run_key=f"failed-alexion-{attempt}"
        )
        assert report.sources_attempted == 1 and report.sources_succeeded == 0
        assert report.scan_outcome == "scan_failed"
        assert report.jobs_fetched == report.jobs_closed == 0
        assert "incomplete inventory" in report.errors[0]["error"]
        with db.engine.connect() as conn:
            row = conn.execute(select(jobs.c.status, jobs.c.missing_count)).one()
            assert row.status == "active" and row.missing_count == 1
    assert len(respx.calls) == (9 if partial else 3)
