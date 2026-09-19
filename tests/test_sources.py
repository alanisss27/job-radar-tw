import json
from copy import deepcopy
from pathlib import Path

import httpx
import pytest
import respx

from job_monitor.config import load_companies
from job_monitor.models import CompanyConfig
from job_monitor.sources import (
    AshbySource,
    GreenhouseSource,
    LeverSource,
    SmartRecruitersSource,
    SourceError,
    WorkdaySource,
    TalemetrySource,
    JibeSource,
)


def company(ats_type, ats_config):
    return CompanyConfig(
        slug="acme",
        name="Acme",
        careers_url="https://example.com/jobs",
        ats_type=ats_type,
        ats_config=ats_config,
        industry="tech",
        profiles=["tech"],
        source_verified=True,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "token",
    [
        "spyretherapeutics",
        "apogeetherapeutics",
        "seaporttherapeutics",
        "peptilogics",
        "kailera",
        "alkeus",
        "faeththerapeutics",
        "iovancebiotherapeutics",
        "relaytherapeutics",
        "legendcareers",
        "citytherapeutics",
        "dianthustherapeutics",
        "nurix",
        "plianttherapeuticsinc",
        "pfm",
        "iterativehealth",
        "mazetherapeutics",
        "oruka",
        "anteristech",
        "immunomeinc",
        "mineralystherapeutics",
        "kuraoncology",
        "tangotherapeutics",
        "arcellx",
        "KymeraTherapeutics",
        "kincellbio",
        "vaxcyte",
        "alumis",
        "revolutionmedicines",
        "lakefrontbiotherapeuticsinc",
        "eikontherapeutics",
        "clinchoice",
        "dispatchbio",
        "genscript",
        "heartflowinc",
        "synerg",
        "natera",
    ],
)
@respx.mock
async def test_greenhouse_adapter(token):
    respx.get(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true").mock(
        return_value=httpx.Response(
            200,
            json={
                "jobs": [
                    {
                        "id": 7,
                        "title": "Senior Data Analyst",
                        "location": {"name": "Remote US"},
                        "content": "<p>SQL</p>",
                        "absolute_url": f"https://boards.greenhouse.io/{token}/jobs/7",
                        "updated_at": "2026-06-17T12:00:00Z",
                    }
                ]
            },
        )
    )
    async with httpx.AsyncClient() as client:
        rows = await GreenhouseSource(company("greenhouse", {"board_token": token}), client).fetch()
    assert rows[0].external_job_id == "7"
    assert rows[0].description_raw == "SQL"


@pytest.mark.asyncio
@respx.mock
async def test_lever_adapter():
    respx.get("https://api.lever.co/v0/postings/acme?mode=json").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": "x",
                    "text": "Data Analyst",
                    "categories": {"location": "Phoenix, AZ"},
                    "descriptionPlain": "SQL",
                    "hostedUrl": "https://jobs.lever.co/acme/x",
                    "createdAt": 1700000000000,
                }
            ],
        )
    )
    async with httpx.AsyncClient() as client:
        rows = await LeverSource(company("lever", {"site": "acme"}), client).fetch()
    assert len(rows) == 1


@pytest.mark.asyncio
@respx.mock
async def test_lightship_lever_adapter():
    respx.get("https://api.lever.co/v0/postings/lightship?mode=json").mock(
        return_value=httpx.Response(200, json=[])
    )
    async with httpx.AsyncClient() as client:
        rows = await LeverSource(company("lever", {"site": "lightship"}), client).fetch()
    assert rows == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "site",
    [
        "januxrx",
        "alimentiv-2",
        "scholarrock",
        "cullinanoncology",
        "endpointclinical",
        "protrials",
        "wepclinical",
        "artbio",
        "abzena",
    ],
)
@respx.mock
async def test_fourth_expansion_lever_adapters(site):
    respx.get(f"https://api.lever.co/v0/postings/{site}?mode=json").mock(
        return_value=httpx.Response(200, json=[])
    )
    async with httpx.AsyncClient() as client:
        rows = await LeverSource(company("lever", {"site": site}), client).fetch()
    assert rows == []


