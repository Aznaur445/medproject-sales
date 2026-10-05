"""Find and download tender documentation (ТЗ, извещение, приложения) linked from a listing page.

Rules (spec §3): only public files allowed by robots.txt are downloaded; nothing behind a login is touched —
when a platform shows a login page instead of a file, the owner is asked to download it and send it to the bot.
"""

import hashlib
import io
import re
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.models import Listing, ListingDocument
from app.services import storage
from app.sources.base import USER_AGENT, SourceError, get_page, page_text, robots_allowed
from app.sources.platforms import platform_of

log = get_logger(__name__)

READABLE = {".pdf", ".docx", ".xlsx", ".xlsm", ".txt", ".csv", ".png", ".jpg", ".jpeg", ".tif", ".tiff"}
CONVERTIBLE = {".doc", ".rtf", ".odt"}  # LibreOffice Writer -> .docx
DOC_EXT = READABLE | CONVERTIBLE | {".zip", ".xls"}
DOC_WORDS = re.compile(
    r"техническ\w*\s+задани|\bТЗ\b|документаци|приложени|спецификаци|извещени|положени\w*\s+о\s+закупк|скачать",
    re.IGNORECASE,
)
FILE_HINT = re.compile(r"download|file|attach|document|getfile|upload", re.IGNORECASE)
MAX_FILE = 30 * 1024 * 1024
MAX_TOTAL = 100 * 1024 * 1024
MAX_LINKS = 15
MAX_ZIP_FILES = 25
SHORT_DESCRIPTION = 600


class DocError(Exception):
    pass


class LoginRequired(DocError):
    pass


@dataclass
class DocLink:
    url: str
    name: str


@dataclass
class CollectReport:
    saved: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    needs_login: bool = False
    page_read: bool = False
    platform: str | None = None  # name of a known platform
    platform_note: str | None = None  # why documents are not downloaded automatically there


def _suffix(name: str) -> str:
    return Path(urlsplit(name).path if "://" in name else name).suffix.lower()


def find_document_links(html: str, page_url: str) -> list[DocLink]:
    soup = BeautifulSoup(html, "html.parser")
    links: list[DocLink] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = urljoin(page_url, a["href"].strip())
        if not href.startswith(("http://", "https://")) or href in seen:
            continue
        text = " ".join(a.get_text(" ").split())[:200]
        by_ext = _suffix(href) in DOC_EXT
        by_words = bool(DOC_WORDS.search(text) or DOC_WORDS.search(a.get("title", ""))) and bool(FILE_HINT.search(href))
        if by_ext or by_words:
            seen.add(href)
            name = unquote(Path(urlsplit(href).path).name) or text or "document"
            links.append(DocLink(href, name if _suffix(name) in DOC_EXT else (text or name)))
        if len(links) >= MAX_LINKS:
            break
    return links


def _filename(resp: httpx.Response, link: DocLink) -> str:
    disposition = resp.headers.get("content-disposition", "")
    match = re.search(r"filename\*\s*=\s*(?:UTF-8|utf-8)''([^;]+)", disposition) or re.search(
        r'filename\s*=\s*"?([^";]+)"?', disposition
    )
    if match:
        return storage.safe_name(unquote(match.group(1).strip()))
    return storage.safe_name(link.name or Path(urlsplit(str(resp.url)).path).name or "document")


def download(http: httpx.Client, link: DocLink) -> tuple[str, bytes]:
    if not robots_allowed(link.url):
        raise DocError(f"robots.txt запрещает скачивание: {link.url}")
    try:
        with http.stream(
            "GET", link.url, headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=60
        ) as resp:
            if resp.status_code in (401, 403):
                raise LoginRequired(f"файл доступен только после входа на площадку: {link.url}")
            if resp.status_code >= 400:
                raise DocError(f"HTTP {resp.status_code}: {link.url}")
            name = _filename(resp, link)
            content_type = resp.headers.get("content-type", "").lower()
            if "text/html" in content_type and _suffix(name) not in DOC_EXT:
                raise LoginRequired(f"вместо файла площадка показывает страницу (вероятно, нужен вход): {link.url}")
            data = bytearray()
            for chunk in resp.iter_bytes():
                data += chunk
                if len(data) > MAX_FILE:
                    raise DocError(f"файл больше {MAX_FILE // 1024 // 1024} МБ: {name}")
    except httpx.HTTPError as exc:
        raise DocError(f"не удалось скачать {link.url}: {type(exc).__name__}") from exc
    if _suffix(name) not in DOC_EXT:
        name = f"{name}{_guess_suffix(bytes(data))}"
    return name, bytes(data)


def _guess_suffix(data: bytes) -> str:
    if data.startswith(b"%PDF"):
        return ".pdf"
    if data.startswith(b"PK"):
        return ".docx" if b"word/" in data[:4000] else ".zip"
    if data.startswith(b"\xd0\xcf\x11\xe0"):
        return ".doc"
    if data.startswith(b"{\\rtf"):
        return ".rtf"
    return ".bin"


