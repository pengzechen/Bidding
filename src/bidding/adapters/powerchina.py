from __future__ import annotations

import asyncio
import re
import urllib.parse
from datetime import date
from typing import AsyncIterator

import structlog
from playwright.async_api import Page

from bidding.adapters.base import AdapterMeta, SiteAdapter
from bidding.adapters.registry import register
from bidding.models.enums import NoticeType, ProjectCategory
from bidding.models.schema import BidNotice
from bidding.utils.pdf import download_and_extract_pdf

logger = structlog.get_logger()

_BASE_URL = "https://ec.powerchina.cn"
_LIST_URL = f"{_BASE_URL}/zgdjcms/category/bulletinList.html"
_PDF_API = f"{_BASE_URL}/zgdjdzzb/cgUploadController.do?openFileById&id="

_CATEGORY_MAP: dict[NoticeType, int] = {
    NoticeType.BID_ANNOUNCEMENT: 2,
    NoticeType.CHANGE_ANNOUNCEMENT: 3,
    NoticeType.WIN_ANNOUNCEMENT: 4,
    NoticeType.TERMINATION: 5,
}

_PROJECT_CATEGORY_MAP: dict[str, ProjectCategory] = {
    "物资类": ProjectCategory.GOODS,
    "设备类": ProjectCategory.GOODS,
    "工程类": ProjectCategory.ENGINEERING,
    "服务类": ProjectCategory.SERVICE,
}

_PAGE_SIZE = 10

_RE_PDF_ID = re.compile(r"openFileById[%&]26id[%&]3[Dd]([a-f0-9]+)|openFileById&id=([a-f0-9]+)")
_RE_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})")
_RE_CATEGORY_TAG = re.compile(r"【([^】]+)】")


def _parse_date(s: str) -> date | None:
    m = _RE_DATE.search(s)
    if m:
        try:
            return date.fromisoformat(m.group(1))
        except ValueError:
            pass
    return None


def _extract_project_category(title: str) -> ProjectCategory | None:
    m = _RE_CATEGORY_TAG.search(title)
    if m:
        return _PROJECT_CATEGORY_MAP.get(m.group(1))
    return None


