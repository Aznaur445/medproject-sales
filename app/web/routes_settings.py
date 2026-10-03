"""Panel: requisites and texts, sending rules, price table, opt-out registry."""

from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from pydantic import ValidationError
from sqlalchemy import delete, select

from app.core.numbers import parse_decimal
from app.models import OptOut, User
from app.services import mailer
from app.services import settings_store as ss
from app.services.audit import audit_sync
from app.services.calculator import PriceTableData
from app.services.listings import active_price_table, save_price_table
from app.web.auth import current_user, require_owner, verify_csrf
from app.web.routes_sales import _flash, in_db, to_local
from app.web.templating import render

router = APIRouter(dependencies=[Depends(verify_csrf)])
MAX_SECTION_ROWS = 40


def _lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


@router.get("/settings")
async def settings_page(request: Request, user: User = Depends(current_user)):
    def load(db):
        return (
            ss.load_sync(db, ss.Requisites),
            ss.load_sync(db, ss.ProposalDefaults),
            ss.load_sync(db, ss.SendingRules),
        )

    req, defaults, rules = await in_db(load)
    return render(
        request,
        "settings.html",
        user=user,
        req=req,
        defaults=defaults,
        rules=rules,
        flash=request.session.pop("flash", None),
    )


@router.post("/settings/requisites")
async def save_requisites(request: Request, user: User = Depends(require_owner)):
    form = await request.form()
    try:
        value = ss.Requisites(**{k: str(form.get(k) or "").strip() for k in ss.Requisites.model_fields})
    except ValidationError as exc:
        _flash(request, f"Ошибка: {exc.errors()[0]['msg']}", "bad")
        return RedirectResponse("/settings", status_code=303)

    def run(db):
        ss.save_sync(db, value, user.id)
        audit_sync(db, "settings_requisites", actor="web", user_id=user.id)

    await in_db(run)
    _flash(request, "Реквизиты сохранены")
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/proposal")
async def save_proposal_defaults(request: Request, user: User = Depends(require_owner)):
    form = await request.form()
    try:
        value = ss.ProposalDefaults(
            validity_days=int(str(form.get("validity_days") or "30")),
            duration_text=str(form.get("duration_text") or ""),
            payment_options=_lines(str(form.get("payment_options") or "")),
            vat_note=str(form.get("vat_note") or ""),
            intro_text=str(form.get("intro_text") or ""),
            quality_text=str(form.get("quality_text") or ""),
            advantages=_lines(str(form.get("advantages") or "")),
            signature=str(form.get("signature") or "").replace("\r\n", "\n"),
        )
    except (ValidationError, ValueError) as exc:
        _flash(request, f"Ошибка: {exc}", "bad")
        return RedirectResponse("/settings#proposal", status_code=303)

    def run(db):
        ss.save_sync(db, value, user.id)
        audit_sync(db, "settings_proposal", actor="web", user_id=user.id)

    await in_db(run)
    _flash(request, "Тексты КП сохранены")
    return RedirectResponse("/settings#proposal", status_code=303)


@router.post("/settings/sending")
async def save_sending(request: Request, user: User = Depends(require_owner)):
    form = await request.form()
    try:
        value = ss.SendingRules(
            paused=form.get("paused") == "1",
            daily_limit=int(str(form.get("daily_limit"))),
            org_interval_days=int(str(form.get("org_interval_days"))),
            warmup_enabled=form.get("warmup_enabled") == "1",
            warmup_start=int(str(form.get("warmup_start"))),
            warmup_step=int(str(form.get("warmup_step"))),
            double_confirm_from=parse_decimal(str(form.get("double_confirm_from"))) or Decimal("0"),
            send_window_start_hour=int(str(form.get("send_window_start_hour"))),
            send_window_end_hour=int(str(form.get("send_window_end_hour"))),
            work_days_only=form.get("work_days_only") == "1",
            unsubscribe_text=str(form.get("unsubscribe_text") or ""),
        )
        if not value.unsubscribe_text.strip():
            raise ValueError("Фраза об отписке обязательна")
        if value.daily_limit > 100:
            raise ValueError("Больше 100 писем в день с одного ящика — риск блокировки домена")
    except (ValidationError, ValueError, TypeError) as exc:
        _flash(request, f"Ошибка: {exc}", "bad")
        return RedirectResponse("/settings#sending", status_code=303)

    def run(db):
        ss.save_sync(db, value, user.id)
        audit_sync(db, "settings_sending", actor="web", user_id=user.id, details={"paused": value.paused})

    await in_db(run)
    _flash(request, "Правила отправки сохранены")
    return RedirectResponse("/settings#sending", status_code=303)


@router.post("/settings/pause")
async def toggle_pause(request: Request, user: User = Depends(require_owner)):
    form = await request.form()
    paused = form.get("paused") == "1"

    def run(db):
        rules = ss.load_sync(db, ss.SendingRules)
        rules.paused = paused
        ss.save_sync(db, rules, user.id)
        audit_sync(db, "sending_paused" if paused else "sending_resumed", actor="web", user_id=user.id)

    await in_db(run)
    _flash(request, "Вся отправка на паузе" if paused else "Отправка возобновлена", "warn" if paused else "ok")
    return RedirectResponse(request.headers.get("referer") or "/", status_code=303)


# --- prices ---------------------------------------------------------------------------------------


@router.get("/prices")
async def prices_page(request: Request, user: User = Depends(current_user)):
    row = await in_db(active_price_table)
    table = PriceTableData.model_validate(row.data)
    return render(
        request, "prices.html", user=user, row=row, table=table, flash=request.session.pop("flash", None), empty_rows=3
    )


