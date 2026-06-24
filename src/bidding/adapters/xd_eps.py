from __future__ import annotations

import asyncio
import re
from datetime import date, datetime
from typing import AsyncIterator

import structlog
from playwright.async_api import Page

from bidding.adapters.base import AdapterMeta, SiteAdapter
from bidding.adapters.registry import register
from bidding.models.enums import NoticeType
from bidding.models.schema import BidNotice

logger = structlog.get_logger()

_BASE_URL = "http://eps.xd.com.cn:8881"

# 西电完整列表需登录（/HomeSite/Site/NewsGroupList 返回"无权访问采购平台"，
# 且其 k= 密钥按会话绑定）。本适配器仅抓首页内联的每类前若干条公开条目，
# 不分页。完整历史需登录账号。

_DATE_RE = re.compile(r"(20\d{2}-\d{1,2}-\d{1,2})\s*$")


def _parse_date_str(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return datetime.strptime(s.strip()[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


@register
class XdEpsAdapter(SiteAdapter):
    meta = AdapterMeta(
        name="xd_eps",
        display_name="西电电子采购平台",
        base_url=_BASE_URL,
        notice_types=[
            NoticeType.BID_ANNOUNCEMENT,
            NoticeType.CHANGE_ANNOUNCEMENT,
            NoticeType.WIN_ANNOUNCEMENT,
            NoticeType.NON_BID_ANNOUNCEMENT,
        ],
        requires_login=False,
        rate_limit=1.0,
    )

    async def scrape_list(
        self, page: Page, notice_type: NoticeType
    ) -> AsyncIterator[BidNotice]:
        try:
            await page.goto(f"{_BASE_URL}/", wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(2)
        except Exception:
            logger.warning("xd_eps.home_failed")
            return

        text = await page.evaluate("() => document.body.innerText || ''")
        items = self._parse_inline(text)
        logger.info(
            "xd_eps.home_parsed",
            total=len(items),
            by_type={k.value: len(v) for k, v in items.items()},
        )
        for title, pub, nt in items.get(notice_type, []):
            yield BidNotice(
                title=title,
                source_site=self.meta.name,
                source_url=_BASE_URL,
                notice_type=nt,
                publish_date=pub,
                content=None,
                raw_data={"title": title, "publish_date": str(pub)},
            )

    def _parse_inline(
        self, text: str
    ) -> dict[NoticeType, list[tuple[str, date | None, NoticeType]]]:
        """按首页 innerText 解析内联条目。

        每个条目形如「标题…<采购/变更/中标/竞卖>公告YYYY-MM-DD」。条目类型直接由
        标题尾缀判定（不依赖栏目容器），因此对 DOM 扁平化稳健。
        ⚠️ 待联网校验：若条目尾缀命名变化，需扩展 _classify_by_title。
        """
        result: dict[NoticeType, list[tuple[str, date | None, NoticeType]]] = {
            nt: [] for nt in self.meta.notice_types
        }
        for raw in text.splitlines():
            line = raw.strip().lstrip("-•*· ").strip()
            if not line:
                continue
            m = _DATE_RE.search(line)
            if not m:
                continue
            pub = _parse_date_str(m.group(1))
            title = line[: m.start()].strip()
            if len(title) < 4:
                continue
            nt = self._classify_by_title(title)
            result[nt].append((title, pub, nt))
        return result

    @staticmethod
    def _classify_by_title(title: str) -> NoticeType:
        if title.endswith(("中标公告", "成交公告")):
            return NoticeType.WIN_ANNOUNCEMENT
        if title.endswith(("变更公告", "更正公告")):
            return NoticeType.CHANGE_ANNOUNCEMENT
        if title.endswith("竞卖公告"):
            return NoticeType.NON_BID_ANNOUNCEMENT
        return NoticeType.BID_ANNOUNCEMENT

    async def scrape_detail(self, page: Page, url: str) -> BidNotice | None:
        # 详情页 NewsContentView?newsId= 多在登录墙后；公开首页条目无稳定详情链接，留空。
        return None
