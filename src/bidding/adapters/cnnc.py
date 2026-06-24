"""中核集团电子采购平台适配器 (one.cnncecp.com)

站点特征：
- Vue SPA (vue-antd-pro) + Ant Design
- 部署瑞数信息(RS) WAF — 必须用persistent context绕过
- Portal页面提供招标/非招标/资格预审/中标结果，每类最新12条
- 详情页在 www.cnncecp.com，有滑块验证码(anji-plus)
  - OpenCV模板匹配识别缺口位置，模拟人类拖拽通过
  - 正文以PDF内嵌(pdfjs viewer)形式展示，通过浏览器context下载+PyMuPDF提取
- API: /cnnc-pm-api/S_PM_IND_XXX?y1AmuISe=<signed_token> (POST JSON)

已知API方法:
- queryTenderBody → 招标公告 (tenderBody.jkzbgg)
- queryPreBody → 资格预审公告 (preBody.jkzgysgg)
- queryWinningBody → 中标结果公示 (winningBody.jkzbjggs)
- queryPurchaseBody → 非招标公告 (purchaseBody.jkcggggs)
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import AsyncIterator

import structlog
from playwright.async_api import Page

from bidding.adapters.base import AdapterMeta, SiteAdapter
from bidding.adapters.registry import register
from bidding.models.enums import NoticeType
from bidding.models.schema import BidNotice

logger = structlog.get_logger()

_BASE_URL = "https://one.cnncecp.com"
_PORTAL_URL = f"{_BASE_URL}/cnnc-pm-web/#/portal"

_TAB_MAP: dict[NoticeType, dict] = {
    NoticeType.BID_ANNOUNCEMENT: {
        "top_tab": "招标信息",
        "sub_tab": "招标公告",
        "method": "queryTenderBody",
        "result_path": ("resultList", "tenderBody", "jkzbgg"),
    },
    NoticeType.PREQUALIFICATION: {
        "top_tab": "招标信息",
        "sub_tab": "资格预审公告",
        "method": "queryPreBody",
        "result_path": ("resultList", "preBody", "jkzgysgg"),
    },
    NoticeType.WIN_ANNOUNCEMENT: {
        "top_tab": "招标信息",
        "sub_tab": "中标结果公示",
        "method": "queryWinningBody",
        "result_path": ("resultList", "winningBody", "jkzbjggs"),
    },
    NoticeType.NON_BID_ANNOUNCEMENT: {
        "top_tab": "非招标信息",
        "sub_tab": None,
        "method": "queryPurchaseBody",
        "result_path": ("resultList", "purchaseBody", "jkcggggs"),
    },
}


def _parse_datetime(s: str | None) -> datetime | None:
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s.strip(), fmt)
        except ValueError:
            continue
    return None


def _extract_items(data: dict, result_path: tuple) -> list[dict]:
    obj = data
    for key in result_path:
        if isinstance(obj, dict):
            obj = obj.get(key, {})
        else:
            return []
    return obj if isinstance(obj, list) else []


@register
class CnncAdapter(SiteAdapter):
    meta = AdapterMeta(
        name="cnnc",
        display_name="中核集团",
        base_url=_BASE_URL,
        notice_types=[
            NoticeType.BID_ANNOUNCEMENT,
            NoticeType.PREQUALIFICATION,
            NoticeType.WIN_ANNOUNCEMENT,
            NoticeType.NON_BID_ANNOUNCEMENT,
        ],
        requires_login=False,
        rate_limit=2.0,
        persistent_context=True,
    )

    def __init__(self, config: dict | None = None):
        super().__init__(config)
        self._portal_loaded = False
        self._cached_responses: dict[str, dict] = {}

    async def scrape_list(
        self, page: Page, notice_type: NoticeType
    ) -> AsyncIterator[BidNotice]:
        tab_info = _TAB_MAP.get(notice_type)
        if not tab_info:
            return

        if not self._portal_loaded:
            if not await self._load_portal(page):
                logger.warning("cnnc.portal_load_failed")
                return

        method_name = tab_info["method"]

        # Check if we already captured this during initial load
        if method_name in self._cached_responses:
            items = _extract_items(self._cached_responses[method_name], tab_info["result_path"])
        else:
            items = await self._click_and_capture(page, tab_info)

        if not items:
            logger.info("cnnc.no_items", notice_type=notice_type.value)
            return

        logger.info("cnnc.items_found", notice_type=notice_type.value, count=len(items))
        for item in items:
            notice = self._parse_item(item, notice_type)
            if notice:
                yield notice

    async def scrape_detail(self, page: Page, url: str) -> BidNotice | None:
        """Fetch detail page content, solving slider captcha if needed."""
        from bidding.utils.captcha import solve_slider_gap

        captcha_data: dict = {}

        async def on_resp(resp):
            if "/captcha/get" in resp.url:
                try:
                    text = await resp.text()
                    if text.startswith("{"):
                        captcha_data["get"] = json.loads(text)
                except Exception:
                    pass

        page.on("response", on_resp)
        try:
            await page.goto(url, wait_until="networkidle", timeout=30000)
            await asyncio.sleep(3)
        finally:
            page.remove_listener("response", on_resp)

        for attempt in range(5):
            body_text = await page.evaluate(
                "() => document.body.innerText.substring(0, 100)"
            )
            if "安全验证" not in body_text and "captcha" not in page.url:
                break

            rep = captcha_data.get("get", {}).get("repData", {})
            if not rep:
                logger.warning("cnnc.detail.no_captcha_data", url=url)
                return None

            bg_b64 = rep.get("originalImageBase64", "")
            jig_b64 = rep.get("jigsawImageBase64", "")
            if not bg_b64 or not jig_b64:
                return None

            gap_x = solve_slider_gap(bg_b64, jig_b64)
            if gap_x is None:
                logger.warning("cnnc.detail.captcha_match_failed", url=url)
                return None

            img_w = 310
            display_w = 400
            drag_distance = gap_x * display_w / img_w

            if not await self._drag_slider(page, drag_distance):
                return None

            await asyncio.sleep(3)

            if "captcha" not in page.url:
                break

            captcha_data.clear()
            page.on("response", on_resp)
            try:
                await page.reload(wait_until="networkidle", timeout=20000)
                await asyncio.sleep(3)
            finally:
                page.remove_listener("response", on_resp)
        else:
            logger.warning("cnnc.detail.captcha_failed_all_attempts", url=url)
            return None

        content = await self._extract_pdf_content(page)
        if content:
            return BidNotice(
                title="",
                source_site=self.meta.name,
                source_url=url,
                notice_type=NoticeType.BID_ANNOUNCEMENT,
                content=content,
            )
        return None

    @staticmethod
    async def _extract_pdf_content(page: Page) -> str | None:
        """Download embedded PDF via browser context and extract text."""
        pdf_url = await page.evaluate(
            """() => {
                const iframe = document.querySelector('iframe[src*="viewer_cms"]');
                if (iframe) {
                    const m = iframe.src.match(/file=([^&]+)/);
                    if (m) return decodeURIComponent(m[1]);
                }
                return null;
            }"""
        )
        if not pdf_url:
            return None

        try:
            resp = await page.request.get(pdf_url)
            if resp.status != 200:
                return None
            body = await resp.body()
        except Exception:
            return None

        import tempfile
        from pathlib import Path
        from bidding.utils.pdf import extract_text_from_pdf

        tmp = Path(tempfile.mktemp(suffix=".pdf"))
        try:
            tmp.write_bytes(body)
            text = extract_text_from_pdf(tmp)
            return text if text and len(text) > 20 else None
        except Exception:
            return None
        finally:
            tmp.unlink(missing_ok=True)

    @staticmethod
    async def _drag_slider(page: Page, distance: float) -> bool:
        """Drag the captcha slider with human-like movement."""
        import random

        slider = page.locator(".verify-move-block")
        box = await slider.bounding_box()
        if not box:
            return False

        sx = box["x"] + box["width"] / 2
        sy = box["y"] + box["height"] / 2

        await page.mouse.move(sx, sy)
        await asyncio.sleep(random.uniform(0.2, 0.4))
        await page.mouse.down()
        await asyncio.sleep(random.uniform(0.05, 0.15))

        steps = random.randint(25, 40)
        for i in range(1, steps + 1):
            t = i / steps
            eased = 1 - (1 - t) ** 3
            x = sx + distance * eased
            y = sy + random.uniform(-1, 1)
            await page.mouse.move(x, y)
            await asyncio.sleep(random.uniform(0.008, 0.02))

        await page.mouse.move(sx + distance + random.uniform(0.5, 2), sy)
        await asyncio.sleep(0.05)
        await page.mouse.move(sx + distance, sy)
        await asyncio.sleep(random.uniform(0.1, 0.2))
        await page.mouse.up()
        return True

    async def _load_portal(self, page: Page) -> bool:
        """Load portal, wait for RS WAF to pass, and capture initial API data."""

        async def on_response(resp):
            ct = resp.headers.get("content-type", "")
            if "json" in ct and "cnnc-pm-api" in resp.url:
                try:
                    body = await resp.text()
                    data = json.loads(body)
                    method = data.get("methodName")
                    if method:
                        self._cached_responses[method] = data
                except Exception:
                    pass

        page.on("response", on_response)

        try:
            await page.goto(_PORTAL_URL, wait_until="load", timeout=30000)
            await asyncio.sleep(3)
            await page.mouse.move(500, 400)
            await asyncio.sleep(2)
            await page.mouse.move(700, 300)

            for _ in range(6):
                await asyncio.sleep(5)
                app_len = await page.evaluate(
                    "() => {"
                    "  const el = document.querySelector('#app');"
                    "  return el ? el.innerHTML.length : 0;"
                    "}"
                )
                if app_len > 1000:
                    self._portal_loaded = True
                    await asyncio.sleep(3)
                    return True

            logger.warning("cnnc.waf_blocked", msg="RS WAF未通过，SPA未渲染")
            return False
        finally:
            page.remove_listener("response", on_response)

    async def _click_and_capture(self, page: Page, tab_info: dict) -> list[dict]:
        """Click tab(s) and capture the triggered API response."""
        method_name = tab_info["method"]
        captured: list[dict] = []

        async def on_response(resp):
            ct = resp.headers.get("content-type", "")
            if "json" in ct and "cnnc-pm-api" in resp.url:
                try:
                    body = await resp.text()
                    data = json.loads(body)
                    if data.get("methodName") == method_name:
                        captured.append(data)
                except Exception:
                    pass

        page.on("response", on_response)
        try:
            top_tab = tab_info["top_tab"]
            sub_tab = tab_info.get("sub_tab")

            # Use force=True to bypass ink-bar intercept
            top_el = page.locator(
                f'.ant-tabs-bar >> .ant-tabs-tab:has-text("{top_tab}")'
            ).first
            await top_el.click(force=True)
            await asyncio.sleep(3)

            if sub_tab and sub_tab != "招标公告":
                sub_el = page.locator(
                    f'.ant-tabs-left >> .ant-tabs-tab:has-text("{sub_tab}")'
                ).first
                await sub_el.click(force=True)
                await asyncio.sleep(3)

            for _ in range(10):
                if captured:
                    break
                await asyncio.sleep(1)

            if not captured:
                return []

            return _extract_items(captured[0], tab_info["result_path"])
        finally:
            page.remove_listener("response", on_response)

    def _parse_item(self, item: dict, notice_type: NoticeType) -> BidNotice | None:
        title = (item.get("title") or "").strip()
        if not title:
            return None

        source_url = item.get("url") or _PORTAL_URL
        publish_date = _parse_datetime(item.get("pendDate"))
        deadline = _parse_datetime(item.get("endDate"))

        return BidNotice(
            title=title,
            source_site=self.meta.name,
            source_url=source_url,
            notice_type=notice_type,
            publish_date=publish_date.date() if publish_date else None,
            deadline=deadline,
            content=None,
        )
