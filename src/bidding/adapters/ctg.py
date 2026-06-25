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

_BASE_URL = "https://eps.ctg.com.cn"

# 中国三峡集团电子采购平台（CMS）。列表页：/cms/channel/<cat>/index.htm
# 详情页：/cms/channel/<cat>/<id>.htm
# ⚠️ 待联网校验：分类对应的实际公告类型需在站点确认。
_CATEGORIES: dict[str, tuple[str, NoticeType]] = {
    "1ywgg1": ("招标公告", NoticeType.BID_ANNOUNCEMENT),
    "1ywgg2": ("采购公告", NoticeType.NON_BID_ANNOUNCEMENT),
    # TODO(联网校验): 补充 1ywgg0qb(全部?)及其它分类
}


def _parse_date_str(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return datetime.strptime(s.strip()[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _id_from_path(href: str) -> str | None:
    m = re.search(r"/(\d+)\.htm", href)
    return m.group(1) if m else None


@register
class CtgAdapter(SiteAdapter):
    meta = AdapterMeta(
        name="ctg",
        display_name="中国三峡集团电子采购平台",
        base_url=_BASE_URL,
        notice_types=list(dict.fromkeys(nt for _, nt in _CATEGORIES.values())),
        requires_login=False,
        rate_limit=1.0,
    )

    def __init__(self, config: dict | None = None):
        super().__init__(config)
        self._pagination = PaginationState(page_size=20)

    async def scrape_list(self, page: Page, notice_type: NoticeType) -> AsyncIterator[BidNotice]:
        cats = [c for c, (_, nt) in _CATEGORIES.items() if nt == notice_type]
        for cat in cats:
            label = _CATEGORIES[cat][0]
            logger.info("ctg.scrape_category", notice_type=notice_type.value, category=cat, label=label)
            page_no = 1
            while True:
                url = (
                    f"{_BASE_URL}/cms/channel/{cat}/index.htm"
                    if page_no == 1
                    else f"{_BASE_URL}/cms/channel/{cat}/index_{page_no}.htm"
                )
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    await asyncio.sleep(1)
                except Exception:
                    logger.warning("ctg.goto_failed", url=url)
                    break
                items = await self._extract_list_items(page, cat)
                logger.info("ctg.list_page", category=cat, page=page_no, items=len(items))
                if not items:
                    break
                for item in items:
                    notice = self._build_notice(item, notice_type, label)
                    if notice:
                        detail = await self._fetch_detail(page, notice.source_url)
                        if detail:
                            notice = notice.merge(detail)
                        yield notice
                # ⚠️ 待联网校验：分页结构（index_N.htm vs ?page=）。先只取首页。
                break

    async def _extract_list_items(self, page: Page, cat: str) -> list[dict]:
        return await page.evaluate(
            """([cat]) => {
                const re = /\\d{4}-\\d{1,2}-\\d{1,2}/;
                const seen = new Set();
                const out = [];
                document.querySelectorAll(`a[href*="/cms/channel/${cat}/"]`).forEach(a => {
                    const href = a.getAttribute('href') || '';
                    if (/index\\.htm$/.test(href)) return;  // 跳过列表页自身
                    if (!/\\.htm$/.test(href)) return;
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

    def _build_notice(self, item: dict, notice_type: NoticeType, label: str) -> BidNotice | None:
        title = (item.get("title") or "").strip()
        if not title:
            return None
        href = item.get("href") or ""
        return BidNotice(
            title=title, source_site=self.meta.name, source_url=self._resolve_url(href),
            notice_type=notice_type, notice_id=_id_from_path(href),
            publish_date=_parse_date_str(item.get("date")), content=None, raw_data=item,
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
            logger.debug("ctg.detail_failed", url=url[:80])
            return None
        finally:
            await dp.close()

    async def _extract_content(self, page: Page) -> str | None:
        for sel in [".article-content", ".content", "#content", ".detail-content", ".cms-content"]:
            el = await page.query_selector(sel)
            if el:
                text = await el.inner_text()
                if text and len(text.strip()) > 20:
                    return text.strip()
        return None

    async def scrape_detail(self, page: Page, url: str) -> BidNotice | None:
        return await self._fetch_detail(page, url)
