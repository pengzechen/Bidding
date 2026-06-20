from __future__ import annotations

import structlog

from bidding.models.schema import BidNotice
from bidding.storage.repository import NoticeRepository

logger = structlog.get_logger()


class Pipeline:
    def __init__(self):
        self._repo = NoticeRepository()
        self._saved_count = 0

    async def process(self, notice: BidNotice) -> bool:
        notice = self._clean(notice)
        record = await self._repo.save(notice)
        if record is None:
            return False
        self._saved_count += 1
        logger.info(
            "pipeline.saved",
            title=notice.title[:60],
            site=notice.source_site,
            id=record.id,
            total=self._saved_count,
        )
        return True

    def _clean(self, notice: BidNotice) -> BidNotice:
        data = notice.model_dump()
        if data.get("title"):
            data["title"] = data["title"].strip()
        if data.get("content"):
            data["content"] = data["content"].strip()
        return BidNotice(**data)

    @property
    def saved_count(self) -> int:
        return self._saved_count
