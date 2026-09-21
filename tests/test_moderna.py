"""Focused Moderna Workday configuration and location-validation contracts."""

import json
from pathlib import Path

import httpx
import pytest
import respx

from job_monitor.config import load_companies
from job_monitor.sources import SOURCE_CLASSES, WorkdaySource


COMPANY = next(
    company for company in load_companies(Path("config/companies.yml"))
    if company.slug == "moderna"
)


def test_moderna_uses_verified_workday_configuration():
    assert COMPANY.enabled and COMPANY.source_verified
    assert COMPANY.profiles == ["clinical-discovery"]
    assert SOURCE_CLASSES[COMPANY.ats_type] is WorkdaySource
    assert COMPANY.ats_config["endpoint"] == (
        "https://modernatx.wd1.myworkdayjobs.com/wday/cxs/modernatx/M_tx/jobs"
    )
    assert COMPANY.ats_config["site"] == "modernatx.wd1.myworkdayjobs.com"
    assert COMPANY.ats_config["detail_base_url"] == (
        "https://modernatx.wd1.myworkdayjobs.com/en-US/M_tx"
    )
    assert COMPANY.ats_config["detail_api_base"] == (
        "https://modernatx.wd1.myworkdayjobs.com/wday/cxs/modernatx/M_tx"
    )
    assert COMPANY.ats_config["limit"] == 20
    assert COMPANY.ats_config["location_facet_parameter"] == "primarylocation"
    assert COMPANY.ats_config["validate_location_facets"] is True
    assert COMPANY.ats_config["location_facet_country"] == "United States of America"
    assert set(COMPANY.ats_config["facet_patterns"]) == {"locations"}


@pytest.mark.asyncio
@respx.mock
async def test_moderna_resolves_primarylocation_and_validates_details():
    cfg = COMPANY.model_copy(deep=True)
    cfg.ats_config["limit"] = 2
    endpoint = cfg.ats_config["endpoint"]
    detail_base = cfg.ats_config["detail_api_base"]
    listing = respx.post(endpoint).mock(side_effect=[
        httpx.Response(200, json={"facets": [{
            "facetParameter": "primarylocation",
            "values": [
                {"id": "cambridge", "descriptor": "Cambridge, Massachusetts"},
                {"id": "remote", "descriptor": "Remote - US"},
                {"id": "london", "descriptor": "London, United Kingdom"},
            ],
        }]}),
        httpx.Response(200, json={"total": 3, "jobPostings": [
            {"externalPath": "/job/remote", "title": "Remote Clinical Associate"},
            {"externalPath": "/job/mixed", "title": "US/Canada Clinical Associate"},
        ]}),
        httpx.Response(200, json={"total": 3, "jobPostings": [
            {"externalPath": "/job/foreign", "title": "Foreign Clinical Associate"},
        ]}),
    ])
    # The first POST above is the facet request; inspect the filtered request body.
    details = {
        "/job/remote": {
            "location": "Remote - US",
            "country": {"descriptor": "United States of America"},
            "additionalLocations": [],
            "jobDescription": "Remote clinical operations support",
        },
        "/job/mixed": {
            "location": "Cambridge, Massachusetts",
            "country": {"descriptor": "United States of America"},
            "additionalLocations": ["Toronto, Canada"],
            "jobDescription": "Clinical operations support",
        },
        "/job/foreign": {
            "location": "London, United Kingdom",
            "country": {"descriptor": "United Kingdom"},
            "additionalLocations": ["Toronto, Canada"],
            "jobDescription": "Foreign clinical operations support",
        },
    }
    for path, detail in details.items():
        respx.get(detail_base + path).mock(
            return_value=httpx.Response(200, json={"jobPostingInfo": detail})
        )

    async with httpx.AsyncClient() as client:
        source = WorkdaySource(cfg, client)
        rows = await source.fetch()

    assert listing.call_count == 3
    filtered_body = json.loads(listing.calls[1].request.content)
    assert filtered_body["appliedFacets"] == {
        "primarylocation": ["cambridge", "remote"]
    }
    assert {row.external_job_id for row in rows} == {"/job/remote", "/job/mixed"}
    remote = next(row for row in rows if row.external_job_id == "/job/remote")
    mixed = next(row for row in rows if row.external_job_id == "/job/mixed")
    assert remote.location_raw.startswith("Remote - US")
    assert mixed.metadata["workday_locations"]["scoped_additional"] == []
    assert len(source.warnings) == 1
    assert source.warnings[0]["title"] == "Foreign Clinical Associate"
