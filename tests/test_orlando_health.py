"""Focused Orlando Health Jibe source configuration tests."""

from pathlib import Path

import httpx
import pytest
import respx

from job_monitor.config import load_companies
from job_monitor.sources import JibeSource, SOURCE_CLASSES


COMPANY = next(
    company for company in load_companies(Path("config/companies.yml"))
    if company.slug == "orlando-health"
)
ENDPOINT = "https://orlandohealth.jibeapply.com/api/jobs"
CATEGORY = "Quality Assurance & Research"


def test_orlando_health_uses_bounded_jibe_category_filter():
    assert COMPANY.enabled and COMPANY.source_verified
    assert COMPANY.profiles == ["clinical-discovery"]
    assert SOURCE_CLASSES[COMPANY.ats_type] is JibeSource
    assert COMPANY.ats_config == {
        "endpoint": ENDPOINT,
        "limit": 20,
        "query_params": {"categories": CATEGORY},
    }


@pytest.mark.asyncio
@respx.mock
async def test_orlando_health_preserves_filtered_metadata_and_pagination():
    cfg = COMPANY.model_copy(deep=True)
    cfg.ats_config["limit"] = 2
    first = respx.get(
        ENDPOINT,
        params={"categories": CATEGORY, "page": "1", "limit": "2"},
    ).respond(
        200,
        json={
            "totalCount": 3,
            "jobs": [
                {
                    "data": {
                        "req_id": "OH-1",
                        "title": "Quality Research Coordinator",
                        "country_code": "US",
                        "full_location": "Orlando, Florida",
                        "description": "Coordinate regulated research projects.",
                        "posted_date": "2026-09-20T12:00:00+0000",
                        "category": "Quality Assurance & Research",
                        "apply_url": "https://jobs.icims.com/orlando/1",
                        "applyable": True,
                        "searchable": True,
                    }
                },
                {
                    "data": {
                        "req_id": "OH-2",
                        "title": "Research Operations Specialist",
                        "country_code": "US",
                        "full_location": "Orlando, Florida",
                        "description": "Support quality operations.",
                        "apply_url": "https://jobs.icims.com/orlando/2",
                        "applyable": True,
                        "searchable": True,
                    }
                },
            ],
        },
    )
    second = respx.get(
        ENDPOINT,
        params={"categories": CATEGORY, "page": "2", "limit": "2"},
    ).respond(
        200,
        json={
            "totalCount": 3,
            "jobs": [
                {
                    "data": {
                        "req_id": "OH-3",
                        "title": "Quality Systems Specialist",
                        "country_code": "US",
                        "full_location": "Orlando, Florida",
                        "description": "Maintain quality systems.",
                        "apply_url": "https://jobs.icims.com/orlando/3",
                        "applyable": True,
                        "searchable": True,
                    }
                }
            ],
        },
    )

    async with httpx.AsyncClient() as client:
        jobs = await JibeSource(cfg, client).fetch()

    assert first.called and second.called
    assert [job.external_job_id for job in jobs] == ["OH-1", "OH-2", "OH-3"]
    assert jobs[0].description_raw == "Coordinate regulated research projects."
    assert jobs[0].location_raw == "Orlando, Florida"
    assert jobs[0].posted_at is not None
    assert jobs[0].metadata["jibe"]["category"] == CATEGORY
    assert jobs[0].metadata["jibe"]["applyable"] is True
    assert jobs[0].metadata["jibe"]["searchable"] is True
    assert str(jobs[0].url) == "https://jobs.icims.com/orlando/1"
