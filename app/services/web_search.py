"""Internet search through the official Yandex Search API (Yandex Cloud), no scraping of search pages.

Docs: https://yandex.cloud/ru/docs/search-api/ — synchronous `v2/web/search`, with the asynchronous
`v2/web/searchAsync` + operation polling as a fallback. The answer is Yandex XML in base64.
"""

import base64
import re
import time
import xml.etree.ElementTree as ET  # noqa: S405 - XML comes from Yandex Cloud over HTTPS, not from users
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

from app.services.runtime_config import SearchConfig

SYNC_URL = "https://searchapi.api.cloud.yandex.net/v2/web/search"
ASYNC_URL = "https://searchapi.api.cloud.yandex.net/v2/web/searchAsync"
OPERATION_URL = "https://operation.api.cloud.yandex.net/operations/{id}"
NO_RESULTS_CODE = "15"
RUSSIA = "225"
SPACE_RE = re.compile(r"\s+")


class SearchError(Exception):
    pass


@dataclass
class SearchResult:
    url: str
    title: str
    snippet: str
    domain: str = ""
    modified_at: datetime | None = None


def _text(el: ET.Element | None) -> str:
    return SPACE_RE.sub(" ", "".join(el.itertext())).strip() if el is not None else ""


def _modtime(value: str) -> datetime | None:
    for fmt in ("%Y%m%dT%H%M%S", "%Y%m%d"):
        try:
            return datetime.strptime(value.strip(), fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def parse_xml(xml: str | bytes) -> list[SearchResult]:
    root = ET.fromstring(xml)  # noqa: S314 - see the import comment
    error = root.find("./response/error")
    if error is not None:
        if error.get("code") == NO_RESULTS_CODE:
            return []
        raise SearchError(f"Yandex Search API: {_text(error) or error.get('code')}")
    results = []
    for doc in root.iterfind(".//results/grouping/group/doc"):
        url = _text(doc.find("url"))
        if not url:
            continue
        passages = [_text(p) for p in doc.iterfind("./passages/passage")]
        snippet = " … ".join(p for p in [_text(doc.find("headline")), *passages] if p)
        modtime = doc.find("modtime")
        results.append(
            SearchResult(
                url=url,
                title=_text(doc.find("title")) or url,
                snippet=snippet,
                domain=_text(doc.find("domain")),
                modified_at=_modtime(modtime.text or "") if modtime is not None and modtime.text else None,
            )
        )
    return results


def _body(query: str, cfg: SearchConfig, page: int) -> dict:
    return {
        "query": {
            "searchType": "SEARCH_TYPE_RU",
            "queryText": query,
            "familyMode": "FAMILY_MODE_MODERATE",
            "page": str(page),
        },
        "sortSpec": {"sortMode": "SORT_MODE_BY_RELEVANCE", "sortOrder": "SORT_ORDER_DESC"},
        "groupSpec": {"groupMode": "GROUP_MODE_DEEP", "groupsOnPage": "20", "docsInGroup": "1"},
        "maxPassages": "4",
        "region": RUSSIA,
        "l10n": "LOCALIZATION_RU",
        "folderId": cfg.folder_id,
        "responseFormat": "FORMAT_XML",
    }


def _raw_data(payload: dict) -> bytes:
    raw = payload.get("rawData") or (payload.get("response") or {}).get("rawData")
    if not raw:
        raise SearchError("Yandex Search API вернул пустой ответ")
    return base64.b64decode(raw)


def _explain(resp: httpx.Response) -> SearchError:
    if resp.status_code in (401, 403):
        return SearchError(
            "доступ запрещён: проверьте ключ API, каталог и роль search-api.webSearch.user у сервисного аккаунта"
        )
    if resp.status_code == 429:
        return SearchError("превышена квота запросов Yandex Search API")
    return SearchError(f"HTTP {resp.status_code}: {resp.text[:200]}")


def yandex_search(
    query: str, cfg: SearchConfig, *, page: int = 0, http: httpx.Client | None = None, poll_seconds: float = 2.0
) -> list[SearchResult]:
    if not cfg.ready:
        raise SearchError("не задан ключ API или каталог Yandex Cloud (Настройки → ИИ и поиск)")
    client = http or httpx.Client(timeout=60)
    headers = {"Authorization": f"Api-Key {cfg.api_key}"}
    try:
        resp = client.post(SYNC_URL, json=_body(query, cfg, page), headers=headers)
        if resp.status_code == 200:
            return parse_xml(_raw_data(resp.json()))
        if resp.status_code not in (404, 501):
            raise _explain(resp)
        # Older API versions: asynchronous request + operation polling.
        resp = client.post(ASYNC_URL, json=_body(query, cfg, page), headers=headers)
        if resp.status_code != 200:
            raise _explain(resp)
        operation = resp.json()
        for _ in range(30):
            if operation.get("done"):
                if operation.get("error"):
                    raise SearchError(str(operation["error"].get("message") or operation["error"])[:300])
                return parse_xml(_raw_data(operation))
            time.sleep(poll_seconds)
            resp = client.get(OPERATION_URL.format(id=operation["id"]), headers=headers)
            if resp.status_code != 200:
                raise _explain(resp)
            operation = resp.json()
        raise SearchError("Yandex Search API не ответил вовремя")
    except httpx.HTTPError as exc:
        raise SearchError(f"сеть: {type(exc).__name__}") from exc
    except (ValueError, KeyError, ET.ParseError) as exc:
        raise SearchError(f"неожиданный ответ: {type(exc).__name__}") from exc
    finally:
        if http is None:
            client.close()
