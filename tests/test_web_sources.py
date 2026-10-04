from sqlalchemy import select

from app.core.db import sync_session
from app.core.security import decrypt_secret
from app.models import Listing, Source
from app.models.enums import ListingStatus, SourceLegalStatus
from app.services import settings_store as ss
from tests.test_web_sales import csrf_of, login


async def test_add_sources_of_each_type(client, monkeypatch):
    await login(client, monkeypatch)
    csrf = await csrf_of(client, "/sources")
    await client.post(
        "/sources",
        data={
            "csrf_token": csrf,
            "connector": "web_page",
            "name": "Сеть «Здоровье»",
            "web_page__url": "https://zdorovie-example.ru/tenders",
            "schedule_minutes": "120",
        },
    )
    await client.post(
        "/sources",
        data={
            "csrf_token": csrf,
            "connector": "email_alerts",
            "email_alerts__user": "tenders@project-med.ru",
            "email_alerts__password": "secret-app-pass",
        },
    )
    await client.post(
        "/sources",
        data={
            "csrf_token": csrf,
            "connector": "manual_link",
            "name": "Avito",
            "manual_link__url": "https://www.avito.ru/rossiya/predlozheniya_uslug",
        },
    )
    resp = await client.post("/sources", data={"csrf_token": csrf, "connector": "web_page", "name": "x"})
    assert resp.status_code == 303  # missing URL -> flash error, nothing created
    with sync_session() as db:
        rows = {s.connector: s for s in db.execute(select(Source)).scalars()}
    assert rows["web_page"].schedule_minutes == 120 and rows["web_page"].name == "Сеть «Здоровье»"
    assert "password" not in rows["email_alerts"].config
    assert decrypt_secret(rows["email_alerts"].config["password_enc"]) == "secret-app-pass"
    assert rows["manual_link"].legal_status == SourceLegalStatus.MANUAL_CHECK
    page = await client.get("/sources")
    assert "secret-app-pass" not in page.text and "ручная проверка" in page.text


async def test_run_now_and_toggle(client, monkeypatch):
    calls = []
    import app.worker.tasks_sales as tasks

    monkeypatch.setattr(tasks.run_source_task, "delay", lambda *a: calls.append(a))
    await login(client, monkeypatch)
    with sync_session() as db:
        db.add(
            Source(
                name="s", kind="web_page", connector="web_page", config={"url": "https://a-example.ru"}, enabled=True
            )
        )
        db.commit()
    csrf = await csrf_of(client, "/sources")
    await client.post("/sources/1/run", data={"csrf_token": csrf})
    assert calls == [(1,)]
    await client.post("/sources/1/toggle", data={"csrf_token": csrf})
    with sync_session() as db:
        assert not db.get(Source, 1).enabled


async def test_filters_save(client, monkeypatch):
    await login(client, monkeypatch)
    csrf = await csrf_of(client, "/filters")
    await client.post(
        "/filters",
        data={
            "csrf_token": csrf,
            "work_words": "проектирование\nРД",
            "object_words": "клиника, стоматология",
            "stop_words": "уборка",
            "regions_allow": "Москва",
            "budget_min": "500 000",
            "notify_new": "1",
        },
    )
    with sync_session() as db:
        f = ss.load_sync(db, ss.Filters)
    assert f.work_words == ["проектирование", "РД"] and f.object_words == ["клиника", "стоматология"]
    assert f.budget_min == 500000 and f.regions_allow == ["Москва"] and not f.keep_unknown_budget


async def test_restore_excluded_but_not_government(client, monkeypatch):
    await login(client, monkeypatch)
    with sync_session() as db:
        db.add_all(
            [
                Listing(
                    title="Проект клиники",
                    status=ListingStatus.EXCLUDED,
                    exclusion_reason="нет признаков",
                    tags=[],
                    extracted={},
                    score_explanation={},
                ),
                Listing(
                    title="ГБУЗ проект",
                    status=ListingStatus.EXCLUDED,
                    exclusion_reason="госзаказчик: «ГБУЗ»",
                    tags=[],
                    extracted={},
                    score_explanation={},
                ),
            ]
        )
        db.commit()
    csrf = await csrf_of(client, "/listings/1")
    await client.post("/listings/1/restore", data={"csrf_token": csrf})
    await client.post("/listings/2/restore", data={"csrf_token": csrf})
    with sync_session() as db:
        assert db.get(Listing, 1).status == ListingStatus.FOUND
        assert db.get(Listing, 2).status == ListingStatus.EXCLUDED
