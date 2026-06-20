from __future__ import annotations

import asyncio

import structlog
from playwright.async_api import async_playwright
from sqlalchemy import select, update

from bidding.models.db import BidNoticeRecord
from bidding.storage.database import get_session_factory, init_db

logger = structlog.get_logger()

_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)


class DetailFetcher:
    def __init__(self, *, headless: bool = True, limit: int = 50):
        self.headless = headless
        self.limit = limit
        self.updated_count = 0

    async def run(self, site_names: list[str] | None = None):
        await init_db()
        factory = get_session_factory()

        async with factory() as session:
            stmt = (
                select(BidNoticeRecord)
                .where(BidNoticeRecord.content.is_(None))
                .order_by(BidNoticeRecord.id)
                .limit(self.limit)
            )
            if site_names:
                stmt = stmt.where(BidNoticeRecord.source_site.in_(site_names))
            result = await session.execute(stmt)
            records = result.scalars().all()

        if not records:
            logger.info("detail_fetcher.nothing_to_do")
            return

        logger.info("detail_fetcher.start", total=len(records))

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=self.headless,
                args=["--disable-blink-features=AutomationControlled"],
            )
            context = await browser.new_context(
                viewport={"width": 1920, "height": 1080},
                user_agent=_DEFAULT_UA,
            )
            try:
                for i, record in enumerate(records):
                    logger.info(
                        "detail_fetcher.fetching",
                        progress=f"{i + 1}/{len(records)}",
                        title=record.title[:50],
                    )
                    content, content_html = await self._fetch_one(
                        context, record.source_url
                    )
                    if content:
                        async with factory() as session:
                            await session.execute(
                                update(BidNoticeRecord)
                                .where(BidNoticeRecord.id == record.id)
                                .values(content=content, content_html=None)
                            )
                            await session.commit()
                        self.updated_count += 1
                        logger.info(
                            "detail_fetcher.saved",
                            id=record.id,
                            chars=len(content),
                        )
                    await asyncio.sleep(2)
            finally:
                await context.close()
                await browser.close()

    async def _fetch_one(self, context, url: str) -> tuple[str, str]:
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="networkidle", timeout=30000)
            await asyncio.sleep(1)

            for selector in [
                ".Main_content",
                ".article-content",
                ".detail-content",
                ".content",
                "#content",
                "article",
            ]:
                el = await page.query_selector(selector)
                if el:
                    text = (await el.inner_text()).strip()
                    html = (await el.inner_html()).strip()
                    if len(text) > 20:
                        return text, html

            text = (await page.inner_text("body")).strip()
            html = (await page.inner_html("body")).strip()
            return text, html
        except Exception:
            logger.exception("detail_fetcher.error", url=url)
            return "", ""
        finally:
            await page.close()
