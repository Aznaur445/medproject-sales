"""Replace personal data with placeholders before text leaves for an external LLM (152-FZ), restore afterwards."""

import re
from typing import Any

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(?<!\d)(?:\+7|8)[\s(-]*\d{3}[\s)-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}(?!\d)")
# «Иванов Иван Иванович», «Иванова Мария Петровна»
FIO_RE = re.compile(
    r"\b[А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?\s+[А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+(?:вич|вна|ична|чна|оглы|кызы)\b"
)
# «Иванов И.И.», «И.И. Иванов»
INITIALS_RE = re.compile(r"\b(?:[А-ЯЁ][а-яё]+\s+[А-ЯЁ]\.\s?[А-ЯЁ]\.|[А-ЯЁ]\.\s?[А-ЯЁ]\.\s?[А-ЯЁ][а-яё]+)")
SNILS_RE = re.compile(r"\b\d{3}-\d{3}-\d{3}\s\d{2}\b")


class Masker:
    def __init__(self) -> None:
        self.mapping: dict[str, str] = {}
        self._reverse: dict[str, str] = {}

    def _token(self, kind: str, value: str) -> str:
        if value in self._reverse:
            return self._reverse[value]
        token = f"[{kind}_{sum(1 for k in self.mapping if k.startswith('[' + kind)) + 1}]"
        self.mapping[token] = value
        self._reverse[value] = token
        return token

    def mask(self, text: str) -> str:
        for kind, pattern in (
            ("EMAIL", EMAIL_RE),
            ("PHONE", PHONE_RE),
            ("SNILS", SNILS_RE),
            ("PERSON", FIO_RE),
            ("PERSON", INITIALS_RE),
        ):
            text = pattern.sub(lambda m, k=kind: self._token(k, m.group(0)), text)
        return text

    def unmask(self, value: Any) -> Any:
        if isinstance(value, str):
            for token, original in self.mapping.items():
                value = value.replace(token, original)
            return value
        if isinstance(value, list):
            return [self.unmask(v) for v in value]
        if isinstance(value, dict):
            return {k: self.unmask(v) for k, v in value.items()}
        return value