def _zip_name(info: zipfile.ZipInfo) -> str:
    name = info.filename
    if not info.flag_bits & 0x800:  # not UTF-8: Russian archives from Windows use cp866
        try:
            name = name.encode("cp437").decode("cp866")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    return storage.safe_name(Path(name).name)


def convert_to_docx(data: bytes, name: str, timeout: int = 120) -> bytes | None:
    with tempfile.TemporaryDirectory(prefix="doc-") as tmp:
        tmp_path = Path(tmp)
        src = tmp_path / f"source{_suffix(name)}"
        src.write_bytes(data)
        try:
            subprocess.run(
                [
                    "soffice",
                    f"-env:UserInstallation=file://{tmp_path}/profile",
                    "--headless",
                    "--norestore",
                    "--convert-to",
                    "docx",
                    "--outdir",
                    str(tmp_path),
                    str(src),
                ],
                check=True,
                capture_output=True,
                timeout=timeout,
            )
        except (OSError, subprocess.SubprocessError):
            log.warning("doc_convert_failed", file=name)
            return None
        out = tmp_path / "source.docx"
        return out.read_bytes() if out.exists() else None


def unpack(name: str, data: bytes) -> list[tuple[str, bytes]]:
    """Archives are opened (one level), old Word/RTF converted to .docx so their text can be read."""
    suffix = _suffix(name)
    if suffix == ".zip":
        files: list[tuple[str, bytes]] = []
        total = 0
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                for info in archive.infolist():
                    if info.is_dir() or _suffix(info.filename) in (".zip", ""):
                        continue
                    if info.file_size > MAX_FILE or total + info.file_size > MAX_TOTAL:
                        continue  # also protects against zip bombs: sizes are checked before reading
                    total += info.file_size
                    inner = _zip_name(info)
                    files += (
                        unpack(inner, archive.read(info))
                        if _suffix(inner) in CONVERTIBLE
                        else [(inner, archive.read(info))]
                    )
                    if len(files) >= MAX_ZIP_FILES:
                        break
        except zipfile.BadZipFile:
            return [(name, data)]
        return files
    if suffix in CONVERTIBLE:
        converted = convert_to_docx(data, name)
        if converted:
            return [(f"{Path(name).stem}.docx", converted)]
    return [(name, data)]


def store_document(db: Session, listing_id: int, name: str, data: bytes, url: str | None = None) -> bool:
    """Save a file for the listing; returns False when the same file is already there."""
    digest = hashlib.sha256(data).hexdigest()
    exists = db.execute(
        select(ListingDocument.id).where(ListingDocument.listing_id == listing_id, ListingDocument.sha256 == digest)
    ).scalar_one_or_none()
    if exists:
        return False
    safe = storage.safe_name(name)
    key = storage.save_bytes(f"listings/{listing_id}/docs/{digest[:12]}_{safe}", data)
    db.add(
        ListingDocument(
            listing_id=listing_id,
            filename=safe,
            url=url,
            storage_key=key,
            sha256=digest,
            size_bytes=len(data),
            parsed={},
        )
    )
    db.flush()
    return True


def collect_documents(db: Session, listing: Listing, http: httpx.Client) -> CollectReport:
    report = CollectReport()
    links: list[DocLink] = []
    platform = platform_of(listing.url)
    if platform is not None:
        report.platform = platform.name
        if not platform.auto_documents:
            # Login-only documentation or rules that forbid automated collection: link only, owner sends the ТЗ.
            report.needs_login = True
            report.platform_note = platform.docs_note
            return report
    if listing.url:
        try:
            html = get_page(http, listing.url)
            report.page_read = True
            links = find_document_links(html, listing.url)
            if len(listing.description or "") < SHORT_DESCRIPTION:  # e.g. an e-mail alert gave only a title
                text = page_text(html)
                if len(text) > len(listing.description or ""):
                    listing.description = text
        except SourceError as exc:
            message = str(exc)
            report.needs_login = "HTTP 401" in message or "HTTP 403" in message
            report.errors.append(message)
        except httpx.HTTPError as exc:
            report.errors.append(f"страница заявки недоступна: {type(exc).__name__}")
    known = set(db.execute(select(ListingDocument.url).where(ListingDocument.listing_id == listing.id)).scalars())
    total = 0
    for link in links:
        if link.url in known:
            continue
        try:
            name, data = download(http, link)
        except LoginRequired as exc:
            report.needs_login = True
            report.errors.append(str(exc))
            continue
        except DocError as exc:
            report.errors.append(str(exc))
            continue
        total += len(data)
        if total > MAX_TOTAL:
            report.errors.append("документации больше 100 МБ: остальные файлы не скачаны")
            break
        for inner_name, inner in unpack(name, data):
            if store_document(db, listing.id, inner_name, inner, url=link.url):
                report.saved.append(inner_name)
    return report
