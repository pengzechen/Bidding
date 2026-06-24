from __future__ import annotations

import asyncio
import re
from datetime import date, datetime
from typing import AsyncIterator

import structlog
from playwright.async_api import Page

from bidding.adapters.base import AdapterMeta, PaginationState, SiteAdapter
from bidding.adapters.registry import register
from bidding.models.enums import NoticeType
from bidding.models.schema import BidNotice

logger = structlog.get_logger()

_BASE_URL = "https://gylpt.nxgyzb.com"

# 分类目录 → (名称, 公告类型)。列表页：/cms/default/webfile/<cat>/index.html
# 详情页：/cms/default/webfile/<cat>/<yyyymmdd>/<snowflake-id>.html
_CATEGORIES: dict[str, tuple[str, NoticeType]] = {
    "ywgg1": ("招标公告", NoticeType.BID_ANNOUNCEMENT),
    "3ywgg1": ("非招标公告", NoticeType.NON_BID_ANNOUNCEMENT),
    "jingpai": ("竞拍公告", NoticeType.NON_BID_ANNOUNCEMENT),
}

_DATE_RE = re.compile(r"\d{4}-\d{1,2}-\d{1,2}")


def _parse_date_str(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return datetime.strptime(s.strip()[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _date_from_path(href: str) -> date | None:
    m = re.search(r"/(\d{4})(\d{2})(\d{2})/", href)
    if m:
        try:
            return date(int(m[1]), int(m[2]), int(m[3]))
        except ValueError:
            return None
    return None


def _id_from_path(href: str) -> str | None:
    m = re.search(r"/(\d{17,20})\.html", href)
    return m.group(1) if m else None


@register
class NxgyzbAdapter(SiteAdapter):
    meta = AdapterMeta(
        name="nxgyzb",
        display_name="宁夏国资运营采购",
        base_url=_BASE_URL,
        notice_types=list(dict.fromkeys(nt for _, nt in _CATEGORIES.values())),
        requires_login=False,
        rate_limit=1.0,
    )

    def __init__(self, config: dict | None = None):
        super().__init__(config)
        self._pagination = PaginationState(page_size=20)

    async def scrape_list(
        self, page: Page, notice_type: NoticeType
    ) -> AsyncIterator[BidNotice]:
        cats = [c for c, (label, nt) in _CATEGORIES.items() if nt == notice_type]
        for cat in cats:
            label = _CATEGORIES[cat][0]
            logger.info(
                "nxgyzb.scrape_category",
                notice_type=notice_type.value,
                category=cat,
                label=label,
            )
            page_no = 1
            while True:
                url = (
                    f"{_BASE_URL}/cms/default/webfile/{cat}/index.html"
                    if page_no == 1
                    else f"{_BASE_URL}/cms/default/webfile/{cat}/index.html?pageNo={page_no}"
                )
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    await asyncio.sleep(1)
                except Exception:
                    logger.warning("nxgyzb.goto_failed", url=url)
                    break

                items = await self._extract_list_items(page, cat)
                logger.info(
                    "nxgyzb.list_page",
                    category=cat,
                    page=page_no,
                    items=len(items),
                )
                if not items:
                    break

                for item in items:
                    notice = self._build_notice(item, notice_type, label)
                    if notice:
                        detail = await self._fetch_detail(page, notice.source_url)
                        if detail:
                            notice = notice.merge(detail)
                        yield notice

                # ⚠️ 待联网校验：分页（pageNo vs index_N.html）。先只取首页，避免空翻。
                break

    async def _extract_list_items(self, page: Page, cat: str) -> list[dict]:
        # 取指向本分类详情页（带日期/雪花id）的 <a>
        return await page.evaluate(
            """([cat]) => {
                const re = /\\d{4}-\\d{1,2}-\\d{1,2}/;
                const seen = new Set();
                const out = [];
                const sel = `a[href*="/cms/default/webfile/${cat}/"]`;
                document.querySelectorAll(sel).forEach(a => {
                    const href = a.getAttribute('href') || '';
                    if (!/\\/\\d{8}\\/\\d+\\.html$/.test(href)) return;
                    if (seen.has(href)) return;
                    seen.add(href);
                    const ctx = (a.closest('li,tr,dd') || a.parentElement || a).innerText || '';
                    const dm = ctx.match(re);
                    const title = (a.getAttribute('title') || a.innerText || '').trim();
                    out.push({href, title, date: dm ? dm[0] : null});
                });
                return out;
            }""",
            [cat],
        )

    def _build_notice(
        self, item: dict, notice_type: NoticeType, label: str
    ) -> BidNotice | None:
        title = (item.get("title") or "").strip()
        if not title:
            return None
        href = item.get("href") or ""
        source_url = self._resolve_url(href)
        return BidNotice(
            title=title,
            source_site=self.meta.name,
            source_url=source_url,
            notice_type=notice_type,
            notice_id=_id_from_path(href),
            publish_date=_parse_date_str(item.get("date")) or _date_from_path(href),
            content=None,
            raw_data=item,
        )

    async def _fetch_detail(self, page: Page, url: str) -> BidNotice | None:
        if not url:
            return None
        detail_page = await page.context.new_page()
        try:
            await detail_page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(0.5)
            content = await self._extract_content(detail_page)
            if not content:
                return None
            return BidNotice(
                title="",
                source_site=self.meta.name,
                source_url=url,
                notice_type=NoticeType.BID_ANNOUNCEMENT,
                content=content,
            )
        except Exception:
            logger.debug("nxgyzb.detail_failed", url=url[:80])
            return None
        finally:
            await detail_page.close()

    async def _extract_content(self, page: Page) -> str | None:
        # ⚠️ 待联网校验：正文容器选择器，取不到回退 body 文本
        for sel in [
            ".article-content",
            ".content",
            "#content",
            ".detail-content",
            ".cms-content",
        ]:
            el = await page.query_selector(sel)
            if el:
                text = await el.inner_text()
                if text and len(text.strip()) > 20:
                    return text.strip()
        return None

    async def scrape_detail(self, page: Page, url: str) -> BidNotice | None:
        return await self._fetch_detail(page, url)
