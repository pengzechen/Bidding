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

_BASE_URL = "http://ep.jxic.com"

# 江投集团电子采购平台（Nuxt.js SSR，curl 可拿真实 HTML）。
# 详情页：/notice/<id>
# 列表页：/notice/list?noticeType=<n>（首页 nav 出现过 noticeType=3、purchase-5 等）
# ⚠️ 待联网校验：
# 1) 列表页确切路径与 noticeType 取值（招标/采购/变更/候选人/中标 各自的 code）需在站点确认。
# 2) 条目精确选择器与分页方式（?page= 还是 Nuxt 路由）需从真实渲染确认。
# 公告类型 → (noticeType 码, 标签)
_NOTICE_TYPES: dict[NoticeType, list[tuple[str, str]]] = {
    NoticeType.BID_ANNOUNCEMENT: [("1", "招标公告")],
    NoticeType.NON_BID_ANNOUNCEMENT: [("3", "采购公告")],
    NoticeType.CHANGE_ANNOUNCEMENT: [("4", "变更公告")],
    NoticeType.CANDIDATE_PUBLICITY: [("5", "候选人公示")],
    NoticeType.WIN_ANNOUNCEMENT: [("6", "中标公告")],
    # TODO(联网校验): 上述 code 为推测，需用站点真实分类码校正
}


def _parse_date_str(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return datetime.strptime(s.strip()[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _notice_id_from_href(href: str) -> str | None:
    import re

    m = re.search(r"/notice/(\d+)", href)
    return m.group(1) if m else None


@register
class JxicAdapter(SiteAdapter):
    meta = AdapterMeta(
        name="jxic",
        display_name="江投集团电子采购平台",
        base_url=_BASE_URL,
        notice_types=list(_NOTICE_TYPES.keys()),
        requires_login=False,
        rate_limit=1.0,
    )

    def __init__(self, config: dict | None = None):
        super().__init__(config)
        self._pagination = PaginationState(page_size=20)

    async def scrape_list(
        self, page: Page, notice_type: NoticeType
    ) -> AsyncIterator[BidNotice]:
        sub_types = _NOTICE_TYPES.get(notice_type)
        if not sub_types:
            return

        for type_code, label in sub_types:
            logger.info(
                "jxic.scrape_subtype",
                notice_type=notice_type.value,
                sub_type=label,
                code=type_code,
            )
            page_no = 1
            while True:
                # ⚠️ 待联网校验：列表路径与分页参数。先按 /notice/list?noticeType=&page= 试探
                url = f"{_BASE_URL}/notice/list?noticeType={type_code}&page={page_no}"
                try:
                    await page.goto(url, wait_until="networkidle", timeout=30000)
                    await asyncio.sleep(1)
                except Exception:
                    logger.warning("jxic.goto_failed", url=url)
                    break

                items = await self._extract_list_items(page)
                logger.info(
                    "jxic.list_page",
                    sub_type=label,
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

                # ⚠️ 待联网校验：分页结构未确认，先只取首页，避免空翻
                break

    async def _extract_list_items(self, page: Page) -> list[dict]:
        # 通用兜底：取所有指向 /notice/<id> 的 <a>，标题取文本/title，日期取行容器内文本
        # ⚠️ 待联网校验：真实列表项 DOM 结构
        return await page.evaluate(
            """() => {
                const re = /\\d{4}-\\d{1,2}-\\d{1,2}/;
                const seen = new Set();
                const out = [];
                document.querySelectorAll('a[href*="/notice/"]').forEach(a => {
                    const href = a.getAttribute('href') || '';
                    if (!/\\/notice\\/\\d+/.test(href)) return;  // 跳过 /notice/list
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
            notice_id=_notice_id_from_href(href),
            publish_date=_parse_date_str(item.get("date")),
            content=None,
            raw_data=item,
        )

    async def _fetch_detail(self, page: Page, url: str) -> BidNotice | None:
        if not url:
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
            logger.debug("jxic.detail_failed", url=url[:80])
            return None
        finally:
            await detail_page.close()

    async def _extract_content(self, page: Page) -> str | None:
        # ⚠️ 待联网校验：正文容器选择器。先试常见命名，取不到回退 body 文本。
        for sel in [
            ".notice-content",
            ".content",
            ".article-content",
            ".detail-content",
            "#content",
            ".rich-text",
        ]:
            el = await page.query_selector(sel)
            if el:
                text = await el.inner_text()
                if text and len(text.strip()) > 20:
                    return text.strip()
        return None

    async def scrape_detail(self, page: Page, url: str) -> BidNotice | None:
        return await self._fetch_detail(page, url)
