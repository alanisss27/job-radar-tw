import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from bs4 import BeautifulSoup

from job_monitor import pipeline
from job_monitor.config import ProfileConfig, SearchPreferences, Settings, load_companies
from job_monitor.models import AtsType
from job_monitor.sources import JobAdderWidgetSource, SOURCE_CLASSES, SourceError, SourceRunner


COMPANY = next(
    c for c in load_companies(Path("config/companies.yml")) if c.slug == "avance-clinical"
)
EMPTY = '<div class="ja-job-list-container"><p class="no-jobs-content">No jobs</p></div>'


def jsonp(fragment):
    return "radar(" + json.dumps(fragment) + ");"


def card(job_id, title="Clinical Trial Assistant"):
    return f'''<div class="job"><h2 class="title"><a data-job-id="{job_id}">{title}</a></h2>
    <ul class="classifications"><li data-id="23519"> Clinical Operations </li>
    <li data-id="23521"> Sydney </li><li data-id="23521"> Sydney </li>
    <li data-id="23522">Permanent / Full Time</li></ul>
    <p class="date-posted">2026-09-30</p><p class="summary">Support &amp; coordinate trials.
    Remote options across Australia.</p><a data-job-id="{job_id}">More</a></div>'''


def page(number=1, total=2, ids=None):
    ids = range((number - 1) * 6 + 1, number * 6 + 1) if ids is None else ids
    return (
        '<div class="ja-job-list-container"><div class="ja-job-list">'
        + "".join(card(i) for i in ids)
        + "</div>"
        + f'<div class="ja-pager-summary">Page {number} of {total}</div></div>'
    )


def transport(pages=None, detail=None, requests=None):
    pages = {1: page(), 2: page(2), 3: EMPTY} if pages is None else pages

    def handle(request):
        if requests is not None:
            requests.append(request)
        assert request.url.params["key"] == COMPANY.ats_config["key"]
        assert request.url.params["callback"] == "radar"
        if request.url.path.endswith("RenderJobDetails"):
            assert detail is not None, "unexpected broad inventory detail request"
            return httpx.Response(200, text=jsonp(detail))
        assert request.url.params["jobsPerPage"] == "6"
        assert request.url.params["showHotJobsOnly"] == "false"
        assert "keywords" not in request.url.params
        assert "classificationIDs" not in request.url.params
        return httpx.Response(200, text=jsonp(pages[int(request.url.params["pageNumber"])]))

    return httpx.MockTransport(handle)


def test_jsonp_decodes_escaped_html():
    assert JobAdderWidgetSource._decode(" \n" + jsonp('<p>"雪" &amp; text</p>') + "\n") == (
        '<p>"雪" &amp; text</p>'
    )


@pytest.mark.parametrize(
    "body",
    [
        "",
        'radar("");',
        'other("html");',
        "radar(null);",
        "radar({});",
        "radar([]);",
        'radar("html");alert(1)',
        "radar(run());",
        'radar("unterminated);',
    ],
)
def test_jsonp_rejects_malformed_or_executable_payload(body):
    with pytest.raises(SourceError):
        JobAdderWidgetSource._decode(body)


@pytest.mark.asyncio
async def test_complete_inventory_normalization_and_stable_ids():
    requests = []
    async with httpx.AsyncClient(transport=transport(requests=requests)) as client:
        jobs = await SourceRunner(client).fetch(COMPANY)
    assert [j.stable_external_id for j in jobs] == [str(i) for i in range(1, 13)]
    assert len({j.title for j in jobs}) == 1  # Same title must not collapse distinct ads.
    assert len(requests) == 3
    raw = jobs[0]
    assert raw.location_raw == "Sydney"  # No inferred Australia or remote location.
    assert raw.description_raw == "Support & coordinate trials. Remote options across Australia."
    assert raw.posted_at == datetime(2026, 9, 30, tzinfo=UTC)
    assert str(raw.url) == "https://www.avancecro.com/careers/?ja-job=1"
    assert raw.canonical_url == "https://www.avancecro.com/careers?ja-job=1"
    assert raw.metadata["jobadder_widget"]["classifications"]["23522"] == ["Permanent / Full Time"]
    assert "eligibility" not in raw.metadata
    assert raw.content_hash == raw.metadata["jobadder_widget"]["listing_hash"]
    changed = JobAdderWidgetSource(COMPANY, client)._raw(
        BeautifulSoup(card(1).replace("Support", "Manage"), "html.parser")
    )
    assert changed.content_hash != raw.content_hash


