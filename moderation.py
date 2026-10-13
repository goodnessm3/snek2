import re
import unicodedata
from typing import Iterable, Optional, Set, Tuple

# Common zero-width / invisible characters
ZERO_WIDTH_RE = re.compile(
    r'[\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff\u00ad\u034f]'
)

def normalize_for_matching(text: str) -> str:
    """Lowercase, strip accents, remove invisibles, keep only a-z letters."""
    if not text:
        return ""
    # Unicode normalize + remove combining marks (accents)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    # Remove zero-width / soft-hyphen etc.
    text = ZERO_WIDTH_RE.sub("", text)
    # Lowercase and keep only alphabetic characters
    return "".join(c.lower() for c in text if c.isalpha())

def contains_forbidden(
    text: str,
    forbidden: Iterable[str],
    allowlist: Optional[Set[str]] = None,
) -> Tuple[bool, Optional[str]]:
    """
    Returns (is_bad, matched_word).
    Works against spacers, zero-width chars, punctuation, etc.
    """
    cleaned = normalize_for_matching(text)
    if not cleaned:
        return False, None

    allow = allowlist or set()
    # Quick allow-list check on the whole cleaned string (or you can
    # check substrings / tokens if you prefer)
    if cleaned in allow:
        return False, None

    for word in forbidden:
        w = word.lower()
        if w and w in cleaned:
            # Optional extra safety: reject the match if the whole cleaned
            # text is a known safe word that happens to contain it
            if cleaned in allow:
                continue
            return True, w
    return False, None