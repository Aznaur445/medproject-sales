"""Panel: documents, analysis results and the case-study registry (stage 4)."""

from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from starlette.datastructures import UploadFile

from app.core.numbers import parse_decimal
from app.models import CaseStudy, Listing, User
from app.services import settings_store as ss
from app.services import storage
from app.services.audit import audit_sync
from app.web.auth import current_user, require_owner, verify_csrf
from app.web.routes_sales import _flash, in_db
from app.web.templating import render

router = APIRouter(dependencies=[Depends(verify_csrf)])
ALLOWED = {
    *(".pdf", ".docx", ".doc", ".rtf", ".odt", ".zip", ".xlsx", ".xlsm", ".txt"),
    *(".png", ".jpg", ".jpeg", ".tif", ".tiff"),
}
MAX_BYTES = 50 * 1024 * 1024


@router.post("/listings/{listing_id}/documents")
async def upload_documents(request: Request, listing_id: int, user: User = Depends(require_owner)):
    form = await request.form()
    files = [f for f in form.getlist("files") if isinstance(f, UploadFile) and f.filename]
    saved, skipped = 0, []
    for upload in files:
        name = storage.safe_name(upload.filename)
        suffix = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
        data = await upload.read(MAX_BYTES + 1)
        if suffix not in ALLOWED or len(data) > MAX_BYTES:
            skipped.append(upload.filename)
            continue

        def run(db, name=name, data=data):
            from app.services.tender_docs import store_document, unpack

            if db.get(Listing, listing_id) is None:
                raise HTTPException(404)
            stored = sum(store_document(db, listing_id, inner, content) for inner, content in unpack(name, data))
            if stored:
                audit_sync(
                    db, "document_uploaded", actor="web", user_id=user.id, entity_type="listing", entity_id=listing_id
                )

        await in_db(run)
        saved += 1
    msg = f"Загружено файлов: {saved}."
    if skipped:
        msg += " Пропущены (формат или размер больше 50 МБ): " + ", ".join(skipped)
    if saved:
        from app.worker.tasks_sales import autopilot_task

        autopilot_task.delay(listing_id, force=True)
        msg += " Разбор и расчёт КП запущены — результат придёт в Telegram и появится здесь."
    _flash(request, msg, "warn" if skipped else "ok")
    return RedirectResponse(f"/listings/{listing_id}#analysis", status_code=303)


@router.post("/listings/{listing_id}/autopilot")
async def run_autopilot(request: Request, listing_id: int, user: User = Depends(require_owner)):
    from app.worker.tasks_sales import autopilot_task

    autopilot_task.delay(listing_id, force=True)
    _flash(request, "Запущено: скачиваю документацию, разбираю и считаю КП. Отчёт придёт в Telegram через 1–3 минуты.")
    return RedirectResponse(f"/listings/{listing_id}#analysis", status_code=303)


@router.post("/listings/{listing_id}/analyze")
async def analyze(request: Request, listing_id: int, user: User = Depends(require_owner)):
    from app.worker.tasks_sales import analyze_listing_task

    analyze_listing_task.delay(listing_id)
    _flash(request, "Анализ запущен: результат появится здесь через 1–2 минуты и придёт в Telegram.")
    return RedirectResponse(f"/listings/{listing_id}#analysis", status_code=303)


# --- cases --------------------------------------------------------------------------------------


@router.get("/cases")
async def cases_page(request: Request, user: User = Depends(current_user)):
    cases = await in_db(
        lambda db: (
            db.execute(select(CaseStudy).order_by(CaseStudy.year.desc().nullslast(), CaseStudy.id.desc()))
            .scalars()
            .all()
        )
    )
    from app.services.calculator import PriceTableData
    from app.services.listings import active_price_table

    table = PriceTableData.model_validate((await in_db(active_price_table)).data)
    return render(
        request,
        "cases.html",
        user=user,
        cases=cases,
        object_types=list(table.object_types),
        flash=request.session.pop("flash", None),
    )


def _list(value: str) -> list[str]:
    return [x.strip() for x in value.replace(";", ",").split(",") if x.strip()]


@router.post("/cases")
async def case_save(request: Request, user: User = Depends(require_owner)):
    form = await request.form()
    title = str(form.get("title") or "").strip()
    if not title:
        _flash(request, "Укажите название проекта", "bad")
        return RedirectResponse("/cases", status_code=303)
    try:
        area = parse_decimal(str(form.get("area_m2") or ""))
        year = int(str(form.get("year"))) if str(form.get("year") or "").isdigit() else None
    except ValueError as exc:
        _flash(request, str(exc), "bad")
        return RedirectResponse("/cases", status_code=303)
    case_id = str(form.get("case_id") or "")

    def run(db):
        case = db.get(CaseStudy, int(case_id)) if case_id.isdigit() else CaseStudy()
        case.title, case.object_type = title, str(form.get("object_type") or "") or None
        case.area_m2 = area if area is None else Decimal(area)
        case.year, case.customer_name = year, str(form.get("customer_name") or "") or None
        case.region = str(form.get("region") or "") or None
        case.stages, case.sections = _list(str(form.get("stages") or "")), _list(str(form.get("sections") or ""))
        case.result, case.review_text = (
            str(form.get("result") or "") or None,
            str(form.get("review_text") or "") or None,
        )
        case.links = _list(str(form.get("links") or ""))
        case.photos = case.photos or []
        case.can_mention_customer = form.get("can_mention_customer") == "1"
        db.add(case)
        db.flush()
        audit_sync(db, "case_saved", actor="web", user_id=user.id, entity_type="case_study", entity_id=case.id)

    await in_db(run)
    _flash(request, "Кейс сохранён")
    return RedirectResponse("/cases", status_code=303)


@router.post("/cases/{case_id}/delete")
async def case_delete(request: Request, case_id: int, user: User = Depends(require_owner)):
    def run(db):
        case = db.get(CaseStudy, case_id)
        if case:
            db.delete(case)
            audit_sync(db, "case_deleted", actor="web", user_id=user.id, entity_type="case_study", entity_id=case_id)

    await in_db(run)
    return RedirectResponse("/cases", status_code=303)


# --- capabilities and scoring weights ------------------------------------------------------------


@router.post("/settings/capabilities")
async def save_capabilities(request: Request, user: User = Depends(require_owner)):
    form = await request.form()

    def num(name: str, default: int = 0) -> int:
        raw = str(form.get(name) or "").strip()
        return int(raw) if raw.isdigit() else default

    caps = ss.Capabilities(
        has_sro_design=form.get("has_sro_design") == "1",
        has_iso=form.get("has_iso") == "1",
        has_ecp=form.get("has_ecp") == "1",
        gip_in_nopriz=form.get("gip_in_nopriz") == "1",
        can_travel=form.get("can_travel") == "1",
        experience_years=num("experience_years"),
        medical_projects_done=num("medical_projects_done"),
        min_advance_percent=num("min_advance_percent", 30),
    )
    scoring = ss.Scoring(**{k: num(k, getattr(ss.Scoring(), k)) for k in ss.Scoring.model_fields})

    def run(db):
        ss.save_sync(db, caps, user.id)
        ss.save_sync(db, scoring, user.id)
        audit_sync(db, "settings_capabilities", actor="web", user_id=user.id)

    await in_db(run)
    _flash(request, "Возможности и веса оценки сохранены")
    return RedirectResponse("/settings#capabilities", status_code=303)
