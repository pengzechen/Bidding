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

# 京能e购。首页 powerbeijing-ec.com 是落地页，实际内容在 powerbeijing-eshop.com。
# 列表页：/Bidding/ /abandonBulletin/ /biddingBulletin/（按日期分页）
# 详情页：/Bidding/YYYY-MM-DD/<id>.html /abandonBulletin/YYYY-MM-DD/<id>.html
# ⚠️ 待联网校验：分页方式（?page= 还是日期滚动）、条目选择器需从真实页面确认。
_BASE_URL = "https://www.powerbeijing-eshop.com"

_CATEGORIES: dict[NoticeType, list[tuple[str, str]]] = {
    NoticeType.BID_ANNOUNCEMENT: [
        ("/Bidding/", "招标公告"),
        ("/biddingBulletin/", "招标公示"),
    ],
    NoticeType.WIN_ANNOUNCEMENT: [
        ("/abandonBulletin/", "废标公告"),
    ],
    # TODO(联网校验): 补充中标公告等其它分类
}


def _parse_date_str(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return datetime.strptime(s.strip()[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _date_from_path(href: str) -> date | None:
    m = re.search(r"/(\d{4})-(\d{2})-(\d{2})/", href)
    if m:
        try:
            return date(int(m[1]), int(m[2]), int(m[3]))
        except ValueError:
            return None
    return None


def _id_from_path(href: str) -> str | None:
    m = re.search(r"/(\d+)\.html", href)
    return m.group(1) if m else None


@register
class JnAdapter(SiteAdapter):
    meta = AdapterMeta(
        name="jn",
        display_name="京能e购",
        base_url=_BASE_URL,
        notice_types=list(_CATEGORIES.keys()),
        requires_login=False,
        rate_limit=1.0,
    )

    def __init__(self, config: dict | None = None):
        super().__init__(config)
        self._pagination = PaginationState(page_size=20)

    async def scrape_list(self, page: Page, notice_type: NoticeType) -> AsyncIterator[BidNotice]:
        cats = _CATEGORIES.get(notice_type)
        if not cats:
            return
        for path, label in cats:
            url = f"{_BASE_URL}{path}"
            logger.info("jn.scrape_list", notice_type=notice_type.value, label=label, url=url)
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(1)
            except Exception:
                logger.warning("jn.goto_failed", url=url)
                return
            items = await self._extract_list_items(page, path)
            logger.info("jn.list_page", label=label, items=len(items))
            for item in items:
                notice = self._build_notice(item, notice_type)
                if notice:
                    detail = await self._fetch_detail(page, notice.source_url)
                    if detail:
                        notice = notice.merge(detail)
                    yield notice
            # ⚠️ 待联网校验：分页结构。先只取首页。

    async def _extract_list_items(self, page: Page, cat_path: str) -> list[dict]:
        # 取指向本分类详情页（/Bidding/YYYY-MM-DD/<id>.html）的 <a>
        return await page.evaluate(
            """([catPath]) => {
                const re = /\\d{4}-\\d{1,2}-\\d{1,2}/;
                const seen = new Set();
                const out = [];
                document.querySelectorAll(`a[href*="${catPath}"]`).forEach(a => {
                    const href = a.getAttribute('href') || '';
                    if (!/\\/\\d{4}-\\d{2}-\\d{2}\\/\\d+\\.html$/.test(href)) return;
                    if (seen.has(href)) return;
                    seen.add(href);
                    const ctx = (a.closest('li,tr,dd,div') || a.parentElement || a).innerText || '';
                    const dm = ctx.match(re);
                    const title = (a.getAttribute('title') || a.innerText || '').trim();
                    if (title && title.length > 4) out.push({href, title, date: dm ? dm[0] : null});
                });
                return out;
            }""",
            [cat_path],
        )

    def _build_notice(self, item: dict, notice_type: NoticeType) -> BidNotice | None:
        title = (item.get("title") or "").strip()
        if not title:
            return None
        href = item.get("href") or ""
        # 处理协议相对 URL（//www.powerbeijing-eshop.com/...）
        if href.startswith("//"):
            href = "https:" + href
        source_url = self._resolve_url(href)
        return BidNotice(
            title=title, source_site=self.meta.name, source_url=source_url,
            notice_type=notice_type, notice_id=_id_from_path(href),
            publish_date=_parse_date_str(item.get("date")) or _date_from_path(href),
            content=None, raw_data=item,
        )

    async def _fetch_detail(self, page: Page, url: str) -> BidNotice | None:
        if not url:
            return None
        dp = await page.context.new_page()
        try:
            await dp.goto(url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(0.5)
            content = await self._extract_content(dp)
            if not content:
                return None
            return BidNotice(title="", source_site=self.meta.name, source_url=url, notice_type=NoticeType.BID_ANNOUNCEMENT, content=content)
        except Exception:
            logger.debug("jn.detail_failed", url=url[:80])
            return None
        finally:
            await dp.close()

    async def _extract_content(self, page: Page) -> str | None:
        # ⚠️ 待联网校验：正文容器选择器
        for sel in [".content", ".article-content", "#content", ".detail-content", ".bulletin-content"]:
            el = await page.query_selector(sel)
            if el:
                text = await el.inner_text()
                if text and len(text.strip()) > 20:
                    return text.strip()
        return None

    async def scrape_detail(self, page: Page, url: str) -> BidNotice | None:
        return await self._fetch_detail(page, url)
