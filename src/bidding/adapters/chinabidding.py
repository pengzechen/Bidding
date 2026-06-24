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

_BASE_URL = "https://www.chinabidding.cn"

# 列表入口：/zbgg/ 为招标公告分类，<token>.html 为该分类的分页索引页。
# ⚠️ 待联网校验：
# 1) 站点挂在阿里云 WAF 后（acw_sc__v2 JS 挑战 → 设 cookie → reload）。直接 fetch/curl 拿不到
#    真实内容，必须用 Playwright 等浏览器执行 JS，页面会自动完成挑战并 reload 出真实列表。
# 2) 站点是一级聚合门户，分类很多（zbgg=招标公告，另有中标/变更等前缀及其 token），
#    此处先接入招标公告入口；其余分类的 token 需在站点导航上确认后补到 _LIST_PATHS。
# 3) 详情页 URL 形态、列表条目的精确选择器需在能访问站点时从真实渲染 HTML 确认。
_LIST_PATHS: dict[NoticeType, list[str]] = {
    NoticeType.BID_ANNOUNCEMENT: ["/zbgg/U-vzaDzyu.html"],
    # TODO(联网校验): 补充中标/变更/候选人等分类入口
}


def _parse_date_str(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return datetime.strptime(s.strip()[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


@register
class ChinabiddingAdapter(SiteAdapter):
    meta = AdapterMeta(
        name="chinabidding",
        display_name="采购与招标网",
        base_url=_BASE_URL,
        notice_types=[nt for nt in _LIST_PATHS],
        requires_login=False,
        rate_limit=1.5,
    )

    def __init__(self, config: dict | None = None):
        super().__init__(config)
        self._pagination = PaginationState(page_size=20)

    async def scrape_list(
        self, page: Page, notice_type: NoticeType
    ) -> AsyncIterator[BidNotice]:
        paths = _LIST_PATHS.get(notice_type)
        if not paths:
            return

        for path in paths:
            url = self._resolve_url(path)
            logger.info(
                "chinabidding.scrape_list",
                notice_type=notice_type.value,
                url=url,
            )
            try:
                # WAF 挑战会触发一次 reload：等 networkidle 让挑战跑完、真实列表渲染
                await page.goto(url, wait_until="networkidle", timeout=45000)
                await asyncio.sleep(2)
            except Exception:
                logger.warning("chinabidding.goto_failed", url=url)
                return

            items = await self._extract_list_items(page)
            logger.info("chinabidding.list_page", url=url, items=len(items))
            for item in items:
                notice = self._build_notice(item, notice_type)
                if not notice:
                    continue
                detail = await self._fetch_detail(page, notice.source_url)
                if detail:
                    notice = notice.merge(detail)
                yield notice

            # ⚠️ 待联网校验：分页结构（下一页 token / ?page= ）。先只取首页索引。

    async def _extract_list_items(self, page: Page) -> list[dict]:
        # 通用兜底解析：取所有看起来是详情的 <a>（href 含 /zbgg/ 且非索引 token），
        # 标题取 title 属性或文本，日期取最近列表行容器内文本。
        # ⚠️ 待联网校验：真实条目结构/详情 href 形态。
        return await page.evaluate(
            """() => {
                const re = /\\d{4}-\\d{1,2}-\\d{1,2}/;
                const seen = new Set();
                const out = [];
                document.querySelectorAll('a[href*="/zbgg/"]').forEach(a => {
                    const href = a.getAttribute('href') || '';
                    // 跳过索引页自身（token 形如 U-xxxx.html）与锚点
                    if (/\\/zbgg\\/[A-Za-z]-[A-Za-z0-9_-]+\\.html$/.test(href)) return;
                    if (href.endsWith('#')) return;
                    if (seen.has(href)) return;
                    seen.add(href);
                    const ctx = (a.closest('li,tr,dd,div') || a.parentElement || a).innerText || '';
                    const dm = ctx.match(re);
                    const title = (a.getAttribute('title') || a.innerText || '').trim();
                    if (title && title.length > 4) out.push({href, title, date: dm ? dm[0] : null});
                });
                return out;
            }"""
        )

    def _build_notice(self, item: dict, notice_type: NoticeType) -> BidNotice | None:
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
            notice_id=None,
            publish_date=_parse_date_str(item.get("date")),
            content=None,
            raw_data=item,
        )

    async def _fetch_detail(self, page: Page, url: str) -> BidNotice | None:
        if not url or url == self.meta.base_url:
            return None
        detail_page = await page.context.new_page()
        try:
            # 详情页同样在 WAF 后：等挑战完成
            await detail_page.goto(url, wait_until="networkidle", timeout=45000)
            await asyncio.sleep(1)
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
            logger.debug("chinabidding.detail_failed", url=url[:80])
            return None
        finally:
            await detail_page.close()

    async def _extract_content(self, page: Page) -> str | None:
        # ⚠️ 待联网校验：正文容器选择器。先试常见命名，取不到回退到 body 文本。
        for sel in [
            ".content",
            ".article-content",
            ".detail-content",
            "#content",
            ".zbxq",
            ".news-content",
        ]:
            el = await page.query_selector(sel)
            if el:
                text = await el.inner_text()
                if text and len(text.strip()) > 20:
                    return text.strip()
        return None

    async def scrape_detail(self, page: Page, url: str) -> BidNotice | None:
        return await self._fetch_detail(page, url)