@pytest.mark.parametrize(
    "pages",
    [
        {1: page(), 2: page()},  # Server repeats page number.
        {1: page(), 2: page(2, ids=range(1, 7))},  # Repeated IDs despite correct number.
        {1: page(), 2: page(2, total=3)},
        {1: page(), 2: EMPTY},
        {1: page(ids=[1, 2])},  # Truncated nonterminal page.
        {1: page(total=1), 2: page(2)},  # Inventory grew after advertised last page.
        {1: page(total=1, ids=[1, 1])},
        {1: "<html>Service unavailable</html>"},
        {1: '<div class="ja-job-list-container"></div>'},
        {1: page().replace("Page 1 of 2", "invalid")},
        {1: page(total=1).replace('data-job-id="1"', 'data-job-id="bad"')},
        {1: page(total=1).replace("2026-09-30", "invalid date")},
        {1: page(total=1).replace('class="summary"', 'class="missing"')},
        {1: page(total=1).replace('data-id="23521"', "")},
    ],
)
@pytest.mark.asyncio
async def test_incomplete_or_malformed_inventory_fails(pages):
    async with httpx.AsyncClient(transport=transport(pages)) as client:
        with pytest.raises(SourceError):
            await JobAdderWidgetSource(COMPANY, client).fetch()


@pytest.mark.asyncio
async def test_explicit_empty_inventory():
    async with httpx.AsyncClient(transport=transport({1: EMPTY})) as client:
        assert await JobAdderWidgetSource(COMPANY, client).fetch() == []


DETAIL = """<div class="ja-job-details"><h2 class="title">Clinical Trial Assistant</h2>
<div class="description"><p>Full clinical trial responsibilities.</p>
<p>Bachelor's degree required.</p></div></div>"""


@pytest.mark.asyncio
async def test_hydration_is_explicit_and_preserves_inventory_hash():
    requests = []
    async with httpx.AsyncClient(transport=transport(detail=DETAIL, requests=requests)) as client:
        runner = SourceRunner(client)
        raw = (await runner.fetch(COMPANY))[0]
        assert all("RenderJobList" in str(r.url) for r in requests)
        hydrated = await runner.hydrate_candidate(COMPANY, raw)
        assert requests[-1].url.params["jobID"] == raw.stable_external_id
        assert hydrated.description_raw == (
            "Full clinical trial responsibilities. Bachelor's degree required."
        )
        assert hydrated.content_hash == raw.content_hash
        assert await runner.hydrate_candidate(COMPANY, hydrated) is hydrated
        assert len(requests) == 4


@pytest.mark.parametrize(
    "detail",
    [
        "<div>Unavailable</div>",
        DETAIL.replace("Clinical Trial Assistant", "Different posting"),
        DETAIL.replace('class="description"', 'class="missing"'),
    ],
)
@pytest.mark.asyncio
async def test_malformed_detail_fails_visibly(detail):
    async with httpx.AsyncClient(transport=transport(detail=detail)) as client:
        source = JobAdderWidgetSource(COMPANY, client)
        raw = (await source.fetch())[0]
        with pytest.raises(SourceError):
            await source.hydrate(raw)


@pytest.mark.asyncio
async def test_pipeline_hydrates_only_jobs_passing_existing_gate(monkeypatch):
    requests = []
    client = httpx.AsyncClient(
        transport=transport(
            {
                1: page(total=1, ids=[1, 2]).replace(
                    'data-job-id="2">Clinical Trial Assistant', 'data-job-id="2">Finance Manager'
                ),
                2: EMPTY,
            },
            detail=DETAIL,
            requests=requests,
        )
    )
    monkeypatch.setattr(pipeline.httpx, "AsyncClient", lambda **kwargs: client)
    profile = ProfileConfig(
        name="clinical-discovery",
        threshold=0.4,
        strong_threshold=0.9,
        weights={"title": 1.0},
        title_terms=["clinical trial assistant"],
        allow_other_job_family=True,
        domain_terms=[],
        skills=[],
    )
    report = await pipeline.run_pipeline(
        Settings(telegram_bot_token=None, telegram_chat_id=None),
        [COMPANY],
        {profile.name: profile},
        SearchPreferences(location_terms=["Sydney"], excluded_seniorities=set()),
        dry_run=True,
    )
    assert not report.errors
    details = [r for r in requests if r.url.path.endswith("RenderJobDetails")]
    assert [r.url.params["jobID"] for r in details] == ["1"]


def test_registry():
    assert COMPANY.enabled and COMPANY.source_verified
    assert COMPANY.ats_type is AtsType.JOBADDER_WIDGET
    assert SOURCE_CLASSES[COMPANY.ats_type] is JobAdderWidgetSource