@pytest.mark.asyncio
@respx.mock
async def test_ashby_adapter():
    respx.get("https://api.ashbyhq.com/posting-api/job-board/acme").mock(
        return_value=httpx.Response(
            200,
            json={
                "jobs": [
                    {
                        "id": "a",
                        "title": "Analytics Engineer",
                        "location": "Remote US",
                        "descriptionHtml": "<p>dbt</p>",
                        "jobUrl": "https://jobs.ashbyhq.com/acme/a",
                    }
                ]
            },
        )
    )
    async with httpx.AsyncClient() as client:
        rows = await AshbySource(company("ashby", {"board_name": "acme"}), client).fetch()
    assert rows[0].description_raw == "dbt"


@pytest.mark.asyncio
@respx.mock
async def test_smartrecruiters_pagination():
    route = respx.get("https://api.smartrecruiters.com/v1/companies/acme/postings")
    route.side_effect = [
        httpx.Response(
            200,
            json={
                "totalFound": 1,
                "content": [
                    {
                        "id": "s",
                        "name": "BI Analyst",
                        "location": {"city": "Dallas", "region": "TX"},
                        "ref": "https://api.smartrecruiters.com/v1/companies/acme/postings/s",
                    }
                ],
            },
        ),
        httpx.Response(200, json={"totalFound": 1, "content": []}),
    ]
    respx.get("https://api.smartrecruiters.com/v1/companies/acme/postings/s").mock(
        return_value=httpx.Response(
            200, json={"jobAd": {"sections": {"jobDescription": {"text": "<p>SQL</p>"}}}}
        )
    )
    async with httpx.AsyncClient() as client:
        rows = await SmartRecruitersSource(
            company("smartrecruiters", {"company_identifier": "acme"}), client
        ).fetch()
    assert len(rows) == 1
    assert "Dallas" in rows[0].location_raw
    assert rows[0].description_raw == "SQL"
    assert str(rows[0].url) == "https://jobs.smartrecruiters.com/acme/s"


@pytest.mark.asyncio
@respx.mock
async def test_talemetry_filters_us_and_reads_details():
    cfg = company("talemetry", {"endpoint": "https://example.com/jobs.json", "detail_base_url": "https://example.com/jobs"})
    respx.get(cfg.ats_config["endpoint"], params={"page": "1"}).respond(200, json={
        "per_page": 25,
        "entries": [
            {"id": "1", "title": "Clinical Data Manager", "location": {"locality": "Boston", "region_abbr": "MA", "country": "United States"}},
            {"id": "2", "title": "Clinical Data Manager", "location": {"locality": "Toronto", "country": "Canada"}},
        ],
    })
    respx.get("https://example.com/jobs/1.json").respond(200, text='<link rel="canonical" href="/careers/1"><div class="job-details__content-description"><p>Clinical trials</p></div>')
    async with httpx.AsyncClient() as client:
        rows = await TalemetrySource(cfg, client).fetch()
    assert len(rows) == 1
    assert rows[0].location_raw == "Boston, MA, United States"
    assert rows[0].description_raw == "Clinical trials"
    assert str(rows[0].url) == "https://example.com/careers/1"


