"""Chinese to English translation through the DeepL API.

Used by sources whose site is not in English (today only Kuaikan) to store
titles in English. A Free-plan key ends in ":fx" and is served from a
different host than a Pro key, so the host follows the key.
"""

import re
from typing import List

import httpx

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

# Words a title keeps in lower case unless they open or close it, or follow a
# colon - the usual English headline rule.
_SMALL_WORDS = {
    "a", "an", "and", "as", "at", "but", "by", "for", "from", "in", "into",
    "nor", "of", "on", "or", "over", "the", "to", "up", "via", "with", "vs",
}
_LEADING_PUNCT = re.compile(r"^[^\w]*")


class TranslationError(RuntimeError):
    pass


def title_case(text: str) -> str:
    """'All my summons have SSS-tier talents.' -> 'All My Summons Have SSS-Tier Talents'.

    Machine translation returns sentence case and sometimes a full stop;
    titles want neither. Words that already carry capitals past their first
    letter (SSS, iPhone) are left exactly as they are.
    """
    words = (text or "").strip().rstrip(".。").split()
    out: List[str] = []
    for i, word in enumerate(words):
        edge = i == 0 or i == len(words) - 1 or (out and out[-1].endswith(":"))
        parts = []
        for part in word.split("-"):
            lead = _LEADING_PUNCT.match(part).group(0)
            core = part[len(lead):]
            if any(c.isupper() for c in core[1:]):
                parts.append(part)
            elif core.lower() in _SMALL_WORDS and not edge and "-" not in word:
                parts.append(lead + core.lower())
            else:
                parts.append(lead + core[:1].upper() + core[1:])
        out.append("-".join(parts))
    return " ".join(out)


class DeepLTranslator:
    def __init__(self, key: str) -> None:
        self._key = key.strip()
        host = "api-free.deepl.com" if self._key.endswith(":fx") else "api.deepl.com"
        self._url = f"https://{host}/v2/translate"

    async def translate(self, texts: List[str], source: str = "ZH", target: str = "EN-US") -> List[str]:
        """Translate a batch in one request, results in the same order."""
        if not texts:
            return []
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    self._url,
                    headers={"Authorization": f"DeepL-Auth-Key {self._key}"},
                    json={"text": texts, "source_lang": source, "target_lang": target},
                )
        except httpx.HTTPError as e:
            raise TranslationError(f"DeepL unreachable: {e}") from e
        if response.status_code != 200:
            # 403 bad key, 456 monthly quota used up, 429 too many requests.
            raise TranslationError(f"DeepL answered {response.status_code}: {response.text[:200]}")
        return [t["text"] for t in response.json()["translations"]]


def get_translator() -> "DeepLTranslator | None":
    """The configured translator, or None when no DEEPL_KEY is set."""
    return DeepLTranslator(settings.deepl_key) if settings.deepl_key else None
