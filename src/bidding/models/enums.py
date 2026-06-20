from enum import Enum


class NoticeType(str, Enum):
    BID_ANNOUNCEMENT = "bid_announcement"
    PREQUALIFICATION = "prequalification"
    NON_BID_ANNOUNCEMENT = "non_bid_announcement"
    CHANGE_ANNOUNCEMENT = "change_announcement"
    CANDIDATE_PUBLICITY = "candidate_publicity"
    WIN_ANNOUNCEMENT = "win_announcement"
    TERMINATION = "termination"


class ProcurementMethod(str, Enum):
    PUBLIC_BID = "public_bid"
    INVITED_BID = "invited_bid"
    INQUIRY = "inquiry"
    COMPETITIVE_PRICE = "competitive_price"
    COMPETITIVE_TALK = "competitive_talk"
    SOLE_SOURCE = "sole_source"


class ProjectCategory(str, Enum):
    GOODS = "goods"
    ENGINEERING = "engineering"
    SERVICE = "service"
