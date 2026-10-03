"""Typed owner settings stored in the `setting` table (editable in the panel).

Each group is a pydantic model; missing keys fall back to defaults, so new fields never break old data.
"""

from decimal import Decimal

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.models import Setting


class Requisites(BaseModel):
    """Executor requisites. Filled by the owner in the panel, never hard-coded."""

    brand: str = "МедПроект"
    tagline: str = "Экспертное проектирование медицинских пространств"
    legal_name: str = ""  # «ИП Иванов Иван Иванович»
    short_name: str = ""  # «ИП Иванов И.И.»
    inn: str = ""
    ogrnip: str = ""
    address: str = ""
    bank_name: str = ""
    bank_bik: str = ""
    bank_account: str = ""
    bank_corr_account: str = ""
    phone: str = ""
    email: str = ""
    website: str = ""
    city: str = "г. Москва"
    okved_note: str = ""
    gip_note: str = ""  # «ГИП ... (НОПРИЗ, стаж 20 лет)»

    @property
    def is_complete(self) -> bool:
        return all([self.legal_name, self.inn, self.ogrnip, self.phone, self.email])


class ProposalDefaults(BaseModel):
    validity_days: int = 30
    duration_text: str = "до 60 рабочих дней с даты получения аванса и исходных данных"
    payment_options: list[str] = Field(
        default_factory=lambda: [
            "предоплата 50%, окончательный расчёт 50% после подписания актов",
            "предоплата 40%, 30% по промежуточному этапу, 30% после подписания актов",
        ]
    )
    vat_note: str = "НДС не облагается (упрощённая система налогообложения). В стоимость включены все налоги и сборы."
    intro_text: str = (
        "Инжиниринговая компания «{brand}» подготовила предложение на выполнение проектных работ "
        "по объекту «{object}». Мы работаем только с медицинскими организациями и разрабатываем "
        "архитектурные, технологические и инженерные решения с учётом требований СанПиН, "
        "Роспотребнадзора и МЧС России."
    )
    quality_text: str = (
        "Проектирование выполняется в лицензионном программном обеспечении (AutoCAD, Revit, nanoCAD). "
        "Решения разрабатываются с учётом действующих норм для объектов здравоохранения; "
        "замечания заказчика и экспертизы устраняются без дополнительной оплаты."
    )
    advantages: list[str] = Field(
        default_factory=lambda: [
            "Комплексный подход: от обследования объекта до рабочей документации и авторского надзора.",
            "Узкая медицинская специализация: зонирование, потоки, помещения с особыми требованиями к чистоте.",
            "Единая точка ответственности: согласованность архитектурных, технологических и инженерных решений.",
        ]
    )
    signature: str = "С уважением к вашему бизнесу,\nкоманда «{brand}»"


class SendingRules(BaseModel):
    paused: bool = False  # global "pause all sending"
    daily_limit: int = 30
    org_interval_days: int = 3
    warmup_enabled: bool = True
    warmup_start: int = 5
    warmup_step: int = 3
    double_confirm_from: Decimal = Decimal("1000000")  # proposals from this amount need a second confirmation
    send_window_start_hour: int = 9  # Moscow time
    send_window_end_hour: int = 19
    work_days_only: bool = True
    unsubscribe_text: str = (
        "Если такие предложения вам неинтересны, ответьте на это письмо словом «СТОП», и мы больше не напишем."
    )


GROUPS: dict[str, type[BaseModel]] = {
    "requisites": Requisites,
    "proposal_defaults": ProposalDefaults,
    "sending_rules": SendingRules,
}


def _key(model: type[BaseModel]) -> str:
    return next(k for k, v in GROUPS.items() if v is model)


async def load[M: BaseModel](db: AsyncSession, model: type[M]) -> M:
    row = await db.get(Setting, _key(model))
    return model.model_validate(row.value if row else {})


def load_sync[M: BaseModel](db: Session, model: type[M]) -> M:
    row = db.get(Setting, _key(model))
    return model.model_validate(row.value if row else {})


async def save(db: AsyncSession, value: BaseModel, user_id: int | None = None) -> None:
    key = _key(type(value))
    data = value.model_dump(mode="json")
    row = await db.get(Setting, key)
    if row is None:
        db.add(Setting(key=key, value=data, updated_by_id=user_id))
    else:
        row.value = data
        row.updated_by_id = user_id


def save_sync(db: Session, value: BaseModel, user_id: int | None = None) -> None:
    key = _key(type(value))
    data = value.model_dump(mode="json")
    row = db.get(Setting, key)
    if row is None:
        db.add(Setting(key=key, value=data, updated_by_id=user_id))
    else:
        row.value = data
        row.updated_by_id = user_id
