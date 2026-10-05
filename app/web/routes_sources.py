"""Panel: search sources (F1) and filters (F2, F3)."""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select

from app.core.security import encrypt_secret
from app.models import Listing, Source, User
from app.models.enums import ListingStatus, SourceLegalStatus
from app.services import runtime_config
from app.services import settings_store as ss
from app.services.audit import audit_sync
from app.sources import REGISTRY, get_connector
from app.sources.platforms import PLATFORMS
from app.web.auth import current_user, require_owner, verify_csrf
from app.web.routes_sales import _flash, in_db, to_local
from app.web.templating import render

router = APIRouter(dependencies=[Depends(verify_csrf)])


@router.get("/sources")
async def sources_page(request: Request, user: User = Depends(current_user)):
    def load(db):
        sources = db.execute(select(Source).order_by(Source.id)).scalars().all()
        counts = dict(
            db.execute(
                select(Listing.source_id, func.count())
                .where(Listing.status != ListingStatus.EXCLUDED)
                .group_by(Listing.source_id)
            ).all()
        )
        return sources, counts

    sources, counts = await in_db(load)
    search_ready = await run_in_threadpool(lambda: runtime_config.search_config().ready)
    return render(
        request,
        "sources.html",
        user=user,
        sources=sources,
        counts=counts,
        has_web_search=any(s.connector == "web_search" and s.enabled for s in sources),
        platforms=PLATFORMS,
        search_ready=search_ready,
        connectors=REGISTRY,
        to_local=to_local,
        flash=request.session.pop("flash", None),
    )


@router.post("/sources")
async def source_add(request: Request, user: User = Depends(require_owner)):
    form = await request.form()
    connector_name = str(form.get("connector") or "")
    if connector_name not in REGISTRY:
        raise HTTPException(400, "Неизвестный тип источника")
    connector = get_connector(connector_name)
    name = str(form.get("name") or "").strip()
    config: dict[str, str] = {}
    for field in connector.config_fields:
        value = str(form.get(f"{connector_name}__{field.key}") or "").strip()
        if field.required and not value:
            _flash(request, f"Заполните поле «{field.label}»", "bad")
            return RedirectResponse("/sources#add", status_code=303)
        if value:
            config[f"{field.key}_enc" if field.secret else field.key] = encrypt_secret(value) if field.secret else value
    if not name:
        name = config.get("url") or config.get("channel") or config.get("user") or connector.title
    try:
        minutes = max(15, int(str(form.get("schedule_minutes") or "20")))
    except ValueError:
        minutes = 20

    def run(db):
        if db.execute(select(Source).where(Source.name == name)).scalar_one_or_none():
            return None
        source = Source(
            name=name[:200],
            kind=connector.kind,
            connector=connector_name,
            config=config,
            enabled=True,
            schedule_minutes=minutes,
            legal_status=SourceLegalStatus.ALLOWED if connector.runnable else SourceLegalStatus.MANUAL_CHECK,
        )
        db.add(source)
        db.flush()
        audit_sync(db, "source_added", actor="web", user_id=user.id, entity_type="source", entity_id=source.id)
        return source.id

    source_id = await in_db(run)
    if source_id is None:
        _flash(request, "Источник с таким названием уже есть", "bad")
    else:
        _flash(request, "Источник добавлен. Первая проверка — в течение 5 минут или нажмите «Проверить сейчас».")
    return RedirectResponse("/sources", status_code=303)


@router.post("/sources/quick-search")
async def source_quick_search(request: Request, user: User = Depends(require_owner)):
    """One click: the internet search source with the default query set, twice a day."""
    from app.sources.web_search import DEFAULT_QUERIES, WebSearchConnector

    def run(db):
        existing = db.execute(select(Source).where(Source.connector == "web_search")).scalars().first()
        if existing is not None:
            existing.enabled = True
            existing.circuit_open_until = None
            existing.consecutive_failures = 0
            return existing.id
        source = Source(
            name="Поиск в интернете: проектирование медобъектов",
            kind=WebSearchConnector.kind,
            connector="web_search",
            config={
                "queries": "\n".join(DEFAULT_QUERIES),
                "days": "60",
                "max_pages": "25",
                "use_ai": "1",
                "per_run": "4",
                "daily_limit": "300",
            },
            enabled=True,
            schedule_minutes=20,
            legal_status=SourceLegalStatus.ALLOWED,
        )
        db.add(source)
        db.flush()
        audit_sync(db, "source_added", actor="web", user_id=user.id, entity_type="source", entity_id=source.id)
        return source.id

    source_id = await in_db(run)
    if runtime_config.search_config().ready:
        from app.worker.tasks_sales import run_source_task

        run_source_task.delay(source_id)
        _flash(request, "Автопоиск включён, первая проверка уже идёт. Найденное появится в «Заявках» и в Telegram.")
    else:
        _flash(
            request,
            "Автопоиск включён, но заработает после ввода ключа Yandex Cloud в «Настройки → ИИ и поиск».",
            "warn",
        )
    return RedirectResponse("/sources", status_code=303)


@router.get("/sources/{source_id}/edit")
async def source_edit_form(request: Request, source_id: int, user: User = Depends(require_owner)):
    source = await in_db(lambda db: db.get(Source, source_id))
    if source is None or source.connector not in REGISTRY:
        raise HTTPException(404)
    return render(request, "source_edit.html", user=user, source=source, connector=get_connector(source.connector))


