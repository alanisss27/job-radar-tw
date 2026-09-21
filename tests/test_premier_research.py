"""Premier Workday onboarding contracts; no live requests or production scans.

Location examples and abbreviated duty excerpts come from the 2026-09-21 audit.
Pagination padding and non-remote arrangements are synthetic regression controls,
not claims about additional current Premier vacancies.
"""

import json
from pathlib import Path

import httpx
import pytest
import respx

from job_monitor.config import load_companies, load_preferences, load_profiles
from job_monitor.eligibility import work_arrangement
from job_monitor.matching import match_job, parse_job
from job_monitor.models import RawJob, RemoteType
from job_monitor.sources import SOURCE_CLASSES, WorkdayRequestController, WorkdaySource


COMPANY = next(c for c in load_companies(Path("config/companies.yml"))
               if c.slug == "premier-research")
HOST = "premierresearch.wd12.myworkdayjobs.com"
API = f"https://{HOST}/wday/cxs/premierresearch/PremierResearch"
US = "United States of America"
PATH_US = "/job/United-States-of-America/Senior-Project-Manager--Dermatology_R6302"
PATH_MIXED = "/job/Bulgaria/Project-Finance-Systems-Specialist-I_R6443"


def facets():
    # A changed ID confirms runtime resolution instead of pinning the audit's ID.
    return [{"facetParameter": "locationMainGroup", "values": [{
        "facetParameter": "locations", "values": [
            {"descriptor": US, "id": "resolved-us-id"},
            {"descriptor": "Bulgaria", "id": "bg"},
            {"descriptor": "Canada", "id": "ca"},
        ],
    }]}]


