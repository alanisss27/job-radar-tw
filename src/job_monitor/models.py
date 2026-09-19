from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator


class AtsType(StrEnum):
    GREENHOUSE = "greenhouse"
    LEVER = "lever"
    ASHBY = "ashby"
    SMARTRECRUITERS = "smartrecruiters"
    WORKDAY = "workday"
    TALEMETRY = "talemetry"
    JIBE = "jibe"
    JSONLD = "jsonld"


class ProfileName(StrEnum):
    HEALTHCARE = "healthcare"
    SEMICONDUCTOR = "semiconductor"
    TECH = "tech"


class Seniority(StrEnum):
    ENTRY = "entry"
    MID = "mid"
    SENIOR = "senior"
    LEAD = "lead"
    DIRECTOR = "director_plus"
    UNKNOWN = "unknown"


class JobLevel(StrEnum):
    UNKNOWN = "unknown"
    ENTRY = "entry"
    MID = "mid"
    SENIOR = "senior"
    LEAD = "lead"
    STAFF = "staff"
    PRINCIPAL = "principal"
    DIRECTOR_PLUS = "director_plus"


class DegreeLevel(StrEnum):
    NONE = "none"
    BACHELOR = "bachelor"
    MASTER = "master"
    PHD = "phd"


class CompanyScale(StrEnum):
    SMALL = "small"
    MID = "mid"
    LARGE = "large"
    BIGTECH = "bigtech"


class RemoteType(StrEnum):
    ONSITE = "onsite"
    HYBRID = "hybrid"
    REMOTE = "remote"
    UNKNOWN = "unknown"


class VisaSupport(StrEnum):
    SUPPORTS = "supports"
    LIKELY_SUPPORTS = "likely_supports"
    CASE_BY_CASE = "case_by_case"
    DOES_NOT_SUPPORT = "does_not_support"
    UNKNOWN = "unknown"


class CompanyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str
    name: str
    careers_url: HttpUrl
    ats_type: AtsType
    ats_config: dict[str, Any] = Field(default_factory=dict)
    industry: str
    profiles: list[str] = Field(min_length=1)
    priority: int = Field(default=2, ge=1, le=3)
    enabled: bool = True
    source_verified: bool = False
    visa_support: VisaSupport = VisaSupport.UNKNOWN
    visa_notes: str | None = None
    ndx_member: bool = False
    ndx_as_of: str | None = None

    @field_validator("slug")
    @classmethod
    def valid_slug(cls, value: str) -> str:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", value):
            raise ValueError("slug must contain lowercase letters, numbers, and hyphens")
        return value

    @field_validator("profiles")
    @classmethod
    def unique_profiles(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("company profiles must be unique")
        if any(len(value) > 40 for value in values):
            raise ValueError("company profile names must be 40 characters or fewer")
        return values

    @model_validator(mode="after")
    def validate_ats_config(self) -> "CompanyConfig":
        required = {
            AtsType.GREENHOUSE: {"board_token"},
            AtsType.LEVER: {"site"},
            AtsType.ASHBY: {"board_name"},
            AtsType.SMARTRECRUITERS: {"company_identifier"},
            AtsType.WORKDAY: {"endpoint", "site", "detail_base_url"},
            AtsType.TALEMETRY: {"endpoint", "detail_base_url"},
            AtsType.JIBE: {"endpoint"},
            AtsType.JSONLD: set(),
        }[self.ats_type]
        missing = required - set(self.ats_config)
        if missing:
            raise ValueError(
                f"{self.ats_type.value} source is missing config keys: {sorted(missing)}"
            )
        return self


TRACKING_QUERY_PARAMS = frozenset(
    {
        "fbclid",
        "gclid",
        "gh_src",
        "mc_cid",
        "mc_eid",
        "msclkid",
        "ref",
        "referrer",
        "source",
        "src",
        "trk",
    }
)


def _canonical_query(query: str) -> str:
    """Keep the query parameters that identify a posting, drop tracking noise.

    Some boards address a posting entirely through the query string (Greenhouse
    proxies such as ``https://instacart.careers/job/?gh_jid=123``), so dropping
    the whole query would collapse every posting onto one URL.
    """
    kept = [
        (key, value)
        for key, value in parse_qsl(query, keep_blank_values=True)
        if key.lower() not in TRACKING_QUERY_PARAMS and not key.lower().startswith("utm_")
    ]
    return urlencode(sorted(kept))


class RawJob(BaseModel):
    source_company: str
    external_job_id: str | None = None
    title: str
    location_raw: str = ""
    description_raw: str = ""
    posted_at: datetime | None = None
    url: HttpUrl
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def canonical_url(self) -> str:
        parts = urlsplit(str(self.url))
        return urlunsplit(
            (
                parts.scheme.lower(),
                parts.netloc.lower(),
                parts.path.rstrip("/"),
                _canonical_query(parts.query),
                "",
            )
        )

    @property
    def stable_external_id(self) -> str:
        if self.external_job_id:
            return str(self.external_job_id)
        basis = "|".join(
            [
                self.source_company.lower(),
                self.title.lower(),
                self.location_raw.lower(),
                self.canonical_url,
            ]
        )
        return "fallback-" + hashlib.sha256(basis.encode()).hexdigest()[:24]

    @property
    def content_hash(self) -> str:
        normalized = "|".join(
            [
                self.title.strip(),
                self.location_raw.strip(),
                self.description_raw.strip(),
                self.canonical_url,
            ]
        )
        if self.metadata.get("eligibility"):
            normalized += "|" + json.dumps(self.metadata["eligibility"], sort_keys=True)
        return hashlib.sha256(normalized.encode()).hexdigest()


class ParsedJob(BaseModel):
    raw: RawJob
    seniority: Seniority = Seniority.UNKNOWN
    level: JobLevel = JobLevel.UNKNOWN
    remote_type: RemoteType = RemoteType.UNKNOWN
    job_family: str = "other"
    tech_keywords: set[str] = Field(default_factory=set)
    requires_citizenship: bool = False
    requires_clearance: bool = False
    visa_support: VisaSupport = VisaSupport.UNKNOWN
    employment_type: str = "unknown"
    required_years_min: int | None = None
    degree_required: DegreeLevel = DegreeLevel.NONE
    people_management: bool = False
    ambiguities: set[str] = Field(default_factory=set)


class ResumeProfile(BaseModel):
    source_path: str | None = None
    keywords: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)