@router.post("/sources/{source_id}/edit")
async def source_edit(request: Request, source_id: int, user: User = Depends(require_owner)):
    form = await request.form()

    def run(db):
        source = db.get(Source, source_id)
        if source is None or source.connector not in REGISTRY:
            raise HTTPException(404)
        config = dict(source.config)
        for field in get_connector(source.connector).config_fields:
            value = str(form.get(field.key) or "").strip()
            if field.secret:
                if value:
                    config[f"{field.key}_enc"] = encrypt_secret(value)
            elif value or not field.required:
                config[field.key] = value
        source.config = config
        try:
            source.schedule_minutes = max(15, int(str(form.get("schedule_minutes") or source.schedule_minutes)))
        except ValueError:
            pass
        source.circuit_open_until = None
        source.consecutive_failures = 0
        audit_sync(db, "source_edited", actor="web", user_id=user.id, entity_type="source", entity_id=source_id)

    await in_db(run)
    _flash(request, "Источник сохранён.")
    return RedirectResponse("/sources", status_code=303)


@router.post("/sources/{source_id}/toggle")
async def source_toggle(request: Request, source_id: int, user: User = Depends(require_owner)):
    def run(db):
        source = db.get(Source, source_id)
        if source is None:
            raise HTTPException(404)
        source.enabled = not source.enabled
        source.circuit_open_until = None
        source.consecutive_failures = 0
        audit_sync(
            db,
            "source_toggled",
            actor="web",
            user_id=user.id,
            entity_type="source",
            entity_id=source_id,
            details={"enabled": source.enabled},
        )

    await in_db(run)
    return RedirectResponse("/sources", status_code=303)


@router.post("/sources/{source_id}/run")
async def source_run_now(request: Request, source_id: int, user: User = Depends(require_owner)):
    from app.worker.tasks_sales import run_source_task

    def reset(db):
        source = db.get(Source, source_id)
        if source is None:
            raise HTTPException(404)
        source.circuit_open_until = None

    await in_db(reset)
    run_source_task.delay(source_id)
    _flash(request, "Проверка запущена. Результат придёт в Telegram и появится в «Заявках».")
    return RedirectResponse("/sources", status_code=303)


@router.post("/sources/{source_id}/delete")
async def source_delete(request: Request, source_id: int, user: User = Depends(require_owner)):
    def run(db):
        source = db.get(Source, source_id)
        if source is None:
            raise HTTPException(404)
        source.enabled = False  # keep the row: listings reference it and history stays intact
        source.name = f"{source.name} (удалён #{source.id})"[:200]
        audit_sync(db, "source_removed", actor="web", user_id=user.id, entity_type="source", entity_id=source_id)

    await in_db(run)
    _flash(request, "Источник отключён и убран из работы. Найденные заявки сохранены.", "warn")
    return RedirectResponse("/sources", status_code=303)


# --- filters --------------------------------------------------------------------------------------


def _lines(text: str) -> list[str]:
    return [line.strip() for line in text.replace(",", "\n").splitlines() if line.strip()]


@router.get("/filters")
async def filters_page(request: Request, user: User = Depends(current_user)):
    filters = await in_db(lambda db: ss.load_sync(db, ss.Filters))
    return render(request, "filters.html", user=user, f=filters, flash=request.session.pop("flash", None))


@router.post("/filters")
async def filters_save(request: Request, user: User = Depends(require_owner)):
    form = await request.form()

    def num(name: str) -> int | None:
        raw = str(form.get(name) or "").replace(" ", "").replace(" ", "")
        return int(raw) if raw.isdigit() else None

    value = ss.Filters(
        work_words=_lines(str(form.get("work_words") or "")),
        object_words=_lines(str(form.get("object_words") or "")),
        stop_words=_lines(str(form.get("stop_words") or "")),
        regions_allow=_lines(str(form.get("regions_allow") or "")),
        regions_deny=_lines(str(form.get("regions_deny") or "")),
        budget_min=num("budget_min"),
        budget_max=num("budget_max"),
        keep_unknown_budget=form.get("keep_unknown_budget") == "1",
        notify_new=form.get("notify_new") == "1",
        strict_medical_design=form.get("strict_medical_design") == "1",
    )
    if not value.work_words or not value.object_words:
        _flash(request, "Списки «работы» и «объекты» не могут быть пустыми", "bad")
        return RedirectResponse("/filters", status_code=303)

    def run(db):
        ss.save_sync(db, value, user.id)
        audit_sync(db, "filters_saved", actor="web", user_id=user.id)

    await in_db(run)
    _flash(request, "Фильтры сохранены. Применяются к новым заявкам.")
    return RedirectResponse("/filters", status_code=303)


@router.post("/listings/{listing_id}/restore")
async def listing_restore(request: Request, listing_id: int, user: User = Depends(require_owner)):
    """Return an auto-excluded listing to work (filters are not perfect)."""

    def run(db):
        listing = db.get(Listing, listing_id)
        if listing is None:
            raise HTTPException(404)
        if listing.exclusion_reason and "гос" in listing.exclusion_reason:
            return False  # government procurement stays excluded by design
        listing.status = ListingStatus.FOUND
        listing.merged_into_id = None
        listing.exclusion_reason = None
        audit_sync(db, "listing_restored", actor="web", user_id=user.id, entity_type="listing", entity_id=listing_id)
        return True

    ok = await in_db(run)
    _flash(
        request,
        "Заявка возвращена в работу." if ok else "Госзаказ вернуть нельзя: он исключён правилами.",
        "ok" if ok else "bad",
    )
    return RedirectResponse(f"/listings/{listing_id}", status_code=303)
