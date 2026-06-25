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

_BASE_URL = "https://ebid.espic.com.cn"

# 电能e招采平台（国家电投）。搜索式列表页：
#   /newgdtcms/category/bulletinListNew.html?dates=300&categoryId=2&tenderMethod=01&tabName=招标信息&page=1
#   /newgdtcms/category/purchaseListNew.html?dates=300&categoryId=2&tenderMethod=00&tabName=采购信息&page=1
# ⚠️ 待联网校验：
# 1) 详情页 URL 形态需从列表页真实渲染确认（列表项 href 未在首页暴露）。
# 2) 其它 categoryId/tenderMethod 组合需从站点确认。
# 3) 列表结果可能是 SSR 或 AJAX 加载，需确认。
_CATEGORIES: dict[NoticeType, list[tuple[str, str]]] = {
    NoticeType.BID_ANNOUNCEMENT: [
        ("/newgdtcms/category/bulletinListNew.html?dates=300&categoryId=2&tenderMethod=01", "招标信息"),
    ],
    NoticeType.NON_BID_ANNOUNCEMENT: [
        ("/newgdtcms/category/purchaseListNew.html?dates=300&categoryId=2&tenderMethod=00", "采购信息"),
    ],
    # TODO(联网校验): 补充中标/变更/候选人等分类
}


def _parse_date_str(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return datetime.strptime(s.strip()[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


@register
class EspicAdapter(SiteAdapter):
    meta = AdapterMeta(
        name="espic",
        display_name="电能e招采平台",
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
        for base_path, label in cats:
            logger.info("espic.scrape_list", notice_type=notice_type.value, label=label)
            page_no = 1
            while True:
                url = f"{_BASE_URL}{base_path}&page={page_no}"
                try:
                    await page.goto(url, wait_until="networkidle", timeout=30000)
                    await asyncio.sleep(1)
                except Exception:
                    logger.warning("espic.goto_failed", url=url)
                    break
                items = await self._extract_list_items(page)
                logger.info("espic.list_page", label=label, page=page_no, items=len(items))
                if not items:
                    break
                for item in items:
                    notice = self._build_notice(item, notice_type)
                    if notice:
                        detail = await self._fetch_detail(page, notice.source_url)
                        if detail:
                            notice = notice.merge(detail)
                        yield notice
                # ⚠️ 待联网校验：分页结构（总页数/下一页）。先只取首页。
                break

    async def _extract_list_items(self, page: Page) -> list[dict]:
        # 通用兜底：取所有 <a> 带 href 且看起来像详情页
        # ⚠️ 待联网校验：真实列表项 DOM 结构与详情 href 形态
        return await page.evaluate(
            """() => {
                const re = /\\d{4}-\\d{1,2}-\\d{1,2}/;
                const seen = new Set();
                const out = [];
                document.querySelectorAll('a[href]').forEach(a => {
                    const href = a.getAttribute('href') || '';
                    if (!href || href === '#' || href.startsWith('javascript')) return;
                    if (/\\.(css|js|png|jpg|gif|ico|woff|svg)(\\?|$)/.test(href)) return;
                    if (seen.has(href)) return;
                    seen.add(href);
                    const title = (a.getAttribute('title') || a.innerText || '').trim();
                    if (!title || title.length < 5) return;
                    const ctx = (a.closest('li,tr,dd,div') || a.parentElement || a).innerText || '';
                    const dm = ctx.match(re);
                    out.push({href, title, date: dm ? dm[0] : null});
                });
                return out;
            }"""
        )

    def _build_notice(self, item: dict, notice_type: NoticeType) -> BidNotice | None:
        title = (item.get("title") or "").strip()
        if not title:
            return None
        href = item.get("href") or ""
        # 处理协议相对 URL
        if href.startswith("//"):
            href = "https:" + href
        source_url = self._resolve_url(href)
        return BidNotice(
            title=title, source_site=self.meta.name, source_url=source_url,
            notice_type=notice_type, notice_id=None,
            publish_date=_parse_date_str(item.get("date")), content=None, raw_data=item,
        )

    async def _fetch_detail(self, page: Page, url: str) -> BidNotice | None:
        if not url or url == self.meta.base_url:
            return None
        dp = await page.context.new_page()
        try:
            await dp.goto(url, wait_until="networkidle", timeout=30000)
            await asyncio.sleep(0.5)
            content = await self._extract_content(dp)
            if not content:
                return None
            return BidNotice(title="", source_site=self.meta.name, source_url=url, notice_type=NoticeType.BID_ANNOUNCEMENT, content=content)
        except Exception:
            logger.debug("espic.detail_failed", url=url[:80])
            return None
        finally:
            await dp.close()

    async def _extract_content(self, page: Page) -> str | None:
        # ⚠️ 待联网校验：正文容器选择器
        for sel in [".content", ".article-content", "#content", ".detail-content", ".bulletin-detail"]:
            el = await page.query_selector(sel)
            if el:
                text = await el.inner_text()
                if text and len(text.strip()) > 20:
                    return text.strip()
        return None

    async def scrape_detail(self, page: Page, url: str) -> BidNotice | None:
        return await self._fetch_detail(page, url)
