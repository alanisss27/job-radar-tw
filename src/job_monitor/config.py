from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .models import CandidateProfile, CompanyConfig, Seniority


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str | None = None
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    openai_api_key: str | None = None
    openai_model: str | None = None
    llm_enabled: bool = False
    resume_path: Path | None = None
    resume_text: SecretStr | None = None
    visa_sponsorship_required: bool = False
    immediate_notification_min_score: float = Field(default=0.82, ge=0, le=1)
    immediate_notification_max_source_age_days: int = Field(default=21, ge=0)
    immediate_notification_max_per_run: int = Field(default=5, ge=0)
    daily_summary_max_matches: int = Field(default=15, ge=1)
    daily_summary_max_reviews: int = Field(default=5, ge=0)
    companies_config: Path = Path("config/companies.yml")
    profiles_config: Path = Path("config/profiles.yml")
    preferences_config: Path = Path("config/preferences.yml")
    candidate_config: Path = Path("config/candidate.yml")
    source_candidates_config: Path = Path("config/source_candidates.yml")
    monitor_timezone: str = "America/New_York"
    monitor_hour: int = Field(default=20, ge=0, le=23)
    schedule_grace_hours: int = Field(default=16, ge=1, le=24)
    request_timeout_seconds: float = 20
    max_concurrency: int = 5

    @field_validator("resume_path", mode="before")
    @classmethod
    def blank_resume_path_is_unset(cls, value: object) -> object:
        if value == "":
            return None
        return value

    @field_validator("resume_text", mode="before")
    @classmethod
    def blank_resume_text_is_unset(cls, value: object) -> object:
        if value == "":
            return None
        return value

    @field_validator("monitor_timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown IANA timezone: {value}") from exc
        return value

    @model_validator(mode="after")
    def validate_llm(self) -> "Settings":
        if self.llm_enabled and not (self.openai_api_key and self.openai_model):
            raise ValueError("LLM_ENABLED requires OPENAI_API_KEY and OPENAI_MODEL")
        if self.resume_path and self.resume_text:
            raise ValueError("Set only one of RESUME_PATH or RESUME_TEXT")
        return self


class ProfileConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    version: str = Field(default="1", min_length=1, max_length=30)
    threshold: float = Field(default=0.60, ge=0, le=1)
    strong_threshold: float = Field(default=0.78, ge=0, le=1)
    weights: dict[str, float]
    title_terms: list[str]
    contextual_title_families: dict[str, list[str]] = Field(default_factory=dict)
    allow_other_job_family: bool = False
    responsibility_terms: list[str] = Field(default_factory=list)
    domain_terms: list[str]
    skills: list[str]
    responsibility_title_terms: list[str] = Field(default_factory=list)
    responsibility_title_exclude_terms: list[str] = Field(default_factory=list)
    responsibility_min_hits: int = Field(default=1, ge=1)
    responsibility_requires_domain: bool = False
    gap_terms: dict[str, list[str]] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,39}", value):
            raise ValueError(
                "profile name must be 1-40 ASCII letters, numbers, hyphens, or underscores"
            )
        return value

    @model_validator(mode="after")
    def weights_total_one(self) -> "ProfileConfig":
        unsupported = set(self.weights) - {
            "title",
            "domain",
            "skills",
            "location",
            "seniority",
        }
        if unsupported:
            raise ValueError(f"unsupported weights: {sorted(unsupported)}")
        if any(weight < 0 for weight in self.weights.values()):
            raise ValueError("weights must not be negative")
        if abs(sum(self.weights.values()) - 1.0) > 0.001:
            raise ValueError(f"weights for {self.name} must sum to 1")
        if self.strong_threshold < self.threshold:
            raise ValueError("strong_threshold must be greater than or equal to threshold")
        return self


