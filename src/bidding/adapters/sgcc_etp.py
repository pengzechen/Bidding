from __future__ import annotations

from bidding.adapters.base import AdapterMeta
from bidding.adapters.registry import register
from bidding.adapters.sgcc_ecp import SgccEcpAdapter
from bidding.models.enums import NoticeType


@register
class SgccEtpAdapter(SgccEcpAdapter):
    meta = AdapterMeta(
        name="sgcc_etp",
        display_name="国网电工交易",
        base_url="https://sgccetp.com.cn",
        notice_types=[
            NoticeType.BID_ANNOUNCEMENT,
            NoticeType.NON_BID_ANNOUNCEMENT,
            NoticeType.WIN_ANNOUNCEMENT,
            NoticeType.CANDIDATE_PUBLICITY,
        ],
        requires_login=False,
        rate_limit=1.0,
    )

    _portal_path = "/portal"
    _api_path = "/ecpwcmcore/index"