@pytest.mark.asyncio
@respx.mock
async def test_jibe_filters_us_and_paginates():
    cfg = company("jibe", {"endpoint": "https://example.com/api/jobs", "limit": 2})
    endpoint = cfg.ats_config["endpoint"]
    respx.get(endpoint, params={"page": "1", "limit": "2"}).respond(200, json={
        "totalCount": 3,
        "jobs": [
            {"data": {"req_id": "1", "title": "CRA", "country_code": "US", "full_location": "Cincinnati, Ohio", "description": "<p>Trials</p>", "posted_date": "2026-09-18T12:00:00+0000", "apply_url": "https://icims.example/1"}},
            {"data": {"req_id": "2", "title": "CRA", "country_code": "GB", "full_location": "London, UK"}},
        ],
    })
    respx.get(endpoint, params={"page": "2", "limit": "2"}).respond(200, json={
        "totalCount": 3,
        "jobs": [{"data": {"req_id": "3", "title": "Clinical PM", "country_code": "US", "full_location": "Boston, Massachusetts", "description": "<p>Manage</p>", "apply_url": "https://icims.example/3"}}],
    })
    async with httpx.AsyncClient() as client:
        rows = await JibeSource(cfg, client).fetch()
    assert [row.external_job_id for row in rows] == ["1", "3"]
    assert rows[0].description_raw == "Trials"


@pytest.mark.asyncio
@pytest.mark.parametrize("country", ["ca", "us"])
@respx.mock
async def test_smartrecruiters_preserves_structured_country_code(country):
    endpoint = "https://api.smartrecruiters.com/v1/companies/acme/postings"
    respx.get(endpoint).mock(
        return_value=httpx.Response(
            200,
            json={
                "totalFound": 1,
                "content": [
                    {
                        "id": "s",
                        "name": "Clinical Project Manager",
                        "location": {"country": country},
                    }
                ],
            },
        )
    )
    respx.get(endpoint + "/s").mock(
        return_value=httpx.Response(200, json={"jobAd": {"sections": {}}})
    )
    async with httpx.AsyncClient() as client:
        rows = await SmartRecruitersSource(
            company("smartrecruiters", {"company_identifier": "acme"}), client
        ).fetch()
    assert rows[0].metadata["smartrecruiters"]["country_code"] == country


@pytest.mark.asyncio
@respx.mock
async def test_workday_searches_and_deduplicates():
    endpoint = "https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/External/jobs"
    route = respx.post(endpoint)
    posting = {
        "title": "Senior Data Analyst",
        "externalPath": "/job/Phoenix/Senior-Data-Analyst_R1",
        "locationsText": "Phoenix, AZ",
        "bulletFields": ["R1"],
        "postedOn": "2026-06-17T00:00:00Z",
    }
    route.side_effect = [
        httpx.Response(200, json={"total": 1, "jobPostings": [posting]}),
        httpx.Response(200, json={"total": 1, "jobPostings": [posting]}),
    ]
    cfg = company(
        "workday",
        {
            "endpoint": endpoint,
            "site": "acme.wd1.myworkdayjobs.com",
            "detail_base_url": "https://acme.wd1.myworkdayjobs.com/en-US/External",
            "search_texts": ["data", "analytics"],
        },
    )
    async with httpx.AsyncClient() as client:
        rows = await WorkdaySource(cfg, client).fetch()
    assert len(rows) == 1
    assert rows[0].external_job_id.endswith("_R1")


@pytest.mark.asyncio
@respx.mock
async def test_workday_paginates_when_later_pages_report_zero_total():
    endpoint = "https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/External/jobs"

    def postings(start, count):
        return [
            {
                "title": f"Project Manager {index}",
                "externalPath": f"/job/US/Project-Manager_{index}",
                "locationsText": "United States",
            }
            for index in range(start, start + count)
        ]

    route = respx.post(endpoint)
    route.side_effect = [
        httpx.Response(200, json={"total": 217, "jobPostings": postings(0, 2)}),
        httpx.Response(200, json={"total": 0, "jobPostings": postings(1, 2)}),
        httpx.Response(200, json={"total": 0, "jobPostings": postings(3, 2)}),
        httpx.Response(200, json={"total": 0, "jobPostings": postings(5, 1)}),
    ]
    cfg = company(
        "workday",
        {
            "endpoint": endpoint,
            "site": "acme.wd1.myworkdayjobs.com",
            "detail_base_url": "https://acme.wd1.myworkdayjobs.com/en-US/External",
            "limit": 2,
        },
    )

    async with httpx.AsyncClient() as client:
        rows = await WorkdaySource(cfg, client).fetch()

    assert len(rows) == 6
    assert len(route.calls) == 4