def _parse_mapping(text: str) -> dict[str, Decimal]:
    result: dict[str, Decimal] = {}
    for line in _lines(text):
        name, _, value = line.rpartition("=")
        if not name.strip():
            raise ValueError(f"Строка «{line}»: нужен формат «Название = 1,1»")
        result[name.strip()] = parse_decimal(value) or Decimal("1")
    return result


def _parse_modifiers(text: str) -> list[dict[str, Any]]:
    items = []
    for line in _lines(text):
        parts = [p.strip() for p in line.split("|")]
        if len(parts) != 3:
            raise ValueError(f"Строка «{line}»: нужен формат «код | Название | 1,1»")
        items.append({"code": parts[0], "name": parts[1], "multiplier": parse_decimal(parts[2])})
    return items


def _parse_curve(text: str) -> list[dict[str, Decimal]]:
    points = []
    for line in _lines(text):
        area, sep, price = line.partition("=")
        if not sep:
            raise ValueError(f"Строка «{line}»: нужен формат «площадь = цена пакета»")
        points.append({"area_m2": parse_decimal(area), "price": parse_decimal(price)})
    return points


def _pct(form, name: str) -> Decimal:
    value = parse_decimal(str(form.get(name) or "0")) or Decimal("0")
    return (value / 100).quantize(Decimal("0.0001"))


@router.post("/prices")
async def save_prices(request: Request, user: User = Depends(require_owner)):
    form = await request.form()
    try:
        sections = []
        for i in range(MAX_SECTION_ROWS):
            code = str(form.get(f"s{i}_code") or "").strip()
            if not code or form.get(f"s{i}_delete") == "1":
                continue
            sections.append(
                {
                    "code": code.upper(),
                    "name": str(form.get(f"s{i}_name") or code),
                    "description": str(form.get(f"s{i}_description") or ""),
                    "stage": str(form.get(f"s{i}_stage")),
                    "weight": _pct(form, f"s{i}_weight") if str(form.get(f"s{i}_weight") or "").strip() else None,
                    "rate_per_m2": parse_decimal(str(form.get(f"s{i}_rate") or "0")) or Decimal("0"),
                    "min_price": parse_decimal(str(form.get(f"s{i}_min") or "0")),
                    "cost_share": _pct(form, f"s{i}_cost"),
                    "default_selected": form.get(f"s{i}_default") == "1",
                }
            )
        data = PriceTableData(
            is_example=form.get("is_example") == "1",
            sections=sections,
            object_types=_parse_mapping(str(form.get("object_types") or "")),
            modifiers=_parse_modifiers(str(form.get("modifiers") or "")),
            regions=_parse_mapping(str(form.get("regions") or "")),
            package_curve=_parse_curve(str(form.get("package_curve") or "")),
            trip_cost=parse_decimal(str(form.get("trip_cost") or "0")) or Decimal("0"),
            gip_share=_pct(form, "gip_share"),
            other_costs_share=_pct(form, "other_costs_share"),
            tax_rate=_pct(form, "tax_rate"),
            target_margin=_pct(form, "target_margin"),
            min_margin=_pct(form, "min_margin"),
            max_uplift=_pct(form, "max_uplift"),
        )
        if not data.sections:
            raise ValueError("Нужен хотя бы один раздел")
    except (ValidationError, ValueError) as exc:
        msg = exc.errors()[0]["msg"] if isinstance(exc, ValidationError) else str(exc)
        _flash(request, f"Не сохранено: {msg}", "bad")
        return RedirectResponse("/prices", status_code=303)
    row = await in_db(lambda db: save_price_table(db, data, user.id, str(form.get("comment") or "")))
    _flash(request, f"Сохранено как версия {row.version}. Новые расчёты используют её.")
    return RedirectResponse("/prices", status_code=303)


# --- opt-outs -------------------------------------------------------------------------------------


@router.get("/optouts")
async def optouts_page(request: Request, user: User = Depends(current_user)):
    rows = await in_db(lambda db: db.execute(select(OptOut).order_by(OptOut.id.desc())).scalars().all())
    return render(
        request, "optouts.html", user=user, rows=rows, to_local=to_local, flash=request.session.pop("flash", None)
    )


@router.post("/optouts")
async def optout_add(request: Request, user: User = Depends(require_owner)):
    form = await request.form()
    value = str(form.get("value") or "").strip().lower()
    if not value or ("@" not in value and "." not in value):
        _flash(request, "Укажите e-mail или домен", "bad")
        return RedirectResponse("/optouts", status_code=303)
    kind = "email" if "@" in value else "domain"

    def run(db):
        mailer.add_optout(db, value, kind=kind, source="manual", reason=str(form.get("reason") or ""))
        audit_sync(db, "optout_added", actor="web", user_id=user.id, details={"kind": kind})

    await in_db(run)
    _flash(request, "Добавлено в реестр отказов. Неотправленные письма этому адресату отменены.")
    return RedirectResponse("/optouts", status_code=303)


@router.post("/optouts/{optout_id}/delete")
async def optout_delete(request: Request, optout_id: int, user: User = Depends(require_owner)):
    def run(db):
        db.execute(delete(OptOut).where(OptOut.id == optout_id))
        audit_sync(db, "optout_deleted", actor="web", user_id=user.id, entity_type="optout", entity_id=optout_id)

    await in_db(run)
    _flash(request, "Запись удалена из реестра отказов", "warn")
    return RedirectResponse("/optouts", status_code=303)
