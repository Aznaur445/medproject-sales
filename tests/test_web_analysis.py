from sqlalchemy import select

from app.core.db import sync_session
from app.models import CaseStudy, ListingDocument
from app.services import settings_store as ss
from tests.factories import make_listing
from tests.test_analysis import TZ, _docx_bytes
from tests.test_web_sales import csrf_of, login


async def test_upload_and_analyze_listing(client, monkeypatch):
    calls = []
    import app.worker.tasks_sales as tasks

    monkeypatch.setattr(tasks.analyze_listing_task, "delay", lambda *a: calls.append(a))
    await login(client, monkeypatch)
    lid = make_listing()
    csrf = await csrf_of(client, f"/listings/{lid}")
    files = [
        ("files", ("ТЗ клиника.docx", _docx_bytes(TZ), "application/octet-stream")),
        ("files", ("virus.exe", b"MZ", "application/octet-stream")),
    ]
    resp = await client.post(f"/listings/{lid}/documents", data={"csrf_token": csrf}, files=files)
    assert resp.status_code == 303
    with sync_session() as db:
        docs = db.execute(select(ListingDocument)).scalars().all()
    assert len(docs) == 1 and docs[0].filename.endswith(".docx")
    page = await client.get(f"/listings/{lid}")
    assert "Пропущены" in page.text and "virus.exe" in page.text
    await client.post(f"/listings/{lid}/analyze", data={"csrf_token": csrf})
    assert calls == [(lid,)]
    # Run the analysis inline and check the page shows results.
    from app.services.analysis import analyze_listing

    with sync_session() as db:
        analyze_listing(db, lid)
        db.commit()
    page = await client.get(f"/listings/{lid}")
    assert "Требования и риски" in page.text and "Без аванса" in page.text and "Почему" in page.text


async def test_cases_crud(client, monkeypatch):
    await login(client, monkeypatch)
    csrf = await csrf_of(client, "/cases")
    await client.post(
        "/cases",
        data={
            "csrf_token": csrf,
            "title": "Медцентр в Воронеже",
            "object_type": "Поликлиника",
            "area_m2": "650",
            "year": "2025",
            "sections": "АР, ЭОМ; ОВиК",
            "stages": "ПД, РД",
            "can_mention_customer": "1",
        },
    )
    with sync_session() as db:
        case = db.execute(select(CaseStudy)).scalar_one()
    assert case.sections == ["АР", "ЭОМ", "ОВиК"] and case.area_m2 == 650 and case.can_mention_customer
    await client.post("/cases", data={"csrf_token": csrf, "case_id": str(case.id), "title": "Медцентр, Воронеж"})
    with sync_session() as db:
        assert db.get(CaseStudy, case.id).title == "Медцентр, Воронеж"
    await client.post(f"/cases/{case.id}/delete", data={"csrf_token": csrf})
    with sync_session() as db:
        assert db.execute(select(CaseStudy)).first() is None


async def test_capabilities_and_scoring_settings(client, monkeypatch):
    await login(client, monkeypatch)
    csrf = await csrf_of(client, "/settings")
    await client.post(
        "/settings/capabilities",
        data={
            "csrf_token": csrf,
            "has_sro_design": "1",
            "has_ecp": "1",
            "experience_years": "8",
            "min_advance_percent": "40",
            "w_margin": "30",
            "auto_exclude_below": "35",
        },
    )
    with sync_session() as db:
        caps, scoring = ss.load_sync(db, ss.Capabilities), ss.load_sync(db, ss.Scoring)
    assert caps.has_sro_design and caps.experience_years == 8 and caps.min_advance_percent == 40
    assert scoring.w_margin == 30 and scoring.auto_exclude_below == 35 and scoring.w_relevance == 25
