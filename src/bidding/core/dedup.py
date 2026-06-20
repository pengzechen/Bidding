from __future__ import annotations

import structlog

from bidding.storage.repository import NoticeRepository

logger = structlog.get_logger()


class DedupChecker:
    def __init__(self):
        self._seen: set[str] = set()
        self._repo = NoticeRepository()

    async def is_duplicate(self, content_hash: str) -> bool:
        if content_hash in self._seen:
            return True
        exists = await self._repo.exists_by_hash(content_hash)
        if exists:
            self._seen.add(content_hash)
            return True
        self._seen.add(content_hash)
        return False

    async def warm_up(self, site_name: str):
        hashes = await self._repo.get_recent_hashes(site_name, days=30)
        self._seen.update(hashes)
        logger.info("dedup.warm_up", site=site_name, loaded=len(hashes))