@pytest.mark.asyncio
@respx.mock
async def test_workday_pagination_stops_at_short_page_and_supports_agc_shape():
    endpoint = "https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/External/jobs"

    def postings(start, count):
        return [
            {
                "title": f"Job {index}",
                "externalPath": f"/job/US/Job_{index}",
                "locationsText": "United States",
            }
            for index in range(start, start + count)
        ]

    route = respx.post(endpoint)
    route.side_effect = [
        httpx.Response(200, json={"total": 25, "jobPostings": postings(0, 20)}),
        httpx.Response(200, json={"total": 0, "jobPostings": postings(20, 5)}),
    ]
    cfg = company(
        "workday",
        {
            "endpoint": endpoint,
            "site": "acme.wd1.myworkdayjobs.com",
            "detail_base_url": "https://acme.wd1.myworkdayjobs.com/en-US/External",
        },
    )

    async with httpx.AsyncClient() as client:
        rows = await WorkdaySource(cfg, client).fetch()

    assert len(rows) == 25
    assert len(route.calls) == 2


@pytest.mark.asyncio
@respx.mock
async def test_workday_pagination_honors_positive_total_on_every_page():
    endpoint = "https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/External/jobs"
    route = respx.post(endpoint)
    route.side_effect = [
        httpx.Response(
            200,
            json={
                "total": 4,
                "jobPostings": [
                    {"title": "Job 1", "externalPath": "/job/US/Job_1"},
                    {"title": "Job 2", "externalPath": "/job/US/Job_2"},
                ],
            },
        ),
        httpx.Response(
            200,
            json={
                "total": 4,
                "jobPostings": [
                    {"title": "Job 3", "externalPath": "/job/US/Job_3"},
                    {"title": "Job 4", "externalPath": "/job/US/Job_4"},
                ],
            },
        ),
    ]
    cfg = company(
        "workday",
        {
            "endpoint": endpoint,
            "site": "acme.wd1.myworkdayjobs.com",
            "detail_base_url": "https://acme.wd1.myworkdayjobs.com/en-US/External",
            "limit": 2,
        },
    )

    async with httpx.AsyncClient() as client:
        rows = await WorkdaySource(cfg, client).fetch()

    assert len(rows) == 4
    assert len(route.calls) == 2