def test_verified_configuration_reuses_workday():
    assert COMPANY.enabled and COMPANY.source_verified
    assert COMPANY.name == "Premier Research"
    assert str(COMPANY.careers_url) == "https://premier-research.com/careers/"
    assert COMPANY.profiles == ["clinical-discovery"]
    assert SOURCE_CLASSES[COMPANY.ats_type] is WorkdaySource
    assert COMPANY.ats_config == {
        "endpoint": API + "/jobs",
        "site": HOST,
        "detail_base_url": f"https://{HOST}/en-US/PremierResearch",
        "detail_api_base": API,
        "limit": 20,
        "facet_patterns": {"locations": "^United States of America$"},
        "validate_location_facets": True,
        "location_facet_country": US,
    }


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize("total", [28, 40])
async def test_pagination_locations_remote_and_external_path_identity(total):
    # 28 reproduces the audited page sizes. 40 additionally proves the remembered
    # positive total terminates a full second page reporting zero, without page 3.
    paths = [PATH_US, PATH_MIXED] + [f"/job/US/control-{i}" for i in range(2, total)]
    listings = [{
        "title": "Location regression control", "externalPath": path,
        "locationsText": "11 Locations" if i == 0 else "3 Locations" if i == 1 else US,
        "bulletFields": ["R6302" if i == 0 else "R6443" if i == 1 else f"R-test-{i}"],
        "postedOn": "Posted 30+ Days Ago",
    } for i, path in enumerate(paths)]
    route = respx.post(API + "/jobs").mock(side_effect=[
        httpx.Response(200, json={"facets": facets()}),
        httpx.Response(200, json={"total": total, "jobPostings": listings[:20]}),
        httpx.Response(200, json={"total": 0, "jobPostings": listings[20:]}),
    ])
    canadian = ["Regional_Manitoba", "Regional_Prince Edward Island", "Toronto, ON",
                "Canada", "Regional_Alberta", "Regional_British Columbia", "Ontario",
                "Regional_Nova Scotia", "Regional_Quebec", "Regional_Ontario"]
    details = []
    for i, path in enumerate(paths):
        detail = {
            "jobReqId": listings[i]["bulletFields"][0],
            "location": "Bulgaria" if i == 1 else US,
            "country": {"descriptor": "Bulgaria" if i == 1 else US},
            "additionalLocations": ["India", US] if i == 1 else canadian if i == 0 else [],
            "jobRequisitionLocation": {
                "descriptor": "Regional_India_40" if i == 1 else "Regional_NC",
                "country": {"alpha2Code": "IN" if i == 1 else "US"},
            },
            "remoteType": "Hybrid" if i == 2 else "On-site" if i == 3 else "Remote",
            "jobDescription": "<p>Review study documentation.</p>",
        }
        details.append(respx.get(API + path).respond(200, json={"jobPostingInfo": detail}))
    async with httpx.AsyncClient() as client:
        controller = WorkdayRequestController(min_interval_seconds=0)
        source = WorkdaySource(COMPANY, client, controller)
        assert source.request_controller is controller
        jobs = await source.fetch()

    assert len(jobs) == total
    assert not source.warnings
    assert route.call_count == 3
    bodies = [json.loads(call.request.content) for call in route.calls]
    assert bodies[0] == {"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""}
    assert bodies[1:] == [
        {"appliedFacets": {"locations": ["resolved-us-id"]},
         "limit": 20, "offset": offset, "searchText": ""}
        for offset in (0, 20)
    ]
    assert all(detail.call_count == 1 for detail in details)
    assert [job.external_job_id for job in jobs] == paths
    assert all(str(job.url) == COMPANY.ats_config["detail_base_url"] + path
               for job, path in zip(jobs, paths))
    assert all(job.description_raw == "Review study documentation." for job in jobs)
    primary, mixed = (job.metadata["workday_locations"] for job in jobs[:2])
    assert primary["primary"] == US
    assert primary["primary_country"] == {"descriptor": US}
    assert primary["additional"] == canadian
    assert primary["scoped_additional"] == []
    assert primary["listing"] == "11 Locations"
    assert primary["requisition_location"]["descriptor"] == "Regional_NC"
    assert mixed["primary"] == "Bulgaria"
    assert mixed["primary_country"] == {"descriptor": "Bulgaria"}
    assert mixed["additional"] == ["India", US]
    assert mixed["scoped_additional"] == [US]
    assert mixed["facet_country"] == US
    assert mixed["listing"] == "3 Locations"
    assert mixed["requisition_location"] == {
        "descriptor": "Regional_India_40", "country": {"alpha2Code": "IN"},
    }
    assert jobs[1].location_raw.startswith("Bulgaria;")
    assert US in jobs[1].location_raw
    for i, expected in enumerate([RemoteType.REMOTE, RemoteType.REMOTE,
                                  RemoteType.HYBRID, RemoteType.ONSITE]):
        assert work_arrangement(jobs[i])[0] == expected
    assert jobs[0].metadata["eligibility"]["work_arrangement"] == "Remote"
    # Existing conflict review is retained even when the ATS says Remote.
    travel = jobs[0].model_copy(update={
        "description_raw": "You must work onsite at study sites. Travel up to 70-85%.",
    })
    arrangement, reasons = work_arrangement(travel)
    assert arrangement == RemoteType.UNKNOWN
    assert any("conflicting_work_arrangement" in reason for reason in reasons)


@pytest.mark.asyncio
@respx.mock
async def test_us_facet_does_not_override_foreign_only_detail():
    respx.post(API + "/jobs").mock(side_effect=[
        httpx.Response(200, json={"facets": facets()}),
        httpx.Response(200, json={"total": 1, "jobPostings": [{
            "title": "Foreign control", "externalPath": PATH_MIXED,
            "locationsText": "2 Locations",
        }]}),
    ])
    respx.get(API + PATH_MIXED).respond(200, json={"jobPostingInfo": {
        "location": "Bulgaria", "additionalLocations": ["India"],
        "country": {"descriptor": "Bulgaria"}, "remoteType": "Remote",
    }})
    async with httpx.AsyncClient() as client:
        source = WorkdaySource(COMPANY, client, WorkdayRequestController(min_interval_seconds=0))
        assert await source.fetch() == []
    assert len(source.warnings) == 1
    assert source.warnings[0]["reason"] == "location validation failed"


# Abbreviated audit excerpts protect the observed negative discovery boundaries;
# these are not full-description snapshots or a fresh inventory measurement.
@pytest.mark.parametrize("title,description", [
    ("Clinical Lead II (Neuroscience)",
     "Plans, presents and participates in sponsor calls, representing Clinical and SSU "
     "function by providing status updates on clinical deliverables. Oversees the quality "
     "of clinical monitoring, central monitoring and site management deliverables. "
     "2 years of experience as a Clinical Lead."),
    ("In-House Clinical Research Associate I, Sponsor-Dedicated (Contract)",
     "Carry out remote monitoring of clinical trials. Assists with audit/inspection "
     "readiness, study start-up activities, data listing reviews, monitoring visit support "
     "and issue resolution. 3 to 5 years of practical experience in clinical trials."),
    ("Clinical Research Associate I (West-Coast)",
     "Delivers quality, timely monitoring reports for sponsor approval per the Clinical "
     "Monitoring Plan timelines. Plans day-to-day activities for monitoring of a clinical "
     "study and sets priorities per site. Travel up to 70-85%."),
    ("Senior Project Manager, Dermatology",
     "Ensures adherence to project budget and scope of work to realize project profitability. "
     "Ensures all project tasks are completed in accordance with project plans. "
     "Acts as the primary liaison between Premier Research and the customer for all assigned "
     "projects and chairs project team meetings and teleconferences. "
     "5+ years of Project Management experience in a CRO/Pharmaceutical/Biotech industry."),
    ("Senior Trial Manager",
     "Provides oversight and trial management of the planning, execution, and completion "
     "of clinical trials. Leads, drives, manages and actively monitors the clinical "
     "monitoring team with a focus on quality and timely project deliverables. "
     "10 years of clinical research experience, including 7 years of global study oversight."),
    ("Project Finance Systems Specialist I",
     "Support Workday and OneStream administration, configuration and business process "
     "enhancements. Assist with system testing, user acceptance testing (UAT), and application "
     "upgrades. Working knowledge of integrated program management systems. "
     "Preferred direct experience in the Clinical Research industry."),
])
def test_audited_role_families_remain_below_discovery_threshold(title, description):
    profile = load_profiles(Path("config/profiles.yml"))["clinical-discovery"]
    preferences = load_preferences(Path("config/preferences.yml"))
    raw = RawJob(
        source_company=COMPANY.slug, external_job_id="/job/audit-excerpt",
        title=title, location_raw=US, description_raw=description,
        url=COMPANY.ats_config["detail_base_url"] + "/job/audit-excerpt",
        metadata={"eligibility": {"work_arrangement": "Remote"}},
    )
    result = match_job(parse_job(raw), profile, preferences)
    assert profile.threshold == 0.70
    assert profile.strong_threshold == 0.90
    assert result.score < profile.threshold
    assert not result.discovery_eligible
