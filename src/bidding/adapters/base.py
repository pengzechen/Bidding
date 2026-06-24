from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, AsyncIterator

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext, Page, Response

from bidding.models.enums import NoticeType
from bidding.models.schema import BidNotice


@dataclass
class AdapterMeta:
    name: str
    display_name: str
    base_url: str
    notice_types: list[NoticeType]
    requires_login: bool = False
    rate_limit: float = 2.0
    max_concurrent_pages: int = 1
    persistent_context: bool = False


@dataclass
class PaginationState:
    current_page: int = 1
    total_pages: int | None = None
    has_next: bool = True
    page_size: int = 20


class SiteAdapter(ABC):
    meta: AdapterMeta

    def __init__(self, config: dict | None = None):
        self.config = config or {}
        self._pagination = PaginationState()

    async def on_context_created(self, context: BrowserContext) -> None:
        pass

    async def login(self, page: Page) -> bool:
        return True

    @abstractmethod
    async def scrape_list(
        self, page: Page, notice_type: NoticeType
    ) -> AsyncIterator[BidNotice]:
        ...

    @abstractmethod
    async def scrape_detail(self, page: Page, url: str) -> BidNotice | None:
        ...

    async def get_list_url(self, notice_type: NoticeType, page_num: int = 1) -> str:
        raise NotImplementedError

    async def has_next_page(self, page: Page) -> bool:
        return self._pagination.has_next

    async def goto_next_page(self, page: Page) -> bool:
        raise NotImplementedError

    def get_api_patterns(self) -> list[str]:
        return []

    async def on_api_response(self, response: Response) -> list[BidNotice]:
        return []

    def _resolve_url(self, href: str) -> str:
        if href.startswith("http"):
            return href
        if href.startswith("/"):
            return f"{self.meta.base_url}{href}"
        return f"{self.meta.base_url}/{href}"
