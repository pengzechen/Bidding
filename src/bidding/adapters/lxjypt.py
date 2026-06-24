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

_BASE_URL = "http://www.lxjypt.cn"

# 频道 → (栏目名, 公告类型)。JeeSite CMS 列表页：/w/list-<channel>.html
# ⚠️ 待联网校验：仅 list-2(工程建设) 已从首页确认；其余频道 ID 需在站点导航上核对后补全。
_CHANNELS: dict[int, tuple[str, NoticeType]] = {
    2: ("工程建设", NoticeType.BID_ANNOUNCEMENT),
    # TODO(联网校验): 补充 政府采购 / 土地矿业权 / 国有产权 / 限额以下 等频道的 channel id
    #   候选（从首页 nav 采到，selected=tab 序号）：
    #   list-24, list-28, list-29, list-36, list-40, list-92, list-102
}

_DATE_RE = re.compile(r"\d{4}-\d{1,2}-\d{1,2}")


def _parse_date_str(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return datetime.strptime(s.strip()[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _channel_id_from_view(href: str) -> str | None:
    m = re.search(r"/w/view-(\d+)-(\d+)\.html", href)
    return m.group(2) if m else None


@register
class LxjyptAdapter(SiteAdapter):
    meta = AdapterMeta(
        name="lxjypt",
        display_name="陇西县公共资源交易",
        base_url=_BASE_URL,
        notice_types=[nt for _, nt in _CHANNELS.values()],
        requires_login=False,
        rate_limit=1.0,
    )

    def __init__(self, config: dict | None = None):
        super().__init__(config)
        self._pagination = PaginationState(page_size=20)

    async def scrape_list(
        self, page: Page, notice_type: NoticeType
    ) -> AsyncIterator[BidNotice]:
        channels = [
            (cid, label) for cid, (label, nt) in _CHANNELS.items() if nt == notice_type
        ]
        if not channels:
            return

        for channel_id, label in channels:
            logger.info(
                "lxjypt.scrape_channel",
                notice_type=notice_type.value,
                channel=channel_id,
                label=label,
            )
            page_no = 1
            while True:
                # ⚠️ 待联网校验：分页方式。先按 JeeSite 常见的 ?pageParam 递增试探，
                # 第 1 页用 list-<c>.html，后续页若空则停。
                url = (
                    f"{_BASE_URL}/w/list-{channel_id}.html?selected=0"
                    if page_no == 1
                    else f"{_BASE_URL}/w/list-{channel_id}.html?selected=0&page={page_no}"
                )
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    await asyncio.sleep(1)
                except Exception:
                    logger.warning("lxjypt.goto_failed", url=url)
                    break

                items = await self._extract_list_items(page)
                logger.info(
                    "lxjypt.list_page",
                    channel=channel_id,
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

                # 仅有第一页确认可达；分页结构待联网确认，先只取首页，避免空翻
                break

    async def _extract_list_items(self, page: Page) -> list[dict]:
        # 通用锚点解析：取所有指向 /w/view-<c>-<id>.html 的 <a>，标题取文本，日期取最近父节点
        return await page.evaluate(
            """() => {
                const re = /\\d{4}-\\d{1,2}-\\d{1,2}/;
                const seen = new Set();
                const out = [];
                document.querySelectorAll('a[href*="/w/view-"]').forEach(a => {
                    const href = a.getAttribute('href') || '';
                    if (seen.has(href)) return;
                    seen.add(href);
                    const ctx = (a.closest('li,tr,dd') || a.parentElement || a).innerText || '';
                    const dm = ctx.match(re);
                    let title = (a.getAttribute('title') || a.innerText || '').trim();
                    out.push({href, title, date: dm ? dm[0] : null});
                });
                return out;
            }"""
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
            notice_id=_channel_id_from_view(href),
            publish_date=_parse_date_str(item.get("date")),
            project_category=None,
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
            logger.debug("lxjypt.detail_failed", url=url[:80])
            return None
        finally:
            await detail_page.close()

    async def _extract_content(self, page: Page) -> str | None:
        # ⚠️ 待联网校验：正文容器选择器。先尝试常见 JeeSite 正文容器，取不到则回退到 body 文本。
        for sel in [
            ".article-content",
            ".content",
            "#cms_content",
            ".detail-content",
            ".view-content",
        ]:
            el = await page.query_selector(sel)
            if el:
                text = await el.inner_text()
                if text and len(text.strip()) > 20:
                    return text.strip()
        return None

    async def scrape_detail(self, page: Page, url: str) -> BidNotice | None:
        return await self._fetch_detail(page, url)
