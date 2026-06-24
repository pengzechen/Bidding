from __future__ import annotations

import asyncio
import html
import re
from datetime import date, datetime
from typing import AsyncIterator

import structlog
from playwright.async_api import Page

from bidding.adapters.base import AdapterMeta, SiteAdapter
from bidding.adapters.registry import register
from bidding.models.enums import NoticeType
from bidding.models.schema import BidNotice

logger = structlog.get_logger()

_BASE_URL = "https://ec.chng.com.cn"
_HOME_URL = f"{_BASE_URL}/channel/home/?SlJfApAfmEBp=1782316806808#/"
_API_BASE = "/scm-uiaoauth-web/s/business/uiaouth"
_QUERY_API = f"{_API_BASE}/queryAnnouncementByTitle"
_DETAIL_API = f"{_API_BASE}/announcementDetail"

_PAGE_SIZE = 20

_CATEGORY_MAP: dict[NoticeType, list[str]] = {
    NoticeType.BID_ANNOUNCEMENT: ["103"],
    NoticeType.PREQUALIFICATION: ["105"],
    NoticeType.CANDIDATE_PUBLICITY: ["ZBHXRGG"],
    NoticeType.WIN_ANNOUNCEMENT: ["104", "108", "131", "132", "128"],
    NoticeType.NON_BID_ANNOUNCEMENT: ["107", "133", "127"],
}

_TYPE_LABELS = {
    "103": "招标公告",
    "105": "资格预审公告",
    "ZBHXRGG": "中标候选人公示",
    "104": "中标结果公示",
    "108": "询比结果公告",
    "131": "直接采购结果公告",
    "132": "谈判结果公告",
    "128": "竞价结果公告",
    "107": "询比公告",
    "133": "谈判公告",
    "127": "竞价公告",
}


def _ts_to_date(ts: int | None) -> date | None:
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(ts / 1000).date()
    except (ValueError, OSError):
        return None


def _html_to_text(raw: str) -> str:
    text = re.sub(r"<[^>]+>", "", raw)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


@register
class ChngAdapter(SiteAdapter):
    meta = AdapterMeta(
        name="chng",
        display_name="华能集团",
        base_url=_BASE_URL,
        notice_types=[
            NoticeType.BID_ANNOUNCEMENT,
            NoticeType.PREQUALIFICATION,
            NoticeType.CANDIDATE_PUBLICITY,
            NoticeType.WIN_ANNOUNCEMENT,
            NoticeType.NON_BID_ANNOUNCEMENT,
        ],
        requires_login=False,
        rate_limit=1.0,
    )

    async def scrape_list(
        self, page: Page, notice_type: NoticeType
    ) -> AsyncIterator[BidNotice]:
        types = _CATEGORY_MAP.get(notice_type)
        if not types:
            return

        await self._ensure_loaded(page)

        for ann_type in types:
            label = _TYPE_LABELS.get(ann_type, ann_type)
            logger.info("chng.scrape_category", category=label, type=ann_type)

            start = 0
            while True:
                items, total = await self._fetch_list(page, ann_type, start)
                if not items:
                    break

                logger.info(
                    "chng.list_page",
                    category=label,
                    start=start,
                    items=len(items),
                    total=total,
                )

                for item in items:
                    notice = await self._process_item(page, item, notice_type)
                    if notice:
                        yield notice
                    await asyncio.sleep(self.meta.rate_limit)

                start += len(items)
                if start >= total:
                    break

    async def _ensure_loaded(self, page: Page) -> None:
        if "ec.chng.com.cn" in page.url:
            return
        await page.goto(_HOME_URL, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(8)

    async def _fetch_list(
        self, page: Page, ann_type: str, start: int
    ) -> tuple[list[dict], int]:
        result = await page.evaluate(
            """async ([url, body]) => {
                try {
                    const resp = await fetch(url, {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify(body)
                    });
                    return await resp.json();
                } catch(e) { return null; }
            }""",
            [_QUERY_API, {"type": ann_type, "start": start, "limit": _PAGE_SIZE}],
        )
        if not result:
            return [], 0
        total = result.get("totalCount", 0)
        items = result.get("root", [])
        return items, total

    async def _process_item(
        self, page: Page, item: dict, notice_type: NoticeType
    ) -> BidNotice | None:
        title = (item.get("announcementTitle") or "").strip()
        if not title:
            return None

        ann_id = item.get("announcementId")
        biz_info = item.get("businessInfo") or ""
        publish_date = _ts_to_date(item.get("createtime"))

        source_url = f"{_BASE_URL}/channel/home/#/detail?id={ann_id}"

        content = None
        purchaser = None

        detail = await self._fetch_detail(page, ann_id)
        if detail:
            raw_html = detail.get("announcementHtml") or ""
            if raw_html:
                content = _html_to_text(raw_html)
                purchaser = self._extract_field(
                    content, r"招\s*标\s*人|采\s*购\s*人|项目业主"
                )
                if not purchaser:
                    m = re.search(r"招标人为(.+?)[。，,]", content)
                    if m and 3 < len(m.group(1).strip()) < 80:
                        purchaser = m.group(1).strip()

        notice = BidNotice(
            title=title,
            source_site=self.meta.name,
            source_url=source_url,
            notice_type=notice_type,
            notice_id=biz_info or None,
            publish_date=publish_date,
            content=content,
            purchaser=purchaser,
        )

        if content and notice_type == NoticeType.WIN_ANNOUNCEMENT:
            winner = self._extract_field(content, r"中\s*标\s*人|成交供应商")
            if winner:
                notice.winner = winner
            amount = self._extract_field(content, r"中标金额|成交金额|中标价")
            if amount:
                notice.win_amount = amount

        return notice

    async def _fetch_detail(self, page: Page, ann_id: int) -> dict | None:
        result = await page.evaluate(
            """async (url) => {
                try {
                    const resp = await fetch(url);
                    const data = await resp.json();
                    if (data.status && data.data) return data.data.announcement;
                    return null;
                } catch(e) { return null; }
            }""",
            f"{_DETAIL_API}?announcementId={ann_id}",
        )
        return result

    async def scrape_detail(self, page: Page, url: str) -> BidNotice | None:
        m = re.search(r"id=(\d+)", url)
        if not m:
            return None
        ann_id = int(m.group(1))
        await self._ensure_loaded(page)
        detail = await self._fetch_detail(page, ann_id)
        if not detail:
            return None
        raw_html = detail.get("announcementHtml") or ""
        content = _html_to_text(raw_html) if raw_html else None
        return BidNotice(
            title=detail.get("announcementTitle", ""),
            source_site=self.meta.name,
            source_url=url,
            notice_type=NoticeType.BID_ANNOUNCEMENT,
            content=content,
        )

    @staticmethod
    def _extract_field(content: str, field_pattern: str) -> str | None:
        pattern = rf"(?:{field_pattern})[：:]\s*(.+?)(?:[,，。\n]|地\s*址|联\s*系|电\s*话|中标金额|标段)"
        match = re.search(pattern, content)
        if match:
            value = match.group(1).strip()
            if value and 3 < len(value) < 80:
                return value
        pattern2 = rf"(?:{field_pattern})[：:]\s*(.{{3,60}})"
        match2 = re.search(pattern2, content)
        if match2:
            value = match2.group(1).strip().split("地")[0].strip()
            if value and 3 < len(value) < 80:
                return value
        return None
