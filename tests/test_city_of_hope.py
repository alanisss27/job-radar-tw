from pathlib import Path

import httpx
import pytest
import respx

from job_monitor.active_status import ActiveStatus
from job_monitor.config import load_companies, load_preferences, load_profiles
from job_monitor.matching import parse_job
from job_monitor.models import AtsType, RemoteType
from job_monitor.pipeline import _potential_city_of_hope_candidate
from job_monitor.matching import match_job
from job_monitor.sources import CityOfHopeSource, SOURCE_CLASSES


COMPANY = next(
    item for item in load_companies(Path("config/companies.yml"))
    if item.slug == "city-of-hope"
)
LISTING = "https://www.cityofhopejobs.org/jobs/"


def card(ref: str, title: str, extra: str = "") -> str:
    return f"""<article class='job-card'>
      <h3><a href='/job/12032/{ref.lower()}-{title.lower().replace(' ', '-')}'>{title}</a></h3>
      <span class='job-location'>{'United States (This is a remote job)' if ref == '10036071' else 'Duarte, CA'}</span>
      <div>Job Ref: {ref} Category: Clinical Research Job Type: Full Time Shift: Days
      Pay Range: $44.90-$69.60/hr</div>
      <p class='job-excerpt'>Clinical study project coordination. {extra}</p>
    </article>"""


@pytest.mark.asyncio
@respx.mock
async def test_city_of_hope_paginates_parses_listing_and_hydrates_candidate_only():
    first = "<html>" + "".join(
        card(str(10036000 + i), f"Administrative Role {i}") for i in range(20)
    ) + "</html>"
    # Include a relevant remote role and a full first page to exercise pagination.
    first = first.replace(
        card("10036000", "Administrative Role 0"),
        card("10036071", "Clinical Research Project Coordinator"),
        1,
    )
    second = "<html>" + card("JR-22", "Research Operations Analyst") + "</html>"
    page2 = respx.get(LISTING, params={"page_jobs": "2"}).mock(
        return_value=httpx.Response(200, text=second)
    )
    page1 = respx.get(LISTING, params={}).mock(return_value=httpx.Response(200, text=first))
    detail_url = "https://www.cityofhopejobs.org/job/12032/10036071-clinical-research-project-coordinator"
    detail = respx.get(detail_url).respond(
        200,
        text="""<main><h1>Clinical Research Project Coordinator</h1>
        <div class='job-description'>Coordinate clinical studies and timelines.</div>
        <a href='/apply/10036071'>Apply Now</a></main>""",
    )
    async with httpx.AsyncClient() as client:
        source = CityOfHopeSource(COMPANY, client)
        jobs = await source.fetch()
        assert len(jobs) == 21
        remote = next(job for job in jobs if job.external_job_id == "10036071")
        assert page1.called and page2.called
        assert not detail.called
        assert remote.metadata["city_of_hope"]["job_ref"] == "10036071"
        assert remote.canonical_url == detail_url
        assert remote.metadata["city_of_hope"]["category"] == "Clinical Research"
        assert remote.metadata["city_of_hope"]["job_type"] == "Full Time"
        assert remote.metadata["city_of_hope"]["shift"] == "Days"
        assert remote.metadata["city_of_hope"]["pay_range"] == "$44.90-$69.60/hr"
        assert parse_job(remote).remote_type is RemoteType.REMOTE
        administrative = next(job for job in jobs if job.title.startswith("Administrative"))
        administrative_match = match_job(
            parse_job(administrative),
            load_profiles(Path("config/profiles.yml"))["clinical-discovery"],
            load_preferences(Path("config/preferences.yml")),
        )
        assert not _potential_city_of_hope_candidate([administrative_match])
        preliminary = match_job(
            parse_job(remote),
            load_profiles(Path("config/profiles.yml"))["clinical-discovery"],
            load_preferences(Path("config/preferences.yml")),
        )
        assert _potential_city_of_hope_candidate([preliminary])
        hydrated = await source.hydrate(remote)
        assert detail.called
        assert "Coordinate clinical studies" in hydrated.description_raw
        assert hydrated.metadata["active_status"] == ActiveStatus.ACTIVE.value
        assert hydrated.content_hash == remote.content_hash


@pytest.mark.asyncio
@respx.mock
async def test_city_of_hope_unavailable_detail_stays_unknown():
    remote = CityOfHopeSource._listing_items(
        card("10036071", "Clinical Research Project Coordinator"), LISTING
    )[0]
    from job_monitor.models import RawJob

    raw = RawJob(
        source_company=COMPANY.slug,
        external_job_id=remote["id"],
        title=remote["title"],
        location_raw="Duarte, CA",
        description_raw=remote["excerpt"],
        url=remote["url"],
        metadata={"city_of_hope": {"detail_url": remote["url"], "listing_hash": remote["listing_hash"]}},
    )
    respx.get(remote["url"]).respond(410)
    async with httpx.AsyncClient() as client:
        hydrated = await CityOfHopeSource(COMPANY, client).hydrate(raw)
    assert hydrated.metadata["active_status"] == ActiveStatus.UNKNOWN.value
    assert hydrated.metadata["active_status_page_checked"] is True
    assert hydrated.content_hash == raw.content_hash


def test_city_of_hope_config_and_source_registration():
    assert COMPANY.ats_type is AtsType.CITY_OF_HOPE
    assert COMPANY.profiles == ["clinical-discovery"]
    assert SOURCE_CLASSES[AtsType.CITY_OF_HOPE] is CityOfHopeSource