class ReviewFlag(BaseModel):
    code: Literal["title_body_level_mismatch", "established_ownership_requirement"]
    evidence: list[str] = Field(max_length=2)


class AttendanceEvidence(BaseModel):
    kind: Literal["weekly", "monthly", "occasional", "unknown"] = "unknown"
    max_days_per_week: int | None = None
    category: Literal["frequent", "limited", "unknown"] = "unknown"
    conflicting: bool = False
    physical_per_diem: bool = False
    evidence: list[str] = Field(default_factory=list)


class LocationEvidence(BaseModel):
    label: str
    status: str = "unresolved"
    source: str = "none"
    states: list[str] = Field(default_factory=list)


class EligibilityAssessment(BaseModel):
    status: Literal["eligible", "review_needed", "unsuitable"] = "eligible"
    hard_reasons: list[str] = Field(default_factory=list)
    review_reasons: list[str] = Field(default_factory=list)
    work_arrangement: RemoteType = RemoteType.UNKNOWN
    attendance: AttendanceEvidence = Field(default_factory=AttendanceEvidence)
    locations: list[LocationEvidence] = Field(default_factory=list)
    review_visible: bool = False


class MatchResult(BaseModel):
    profile: str
    score: float = Field(ge=0, le=1)
    eligible: bool
    tier: str
    reasons: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    # Preserve existing serialized payloads (and notification claims) when there is no advice.
    review_flags: list[ReviewFlag] = Field(default_factory=list, exclude_if=lambda value: not value)
    filtered_reason: str | None = None
    eligibility_reasons: list[str] = Field(default_factory=list, exclude_if=lambda value: not value)
    candidate_eligibility: EligibilityAssessment | None = Field(
        default=None, exclude_if=lambda v: v is None
    )
    discovery_eligible: bool | None = Field(default=None, exclude_if=lambda v: v is None)
    used_llm: bool = False
    fit: float = 0.0
    reach: float = 1.0
    bucket: str = "target"
    level: str | None = None
    required_years_min: int | None = None

    @property
    def notification_eligible(self) -> bool:
        return (
            self.eligible
            and not self.eligibility_reasons
            and (
                self.candidate_eligibility is None
                or (
                    self.candidate_eligibility.status == "eligible"
                    and not self.candidate_eligibility.hard_reasons
                    and not self.candidate_eligibility.review_reasons
                )
            )
        )

    @property
    def needs_eligibility_review(self) -> bool:
        return bool(
            self.discovery_eligible
            and self.candidate_eligibility
            and self.candidate_eligibility.status == "review_needed"
            and not self.candidate_eligibility.hard_reasons
        )


class CandidateProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    years_experience: int = Field(ge=0)
    current_level: JobLevel
    company_scale: CompanyScale | None = None
    has_advanced_degree: DegreeLevel = DegreeLevel.NONE
    people_managed: int = Field(default=0, ge=0)
    max_level_reach: int = Field(default=1, ge=0)

    @field_validator("current_level")
    @classmethod
    def current_level_is_known(cls, value: JobLevel) -> JobLevel:
        if value == JobLevel.UNKNOWN:
            raise ValueError("current_level must not be unknown")
        return value


@dataclass(frozen=True)
class MatchedJob:
    company_name: str
    job: ParsedJob
    result: MatchResult
    first_seen_at: datetime
    is_new: bool
    changed: bool
