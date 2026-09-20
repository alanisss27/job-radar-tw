from __future__ import annotations

import html
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from .models import MatchedJob, MatchResult, ParsedJob

REVIEW_LABELS = {
    "title_body_level_mismatch": "職稱與要求層級不一致",
    "established_ownership_requirement": "要求直接 PM/CTM 經驗或完整管理責任",
}


def _summary_score(result: MatchResult) -> str:
    label = "探索相關度 " if result.profile == "clinical-discovery" else ""
    return f"{html.escape(str(result.profile))} {label}{result.score:.0%}"


def _summary_review(result: MatchResult) -> str:
    if result.profile != "clinical-discovery":
        return ""
    labels = "；".join(REVIEW_LABELS[flag.code] for flag in result.review_flags)
    return "\n  待確認：" + (labels or "候選人資格尚未核實")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def source_age_days(posted_at: datetime | None, reference_at: datetime | None = None) -> int | None:
    if posted_at is None:
        return None
    reference = _as_utc(reference_at or datetime.now(UTC))
    posted = _as_utc(posted_at)
    return max(0, int((reference - posted).total_seconds() // 86400))


def render_freshness(
    posted_at: datetime | None,
    first_seen_at: datetime,
    *,
    is_new: bool = False,
    changed: bool = False,
    display_timezone: str = "America/New_York",
) -> str:
    first_seen = _as_utc(first_seen_at)
    first_seen_display_date = first_seen.astimezone(ZoneInfo(display_timezone)).date()
    if posted_at is None:
        suffix = "｜本次新發現" if is_new else "｜內容更新" if changed else ""
        return f"首次發現 {first_seen_display_date.isoformat()}；來源日期未知{suffix}"

    posted = _as_utc(posted_at)
    age = source_age_days(posted, first_seen)
    label = (
        "新發布"
        if age <= 3
        else "近期"
        if age <= 7
        else "一般"
        if age <= 14
        else "較早"
        if age <= 30
        else "較舊"
    )
    suffix = "｜內容更新" if changed and not is_new else ""
    if is_new and age > 3:
        suffix += "（本次新發現）"
    freshness = f"來源 {age} 天前｜{label}{suffix}"
    return f"\u9996\u6b21\u767c\u73fe\uff1a{first_seen_display_date.isoformat()}\uff1b\u4f86\u6e90\u65e5\u671f\uff1a{posted.date().isoformat()}\uff08{freshness}\uff09"


def render_job_message(
    company_name: str,
    job: ParsedJob,
    result: MatchResult,
    first_seen_at: datetime,
    *,
    is_new: bool = False,
    changed: bool = False,
    display_timezone: str = "America/New_York",
) -> str:
    badge = (
        "🪜 延伸挑戰"
        if result.bucket == "stretch"
        else ("🔥 強烈推薦" if result.tier == "strong" else "✅ 符合")
    )
    reasons = "、".join(result.reasons) or "規則配對"
    gaps = "、".join(result.gaps) or "無明顯缺口"
    freshness = render_freshness(
        job.raw.posted_at,
        first_seen_at,
        is_new=is_new,
        changed=changed,
        display_timezone=display_timezone,
    )
    if result.profile == "clinical-discovery":
        evidence = (
            "、".join(
                reason
                for reason in result.reasons
                if not reason.startswith(("location:", "seniority:", "skills:"))
            )
            or "探索規則命中"
        )
        reviews = (
            "\n".join(
                f"{REVIEW_LABELS[flag.code]}：{'；'.join(flag.evidence)}"
                for flag in result.review_flags
            )
            or "未偵測到本次兩類規則提示；不代表符合全部資格"
        )
        checks = "、".join(result.gaps) or "未產生其他規則提示；候選人資格尚未核實"
        return (
            f"🔎 探索職缺 | clinical-discovery | 探索相關度 {result.score:.0%}\n"
            f"<b>{html.escape(company_name)} - {html.escape(job.raw.title)}</b>\n"
            f"📍 {html.escape(job.raw.location_raw or '未提供')}\n"
            f"探索依據：{html.escape(evidence)}\n"
            "地點篩選：通過設定條件；非地理適配評分\n"
            f"待確認要求：{html.escape(reviews)}\n"
            f"自動檢查提示：{html.escape(checks)}\n"
            "適配說明：探索相關度不代表候選人適配；由 Career-ops 深評後自行決定。\n"
            f"新鮮度：{html.escape(freshness)}\n"
            f'<a href="{html.escape(str(job.raw.url), quote=True)}">官方申請連結</a>'
        )
    return (
        f"{badge} | {html.escape(str(result.profile))} | {result.score:.0%}\n"
        f"<b>{html.escape(company_name)} - {html.escape(job.raw.title)}</b>\n"
        f"📍 {html.escape(job.raw.location_raw or '未提供')}\n"
        f"命中：{html.escape(reasons)}\n"
        f"缺口：{html.escape(gaps)}\n"
        f"新鮮度：{html.escape(freshness)}\n"
        f'<a href="{html.escape(str(job.raw.url), quote=True)}">官方申請連結</a>'
    )


def render_failure_alert(company_name: str, failures: int, error: str) -> str:
    return (
        "⚠️ 來源連續失敗\n"
        f"<b>{html.escape(company_name)}</b>\n"
        f"連續失敗：{failures} 次\n"
        f"錯誤：{html.escape(error[:1000])}"
    )


def render_run_summary(
    *,
    run_key: str,
    stats: dict[str, int],
    errors: list[dict[str, str]],
    matched_jobs: list[MatchedJob],
    zero_job_sources: list[str],
    max_matches: int = 8,
    max_reviews: int = 5,
    display_timezone: str = "America/New_York",
) -> str:
    reviews = [item for item in matched_jobs if item.result.needs_eligibility_review]
    unresolved_reviews = [
        item for item in reviews if not item.result.candidate_eligibility.review_visible
    ]
    unresolved_count = len(unresolved_reviews)
    reviews = [item for item in reviews if item.result.candidate_eligibility.review_visible]
    matched_jobs = [item for item in matched_jobs if item.result.notification_eligible]
    fresh_matches = sum(1 for item in matched_jobs if item.is_new)
    lines = [
        f"📊 Job Radar TW｜職缺雷達 Daily Summary - {html.escape(run_key)}",
        f"來源：{stats.get('sources_succeeded', 0)}/{stats.get('sources_attempted', 0)} 成功",
        f"抓到職缺：{stats.get('jobs_fetched', 0)}",
        f"新職缺：{stats.get('jobs_new', 0)}；內容變更：{stats.get('jobs_changed', 0)}；關閉：{stats.get('jobs_closed', 0)}",
        f"符合門檻：{stats.get('matches', 0)}；逐筆通知：{stats.get('notifications', 0)}",
        f"本次新匹配：{fresh_matches}；逐筆候選：{stats.get('immediate_candidates', 0)}；"
        f"限量保留：{stats.get('notifications_suppressed', 0)}；"
        f"待重試：{stats.get('notifications_pending', 0)}",
    ]

    if zero_job_sources:
        lines.append("")
        lines.append("⚠️ 已驗證來源本次抓到 0 筆：")
        lines.extend(f"- {html.escape(name)}" for name in zero_job_sources[:8])

    if errors:
        lines.append("")
        lines.append("⚠️ 來源錯誤：")
        for item in errors[:8]:
            company = html.escape(item.get("company", "unknown"))
            error = html.escape(item.get("error", "")[:160])
            lines.append(f"- {company}: {error}")

    lines.append("")
    if matched_jobs:
        if any(item.result.profile == "clinical-discovery" for item in matched_jobs):
            lines.append(
                "clinical-discovery 的百分比為探索相關度，非候選人適配；待 Career-ops 深評。"
            )
        ordered = sorted(matched_jobs, key=lambda match: match.result.score, reverse=True)
        target_jobs = [item for item in ordered if item.result.bucket == "target"]
        stretch_jobs = [item for item in ordered if item.result.bucket == "stretch"]
        shown = 0
        if target_jobs:
            lines.append(
                "本次探索／配對結果："
                if any(item.result.profile == "clinical-discovery" for item in target_jobs)
                else "本次符合職缺："
            )
        for item in target_jobs[:max_matches]:
            freshness = render_freshness(
                item.job.raw.posted_at,
                item.first_seen_at,
                is_new=item.is_new,
                changed=item.changed,
                display_timezone=display_timezone,
            )
            lines.append(
                f"- {html.escape(item.company_name)} - "
                f'<a href="{html.escape(str(item.job.raw.url), quote=True)}">{html.escape(item.job.raw.title)}</a> '
                f"({_summary_score(item.result)}, "
                f"{html.escape(item.job.raw.location_raw or '未提供')}; {html.escape(freshness)})"
                f"{_summary_review(item.result)}"
            )
            shown += 1
        remaining = max_matches - shown
        if stretch_jobs and remaining > 0:
            lines.append(
                "🪜 延伸探索／配對結果："
                if any(item.result.profile == "clinical-discovery" for item in stretch_jobs)
                else "🪜 延伸職缺（高於你目前職級，可作為挑戰）："
            )
        for item in stretch_jobs[:remaining]:
            freshness = render_freshness(
                item.job.raw.posted_at,
                item.first_seen_at,
                is_new=item.is_new,
                changed=item.changed,
                display_timezone=display_timezone,
            )
            lines.append(
                f"- {html.escape(item.company_name)} - "
                f'<a href="{html.escape(str(item.job.raw.url), quote=True)}">{html.escape(item.job.raw.title)}</a> '
                f"({_summary_score(item.result)}, "
                f"{html.escape(item.job.raw.location_raw or '未提供')}; {html.escape(freshness)})"
                f"{_summary_review(item.result)}"
            )
            shown += 1
        if len(matched_jobs) > shown:
            lines.append(
                f"...另有 {len(matched_jobs) - shown} 筆，請到 Supabase match_results 查看完整清單。"
            )
    else:
        lines.append("本次沒有符合門檻的新/變更職缺。")

    if reviews:
        lines.extend(["", "Commute/eligibility review needed (eligibility NOT confirmed):"])
        ordered_reviews = sorted(reviews, key=lambda item: item.result.score, reverse=True)
        for item in ordered_reviews[:max_reviews]:
            assessment = item.result.candidate_eligibility
            explanation = "; ".join(assessment.review_reasons)[:450]
            lines.append(
                f'- {html.escape(item.company_name)} - '
                f'<a href="{html.escape(str(item.job.raw.url), quote=True)}">{html.escape(item.job.raw.title)}</a> '
                f'({html.escape(item.job.raw.location_raw or "Location unresolved")}; '
                f'{assessment.work_arrangement.value}; attendance: {assessment.attendance.category}) '
                f'{html.escape(explanation)}'
            )
        if len(reviews) > max_reviews:
            lines.append(f"{len(reviews) - max_reviews} additional review records retained in match history.")
    if unresolved_count:
        lines.append(f"Unresolved eligibility/location: {unresolved_count} records retained for inspection; not confirmed local opportunities.")
        lines.append("Retained unresolved records (not confirmed local opportunities):")
        for item in sorted(unresolved_reviews, key=lambda match: match.result.score, reverse=True):
            assessment = item.result.candidate_eligibility
            company = html.escape(item.company_name or "Company unavailable")
            title = html.escape(item.job.raw.title or "Title unavailable")
            location = html.escape(item.job.raw.location_raw or "Location unavailable")
            reasons = "; ".join(assessment.review_reasons) or "Eligibility/location unresolved"
            line = f"- {company} - {title} ({location}) — {html.escape(reasons[:450])}"
            url = str(item.job.raw.url) if item.job.raw.url else ""
            if url:
                line += f' <a href="{html.escape(url, quote=True)}">Official posting</a>'
            lines.append(line)

    if not errors and not zero_job_sources:
        lines.append("")
        lines.append("系統狀態：正常")

    return "\n".join(lines)


def split_message(text: str, limit: int = 3900) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        if len(current) + len(line) > limit:
            if current:
                chunks.append(current.rstrip())
            while len(line) > limit:
                chunks.append(line[:limit])
                line = line[limit:]
            current = line
        else:
            current += line
    if current:
        chunks.append(current.rstrip())
    return chunks


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str, client: httpx.AsyncClient):
        self.url = f"https://api.telegram.org/bot{token}/sendMessage"
        self.chat_id = chat_id
        self.client = client

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=5),
        retry=retry_if_exception_type(httpx.HTTPError),
        reraise=True,
    )
    async def _send_chunk(self, chunk: str) -> None:
        response = await self.client.post(
            self.url,
            json={
                "chat_id": self.chat_id,
                "text": chunk,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
        )
        response.raise_for_status()

    async def send(self, text: str) -> None:
        for chunk in split_message(text):
            try:
                await self._send_chunk(chunk)
            except httpx.HTTPStatusError as exc:
                raise RuntimeError(
                    f"Telegram API returned HTTP {exc.response.status_code}"
                ) from None
            except httpx.HTTPError:
                raise RuntimeError("Telegram API request failed") from None