@pytest.mark.asyncio
@respx.mock
async def test_workday_detail_api_replaces_listing_description():
    endpoint = "https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/External/jobs"
    external_path = "/job/Bothell-Washington-USA/Project-Manager_JR103037"
    detail_api_base = "https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/External"
    respx.post(endpoint).mock(
        return_value=httpx.Response(
            200,
            json={
                "total": 1,
                "jobPostings": [
                    {
                        "title": "Project Manager",
                        "externalPath": external_path,
                        "locationsText": "Bothell, Washington, USA",
                        "bulletFields": ["JR103037"],
                    }
                ],
            },
        )
    )
    respx.get(detail_api_base + external_path).mock(
        return_value=httpx.Response(
            200,
            json={"jobPostingInfo": {"jobDescription": "<p>Full CMC GMP project description</p>"}},
        )
    )
    cfg = company(
        "workday",
        {
            "endpoint": endpoint,
            "site": "acme.wd1.myworkdayjobs.com",
            "detail_base_url": "https://acme.wd1.myworkdayjobs.com/en-US/External",
            "detail_api_base": detail_api_base,
        },
    )

    async with httpx.AsyncClient() as client:
        rows = await WorkdaySource(cfg, client).fetch()

    assert len(rows) == 1
    assert rows[0].external_job_id == external_path
    assert rows[0].description_raw == "Full CMC GMP project description"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("listing_location", "detail_location", "additional", "country", "code"),
    [
        (
            "2 Locations",
            "England, United Kingdom",
            ["Madrid, Spain"],
            "United Kingdom",
            "GB",
        ),
        (
            "4 Locations",
            "Durham, North Carolina",
            ["Ontario, Canada", "Quebec, Canada"],
            "United States of America",
            "US",
        ),
    ],
)
@respx.mock
async def test_workday_detail_api_enriches_location(
    listing_location, detail_location, additional, country, code
):
    endpoint = "https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/External/jobs"
    external_path = "/job/detail/Project-Manager_R1"
    detail_api_base = "https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/External"
    respx.post(endpoint).mock(
        return_value=httpx.Response(
            200,
            json={
                "total": 1,
                "jobPostings": [
                    {
                        "title": "Project Manager",
                        "externalPath": external_path,
                        "locationsText": listing_location,
                    }
                ],
            },
        )
    )
    respx.get(detail_api_base + external_path).mock(
        return_value=httpx.Response(
            200,
            json={
                "jobPostingInfo": {
                    "location": detail_location,
                    "additionalLocations": additional,
                    "country": {"descriptor": country},
                    "jobRequisitionLocation": {"country": {"alpha2Code": code}},
                }
            },
        )
    )
    cfg = company(
        "workday",
        {
            "endpoint": endpoint,
            "site": "acme.wd1.myworkdayjobs.com",
            "detail_base_url": "https://acme.wd1.myworkdayjobs.com/en-US/External",
            "detail_api_base": detail_api_base,
        },
    )

    async with httpx.AsyncClient() as client:
        rows = await WorkdaySource(cfg, client).fetch()

    assert listing_location in rows[0].location_raw
    assert detail_location in rows[0].location_raw
    assert all(location in rows[0].location_raw for location in additional)
    assert country in rows[0].location_raw
    assert code in rows[0].location_raw


@pytest.mark.asyncio
@respx.mock
async def test_workday_without_detail_api_preserves_listing_location():
    endpoint = "https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/External/jobs"
    listing_location = "2 Locations"
    respx.post(endpoint).mock(
        return_value=httpx.Response(
            200,
            json={
                "total": 1,
                "jobPostings": [
                    {
                        "title": "Project Manager",
                        "externalPath": "/job/detail/Project-Manager_R1",
                        "locationsText": listing_location,
                    }
                ],
            },
        )
    )
    cfg = company(
        "workday",
        {
            "endpoint": endpoint,
            "site": "acme.wd1.myworkdayjobs.com",
            "detail_base_url": "https://acme.wd1.myworkdayjobs.com/en-US/External",
        },
    )

    async with httpx.AsyncClient() as client:
        rows = await WorkdaySource(cfg, client).fetch()

    assert rows[0].location_raw == listing_location


@pytest.mark.asyncio
@respx.mock
async def test_workday_detail_api_failure_preserves_listing_location(caplog):
    endpoint = "https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/External/jobs"
    external_path = "/job/detail/Project-Manager_R1"
    detail_api_base = "https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/External"
    respx.post(endpoint).mock(
        return_value=httpx.Response(
            200,
            json={
                "total": 1,
                "jobPostings": [
                    {
                        "title": "Project Manager",
                        "externalPath": external_path,
                        "locationsText": "2 Locations",
                        "bulletFields": ["Listing fallback"],
                    }
                ],
            },
        )
    )
    respx.get(detail_api_base + external_path).mock(return_value=httpx.Response(503))
    cfg = company(
        "workday",
        {
            "endpoint": endpoint,
            "site": "acme.wd1.myworkdayjobs.com",
            "detail_base_url": "https://acme.wd1.myworkdayjobs.com/en-US/External",
            "detail_api_base": detail_api_base,
        },
    )

    async with httpx.AsyncClient() as client:
        rows = await WorkdaySource(cfg, client).fetch()

    assert rows[0].location_raw == "2 Locations"
    assert rows[0].description_raw == "Listing fallback"
    assert "using listing fields" in caplog.text


