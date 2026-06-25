from __future__ import annotations

import asyncio
from datetime import date, datetime
from typing import AsyncIterator

import structlog
from playwright.async_api import Page

from bidding.adapters.base import AdapterMeta, PaginationState, SiteAdapter
from bidding.adapters.registry import register
from bidding.models.enums import NoticeType
from bidding.models.schema import BidNotice

logger = structlog.get_logger()

_BASE_URL = "https://cgpt.china-an.cn"

# 中国安能电子采购平台。站点挂在 WAF 后（反爬 JS 挑战 → 设 cookie → reload），
# 直接 curl/fetch 拿不到真实内容（返回 412），必须用 Playwright 等浏览器执行 JS 过挑战。
# ⚠️ 待联网校验（重要）：
# 1) 列表页 URL、分类结构、详情页 href 形态均未知（WAF 阻止了静态抓取），需用 Playwright
#    进站点后从真实渲染 HTML 确认。
# 2) 公告类型映射需在站点导航上确认后补全。
# 先以 BID_ANNOUNCEMENT 占位，联网后补全。
_LIST_URLS: dict[NoticeType, str] = {
    NoticeType.BID_ANNOUNCEMENT: "/",
    # TODO(联网校验): 补充实际列表页 URL 及对应公告类型
}


def _parse_date_str(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return datetime.strptime(s.strip()[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


@register
class ChinaAnAdapter(SiteAdapter):
    meta = AdapterMeta(
        name="china_an",
        display_name="中国安能电子采购平台",
        base_url=_BASE_URL,
        notice_types=list(_LIST_URLS.keys()),
        requires_login=False,
        rate_limit=1.5,
    )

    def __init__(self, config: dict | None = None):
        super().__init__(config)
        self._pagination = PaginationState(page_size=20)

    async def scrape_list(self, page: Page, notice_type: NoticeType) -> AsyncIterator[BidNotice]:
        path = _LIST_URLS.get(notice_type)
        if not path:
            return
        url = self._resolve_url(path)
        logger.info("china_an.scrape_list", notice_type=notice_type.value, url=url)
        try:
            # WAF 挑战会触发一次 reload：等 networkidle 让挑战跑完、真实列表渲染
            await page.goto(url, wait_until="networkidle", timeout=45000)
            await asyncio.sleep(2)
        except Exception:
            logger.warning("china_an.goto_failed", url=url)
            return

        items = await self._extract_list_items(page)
        logger.info("china_an.list_page", url=url, items=len(items))
        for item in items:
            notice = self._build_notice(item, notice_type)
            if not notice:
                continue
            detail = await self._fetch_detail(page, notice.source_url)
            if detail:
                notice = notice.merge(detail)
            yield notice

    async def _extract_list_items(self, page: Page) -> list[dict]:
        # 通用兜底：取所有 <a> 带 title/文本且 href 看起来像详情页
        # ⚠️ 待联网校验：真实列表项 DOM 结构与详情 href 形态
        return await page.evaluate(
            """() => {
                const re = /\\d{4}-\\d{1,2}-\\d{1,2}/;
                const seen = new Set();
                const out = [];
                document.querySelectorAll('a[href]').forEach(a => {
                    const href = a.getAttribute('href') || '';
                    if (href === '/' || href === '#' || href.startsWith('javascript')) return;
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
        return BidNotice(
            title=title, source_site=self.meta.name, source_url=self._resolve_url(href),
            notice_type=notice_type, notice_id=None,
            publish_date=_parse_date_str(item.get("date")), content=None, raw_data=item,
        )

    async def _fetch_detail(self, page: Page, url: str) -> BidNotice | None:
        if not url or url == self.meta.base_url:
            return None
        dp = await page.context.new_page()
        try:
            await dp.goto(url, wait_until="networkidle", timeout=45000)
            await asyncio.sleep(1)
            content = await self._extract_content(dp)
            if not content:
                return None
            return BidNotice(title="", source_site=self.meta.name, source_url=url, notice_type=NoticeType.BID_ANNOUNCEMENT, content=content)
        except Exception:
            logger.debug("china_an.detail_failed", url=url[:80])
            return None
        finally:
            await dp.close()

    async def _extract_content(self, page: Page) -> str | None:
        # ⚠️ 待联网校验：正文容器选择器
        for sel in [".content", ".article-content", ".detail-content", "#content", ".zbxq"]:
            el = await page.query_selector(sel)
            if el:
                text = await el.inner_text()
                if text and len(text.strip()) > 20:
                    return text.strip()
        return None

    async def scrape_detail(self, page: Page, url: str) -> BidNotice | None:
        return await self._fetch_detail(page, url)
