from __future__ import annotations

import re
from typing import AsyncIterator

import structlog
from playwright.async_api import Page

from bidding.adapters.base import AdapterMeta, SiteAdapter
from bidding.adapters.registry import register
from bidding.models.enums import NoticeType
from bidding.models.schema import BidNotice

logger = structlog.get_logger()

CATEGORY_CODES = {
    NoticeType.BID_ANNOUNCEMENT: "001002",
    NoticeType.PREQUALIFICATION: "001001",
    NoticeType.NON_BID_ANNOUNCEMENT: "001003",
    NoticeType.CHANGE_ANNOUNCEMENT: "001004",
    NoticeType.CANDIDATE_PUBLICITY: "001005",
    NoticeType.WIN_ANNOUNCEMENT: "001006",
    NoticeType.TERMINATION: "001007",
}


@register
class ChnEnergyAdapter(SiteAdapter):
    meta = AdapterMeta(
        name="chnenergy",
        display_name="国能e招",
        base_url="https://www.chnenergybidding.com.cn",
        notice_types=[
            NoticeType.BID_ANNOUNCEMENT,
            NoticeType.WIN_ANNOUNCEMENT,
            NoticeType.CHANGE_ANNOUNCEMENT,
        ],
        requires_login=False,
        rate_limit=2.0,
    )

    async def get_list_url(self, notice_type: NoticeType, page_num: int = 1) -> str:
        code = CATEGORY_CODES[notice_type]
        base = f"{self.meta.base_url}/bidweb/001/{code}"
        if page_num == 1:
            return f"{base}/moreinfo.html"
        return f"{base}/{page_num}.html"

    async def scrape_list(
        self, page: Page, notice_type: NoticeType
    ) -> AsyncIterator[BidNotice]:
        url = await self.get_list_url(notice_type)
        await page.goto(url, wait_until="networkidle", timeout=30000)

        # Wait for list to render
        await page.wait_for_selector("li a", timeout=15000)

        # Parse total pages from "1/9320" text
        page_text = await page.text_content("body")
        m = re.search(r"(\d+)/(\d+)", page_text or "")
        if m:
            self._pagination.current_page = int(m.group(1))
            self._pagination.total_pages = int(m.group(2))
            logger.info(
                "chnenergy.pages",
                current=self._pagination.current_page,
                total=self._pagination.total_pages,
            )

        detail_pattern = re.compile(r"/\d{8}/[0-9a-f-]+\.html")

        while True:
            items = await page.query_selector_all("li")
            for item in items:
                links = await item.query_selector_all("a")
                detail_links = []
                for lnk in links:
                    h = await lnk.get_attribute("href")
                    if h and detail_pattern.search(h):
                        detail_links.append(lnk)

                if not detail_links:
                    continue

                href = await detail_links[0].get_attribute("href")

                # Extract notice_id and title from the links
                notice_id_text = None
                if len(detail_links) >= 2:
                    first_text = (await detail_links[0].inner_text()).strip()
                    title = (await detail_links[1].inner_text()).strip()
                    if re.match(r"^CE[A-Z]{2}\d{6,}", first_text):
                        notice_id_text = first_text
                    elif not title:
                        title = first_text
                else:
                    title = (await detail_links[0].inner_text()).strip()

                if not title or not href:
                    continue

                # Extract date
                li_text = await item.text_content()
                date_match = re.search(r"(\d{4}-\d{2}-\d{2})", li_text or "")
                publish_date = None
                if date_match:
                    from datetime import date as date_type

                    try:
                        parts = date_match.group(1).split("-")
                        publish_date = date_type(
                            int(parts[0]), int(parts[1]), int(parts[2])
                        )
                    except (ValueError, IndexError):
                        pass

                full_url = self._resolve_url(href)

                notice = BidNotice(
                    title=title,
                    source_site=self.meta.name,
                    source_url=full_url,
                    notice_type=notice_type,
                    notice_id=notice_id_text,
                    publish_date=publish_date,
                )
                yield notice

            # Next page
            next_link = await page.query_selector('a:has-text("下页")')
            if not next_link:
                break
            next_href = await next_link.get_attribute("href")
            if not next_href:
                break

            self._pagination.current_page += 1
            logger.info(
                "chnenergy.next_page", page=self._pagination.current_page
            )
            await page.goto(
                self._resolve_url(next_href),
                wait_until="networkidle",
                timeout=30000,
            )
            await page.wait_for_selector("li a", timeout=15000)

    async def scrape_detail(self, page: Page, url: str) -> BidNotice | None:
        detail_page = await page.context.new_page()
        try:
            await detail_page.goto(url, wait_until="networkidle", timeout=30000)

            # Wait for content area
            content_el = await detail_page.query_selector(
                ".article-content, .detail-content, .content, #content, .Main_content"
            )
            content = ""
            content_html = ""
            if content_el:
                content = (await content_el.inner_text()).strip()
                content_html = (await content_el.inner_html()).strip()
            else:
                content = (await detail_page.inner_text("body")).strip()

            return BidNotice(
                title="",
                source_site=self.meta.name,
                source_url=url,
                notice_type=NoticeType.BID_ANNOUNCEMENT,
                content=content,
                content_html=content_html,
            )
        except Exception:
            logger.exception("chnenergy.detail_error", url=url)
            return None
        finally:
            await detail_page.close()
