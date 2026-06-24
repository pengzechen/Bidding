from __future__ import annotations

import asyncio
import html
import json
import re
from datetime import date, datetime
from typing import AsyncIterator

import structlog
from playwright.async_api import Page

from bidding.adapters.base import AdapterMeta, SiteAdapter
from bidding.adapters.registry import register
from bidding.models.enums import NoticeType, ProjectCategory
from bidding.models.schema import BidNotice

logger = structlog.get_logger()

_BASE_URL = "https://www.hebztb.com"
_API = f"{_BASE_URL}/zbxhcms/api/directive/contentList"
_PAGE_SIZE = 20

_CATEGORY_MAP: dict[NoticeType, list[int]] = {
    NoticeType.BID_ANNOUNCEMENT: [88],
    NoticeType.CHANGE_ANNOUNCEMENT: [89],
    NoticeType.WIN_ANNOUNCEMENT: [90],
    NoticeType.TERMINATION: [91],
}

_CATEGORY_LABELS = {
    88: "招标公告",
    89: "变更/终止公告",
    90: "结果公示",
    91: "废标公告",
}

_INDUSTRY_MAP: dict[str, ProjectCategory] = {
    "工程": ProjectCategory.ENGINEERING,
    "货物": ProjectCategory.GOODS,
    "服务": ProjectCategory.SERVICE,
}


def _ts_to_date(ts: int | None) -> date | None:
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(ts / 1000).date()
    except (ValueError, OSError):
        return None


def _parse_datetime(s: str | None) -> datetime | None:
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s.strip(), fmt)
        except ValueError:
            continue
    return None


def _html_to_text(raw: str) -> str:
    text = re.sub(r"<[^>]+>", "", raw)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _parse_blursearch_json(blursearch: str) -> dict:
    if not blursearch:
        return {}
    idx = blursearch.find("{")
    if idx < 0:
        return {}
    json_str = blursearch[idx:]
    last = json_str.rfind("}")
    if last < 0:
        return {}
    json_str = json_str[: last + 1]
    try:
        return json.loads(json_str)
    except (json.JSONDecodeError, ValueError):
        return {}