class CommutingPolicy(BaseModel):
    """State screening is not a claim that every city in a state is commutable."""

    model_config = ConfigDict(extra="forbid")
    onsite_states: set[str] | None = None
    # Additional states to retain for review for limited/occasional attendance.
    limited_attendance_states: set[str] = Field(default_factory=set)
    search_area: str | None = None  # Descriptive only; never interpreted as geography.

    @field_validator("onsite_states", "limited_attendance_states")
    @classmethod
    def valid_states(cls, values: set[str] | None) -> set[str] | None:
        if values is None:
            return None
        from .eligibility import normalize_state

        result = {normalize_state(value) for value in values}
        if None in result:
            raise ValueError("commuting states must be US state names or abbreviations")
        return result


class CandidateEligibilityConfig(BaseModel):
    """Private candidate facts, independent of career-level scoring."""

    model_config = ConfigDict(extra="forbid")
    residence_state: str | None = None
    held_professional_licenses: set[str] | None = None
    commuting_policy: CommutingPolicy = Field(default_factory=CommutingPolicy)

    @field_validator("residence_state")
    @classmethod
    def valid_residence(cls, value: str | None) -> str | None:
        if value is None:
            return None
        from .eligibility import normalize_state

        normalized = normalize_state(value)
        if normalized is None:
            raise ValueError("residence_state must be a US state name or abbreviation")
        return normalized

    @field_validator("held_professional_licenses")
    @classmethod
    def clean_licenses(cls, values: set[str] | None) -> set[str] | None:
        if values is None:
            return None
        from .eligibility import license_names

        normalized = set()
        for value in values:
            names = license_names(value)
            if len(names) != 1:
                raise ValueError(f"Unrecognized or ambiguous professional license: {value}")
            normalized.update(names)
        return normalized


class SearchPreferences(BaseModel):
    model_config = ConfigDict(extra="forbid")

    location_terms: list[str] = Field(default_factory=list)
    include_remote: bool = True
    candidate_eligibility: CandidateEligibilityConfig | None = None
    exclude_citizenship_required: bool = True
    exclude_clearance_required: bool = True
    excluded_seniorities: set[Seniority] = Field(
        default_factory=lambda: {Seniority.ENTRY, Seniority.DIRECTOR}
    )

    @field_validator("location_terms")
    @classmethod
    def clean_location_terms(cls, values: list[str]) -> list[str]:
        cleaned = [value.strip() for value in values if value.strip()]
        if len({value.casefold() for value in cleaned}) != len(cleaned):
            raise ValueError("location_terms must be unique")
        return cleaned


def _read_yaml(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def load_companies(path: Path) -> list[CompanyConfig]:
    payload = _read_yaml(path) or {}
    if not isinstance(payload, dict):
        raise ValueError("companies config must be a YAML mapping")
    companies = [CompanyConfig.model_validate(item) for item in payload.get("companies", [])]
    slugs = [company.slug for company in companies]
    if len(slugs) != len(set(slugs)):
        raise ValueError("company slugs must be unique")
    return companies


def load_profiles(path: Path) -> dict[str, ProfileConfig]:
    payload = _read_yaml(path) or {}
    if not isinstance(payload, dict):
        raise ValueError("profiles config must be a YAML mapping")
    profiles = [ProfileConfig.model_validate(item) for item in payload.get("profiles", [])]
    result = {profile.name: profile for profile in profiles}
    if not result:
        raise ValueError("at least one matching profile is required")
    if len(result) != len(profiles):
        raise ValueError("profile names must be unique")
    return result


def load_preferences(path: Path) -> SearchPreferences:
    payload = _read_yaml(path) or {}
    if not isinstance(payload, dict):
        raise ValueError("preferences config must be a YAML mapping")
    return SearchPreferences.model_validate(payload.get("preferences", payload))


def load_candidate(path: Path) -> CandidateProfile | None:
    if not path.exists():
        return None
    payload = _read_yaml(path) or {}
    if not isinstance(payload, dict):
        raise ValueError("candidate config must be a YAML mapping")
    candidate = payload.get("candidate", payload)
    if candidate is None:
        return None
    return CandidateProfile.model_validate(candidate)
