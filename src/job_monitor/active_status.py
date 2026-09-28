"""Small shared, tri-state check for otherwise actionable postings."""

from __future__ import annotations

import re
from enum import StrEnum
from html import unescape
from typing import Any

import httpx
from bs4 import BeautifulSoup

from .models import RawJob


class ActiveStatus(StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    UNKNOWN = "unknown"


_CLOSED = re.compile(
    r"\b(?:job posting|position|job)\s+(?:is|has been|was)\s+"
    r"(?:no longer active|no longer available|filled|closed|expired)\b|"
    r"\bapplications?\s+(?:are|is)\s+(?:closed|no longer being accepted|no longer accepted)\b|"
    r"\bno longer accepting applications\b",
    re.I,
)
_APPLY = re.compile(r"^(?:apply(?: now)?|start application|submit application)$", re.I)
_ACCEPTING = re.compile(
    r"\b(?:currently accepting applications|applications are now being accepted|"
    r"applications are now open|now accepting applications)\b",
    re.I,
)


def structured_status(metadata: dict[str, Any]) -> ActiveStatus:
    """Read only explicit applyability fields; page URLs and posting text do not count."""
    recorded = metadata.get("active_status")
    if isinstance(recorded, str) and recorded in ActiveStatus._value2member_map_:
        return ActiveStatus(recorded)
    values: list[tuple[str, Any]] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                normalized = key.casefold().replace("_", "")
                if normalized in {"posted", "canapply"}:
                    values.append((normalized, child))
                if normalized in {"applyurl", "applicationurl"} and isinstance(child, str):
                    values.append(("applyurl", child))
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(metadata)
    if any(key in {"posted", "canapply"} and value is False for key, value in values):
        return ActiveStatus.INACTIVE
    if any(key == "canapply" and value is True for key, value in values):
        return ActiveStatus.ACTIVE
    if any(key == "applyurl" and value.strip().startswith(("https://", "http://", "/"))
           for key, value in values):
        return ActiveStatus.ACTIVE
    return ActiveStatus.UNKNOWN


def page_status(content: str) -> ActiveStatus:
    soup = BeautifulSoup(content, "html.parser")
    text = unescape(soup.get_text(" ", strip=True))
    if _CLOSED.search(text):
        return ActiveStatus.INACTIVE
    if _ACCEPTING.search(text):
        return ActiveStatus.ACTIVE
    for node in soup.select("a[href], button, input[type=submit]"):
        label = " ".join(
            [str(node.get_text(" ", strip=True)), str(node.get("aria-label", "")),
             str(node.get("value", ""))]
        ).strip()
        if _APPLY.fullmatch(label):
            if node.name in {"button", "input"} and node.has_attr("disabled"):
                return ActiveStatus.INACTIVE
            href = (node.get("href") or "").strip()
            if node.name == "a" and href and href != "#" and not href.casefold().startswith("javascript:"):
                return ActiveStatus.ACTIVE
            if node.name in {"button", "input"}:
                return ActiveStatus.ACTIVE
    return ActiveStatus.UNKNOWN


async def verify_active_status(
    raw: RawJob, client: httpx.AsyncClient, cache: dict[str, ActiveStatus]
) -> ActiveStatus:
    status = structured_status(raw.metadata)
    if status is not ActiveStatus.UNKNOWN:
        raw.metadata["active_status"] = status.value
        return status
    if raw.metadata.get("active_status_page_checked") is True:
        raw.metadata["active_status"] = ActiveStatus.UNKNOWN.value
        return ActiveStatus.UNKNOWN
    url = raw.canonical_url
    if url not in cache:
        try:
            response = await client.get(url, timeout=10, follow_redirects=True)
            if response.status_code in {404, 410}:
                cache[url] = ActiveStatus.INACTIVE
            elif response.is_success:
                cache[url] = page_status(response.text)
            else:
                cache[url] = ActiveStatus.UNKNOWN
        except (httpx.HTTPError, ValueError):
            cache[url] = ActiveStatus.UNKNOWN
    status = cache[url]
    raw.metadata["active_status"] = status.value
    return status


async def verify_if_actionable(
    actionable: bool,
    raw: RawJob,
    client: httpx.AsyncClient,
    cache: dict[str, ActiveStatus],
) -> ActiveStatus | None:
    """Do no status I/O before all existing discovery/score gates pass."""
    if not actionable:
        return None
    return await verify_active_status(raw, client, cache)


def notification_status_allows(status: ActiveStatus) -> bool:
    return status is not ActiveStatus.INACTIVE


def manual_verification_label(status: ActiveStatus) -> str:
    return (
        "Active status not confirmed — manual verification needed"
        if status is ActiveStatus.UNKNOWN
        else ""
    )
