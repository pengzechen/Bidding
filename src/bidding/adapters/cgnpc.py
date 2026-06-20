from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import AsyncIterator

import structlog
from playwright.async_api import Page

from bidding.adapters.base import AdapterMeta, SiteAdapter
from bidding.adapters.registry import register
from bidding.models.enums import NoticeType
from bidding.models.schema import BidNotice

logger = structlog.get_logger()

_BASE_URL = "https://ecp.cgnpc.com.cn"

_PAGE_TYPES: dict[NoticeType, list[tuple[str, str, str, str]]] = {
    NoticeType.BID_ANNOUNCEMENT: [
        (
            "567dafa9ae447eea50584d794e5ad5d8",
            "350ea2d859f7a2797c9be4b6cb3b5ebe",
            "65e43b2fbc914f7e98d966d85f78d5de",
            "招标公告",
        ),
    ],
    NoticeType.PREQUALIFICATION: [
        (
            "859a2e558f3ef97542205f36bca99a35",
            "350ea2d859f7a2797c9be4b6cb3b5ebe",
            "0e7755722d294001bf41f467ddfe04b2",
            "资格预审",
        ),
    ],
    NoticeType.CANDIDATE_PUBLICITY: [
        (
            "480d6180a2b17c8b5cebd14fd3ddf6ef",
            "350ea2d859f7a2797c9be4b6cb3b5ebe",
            "aa2e86adfa624027b1cf9d1598fb90b6",
            "候选人公示",
        ),
    ],
    NoticeType.WIN_ANNOUNCEMENT: [
        (
            "cd56a8517f7dd4ae1a2ba0be732c51da",
            "350ea2d859f7a2797c9be4b6cb3b5ebe",
            "c5ef47bc30844c4683639c66325ec0d7",
            "中标结果",
        ),
        (
            "2a79a68391ecf137b7e3ebbc43f6aed6",
            "6aafa6a8a2acaf9c12afc657db3a5b18",
            "924e0e09dd90451ba1394cae3e06f326",
            "采购结果",
        ),
    ],
    NoticeType.NON_BID_ANNOUNCEMENT: [
        (
            "02513be4ba1d5cb7866970b31463b4a1",
            "6aafa6a8a2acaf9c12afc657db3a5b18",
            "bbf3c0e849e042459392b379c4fb343b",
            "采购公告",
        ),
    ],
}

_PAGE_SIZE = 15