@register
class HebztbAdapter(SiteAdapter):
    meta = AdapterMeta(
        name="hebztb",
        display_name="招标通",
        base_url=_BASE_URL,
        notice_types=[
            NoticeType.BID_ANNOUNCEMENT,
            NoticeType.CHANGE_ANNOUNCEMENT,
            NoticeType.WIN_ANNOUNCEMENT,
            NoticeType.TERMINATION,
        ],
        requires_login=False,
        rate_limit=1.0,
    )

    async def scrape_list(
        self, page: Page, notice_type: NoticeType
    ) -> AsyncIterator[BidNotice]:
        categories = _CATEGORY_MAP.get(notice_type)
        if not categories:
            return

        await self._ensure_loaded(page)

        for cat_id in categories:
            label = _CATEGORY_LABELS.get(cat_id, str(cat_id))
            logger.info("hebztb.scrape_category", category=label, id=cat_id)

            page_idx = 1
            while True:
                items, total = await self._fetch_list(page, cat_id, page_idx)
                if not items:
                    break

                logger.info(
                    "hebztb.list_page",
                    category=label,
                    page=page_idx,
                    items=len(items),
                    total=total,
                )

                for item in items:
                    notice = await self._process_item(page, item, notice_type)
                    if notice:
                        yield notice
                    await asyncio.sleep(self.meta.rate_limit)

                if page_idx * _PAGE_SIZE >= total:
                    break
                page_idx += 1

    async def _ensure_loaded(self, page: Page) -> None:
        if "hebztb.com" in page.url:
            return
        await page.goto(
            f"{_BASE_URL}/zbxhcms/category/bulletinList.html?categoryId=88",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        await asyncio.sleep(3)

    async def _fetch_list(
        self, page: Page, cat_id: int, page_idx: int
    ) -> tuple[list[dict], int]:
        url = (
            f"{_API}?categoryId={cat_id}"
            f"&pageIndex={page_idx}&count={_PAGE_SIZE}"
            f"&blurSearch=&startPublishDate=2020-01-01"
            f"&area=&signDate=&precise="
        )
        result = await page.evaluate(
            """async (url) => {
                try {
                    const resp = await fetch(url);
                    return await resp.json();
                } catch(e) { return null; }
            }""",
            url,
        )
        if not result:
            return [], 0
        page_data = result.get("page", {})
        return page_data.get("list", []), page_data.get("totalCount", 0)

    async def _process_item(
        self, page: Page, item: dict, notice_type: NoticeType
    ) -> BidNotice | None:
        title = (item.get("title") or "").strip()
        title = html.unescape(title)
        if not title:
            return None

        detail_url = item.get("url") or ""
        source_url = detail_url or _BASE_URL
        publish_date = _ts_to_date(item.get("publishDate"))

        meta = _parse_blursearch_json(item.get("blurSearch") or "")

        notice_id = meta.get("tenderno") or None
        purchaser = meta.get("buyersName") or None
        agency = meta.get("agentName") or None
        industry = meta.get("industryName") or item.get("precise") or ""
        project_category = _INDUSTRY_MAP.get(industry)
        area = meta.get("projectAreaName") or item.get("area") or None

        deadline = _parse_datetime(meta.get("submitEndDate"))
        if not deadline:
            deadline = _parse_datetime(meta.get("openBidStartDate"))

        content = None
        winner = None
        win_amount = None

        if detail_url:
            content = await self._fetch_detail_content(page, detail_url)

        if content and notice_type == NoticeType.WIN_ANNOUNCEMENT:
            winner = self._extract_field(
                content, r"中\s*标\s*人|成交供应商|中标单位"
            )
            win_amount = self._extract_field(
                content, r"中标价格|中标金额|成交金额|中标价"
            )
            if not winner:
                winner, win_amount = self._extract_table_winner(content)

        notice = BidNotice(
            title=title,
            source_site=self.meta.name,
            source_url=source_url,
            notice_type=notice_type,
            notice_id=notice_id,
            publish_date=publish_date,
            deadline=deadline,
            project_category=project_category,
            project_location=area,
            content=content,
            purchaser=purchaser,
            agency=agency,
            winner=winner,
            win_amount=win_amount,
        )
        return notice

    async def _fetch_detail_content(self, page: Page, url: str) -> str | None:
        detail_page = await page.context.new_page()
        try:
            resp = await detail_page.goto(
                url, wait_until="networkidle", timeout=20000
            )
            if not resp or resp.status != 200:
                return None
            await asyncio.sleep(2)

            content_text = await detail_page.evaluate("""() => {
                const zbText = document.querySelector('.ZhaobiaoText');
                if (zbText) return zbText.textContent.trim();
                const block = document.querySelector('.block');
                if (block) return block.textContent.trim();
                return '';
            }""")

            if content_text and len(content_text) > 50:
                return content_text
            return None
        except Exception:
            logger.debug("hebztb.detail_error", url=url[:80])
            return None
        finally:
            await detail_page.close()

    async def scrape_detail(self, page: Page, url: str) -> BidNotice | None:
        await self._ensure_loaded(page)
        content = await self._fetch_detail_content(page, url)
        if not content:
            return None
        return BidNotice(
            title="",
            source_site=self.meta.name,
            source_url=url,
            notice_type=NoticeType.BID_ANNOUNCEMENT,
            content=content,
        )

    @staticmethod
    def _extract_table_winner(content: str) -> tuple[str | None, str | None]:
        import re as _re
        m = _re.search(
            r"成交单位名称.*?[A-Z0-9]{18}([一-龥（）()]{4,60}?)(\d[\d,.]+(?:万?元)?|《|符合|质量)",
            content,
        )
        if m:
            winner = m.group(1).strip()
            amount = m.group(2).strip() if m.group(2)[0].isdigit() else None
            return winner, amount
        m2 = _re.search(r"中标候选人.*?第一名\s*(\S{4,60})", content)
        if m2:
            return m2.group(1).strip(), None
        return None, None

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