@register
class PowerchinaAdapter(SiteAdapter):
    meta = AdapterMeta(
        name="powerchina",
        display_name="中国电建",
        base_url=_BASE_URL,
        notice_types=[
            NoticeType.BID_ANNOUNCEMENT,
            NoticeType.CHANGE_ANNOUNCEMENT,
            NoticeType.WIN_ANNOUNCEMENT,
            NoticeType.TERMINATION,
        ],
        requires_login=False,
        rate_limit=1.5,
    )

    async def scrape_list(
        self, page: Page, notice_type: NoticeType
    ) -> AsyncIterator[BidNotice]:
        category_id = _CATEGORY_MAP.get(notice_type)
        if category_id is None:
            return

        await page.goto(
            f"{_LIST_URL}?categoryId={category_id}&page=1",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        await asyncio.sleep(3)

        total_pages = await self._get_total_pages(page)
        logger.info(
            "powerchina.start_category",
            notice_type=notice_type.value,
            total_pages=total_pages,
        )

        page_no = 1
        while page_no <= total_pages:
            if page_no > 1:
                await page.goto(
                    f"{_LIST_URL}?categoryId={category_id}&page={page_no}",
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
                await asyncio.sleep(2)

            items = await self._parse_list_page(page)
            logger.info(
                "powerchina.list_page",
                page=page_no,
                items=len(items),
            )

            if not items:
                break

            for item in items:
                notice = await self._process_item(page, item, notice_type)
                if notice:
                    yield notice
                await asyncio.sleep(self.meta.rate_limit)

            page_no += 1

    async def _get_total_pages(self, page: Page) -> int:
        label = await page.evaluate("""() => {
            const el = document.querySelector('.pages label');
            return el ? el.textContent.trim() : '';
        }""")
        m = re.match(r"(\d+)/(\d+)页", label)
        if m:
            return int(m.group(2))
        return 1

    async def _parse_list_page(self, page: Page) -> list[dict]:
        return await page.evaluate("""() => {
            const results = [];
            document.querySelectorAll('li[style*="padding"]').forEach(li => {
                const a = li.querySelector('a');
                if (!a) return;
                const hasOnclick = !!a.getAttribute('onclick');
                const href = hasOnclick ? '' : (a.getAttribute('href') || '');
                const title = a.title || a.textContent.trim();
                const dateDiv = li.querySelector('.newsDate div');
                const dateStr = dateDiv ? dateDiv.textContent.trim() : '';
                const hidden = li.querySelector('input[type=hidden]');
                const hiddenVal = hidden ? hidden.value : '';
                results.push({title, href, dateStr, hasOnclick, hiddenVal});
            });
            return results;
        }""")

    async def _process_item(
        self, page: Page, item: dict, notice_type: NoticeType
    ) -> BidNotice | None:
        title = item.get("title", "").strip()
        if not title:
            return None

        href = item.get("href", "")
        if href.startswith("//"):
            href = "https:" + href
        elif href.startswith("/"):
            href = _BASE_URL + href

        has_onclick = item.get("hasOnclick", False)
        date_str = item.get("dateStr", "")
        publish_date = _parse_date(date_str)
        project_category = _extract_project_category(title)

        source_url = href if href else _BASE_URL
        content = None
        pdf_path = None
        attachments = []

        if href and not has_onclick:
            pdf_id, pdf_text, pdf_filename = await self._fetch_detail_pdf(page, href)
            if pdf_id:
                attachments.append(f"{_PDF_API}{pdf_id}")
            if pdf_text:
                content = pdf_text
            if pdf_filename:
                pdf_path = pdf_filename

        notice = BidNotice(
            title=title,
            source_site=self.meta.name,
            source_url=source_url,
            notice_type=notice_type,
            publish_date=publish_date,
            project_category=project_category,
            content=content,
            pdf_path=pdf_path,
            attachments=attachments,
        )

        if content:
            purchaser = self._extract_field(
                content, r"采\s*购\s*人\s*名?\s*称?|招\s*标\s*人|项目业主|建设单位"
            )
            if purchaser:
                notice.purchaser = purchaser

        return notice

    async def _fetch_detail_pdf(
        self, page: Page, detail_url: str
    ) -> tuple[str | None, str | None, str | None]:
        detail_page = await page.context.new_page()
        try:
            resp = await detail_page.goto(
                detail_url, wait_until="domcontentloaded", timeout=20000
            )
            if not resp or resp.status != 200:
                return None, None, None
            await asyncio.sleep(1)

            iframe_src = await detail_page.evaluate("""() => {
                const iframe = document.querySelector('#pdfContainer');
                return iframe ? iframe.src : '';
            }""")

            if not iframe_src:
                return None, None, None

            decoded = urllib.parse.unquote(iframe_src)
            m = _RE_PDF_ID.search(decoded)
            if not m:
                m = _RE_PDF_ID.search(iframe_src)
            if not m:
                return None, None, None

            pdf_id = m.group(1) or m.group(2)
            pdf_url = f"{_PDF_API}{pdf_id}"

            pdf_filename, pdf_text = await download_and_extract_pdf(pdf_url)
            return pdf_id, pdf_text, pdf_filename

        except Exception:
            logger.debug("powerchina.detail_error", url=detail_url[:80])
            return None, None, None
        finally:
            await detail_page.close()

    async def scrape_detail(self, page: Page, url: str) -> BidNotice | None:
        pdf_id, pdf_text, pdf_filename = await self._fetch_detail_pdf(page, url)
        if not pdf_text:
            return None
        return BidNotice(
            title="",
            source_site=self.meta.name,
            source_url=url,
            notice_type=NoticeType.BID_ANNOUNCEMENT,
            content=pdf_text,
            pdf_path=pdf_filename,
            attachments=[f"{_PDF_API}{pdf_id}"] if pdf_id else [],
        )

    @staticmethod
    def _extract_field(content: str, field_pattern: str) -> str | None:
        pattern = rf"(?:{field_pattern})[：:]\s*(.+)"
        match = re.search(pattern, content)
        if match:
            value = match.group(1).strip().split("\n")[0].strip()
            if value and len(value) < 100:
                return value
        return None
