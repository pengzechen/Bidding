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

_BASE_URL = "https://bulletin.cebpubservice.com"

# 中国招标投标公共服务平台（jQuery 老站，SSR）。搜索结果页：
#   /xxfbcmses/search/bulletin.html?categoryId=<N>&page=<N>&dates=300&showStatus=1
# 站点还有分类搜索入口 /search/bulletin /search/candidate /search/change /search/qualify /search/result
# ⚠️ 待联网校验：
# 1) 各公告类型对应的 categoryId 仅在首页见到 categoryId=88，其余取值需在站点确认后补全。
# 2) 结果列表可能是 SSR，也可能由 .do 接口 AJAX 加载（见 /ctpsp_iiss/...getSearch.do）；
#    若 SSR 取不到，改为 page.evaluate 调该 .do 接口。
# 3) 结果行的精确选择器与详情页 URL 形态需从真实页面确认。
_SEARCH_PATH = "/xxfbcmses/search/bulletin.html"

# 公告类型 → (categoryId, 标签)
_CATEGORY_IDS: dict[NoticeType, list[tuple[str, str]]] = {
    NoticeType.BID_ANNOUNCEMENT: [("88", "招标公告")],
    # TODO(联网校验): 补全 候选人/变更/资格预审/中标 的 categoryId
    # 候选对应 /search/candidate、变更 /search/change、资格预审 /search/qualify、中标 /search/result
}

_DATES = 300  # 近 300 天


def _parse_date_str(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return datetime.strptime(s.strip()[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


@register
class CebpubserviceAdapter(SiteAdapter):
    meta = AdapterMeta(
        name="cebpubservice",
        display_name="中国招标投标公共服务平台",
        base_url=_BASE_URL,
        notice_types=[nt for nt in _CATEGORY_IDS],
        requires_login=False,
        rate_limit=1.0,
    )

    def __init__(self, config: dict | None = None):
        super().__init__(config)
        self._pagination = PaginationState(page_size=20)

    async def scrape_list(
        self, page: Page, notice_type: NoticeType
    ) -> AsyncIterator[BidNotice]:
        cats = _CATEGORY_IDS.get(notice_type)
        if not cats:
            return

        for category_id, label in cats:
            logger.info(
                "cebpubservice.scrape_category",
                notice_type=notice_type.value,
                category=category_id,
                label=label,
            )
            page_no = 1
            while True:
                url = (
                    f"{_BASE_URL}{_SEARCH_PATH}?categoryId={category_id}"
                    f"&dates={_DATES}&page={page_no}&showStatus=1"
                )
                try:
                    await page.goto(url, wait_until="networkidle", timeout=30000)
                    await asyncio.sleep(1)
                except Exception:
                    logger.warning("cebpubservice.goto_failed", url=url)
                    break

                items = await self._extract_list_items(page)
                logger.info(
                    "cebpubservice.list_page",
                    category=category_id,
                    page=page_no,
                    items=len(items),
                )
                if not items:
                    break

                for item in items:
                    notice = self._build_notice(item, notice_type)
                    if not notice:
                        continue
                    detail = await self._fetch_detail(page, notice.source_url)
                    if detail:
                        notice = notice.merge(detail)
                    yield notice

                # ⚠️ 待联网校验：分页判定（总页数/下一页按钮）。先只取首页，避免空翻
                break

    async def _extract_list_items(self, page: Page) -> list[dict]:
        # 通用兜底：取结果区里指向详情的 <a>（href 含 bulletin/xxfbcmses 且带 id），
        # 标题取 title/文本，日期取行容器内文本。
        # ⚠️ 待联网校验：真实结果行 DOM 结构与详情 href 形态
        return await page.evaluate(
            """() => {
                const re = /\\d{4}-\\d{1,2}-\\d{1,2}/;
                const seen = new Set();
                const out = [];
                document.querySelectorAll('a[href]').forEach(a => {
                    const href = a.getAttribute('href') || '';
                    if (!/bulletin|xxfbcmses|\\/detail|notice/i.test(href)) return;
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
            await detail_page.goto(url, wait_until="networkidle", timeout=30000)
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
            logger.debug("cebpubservice.detail_failed", url=url[:80])
            return None
        finally:
            await detail_page.close()

    async def _extract_content(self, page: Page) -> str | None:
        # ⚠️ 待联网校验：正文容器选择器。先试常见命名，取不到回退 body 文本。
        for sel in [
            ".content",
            ".article-content",
            ".detail-content",
            "#content",
            ".bulletin-content",
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
