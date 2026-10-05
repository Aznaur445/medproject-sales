"""Strict relevance: the listing must be DESIGN work AND for a MEDICAL object (owner's niche).

The keyword filters (filtering.py) are broad and editable; this check is the niche guard on top of them:
* «проект договора», «в соответствии с проектной документацией», supply, construction-only, repair-only,
  maintenance and veterinary tenders are not design work for medical organisations;
* design + construction («проектирование и строительство») is kept and marked.
"""

import re
from dataclasses import dataclass, field

# Phrases where «проект» is not design work: removed before matching.
NOT_DESIGN_PHRASES = re.compile(
    r"проект\w*\s+(?:договор|контракт|соглашени|решени|приказ|положени|постановлени|закон|документа\s+о\s+закупк)\w*"
    r"|в\s+соответствии\s+с\s+(?:утвержд\w+\s+)?проектн\w+\s+документаци\w*"
    r"|согласно\s+проектн\w+\s+документаци\w*"
    r"|по\s+проекту\s+заказчика"
    r"|экспертиз\w+\s+проектн\w+\s+документаци\w*",
    re.IGNORECASE,
)
DESIGN = {
    "проектирование": r"\bпроектировани\w*|\bспроектировать\b|\bпроектировщик\w*",
    "проектные работы": r"\bпроектн\w*\s+работ\w*|\bпроектно-изыскательск\w*|\bпроектн\w*\s+решени\w*",
    "разработка документации": r"\bразработк\w*\s+(?:\w+\s+){0,2}(?:проектн\w*|рабоч\w*|проектно-сметн\w*)\s+документац\w*",
    "ПСД/ПД/РД": r"\bПСД\b|\bПД\b|\bРД\b|\bпроектно-сметн\w*\s+документац\w*|\bстади\w*\s+[«\"]?[ПР][»\"]?\b",
    "эскизный/дизайн-проект": r"\bэскизн\w*\s+проект\w*|\bдизайн-проект\w*|\bархитектурн\w*\s+(?:решени|концепци)\w*",
    "проект перепланировки": r"\bпроект\w*\s+(?:перепланировк|реконструкци|капитальн\w+\s+ремонт|размещени|организаци\w+\s+медицинск)\w*",
    "технологический проект": r"\bмедико-технологическ\w*|\bтехнологическ\w*\s+(?:проект|решени|задани|планировк)\w*",
    "авторский надзор": r"\bавторск\w*\s+надзор\w*",
}
MEDICAL = {
    "клиника/медцентр": r"\bклиник\w*|\bмедцентр\w*|\bмедицинск\w*\s+(?:центр|организаци|учреждени|объект|клиник|кабинет|помещени|комплекс|деятельност|назначени)\w*",
    "поликлиника/больница": r"\bполиклиник\w*|\bбольниц\w*|\bгоспитал\w*|\bстационар\w*|\bамбулатори\w*|\bтравмпункт\w*|\bхоспис\w*",
    "стоматология": r"\bстоматолог\w*|\bдентальн\w*",
    "лаборатория": r"\b(?:клинико-диагностическ|медицинск|ПЦР|диагностическ)\w*\s+лаборатори\w*|\bлаборатори\w*\s+(?:ПЦР|клинико|медицинск)\w*",
    "диагностика": r"\bдиагностическ\w*\s+центр\w*|\bМРТ\b|\bКТ\b|\bрентген\w*|\bмаммограф\w*|\bангиограф\w*",
    "роддом/ЭКО": r"\bродильн\w*|\bроддом\w*|\bперинатальн\w*|\bженск\w*\s+консультаци\w*|\bЭКО\b|\bрепродуктивн\w*",
    "реабилитация/санаторий": r"\bреабилитац\w*\s+центр\w*|\bсанатори\w*",
    "операционные/чистые помещения": r"\bоперационн\w*\s+(?:блок|зал)\w*|\bоперационн\w*\b|\bчист\w*\s+помещени\w*|\bасептическ\w*",
    "СанПиН для медорганизаций": r"\bСанПиН\s*2\.1\.3678|\bлицензировани\w*\s+медицинск\w*",
    "профильные отделения": r"\bдиализ\w*|\bонкологическ\w*|\bофтальмолог\w*|\bдерматолог\w*|\bкосметолог\w*",
}
NOT_MEDICAL = {
    "ветеринария": r"\bветеринар\w*|\bзоо\w*клиник\w*",
    "медицинские изделия/одежда": r"\bмедицинск\w*\s+(?:одежд|халат|маск|издели|расходн|мебел|оборудовани\w*\s+поставк)\w*",
}
WORK_ONLY = {
    "СМР": r"\bстроительно-монтажн\w*|\bСМР\b|\bстроительств\w*",
    "поставка": r"\bпоставк\w*",
    "ремонт": r"\b(?:текущ|косметическ|капитальн)\w*\s+ремонт\w*|\bремонтн\w*\s+работ\w*",
    "монтаж/пусконаладка": r"\bмонтаж\w*|\bпусконаладочн\w*",
    "обслуживание": r"\bтехническ\w*\s+обслуживани\w*|\bэксплуатаци\w*|\bуборк\w*|\bклининг\w*|\bохран\w*",
}


def _hits(patterns: dict[str, str], text: str) -> list[str]:
    return [name for name, rx in patterns.items() if re.search(rx, text, re.IGNORECASE)]


@dataclass
class Relevance:
    relevant: bool
    design: list[str] = field(default_factory=list)
    medical: list[str] = field(default_factory=list)
    not_medical: list[str] = field(default_factory=list)
    work_only: list[str] = field(default_factory=list)
    reason: str | None = None

    @property
    def tags(self) -> list[str]:
        tags = []
        if self.relevant and self.work_only:
            tags.append("проектирование + " + "/".join(self.work_only[:2]))
        return tags


def check_medical_design(text: str) -> Relevance:
    cleaned = NOT_DESIGN_PHRASES.sub(" ", text or "")
    result = Relevance(
        relevant=True,
        design=_hits(DESIGN, cleaned),
        medical=_hits(MEDICAL, cleaned),
        not_medical=_hits(NOT_MEDICAL, cleaned),
        work_only=_hits(WORK_ONLY, cleaned),
    )
    if not result.design:
        result.relevant = False
        result.reason = (
            "не проектирование: " + ", ".join(result.work_only[:3])
            if result.work_only
            else "нет признаков проектных работ"
        )
    elif "ветеринария" in result.not_medical or (result.not_medical and not result.medical):
        result.relevant = False  # veterinary clinics are not medical organisations (no medical licence)
        result.reason = "не медицинский объект: " + ", ".join(result.not_medical)
    elif not result.medical:
        result.relevant = False
        result.reason = "объект не медицинский (нет признаков медицинской организации)"
    return result