@pytest.mark.asyncio
@respx.mock
async def test_workday_skips_posting_missing_title(caplog):
    endpoint = "https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/External/jobs"
    respx.post(endpoint).mock(
        return_value=httpx.Response(
            200,
            json={
                "total": 3,
                "jobPostings": [
                    {
                        "title": "Data Analyst",
                        "externalPath": "/job/Phoenix/Data-Analyst_R1",
                    },
                    {"externalPath": "/job/Phoenix/Malformed_R2"},
                    {"title": "Missing URL"},
                ],
            },
        )
    )
    cfg = company(
        "workday",
        {
            "endpoint": endpoint,
            "site": "acme.wd1.myworkdayjobs.com",
            "detail_base_url": "https://acme.wd1.myworkdayjobs.com/en-US/External",
        },
    )
    async with httpx.AsyncClient() as client:
        rows = await WorkdaySource(cfg, client).fetch()
    assert len(rows) == 1
    assert rows[0].title == "Data Analyst"
    assert "Skipping malformed Workday posting for acme" in caplog.text


@pytest.mark.asyncio
@respx.mock
async def test_workday_skips_posting_with_invalid_url(caplog):
    endpoint = "https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/External/jobs"
    respx.post(endpoint).mock(
        return_value=httpx.Response(
            200,
            json={
                "total": 2,
                "jobPostings": [
                    {
                        "title": "Data Analyst",
                        "externalPath": "https://jobs.example.com/usable",
                    },
                    {
                        "title": "Invalid URL",
                        "externalPath": "/job/Phoenix/Invalid_R2",
                    },
                ],
            },
        )
    )
    cfg = company(
        "workday",
        {
            "endpoint": endpoint,
            "site": "acme.wd1.myworkdayjobs.com",
            "detail_base_url": "",
        },
    )
    async with httpx.AsyncClient() as client:
        rows = await WorkdaySource(cfg, client).fetch()
    assert len(rows) == 1
    assert rows[0].title == "Data Analyst"
    assert "invalid fields" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"jobPostings": {}},
        {"jobPostings": None},
    ],
)
@respx.mock
async def test_workday_rejects_invalid_job_postings(payload):
    endpoint = "https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/External/jobs"
    respx.post(endpoint).mock(return_value=httpx.Response(200, json=payload))
    cfg = company(
        "workday",
        {
            "endpoint": endpoint,
            "site": "acme.wd1.myworkdayjobs.com",
            "detail_base_url": "https://acme.wd1.myworkdayjobs.com/en-US/External",
        },
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(SourceError, match="jobPostings"):
            await WorkdaySource(cfg, client).fetch()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "facet_config",
    [
        {},
        {"applied_facets": {}},
        {"applied_facets": {"country": ["US", "CA"], "type": ["regular"]}},
    ],
)
@respx.mock
async def test_workday_facets_across_pages_and_searches(facet_config):
    endpoint = "https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/External/jobs"
    cfg = company(
        "workday",
        {
            "endpoint": endpoint,
            "site": "acme.wd1.myworkdayjobs.com",
            "detail_base_url": "https://acme.wd1.myworkdayjobs.com/en-US/External",
            "limit": 1,
            "search_texts": ["clinical", "project"],
            **facet_config,
        },
    )
    original = deepcopy(cfg.ats_config)

    def respond(request):
        body = json.loads(request.content)
        offset = body["offset"]
        return httpx.Response(
            200,
            json={
                "total": 2,
                "jobPostings": [
                    {
                        "title": "Clinical Project Manager",
                        "externalPath": f"/job/US/R{offset}",
                        "locationsText": "United States",
                    }
                ],
            },
        )

    route = respx.post(endpoint).mock(side_effect=respond)
    async with httpx.AsyncClient() as client:
        rows = await WorkdaySource(cfg, client).fetch()
    bodies = [json.loads(call.request.content) for call in route.calls]
    assert [(b["searchText"], b["offset"]) for b in bodies] == [
        ("clinical", 0),
        ("clinical", 1),
        ("project", 0),
        ("project", 1),
    ]
    assert all(b["appliedFacets"] == facet_config.get("applied_facets", {}) for b in bodies)
    assert all(b["limit"] == 1 for b in bodies)
    assert len(rows) == 2
    assert cfg.ats_config == original


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "facets",
    [
        None,
        False,
        [],
        "US",
        {"": ["US"]},
        {" ": ["US"]},
        {1: ["US"]},
        {"country": None},
        {"country": "US"},
        {"country": ("US",)},
        {"country": [None]},
        {"country": [1]},
        {"country": [""]},
        {"country": [" "]},
    ],
)
@respx.mock
async def test_workday_invalid_facets_fail_before_http(facets):
    cfg = company(
        "workday",
        {
            "endpoint": "https://example.com/jobs",
            "site": "example.com",
            "detail_base_url": "https://example.com",
            "applied_facets": facets,
        },
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(SourceError, match="applied_facets"):
            await WorkdaySource(cfg, client).fetch()
    assert len(respx.calls) == 0


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize(
    ("slug", "facet_key"),
    [
        ("parexel", "locationCountry"),
        ("sartorius", "Country"),
        ("icon", "locationCountry"),
        ("iqvia", "Location_Country"),
    ],
)
async def test_enabled_workday_us_facet_contract(slug, facet_key):
    cfg = next(
        c
        for c in load_companies(Path(__file__).resolve().parents[1] / "config/companies.yml")
        if c.slug == slug
    )
    assert cfg.enabled is True
    assert cfg.source_verified is True
    assert cfg.profiles == ["clinical-discovery"]
    facets = {facet_key: ["bc33aa3152ec42d4995f4791a106ed09"]}
    assert cfg.ats_config["applied_facets"] == facets
    # A scoped response fixture tests our request/response contract, not Workday's
    # server-side geographic classification. No client-side country filter is implied.
    samples = [
        (
            "R0000044679",
            "Bilingual Research Associate (English and Mandarin)",
            "United States - Glendale - California",
            [],
        ),
        ("R0000044686", "Digital Pathology Project Manager", "United States - Remote", []),
        (
            "R0000045298",
            "Clinical Research Associate",
            "United States-New York-Remote",
            ["United States - Remote"],
        ),
    ]
    postings = []
    for job_id, title, location, additional in samples:
        path = f"/job/US/{job_id}"
        postings.append(
            {
                "title": title,
                "externalPath": path,
                "locationsText": "2 Locations" if additional else location,
            }
        )
        respx.get(cfg.ats_config["detail_api_base"] + path).respond(
            200,
            json={
                "jobPostingInfo": {
                    "jobDescription": "<p>Clinical study coordination</p>",
                    "location": location,
                    "additionalLocations": additional,
                    "country": {"descriptor": "United States of America"},
                    "jobRequisitionLocation": {"country": {"alpha2Code": "US"}},
                }
            },
        )
    route = respx.post(cfg.ats_config["endpoint"]).respond(
        200, json={"total": 3, "jobPostings": postings}
    )
    async with httpx.AsyncClient() as client:
        rows = await WorkdaySource(cfg, client).fetch()
    request = json.loads(route.calls[0].request.content)
    assert request["appliedFacets"] == facets
    assert request["searchText"] == ""
    assert {r.external_job_id.rsplit("/", 1)[-1] for r in rows} == {s[0] for s in samples}
    assert not any(
        china_id in r.external_job_id for r in rows for china_id in ("R0000034848", "R0000036586")
    )
    assert all(r.description_raw == "Clinical study coordination" for r in rows)
    assert "Glendale" in rows[0].location_raw
    assert "United States - Remote" in rows[1].location_raw
    assert "United States-New York-Remote" in rows[2].location_raw
    assert "United States - Remote" in rows[2].location_raw


@pytest.mark.asyncio
@respx.mock
async def test_thermo_fisher_dynamic_us_locations_and_details():
    cfg = next(
        c
        for c in load_companies(Path(__file__).resolve().parents[1] / "config/companies.yml")
        if c.slug == "thermo-fisher-ppd"
    )
    assert cfg.enabled and cfg.source_verified
    assert cfg.profiles == ["clinical-discovery"]
    cfg.ats_config["limit"] = 1
    locations = [
        {"id": "us-office", "descriptor": "Middleton, Wisconsin, USA"},
        {"id": "us-remote", "descriptor": "Remote, United States of America"},
        {"id": "my", "descriptor": "Remote, Malaysia"},
    ]

    def respond(request):
        body = json.loads(request.content)
        assert body["searchText"] == ""
        if not body["appliedFacets"]:
            return httpx.Response(
                200,
                json={
                    "facets": [
                        {
                            "facetParameter": "locationMainGroup",
                            "values": [{"facetParameter": "locations", "values": locations}],
                        }
                    ]
                },
            )
        assert body["appliedFacets"] == {"locations": ["us-office", "us-remote"]}
        i = body["offset"]
        return httpx.Response(
            200,
            json={
                "total": 2,
                "jobPostings": [
                    {
                        "title": ["Clinical Trial Coordinator", "Project Coordinator"][i],
                        "externalPath": f"/job/test/R-{i}",
                        "locationsText": locations[i]["descriptor"],
                    }
                ],
            },
        )

    route = respx.post(cfg.ats_config["endpoint"]).mock(side_effect=respond)
    for i in range(2):
        respx.get(cfg.ats_config["detail_api_base"] + f"/job/test/R-{i}").respond(
            200,
            json={
                "jobPostingInfo": {
                    "jobDescription": "<p>PPD clinical trial support</p>",
                    "location": locations[i]["descriptor"],
                    "country": {"descriptor": "United States of America"},
                }
            },
        )
    async with httpx.AsyncClient() as client:
        rows = await WorkdaySource(cfg, client).fetch()
    assert route.call_count == 3  # metadata request plus two filtered pages
    assert len(rows) == 2
    assert all(row.description_raw == "PPD clinical trial support" for row in rows)
    assert all(str(row.url).startswith(cfg.ats_config["detail_base_url"]) for row in rows)


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize(
    "facets",
    [
        None,
        [],
        [
            {
                "facetParameter": "locations",
                "values": [
                    {"id": "foreign", "descriptor": "Remote, Malaysia"},
                    {"descriptor": "Boston, USA"},
                ],
            }
        ],
    ],
)
async def test_workday_unresolved_facet_patterns_fail_closed(facets):
    cfg = company(
        "workday",
        {
            "endpoint": "https://example.com/jobs",
            "site": "example.com",
            "facet_patterns": {"locations": ", USA$"},
            "detail_base_url": "https://example.com",
        },
    )
    route = respx.post(cfg.ats_config["endpoint"]).respond(200, json={"facets": facets})
    async with httpx.AsyncClient() as client:
        with pytest.raises(SourceError, match="refusing unscoped"):
            await WorkdaySource(cfg, client).fetch()
    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize(
    "patterns", [[], {"locations": "["}, {"locations": ""}, {"locations": ["USA"]}]
)
async def test_workday_invalid_facet_patterns_fail_before_http(patterns):
    cfg = company(
        "workday",
        {"endpoint": "https://example.com/jobs", "site": "example.com", "facet_patterns": patterns,
         "detail_base_url": "https://example.com"},
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(SourceError):
            await WorkdaySource(cfg, client).fetch()
    assert not respx.calls
