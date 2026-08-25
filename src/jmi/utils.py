"""Small pure helpers shared across the pipeline.

Kept dependency-light and side-effect free so they are trivially unit testable.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import UTC, datetime

from bs4 import BeautifulSoup

_WS_RE = re.compile(r"[ \t\r\f\v]+")
_MULTINEWLINE_RE = re.compile(r"\n{3,}")
_NON_SLUG_RE = re.compile(r"[^a-z0-9]+")
_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9+#.\-]*")

# Legal-entity suffixes stripped when building a company identity key.
_COMPANY_NOISE = {
    "inc", "inc.", "llc", "l.l.c", "ltd", "ltd.", "limited", "corp", "corp.",
    "corporation", "co", "co.", "gmbh", "bv", "b.v", "nv", "plc", "sa", "ag",
    "pty", "pte", "oy", "ab", "as", "srl", "spa", "kk", "the",
}


def sha1_hex(*parts: str) -> str:
    """Stable content hash over the given parts."""
    digest = hashlib.sha1(usedforsecurity=False)
    for part in parts:
        digest.update(part.encode("utf-8", errors="replace"))
        digest.update(b"\x1f")
    return digest.hexdigest()


def slugify(value: str, max_length: int = 80) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    value = value.encode("ascii", "ignore").decode("ascii").lower()
    value = _NON_SLUG_RE.sub("-", value).strip("-")
    return value[:max_length] or "unknown"


def company_key(name: str) -> str:
    """Identity key for a company: lowercased, de-suffixed, punctuation-free.

    ``Acme Inc.`` / ``ACME, LLC`` / ``acme`` all collapse to ``acme``.
    """
    cleaned = unicodedata.normalize("NFKD", name or "")
    cleaned = cleaned.encode("ascii", "ignore").decode("ascii").lower()
    cleaned = re.sub(r"[^a-z0-9\s.]", " ", cleaned)
    tokens = [t for t in cleaned.split() if t.strip(".") and t not in _COMPANY_NOISE]
    return "-".join(tokens)[:120] or slugify(name)


def html_to_text(html: str | None) -> str:
    """Flatten an HTML fragment to readable plain text."""
    if not html:
        return ""
    if "<" not in html:
        return normalize_whitespace(html)
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    for br in soup.find_all(["br"]):
        br.replace_with("\n")
    for block in soup.find_all(["p", "li", "div", "tr", "h1", "h2", "h3", "h4"]):
        block.append("\n")
    return normalize_whitespace(soup.get_text(" "))


def normalize_whitespace(text: str) -> str:
    text = text.replace("\xa0", " ").replace("​", "")
    text = _WS_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.splitlines())
    return _MULTINEWLINE_RE.sub("\n\n", text).strip()


def tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall((text or "").lower())


def simhash(text: str, bits: int = 64) -> int:
    """Charikar SimHash over 3-word shingles.

    Near-identical postings land within a small Hamming distance of each other,
    which is what makes cross-board duplicate detection cheap.
    """
    tokens = tokenize(text)
    if not tokens:
        return 0
    shingles = (
        [" ".join(tokens[i:i + 3]) for i in range(len(tokens) - 2)]
        if len(tokens) >= 3
        else tokens
    )
    vector = [0] * bits
    for shingle in shingles:
        h = int.from_bytes(
            hashlib.blake2b(shingle.encode("utf-8"), digest_size=bits // 8).digest(), "big"
        )
        for i in range(bits):
            vector[i] += 1 if (h >> i) & 1 else -1
    result = 0
    for i, weight in enumerate(vector):
        if weight > 0:
            result |= 1 << i
    return result


def hamming_distance(a: int, b: int) -> int:
    return ((a ^ b) & ((1 << 64) - 1)).bit_count()


def to_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def truncate(text: str | None, limit: int) -> str | None:
    if text is None:
        return None
    return text if len(text) <= limit else text[: limit - 1] + "…"


def signed_64(value: int) -> int:
    """Fold an unsigned 64-bit hash into the signed range SQL integers accept."""
    value &= (1 << 64) - 1
    return value - (1 << 64) if value >= (1 << 63) else value
