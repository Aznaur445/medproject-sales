"""Commercial tender platforms (ЭТП) and aggregators where private medical chains publish procurements.

What the service may do automatically is decided per platform (researched 2026-10, see docs/SOURCES.md):
* every platform is searched through the official Yandex Search API with `site:` queries (public indexed pages);
* the procedure page and its documents are downloaded only where they are public (`auto_documents=True`) and
  robots.txt allows it; B2B-Center forbids automated collection in its rules, Bidzaar and most others show
  documentation only after login — for them the owner gets a link and sends the ТЗ to the bot;
* e-mail alerts from platforms (owner's subscription) are parsed by the e-mail source; known procedure links are
  recognised by `url_re`.
"""

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Platform:
    key: str
    name: str
    domains: tuple[str, ...]
    url_re: str  # public procedure page
    queries: tuple[str, ...]
    auto_documents: bool  # documents public and automated download not forbidden
    docs_note: str
    alerts_note: str = "уведомления по ключевым словам — в личном кабинете (нужна бесплатная регистрация)"
    medical_buyers: tuple[str, ...] = ()
    government_re: str | None = None  # procedure ids that are 44-ФЗ/223-ФЗ on mixed platforms
    paid: bool = False
    tags: tuple[str, ...] = field(default=())

    def matches(self, url: str) -> bool:
        host = urlsplit(url).netloc.lower().split(":")[0]
        return any(host == d or host.endswith("." + d) for d in self.domains)

    def is_procedure(self, url: str) -> bool:
        return self.matches(url) and re.search(self.url_re, url) is not None

    def is_government(self, url: str) -> bool:
        return bool(self.government_re and re.search(self.government_re, url))


MED_DESIGN = ("проектирование клиники", "проектирование медицинского центра", "проектная документация медицинского")

PLATFORMS: tuple[Platform, ...] = (
    Platform(
        key="bidzaar",
        name="Bidzaar",
        domains=("bidzaar.com",),
        url_re=r"/app/process/",
        queries=tuple(f"site:bidzaar.com/app/process {q}" for q in MED_DESIGN),
        auto_documents=False,
        docs_note="подробности и документы — после бесплатной регистрации поставщика",
        alerts_note="бесплатные уведомления по ключевым словам после регистрации; API поставщика (токен в профиле)",
        medical_buyers=("СМ-Клиника", "Медси", "ЕМС", "Медскан", "Хеликс", "К+31"),
    ),
    Platform(
        key="b2b_center",
        name="B2B-Center",
        domains=("b2b-center.ru",),
        url_re=r"/market/.*tender-\d+|/market/view\.html\?id=\d+",
        queries=tuple(f"site:b2b-center.ru/market {q}" for q in MED_DESIGN),
        auto_documents=False,
        docs_note="регламент площадки запрещает автоматический сбор информации и файлов — только ссылка",
        medical_buyers=("Медси", "ЕМС", "Инвитро", "Медскан", "АО «Медицина»"),
    ),
    Platform(
        key="roseltorg",
        name="Росэлторг (коммерческие, COM)",
        domains=("roseltorg.ru",),
        url_re=r"/procedure/COM\d+",
        queries=tuple(f"site:roseltorg.ru/procedure/COM {q}" for q in MED_DESIGN[:2]),
        auto_documents=True,
        docs_note="просмотр и документы без регистрации",
        medical_buyers=("Мать и дитя",),
        government_re=r"/procedure/\d{19}",  # 19-digit numbers are 44-ФЗ notices
    ),
    Platform(
        key="tektorg",
        name="ТЭК-Торг",
        domains=("tektorg.ru",),
        url_re=r"/market/procedures/\d+",
        queries=("site:tektorg.ru/market/procedures проектирование клиники",),
        auto_documents=True,
        docs_note="лоты и вкладка «Документация процедуры» открыты без регистрации",
    ),
    Platform(
        key="fabrikant",
        name="Фабрикант",
        domains=("fabrikant.ru",),
        url_re=r"/v2/trades/procedure/",
        queries=("site:fabrikant.ru/v2/trades/procedure проектирование медицинского",),
        auto_documents=True,
        docs_note="поиск и просмотр без регистрации, документация частично открыта",
    ),
    Platform(
        key="tenderpro",
        name="Tender.Pro",
        domains=("tender.pro",),
        url_re=r"/api/tender/\d+/view_public",
        queries=("site:tender.pro проектирование клиники",),
        auto_documents=True,
        docs_note="открытая карточка view_public; полные документы — после бесплатного входа",
        alerts_note="уведомления в кабинете; есть JSON API и выгрузка ленты процедур",
    ),
    Platform(
        key="etpgpb",
        name="ЭТП ГПБ (Закупки.Бизнес)",
        domains=("etpgpb.ru",),
        url_re=r"/procedures?/",
        queries=("site:new.etpgpb.ru/procedures проектирование медицинского",),
        auto_documents=True,
        docs_note="просмотр без регистрации; скачивание документов — как разрешит площадка",
    ),
    Platform(
        key="medsi",
        name="ЭТП Медси",
        domains=("etp.medsi.ru",),
        url_re=r"/trades/\d+",
        queries=("site:etp.medsi.ru проектирование", "site:etp.medsi.ru техническое задание"),
        auto_documents=True,
        docs_note="собственная площадка сети Медси, файлы ТЗ открыты",
        medical_buyers=("Медси",),
    ),
    Platform(
        key="lot_online",
        name="РАД / Lot-online (тендеры)",
        domains=("tender.lot-online.ru",),
        url_re=r"/etp/app/OfferCard|/etp/downloadppf",
        queries=("site:tender.lot-online.ru техническое задание проектирование медицинского",),
        auto_documents=True,
        docs_note="ТЗ в открытом доступе",
    ),
    Platform(
        key="otc",
        name="OTC-tender",
        domains=("otc.ru",),
        url_re=r"/tender/\d+",
        queries=("site:otc.ru/tender проектирование клиники",),
        auto_documents=True,
        docs_note="карточка открыта, документы — как разрешит площадка",
    ),
    Platform(
        key="onlinecontract",
        name="OnlineContract",
        domains=("onlinecontract.ru",),
        url_re=r"/tenders/\d+",
        queries=("site:onlinecontract.ru проектирование медицинского",),
        auto_documents=True,
        docs_note="карточка открыта",
    ),
    Platform(
        key="rostender",
        name="Ростендер (агрегатор)",
        domains=("rostender.info",),
        url_re=r"/tender/\d+|/region/.+-tender-",
        queries=("site:rostender.info/tender проектирование клиники",),
        auto_documents=False,
        docs_note="агрегатор: ведёт на исходную площадку, документы — там",
        alerts_note="бесплатная ежедневная рассылка по форме; платные уведомления",
        paid=True,
    ),
    Platform(
        key="kontur",
        name="Контур.Закупки (агрегатор)",
        domains=("zakupki.kontur.ru",),
        url_re=r"/IS?\d+|/\d{6,}",
        queries=("site:zakupki.kontur.ru проектирование клиники",),
        auto_documents=False,
        docs_note="агрегатор по подписке: документы — у подписчиков",
        paid=True,
    ),
)


def platform_of(url: str | None) -> Platform | None:
    if not url:
        return None
    return next((p for p in PLATFORMS if p.matches(url)), None)


def platform_queries() -> list[str]:
    return [q for p in PLATFORMS for q in p.queries]
