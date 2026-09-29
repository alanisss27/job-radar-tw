from pathlib import Path

import httpx
import pytest
import respx

from job_monitor.active_status import ActiveStatus
from job_monitor.config import load_companies
from job_monitor.matching import parse_job
from job_monitor.models import AtsType, RemoteType
from job_monitor.sources import SOURCE_CLASSES, TalentBrewSource


COMPANY = next(
    item for item in load_companies(Path("config/companies.yml"))
    if item.slug == "alexion-astrazeneca-rare-disease"
)
ENDPOINT = "https://careers.astrazeneca.com/search-jobs/results"


def listing(req: str, url_id: str, title: str, location: str, company: str = "Alexion") -> str:
    return f"""<article class='job-result'>
      <h2><a href='/job/43991/{url_id}'>{title}</a></h2>
      <span class='job-location'>{location}</span><span class='company-name'>{company}</span>
      <span>Requisition ID: {req}</span>
    </article>"""


def detail(req: str, title: str, close_date: str, location: str, apply: bool = True) -> str:
    action = '<a href="/apply">Apply Now</a>' if apply else ""
    return f"""<html><head><link rel='canonical' href='/job/43991/99395335280'></head>
      <main><h1>{title}</h1><span class='company-name'>Alexion</span>
      <span class='job-location'>{location}</span>
      <div>Requisition ID: {req} Date Posted: 09/14/2026 Closing Date: {close_date}
      Salary: $211,655-$317,482</div>
      <div class='job-description'>Clinical study operations, study start-up, and vendor coordination.</div>
      {action}</main></html>"""


@pytest.mark.asyncio
@respx.mock
async def test_alexion_filter_deduplicates_requisitions_and_normalizes_details():
    # R-258358 occurs twice through overlapping parent/brand result entries.
    page = "<html>" + listing(
        "R-258358", "99395335280", "Senior Director, Study Start-up", "Boston, MA (Hybrid)"
    ) + listing(
        "R-258358", "99395335280-copy", "Senior Director, Study Start-up", "Boston, MA (Hybrid)"
    ) + listing("R-252260", "99395111222", "Director, Country Operations", "Boston, MA (Onsite)") + listing(
        "R-999999", "99395555111", "Unrelated Role", "New York, NY", "Other Pharma"
    ) + "</html>"
    route = respx.get(ENDPOINT).mock(return_value=httpx.Response(200, text=page))
    respx.get("https://careers.astrazeneca.com/job/43991/99395335280").respond(
        200, text=detail("R-258358", "Senior Director, Study Start-up", "10/01/2026", "Boston, MA (Hybrid)")
    )
    respx.get("https://careers.astrazeneca.com/job/43991/99395111222").respond(
        200, text=detail("R-252260", "Director, Country Operations", "09/27/2026", "Boston, MA (Onsite)")
    )
    async with httpx.AsyncClient() as client:
        jobs = await TalentBrewSource(COMPANY, client).fetch()

    assert route.called
    params = route.calls[0].request.url.params
    assert params["custom_fields.jobPostingSite"] == "Alexion"
    assert params["orgIds"] == "7684"
    assert {job.external_job_id for job in jobs} == {"R-258358", "R-252260"}
    assert len(jobs) == 2
    active = next(job for job in jobs if job.external_job_id == "R-258358")
    closed = next(job for job in jobs if job.external_job_id == "R-252260")
    assert active.canonical_url == "https://careers.astrazeneca.com/job/43991/99395335280"
    assert active.metadata["talentbrew"]["company"] == "Alexion"
    assert active.posted_at.isoformat().startswith("2026-09-14")
    assert active.metadata["talentbrew"]["closing_date"].startswith("2026-10-01")
    assert active.metadata["active_status"] == ActiveStatus.ACTIVE.value
    assert closed.metadata["active_status"] == ActiveStatus.INACTIVE.value
    assert parse_job(active).remote_type is RemoteType.HYBRID
    assert parse_job(closed).remote_type is RemoteType.ONSITE


def test_talentbrew_page_without_apply_is_unknown_and_source_is_company_scoped():
    fields = TalentBrewSource._detail_fields(
        detail("R-100001", "Clinical Project Manager", "10/01/2026", "Boston, MA", apply=False),
        "https://careers.astrazeneca.com/job/1/1",
        "Alexion, AstraZeneca Rare Disease",
    )
    assert fields["active_status"] is ActiveStatus.UNKNOWN
    assert COMPANY.ats_type is AtsType.TALENTBREW
    assert COMPANY.profiles == ["clinical-discovery"]
    assert SOURCE_CLASSES[AtsType.TALENTBREW] is TalentBrewSource
