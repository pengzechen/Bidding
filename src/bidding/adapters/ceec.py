from __future__ import annotations

import asyncio
import json
import re
from datetime import date, datetime
from typing import AsyncIterator

import structlog
from playwright.async_api import Page

from bidding.adapters.base import AdapterMeta, SiteAdapter
from bidding.adapters.registry import register
from bidding.models.enums import NoticeType, ProcurementMethod, ProjectCategory
from bidding.models.schema import BidNotice

logger = structlog.get_logger()

_BASE_URL = "https://ec.ceec.net.cn"
_ASHX = f"{_BASE_URL}/ajaxpro/CeecBidWeb.HomeInfo.ProjectList,CeecBidWeb.ashx"

_CATEGORY_MAP: dict[NoticeType, list[tuple[str, str, str]]] = {
    NoticeType.BID_ANNOUNCEMENT: [
        ("WgBCAEcARwA=", "", "招标公告"),
    ],
    NoticeType.NON_BID_ANNOUNCEMENT: [
        ("QwBHAEcARwA=", "aAB3AA==", "采购公告-货物"),
        ("QwBHAEcARwA=", "ZwBjAA==", "采购公告-工程"),
        ("QwBHAEcARwA=", "ZgB3AA==", "采购公告-服务"),
    ],
    NoticeType.PREQUALIFICATION: [
        ("WgBHAFkAUwBHAEcA", "", "资格预审公告"),
    ],
    NoticeType.CANDIDATE_PUBLICITY: [
        ("SABYAFIARwBTAA==", "aAB3AA==", "候选人公示-货物"),
        ("SABYAFIARwBTAA==", "ZwBjAA==", "候选人公示-工程"),
        ("SABYAFIARwBTAA==", "ZgB3AA==", "候选人公示-服务"),
    ],
    NoticeType.WIN_ANNOUNCEMENT: [
        ("WgBCAEcAUwA=", "aAB3AA==", "中标公示-货物"),
        ("WgBCAEcAUwA=", "ZwBjAA==", "中标公示-工程"),
        ("WgBCAEcAUwA=", "ZgB3AA==", "中标公示-服务"),
        ("WgBYAEcAUwA=", "aAB3AA==", "中选公示-货物"),
        ("WgBYAEcAUwA=", "ZwBjAA==", "中选公示-工程"),
        ("WgBYAEcAUwA=", "ZgB3AA==", "中选公示-服务"),
    ],
}

_CAIGOULB_MAP: dict[str, ProjectCategory] = {
    "货物采购": ProjectCategory.GOODS,
    "工程分包": ProjectCategory.ENGINEERING,
    "服务采购": ProjectCategory.SERVICE,
}

_PAGE_SIZE = 20


