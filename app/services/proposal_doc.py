"""Customer-facing proposal document: DOCX from a docxtpl template, PDF via LibreOffice headless.

Only `ProposalContent` reaches the template. It has no fields for costs, margin or rates, so internal
numbers cannot leak into the document by construction.
"""

import io
import subprocess
import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path

from docxtpl import DocxTemplate, Listing, RichText
from pydantic import BaseModel, ConfigDict

from app.services.calculator import STAGE_TITLES
from app.services.settings_store import Requisites

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "docx_templates"
STAGE_ORDER = ["pre", "pd", "rd", "extra"]
MONTHS = [
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
]


class ProposalSection(BaseModel):
    code: str
    name: str
    description: str = ""
    stage: str
    price: Decimal


class ProposalContent(BaseModel):
    """Everything the customer sees. Deliberately contains no cost / margin fields."""

    model_config = ConfigDict(extra="forbid")

    customer_name: str
    object_name: str
    address: str = ""
    area_m2: Decimal | None = None
    intro: str
    sections: list[ProposalSection]
    total: Decimal
    duration: str
    payment_options: list[str]
    vat_note: str
    quality: str
    advantages: list[str]
    cases: list[str] = []
    signature: str
    valid_until: date
    requisites: Requisites


def money(value: Decimal) -> str:
    whole = int(value.quantize(Decimal("1")))
    return f"{whole:,}".replace(",", " ") + " ₽"


def ru_date(value: date) -> str:
    return f"{value.day} {MONTHS[value.month - 1]} {value.year} г."


def _rows(sections: list[ProposalSection]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    stages = [s for s in STAGE_ORDER if any(x.stage == s for x in sections)]
    for stage_index, stage in enumerate(stages, start=1):
        items = [x for x in sections if x.stage == stage]
        subtotal = sum((x.price for x in items), Decimal("0"))
        title = RichText()
        title.add(STAGE_TITLES[stage], bold=True)
        price = RichText()
        price.add(money(subtotal), bold=True)
        rows.append({"num": str(stage_index), "title": title, "price": price})
        for item_index, item in enumerate(items, start=1):
            text = RichText()
            text.add(item.name, bold=True)
            if item.description:
                text.add("\n" + item.description, size=17, color="667085")  # size in half-points
            rows.append({"num": f"{stage_index}.{item_index}", "title": text, "price": RichText(money(item.price))})
    return rows


def render_docx(content: ProposalContent, template: str = "default") -> bytes:
    if sum((s.price for s in content.sections), Decimal("0")) != content.total:
        raise ValueError("Сумма разделов не равна итоговой цене")
    path = TEMPLATES_DIR / f"{template}.docx"
    if not path.exists():
        raise FileNotFoundError(f"Шаблон КП не найден: {template}")
    doc = DocxTemplate(path)
    area = f"{content.area_m2:,.2f}".replace(",", " ").replace(".", ",") + " м²" if content.area_m2 else "—"
    context = {
        "req": content.requisites.model_dump(),
        "customer_name": content.customer_name,
        "object_name": content.object_name,
        "address": content.address or "—",
        "area_text": area,
        "intro": content.intro,
        "rows": _rows(content.sections),
        "total_text": money(content.total),
        "vat_note": content.vat_note,
        "duration": content.duration,
        "payment_options": content.payment_options,
        "quality": content.quality,
        "advantages": content.advantages,
        "cases": content.cases,
        "signature": Listing(content.signature),
        "valid_until": ru_date(content.valid_until),
    }
    doc.render(context, autoescape=True)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def docx_to_pdf(docx: bytes, timeout: int = 180) -> bytes:
    """Convert with LibreOffice in an isolated profile (safe to run several conversions in parallel)."""
    with tempfile.TemporaryDirectory(prefix="kp-") as tmp:
        tmp_path = Path(tmp)
        src = tmp_path / "proposal.docx"
        src.write_bytes(docx)
        subprocess.run(
            [
                "soffice",
                f"-env:UserInstallation=file://{tmp_path}/profile",
                "--headless",
                "--norestore",
                "--convert-to",
                "pdf",
                "--outdir",
                str(tmp_path),
                str(src),
            ],
            check=True,
            capture_output=True,
            timeout=timeout,
        )
        pdf = tmp_path / "proposal.pdf"
        if not pdf.exists():
            raise RuntimeError("LibreOffice не создал PDF")
        return pdf.read_bytes()
