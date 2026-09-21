"""Dedicated SCRI Workday board configuration and enrichment contracts."""

import json
from pathlib import Path

import httpx
import pytest
import respx

from job_monitor.config import load_companies
from job_monitor.sources import SOURCE_CLASSES, WorkdayRequestController, WorkdaySource


COMPANY = next(c for c in load_companies(Path("config/companies.yml"))
               if c.slug == "mckesson-scri")
HOST = "https://mckesson.wd3.myworkdayjobs.com"
API = HOST + "/wday/cxs/mckesson/SCRI_Careers"
FACETS = {"Location_Country": ["bc33aa3152ec42d4995f4791a106ed09"]}


def test_official_scri_board_configuration():
    assert COMPANY.enabled and COMPANY.source_verified
    assert str(COMPANY.careers_url) == "https://www.scri.com/careers/"
    assert COMPANY.profiles == ["clinical-discovery"]
    assert SOURCE_CLASSES[COMPANY.ats_type] is WorkdaySource
    assert COMPANY.ats_config == {
        "endpoint": API + "/jobs",
        "site": "mckesson.wd3.myworkdayjobs.com",
        "detail_base_url": HOST + "/en-US/SCRI_Careers",
        "detail_api_base": API,
        "limit": 20,
        "applied_facets": FACETS,
    }


@pytest.mark.asyncio
@respx.mock
async def test_scri_country_scope_pagination_identity_and_detail_enrichment():
    cfg = COMPANY.model_copy(deep=True)
    cfg.ats_config["limit"] = 1
    paths = [
        "/job/USA-TN-Remote/Clinical-Project-Associate_JR0153944",
        "/job/USA-TN-Remote/Clinical-IT-Quality-Control-Analyst---Remote-US_JR0151710",
    ]
    titles = ["Clinical Project Associate", "Clinical IT Quality Control Analyst - Remote US"]
    listings = respx.post(API + "/jobs").mock(side_effect=[
        httpx.Response(200, json={"total": 2 if i == 0 else 0, "jobPostings": [{
            "title": title, "externalPath": path,
            "locationsText": "USA, TN, Remote" if i == 0 else "2 Locations",
            "bulletFields": ["JR0153944" if i == 0 else "JR0151710"],
        }]}) for i, (path, title) in enumerate(zip(paths, titles))
    ])
    # Synthetic description; location and arrangement shapes verified on the board.
    for path in paths:
        respx.get(API + path).respond(200, json={"jobPostingInfo": {
            "location": "USA, TN, Remote",
            "additionalLocations": ["Work at Home - North Carolina, USA (WNCA)"],
            "country": {"descriptor": "United States of America"},
            "remoteType": "Fully Remote",
            "jobDescription": "<p>Clinical study documentation support.</p>",
        }})
    async with httpx.AsyncClient() as client:
        source = WorkdaySource(cfg, client, WorkdayRequestController(min_interval_seconds=0))
        jobs = await source.fetch()
    assert listings.call_count == 2
    assert [json.loads(call.request.content) for call in listings.calls] == [
        {"appliedFacets": FACETS, "limit": 1, "offset": i, "searchText": ""}
        for i in range(2)
    ]
    assert [job.external_job_id for job in jobs] == paths
    assert [job.title for job in jobs] == titles  # Ingestion does not alter title filtering.
    for job, path in zip(jobs, paths):
        assert str(job.url) == HOST + "/en-US/SCRI_Careers" + path
        assert job.description_raw == "Clinical study documentation support."
        assert "USA, TN, Remote" in job.location_raw
        assert "Work at Home - North Carolina, USA (WNCA)" in job.location_raw
        assert job.metadata["eligibility"]["work_arrangement"] == "Fully Remote"