def _parse_date_str(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _parse_datetime_str(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return None


@register
class CgnpcAdapter(SiteAdapter):
    meta = AdapterMeta(
        name="cgnpc",
        display_name="中广核电商",
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
        sub_types = _PAGE_TYPES.get(notice_type)
        if not sub_types:
            return

        await page.goto(
            f"{_BASE_URL}/Default.html",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        await asyncio.sleep(2)

        for site_store_id, content_store_id, detail_data_id, type_label in sub_types:
            logger.info(
                "cgnpc.scrape_subtype",
                notice_type=notice_type.value,
                sub_type=type_label,
            )
            page_no = 1
            while True:
                url = f"{_BASE_URL}/content/{site_store_id}/{content_store_id}/{page_no}.json"
                data = await self._fetch_json(page, url)
                if not data:
                    break

                items = data.get("list", [])
                total = data.get("total", 0)
                total_pages = (total + _PAGE_SIZE - 1) // _PAGE_SIZE if total else 0

                if not items:
                    break

                logger.info(
                    "cgnpc.list_page",
                    sub_type=type_label,
                    page=page_no,
                    items=len(items),
                    total=total,
                    total_pages=total_pages,
                )

                for item in items:
                    notice = self._parse_list_item(
                        item, notice_type, detail_data_id, type_label
                    )
                    if notice:
                        detail = await self._fetch_detail(page, notice.source_url)
                        if detail:
                            notice = notice.merge(detail)
                        yield notice

                if page_no >= total_pages:
                    break
                page_no += 1

    async def _fetch_json(self, page: Page, url: str) -> dict | None:
        result = await page.evaluate(
            """async (url) => {
                try {
                    const r = await fetch(url);
                    if (!r.ok) return null;
                    return await r.text();
                } catch(e) { return null; }
            }""",
            url,
        )
        if not result:
            return None
        try:
            return json.loads(result)
        except json.JSONDecodeError:
            return None

    def _parse_list_item(
        self,
        item: dict,
        notice_type: NoticeType,
        detail_data_id: str,
        type_label: str,
    ) -> BidNotice | None:
        title = (item.get("Title") or "").strip()
        if not title:
            return None

        item_id = item.get("Id", "")
        detail_url = (
            f"{_BASE_URL}/Details.html?dataId={detail_data_id}&detailId={item_id}"
        )

        tender_no = item.get("TenderNo")
        publish_date = _parse_date_str(item.get("IssueTime"))
        purchaser = item.get("BidInvitingParty")

        deadline = _parse_datetime_str(
            item.get("BidEndTime")
            or item.get("QualificationsSubmitEndTime")
        )

        return BidNotice(
            title=title,
            source_site=self.meta.name,
            source_url=detail_url,
            notice_type=notice_type,
            notice_id=tender_no,
            publish_date=publish_date,
            deadline=deadline,
            purchaser=purchaser,
            raw_data=item,
        )

    async def _fetch_detail(self, page: Page, url: str) -> BidNotice | None:
        detail_page = await page.context.new_page()
        try:
            await detail_page.goto(url, wait_until="networkidle", timeout=30000)
            await asyncio.sleep(1)

            pdf_links = await detail_page.evaluate("""() => {
                const links = document.querySelectorAll('a[href*=".pdf"], a[href*="downLoadCmsFile"]');
                return Array.from(links).map(a => a.href).filter(h => h.includes('ecpmanage'));
            }""")

            content = None
            pdf_path = None
            attachments = []

            if pdf_links:
                attachments = pdf_links
                pdf_path, content = await self._download_pdf_via_browser(
                    detail_page, pdf_links[0]
                )

            if not content:
                content = await self._extract_page_content(detail_page)

            purchaser = self._extract_field(content, r"招标人|采购单位|采购人") if content else None
            agency = self._extract_field(content, r"代理机构|招标代理") if content else None
            purchaser_contact = self._extract_field(content, r"联系人") if content else None
            agency_contact = self._extract_field(content, r"电话") if content else None

            result = BidNotice(
                title="",
                source_site=self.meta.name,
                source_url=url,
                notice_type=NoticeType.BID_ANNOUNCEMENT,
                content=content,
                pdf_path=pdf_path,
                attachments=attachments,
            )
            if purchaser:
                result.purchaser = purchaser
            if agency:
                result.agency = agency
            if purchaser_contact:
                result.purchaser_contact = purchaser_contact
            if agency_contact:
                result.agency_contact = agency_contact
            return result
        except Exception:
            logger.debug("cgnpc.detail_error", url=url)
            return None
        finally:
            await detail_page.close()

    async def _download_pdf_via_browser(
        self, page: Page, pdf_url: str
    ) -> tuple[str | None, str | None]:
        from bidding.utils.pdf import PDF_DIR, extract_text_from_pdf

        pdf_dir = PDF_DIR
        pdf_dir.mkdir(parents=True, exist_ok=True)
        filename = hashlib.md5(pdf_url.encode()).hexdigest()[:12] + ".pdf"
        dest = pdf_dir / filename

        if dest.exists() and dest.stat().st_size > 0:
            try:
                text = extract_text_from_pdf(dest)
                return filename, text if text else None
            except Exception:
                return filename, None

        b64 = await page.evaluate("""async (url) => {
            try {
                const r = await fetch(url);
                if (!r.ok) return null;
                const buf = await r.arrayBuffer();
                const bytes = new Uint8Array(buf);
                let binary = '';
                for (let i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i]);
                return btoa(binary);
            } catch(e) { return null; }
        }""", pdf_url)

        if not b64:
            return None, None

        try:
            data = base64.b64decode(b64)
            dest.write_bytes(data)
            logger.info("cgnpc.pdf_downloaded", path=str(dest), size=len(data))
        except Exception:
            logger.debug("cgnpc.pdf_download_failed", url=pdf_url[:80])
            return None, None

        try:
            text = extract_text_from_pdf(dest)
            if text:
                logger.info("cgnpc.pdf_extracted", chars=len(text))
                return filename, text
            return filename, None
        except Exception:
            logger.debug("cgnpc.pdf_extract_failed", url=pdf_url[:80])
            return filename, None

    async def scrape_detail(self, page: Page, url: str) -> BidNotice | None:
        return await self._fetch_detail(page, url)

    async def _extract_page_content(self, page: Page) -> str | None:
        for sel in [".zbxq", ".list-details-layout"]:
            el = await page.query_selector(sel)
            if el:
                text = await el.inner_text()
                if text and len(text.strip()) > 20:
                    return text.strip()
        return None

    @staticmethod
    def _extract_field(content: str, field_pattern: str) -> str | None:
        pattern = rf"(?:{field_pattern})[：:]\s*(.+)"
        match = re.search(pattern, content)
        if match:
            value = match.group(1).strip()
            return value if value else None
        return None
