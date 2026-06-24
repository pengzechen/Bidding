from __future__ import annotations

import asyncio
import json
from datetime import date, datetime
from typing import AsyncIterator

import structlog
from playwright.async_api import Page

from bidding.adapters.base import AdapterMeta, PaginationState, SiteAdapter
from bidding.adapters.registry import register
from bidding.models.enums import NoticeType
from bidding.models.schema import BidNotice

logger = structlog.get_logger()

_BASE_URL = "https://zjzcw.iccec.cn"

# 从前端 app.js 逆向得到的公开（users/signup 匿名命名空间）接口：
#   POST /apis/jrw/common/users/signup/qryNoticePageList   列表 {pageNum, pageSize, ...}
#   POST /apis/jrw/common/users/signup/qryNoticeInfoDetails 详情
# SPA 路由：/announcementsList、/announcementDetail
_LIST_API = f"{_BASE_URL}/apis/jrw/common/users/signup/qryNoticePageList"
_DETAIL_API = f"{_BASE_URL}/apis/jrw/common/users/signup/qryNoticeInfoDetails"

# ⚠️ 待联网校验（重要）：
# 1) 签名：app.js 中存在 token/sign/nonce。直接用 fetch 调可能被拦（返回 code:999）。
#    若如此，请改用以下任一方式（页面上下文内自带签名）：
#    a) 复用 SPA 的 axios：在 page.evaluate 内通过 window 上的 vue 实例/$http 发请求；
#    b) 监听 SPA 自然请求：page.on('response') 捕获它自己发出的 qryNoticePageList 响应。
# 2) 请求体字段名（除 pageNum/pageSize 外的分类筛选字段）与响应字段名（标题/日期/id/详情字段）
#    均需在能访问站点时从真实响应确认后补全。
_PAGE_SIZE = 10


def _parse_date_str(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return datetime.strptime(s.strip()[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _first_present(d: dict, *keys):
    for k in keys:
        if d.get(k):
            return d[k]
    return None


@register
class IccecAdapter(SiteAdapter):
    meta = AdapterMeta(
        name="iccec",
        display_name="中交招采网",
        base_url=_BASE_URL,
        # ⚠️ 待联网校验：分类码确认后可拆分为招标/非招标/中标等
        notice_types=[NoticeType.BID_ANNOUNCEMENT],
        requires_login=False,
        rate_limit=1.0,
    )

    def __init__(self, config: dict | None = None):
        super().__init__(config)
        self._pagination = PaginationState(page_size=_PAGE_SIZE)

    async def scrape_list(
        self, page: Page, notice_type: NoticeType
    ) -> AsyncIterator[BidNotice]:
        # 先加载 SPA，让前端 JS 初始化（建立 token/签名所需的状态/cookie）
        try:
            await page.goto(f"{_BASE_URL}/", wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(3)
        except Exception:
            logger.warning("iccec.home_failed")
            return

        page_no = 1
        while True:
            data = await self._post_json(
                page, _LIST_API, {"pageNum": page_no, "pageSize": _PAGE_SIZE}
            )
            if not data:
                logger.warning("iccec.list_empty", page=page_no)
                break

            rows = self._extract_rows(data)
            total = self._extract_total(data)
            logger.info("iccec.list_page", page=page_no, items=len(rows), total=total)
            if not rows:
                break

            for item in rows:
                notice = self._parse_item(item, notice_type)
                if not notice:
                    continue
                detail = await self._fetch_detail(page, notice.notice_id)
                if detail:
                    notice = notice.merge(detail)
                yield notice

            total_pages = (total + _PAGE_SIZE - 1) // _PAGE_SIZE if total else 0
            if total_pages and page_no >= total_pages:
                break
            if len(rows) < _PAGE_SIZE:  # 没有更多
                break
            page_no += 1

    async def _post_json(self, page: Page, url: str, body: dict) -> dict | None:
        result = await page.evaluate(
            """async ([url, body]) => {
                try {
                    const r = await fetch(url, {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json;charset=UTF-8'},
                        body: JSON.stringify(body),
                    });
                    if (!r.ok) return null;
                    return await r.text();
                } catch(e) { return null; }
            }""",
            [url, body],
        )
        if not result:
            return None
        try:
            return json.loads(result)
        except json.JSONDecodeError:
            return None

    def _extract_rows(self, data: dict) -> list[dict]:
        # ⚠️ 待联网校验：真实响应的列表字段名。取常见命名兜底。
        for key in ("rows", "list", "records", "data", "items", "result"):
            v = data.get(key)
            if isinstance(v, list):
                return v
            if isinstance(v, dict):
                for sub in ("rows", "list", "records", "data", "items"):
                    if isinstance(v.get(sub), list):
                        return v[sub]
        return []

    def _extract_total(self, data: dict) -> int:
        for key in ("total", "totalCount", "totalRecords", "totalSize", "count"):
            v = data.get(key)
            if isinstance(v, int):
                return v
            if isinstance(v, str) and v.isdigit():
                return int(v)
        return 0

    def _parse_item(self, item: dict, notice_type: NoticeType) -> BidNotice | None:
        title = (_first_present(item, "title", "noticeName", "name", "noticeTitle") or "").strip()
        if not title:
            return None
        notice_id = _first_present(item, "id", "noticeId", "noticeCode")
        publish_date = _parse_date_str(
            _first_present(item, "publishTime", "publishDate", "releaseTime", "createTime")
        )
        source_url = f"{_BASE_URL}/announcementDetail?id={notice_id}" if notice_id else _BASE_URL
        return BidNotice(
            title=title,
            source_site=self.meta.name,
            source_url=source_url,
            notice_type=notice_type,
            notice_id=str(notice_id) if notice_id else None,
            publish_date=publish_date,
            content=None,
            raw_data=item,
        )

    async def _fetch_detail(self, page: Page, notice_id: str | None) -> BidNotice | None:
        if not notice_id:
            return None
        data = await self._post_json(page, _DETAIL_API, {"id": notice_id})
        if not data:
            return None
        content = (
            _first_present(data, "content", "noticeContent", "body", "htmlContent")
            or None
        )
        if not content:
            return None
        return BidNotice(
            title="",
            source_site=self.meta.name,
            source_url=f"{_BASE_URL}/announcementDetail?id={notice_id}",
            notice_type=NoticeType.BID_ANNOUNCEMENT,
            content=content,
        )

    async def scrape_detail(self, page: Page, url: str) -> BidNotice | None:
        import re

        m = re.search(r"[?&]id=([^&]+)", url)
        return await self._fetch_detail(page, m.group(1) if m else None)
