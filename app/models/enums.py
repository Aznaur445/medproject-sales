import enum


class UserRole(enum.StrEnum):
    OWNER = "owner"
    VIEWER = "viewer"


class SourceKind(enum.StrEnum):
    EMAIL_ALERT = "email_alert"  # subscription e-mails from trading platforms
    WEB_PAGE = "web_page"  # public procurement pages of clinic chains
    TELEGRAM = "telegram"
    BOARD = "board"  # Avito and similar (manual check unless an official API exists)
    WEBSITE_FORM = "website_form"
    MANUAL = "manual"


class SourceLegalStatus(enum.StrEnum):
    ALLOWED = "allowed"
    MANUAL_CHECK = "manual_check"  # cannot be fetched automatically; owner gets a link


class ListingStatus(enum.StrEnum):
    FOUND = "found"
    SHORTLISTED = "shortlisted"
    PROPOSAL_PENDING = "proposal_pending"
    PROPOSAL_SENT = "proposal_sent"
    NEGOTIATION = "negotiation"
    CONTRACT = "contract"
    WON = "won"
    LOST = "lost"
    REJECTED = "rejected"
    EXCLUDED = "excluded"  # auto-excluded (e.g. government customer, stop words, low score)


class OrgType(enum.StrEnum):
    COMMERCIAL = "commercial"
    GOVERNMENT = "government"
    UNKNOWN = "unknown"


class ChannelKind(enum.StrEnum):
    EMAIL = "email"
    PHONE = "phone"
    TELEGRAM = "telegram"
    MAX = "max"
    WHATSAPP = "whatsapp"
    WEB_FORM = "web_form"


class MessageDirection(enum.StrEnum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class MessageStatus(enum.StrEnum):
    DRAFT = "draft"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    QUEUED = "queued"
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"
    BOUNCED = "bounced"
    CANCELLED = "cancelled"
    RECEIVED = "received"


class ProposalStatus(enum.StrEnum):
    DRAFT = "draft"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    SENT = "sent"
    REJECTED = "rejected"


class ApprovalDecision(enum.StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"
    POSTPONED = "postponed"


class RiskLevel(enum.StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