def _parse_datetime_str(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.strptime(s[:19], "%Y/%m/%d %H:%M:%S")
    except (ValueError, TypeError):
        return None


def _parse_date_from_datetime(s: str | None) -> date | None:
    dt = _parse_datetime_str(s)
    return dt.date() if dt else None


@register
class CeecAdapter(SiteAdapter):
    meta = AdapterMeta(
        name="ceec",
        display_name="中国能建",
        base_url=_BASE_URL,
        notice_types=[
            NoticeType.BID_ANNOUNCEMENT,
            NoticeType.NON_BID_ANNOUNCEMENT,
            NoticeType.PREQUALIFICATION,
            NoticeType.CANDIDATE_PUBLICITY,
            NoticeType.WIN_ANNOUNCEMENT,
        ],
        requires_login=False,
        rate_limit=1.0,
    )

    async def scrape_list(
        self, page: Page, notice_type: NoticeType
    ) -> AsyncIterator[BidNotice]:
        cats = _CATEGORY_MAP.get(notice_type)
        if not cats:
            return

        await page.goto(
            f"{_BASE_URL}/HomeInfo/ProjectList.aspx?InfoLevel=MQA=&bigType=WgBCAEcARwA=",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        await asyncio.sleep(3)

        for bigtype, smalltype, label in cats:
            logger.info("ceec.scrape_category", category=label)
            page_no = 1
            while True:
                data = await self._fetch_list(page, bigtype, smalltype, page_no)
                if not data:
                    break

                total = data.get("total", [0])[0]
                items = data.get("maindata", [[]])[0]
                if not items:
                    break

                total_pages = (total + _PAGE_SIZE - 1) // _PAGE_SIZE
                logger.info(
                    "ceec.list_page",
                    category=label,
                    page=page_no,
                    items=len(items),
                    total=total,
                )

                for item in items:
                    notice = await self._process_item(
                        page, item, notice_type, bigtype
                    )
                    if notice:
                        yield notice
                    await asyncio.sleep(self.meta.rate_limit)

                if page_no >= total_pages or page_no >= 5:
                    break
                page_no += 1
                await asyncio.sleep(self.meta.rate_limit)

    async def _fetch_list(
        self, page: Page, bigtype: str, smalltype: str, page_no: int
    ) -> dict | None:
        result = await page.evaluate(
            """async ([url, body]) => {
                try {
                    const resp = await fetch(url, {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'text/plain; charset=UTF-8',
                            'X-AjaxPro-Method': 'getdata'
                        },
                        body: JSON.stringify(body)
                    });
                    let text = await resp.text();
                    if (text.endsWith(';/*')) text = text.slice(0, -3);
                    if (text.startsWith('"') && text.endsWith('"')) text = JSON.parse(text);
                    return text;
                } catch(e) { return null; }
            }""",
            [
                _ASHX,
                {
                    "_bigtype_base64": bigtype,
                    "_smalltype_base64": smalltype,
                    "_pageIndex": page_no,
                    "_pageSize": _PAGE_SIZE,
                },
            ],
        )
        if not result:
            return None
        try:
            return json.loads(result)
        except (json.JSONDecodeError, TypeError):
            return None

    async def _process_item(
        self, page: Page, item: dict, notice_type: NoticeType, bigtype: str
    ) -> BidNotice | None:
        notice = self._parse_item(item, notice_type, bigtype)
        if not notice:
            return None

        if notice.content:
            return notice

        zbxmbh = item.get("ZhaoBiaoXMBH") or ""
        sys_id = item.get("sys_epsid") or item.get("sys_id") or ""

        if notice_type in (NoticeType.CANDIDATE_PUBLICITY, NoticeType.WIN_ANNOUNCEMENT):
            detail_url = (
                f"{_BASE_URL}/HomeInfo/winDidDetails.aspx"
                f"?bigtype={bigtype}&threadID={sys_id}"
            )
        elif not zbxmbh:
            return notice
        else:
            encoded = await self._encode_zbxmbh(page, zbxmbh)
            if not encoded:
                return notice
            detail_url = (
                f"{_BASE_URL}/HomeInfo/ZhaoBiaoGG_Details.aspx?zbxmbh={encoded}"
            )
            notice.source_url = detail_url

        detail = await self._fetch_detail_content(page, detail_url)
        if detail:
            notice = notice.merge(detail)
        return notice

    async def _encode_zbxmbh(self, page: Page, code: str) -> str | None:
        result = await page.evaluate(
            """async ([url, code]) => {
                try {
                    const resp = await fetch(url, {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'text/plain; charset=UTF-8',
                            'X-AjaxPro-Method': 'encode'
                        },
                        body: JSON.stringify({"s": code})
                    });
                    let text = await resp.text();
                    if (text.endsWith(';/*')) text = text.slice(0, -3);
                    if (text.startsWith('"')) text = JSON.parse(text);
                    return text;
                } catch(e) { return null; }
            }""",
            [_ASHX, code],
        )
        return result if result else None

    async def _fetch_detail_content(self, page: Page, url: str) -> BidNotice | None:
        detail_page = await page.context.new_page()
        try:
            await detail_page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(3)

            content = await detail_page.evaluate("""() => {
                const el = document.querySelector('.gg_info');
                if (el && el.innerText.trim().length > 50) return el.innerText.trim();
                const body = document.body.innerText;
                const start = body.indexOf('郑重声明');
                if (start > 0) return body.substring(start, start + 10000).trim();
                return null;
            }""")

            if not content or len(content) < 50:
                return None

            purchaser = self._extract_field(content, r"招标采购单位|招标人|采购人")

            return BidNotice(
                title="",
                source_site=self.meta.name,
                source_url=url,
                notice_type=NoticeType.BID_ANNOUNCEMENT,
                content=content,
                purchaser=purchaser,
            )
        except Exception:
            logger.debug("ceec.detail_error", url=url[:80])
            return None
        finally:
            await detail_page.close()

    def _parse_item(
        self, item: dict, notice_type: NoticeType, bigtype: str
    ) -> BidNotice | None:
        title = (
            item.get("GongGaoBT")
            or item.get("zbxmmc")
            or item.get("ZhaoBiaoXMMC")
            or ""
        ).strip()
        if not title:
            return None

        zbxmbh = item.get("ZhaoBiaoXMBH") or item.get("zbxmbh") or ""
        sys_id = item.get("sys_epsid") or item.get("sys_id") or ""

        if notice_type in (NoticeType.CANDIDATE_PUBLICITY, NoticeType.WIN_ANNOUNCEMENT):
            source_url = (
                f"{_BASE_URL}/HomeInfo/winDidDetails.aspx"
                f"?bigtype={bigtype}&threadID={sys_id}"
            )
        elif sys_id and not item.get("GongGaoBT"):
            source_url = (
                f"{_BASE_URL}/HomeInfo/ProjectDetail.aspx"
                f"?bigtype={bigtype}&threadID={sys_id}"
            )
        else:
            source_url = f"{_BASE_URL}/HomeInfo/ZhaoBiaoGG_Details.aspx?zbxmbh={zbxmbh}"

        publish_date = _parse_date_from_datetime(
            item.get("GongGaoFBSJ") or item.get("fbsj")
        )
        deadline = _parse_datetime_str(
            item.get("BaoMingJZSJ") or item.get("kaiBiaoSJ")
        )

        caigoulb = item.get("CaiGouLB") or item.get("zhaoBiaoLB") or ""
        project_category = _CAIGOULB_MAP.get(caigoulb)

        purchaser = (
            item.get("ZhaoBiaoDW_JC")
            or item.get("XiangMuJC")
            or item.get("zhaoBiaoLXR")
        )

        content = (item.get("biaoDiWSM") or "").strip() or None
        agency_contact = item.get("zhaoBiaoLXR")

        return BidNotice(
            title=title,
            source_site=self.meta.name,
            source_url=source_url,
            notice_type=notice_type,
            notice_id=zbxmbh,
            publish_date=publish_date,
            deadline=deadline,
            project_category=project_category,
            purchaser=purchaser,
            agency_contact=agency_contact,
            content=content,
            raw_data=item,
        )

    async def scrape_detail(self, page: Page, url: str) -> BidNotice | None:
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(3)
        except Exception:
            logger.debug("ceec.detail_timeout", url=url[:80])
            return None

        content = await page.evaluate("""() => {
            const el = document.querySelector('.gg_info');
            if (el) return el.innerText;
            return document.body.innerText.substring(0, 10000);
        }""")

        if not content or len(content.strip()) < 50:
            return None

        purchaser = self._extract_field(content, r"招标采购单位|招标人")

        return BidNotice(
            title="",
            source_site=self.meta.name,
            source_url=url,
            notice_type=NoticeType.BID_ANNOUNCEMENT,
            content=content.strip(),
            purchaser=purchaser,
        )

    @staticmethod
    def _extract_field(content: str, field_pattern: str) -> str | None:
        pattern = rf"(?:{field_pattern})[：:]\s*(.+)"
        match = re.search(pattern, content)
        if match:
            value = match.group(1).strip()
            return value if value else None
        return None
