"""Shared Pixiv tag classification rules used by gallery and organizer."""

import re
import unicodedata


R18_TAG_NAMES = frozenset({"r-18", "r18", "18r"})

# Fallback markers used only when the official Pixiv AI field is absent.
# Ordinary tags such as "AI" are intentionally excluded.
AI_TAG_NAMES = frozenset({
    "ai-generated",
    "ai_generated",
    "ai generated",
    "ai生成",
    "ai生成イラスト",
    "ai生成作品",
    "ai画像",
    "ai绘画",
    "ai作品",
    "ai-assisted",
    "aiartwork",
    "aigenerated",
    "novelai",
    "novelaidiffusion",
    "stablediffusion",
    "stable diffusion",
})


def normalize_ai_type(value):
    """Normalize Pixiv's official AI marker to 1 (Human), 2 (AI), or None."""
    if isinstance(value, bool):
        return 2 if value else 1
    try:
        number = int(value)
    except (TypeError, ValueError):
        text = normalize_tag_name(value)
        if text in {"true", "yes", "ai", "ai generated", "ai-generated"}:
            return 2
        if text in {"false", "no", "human", "not ai", "not-ai"}:
            return 1
        return None
    return number if number in (1, 2) else None


def normalize_tag_name(value):
    text = unicodedata.normalize("NFKC", (value or "")).strip().casefold()
    text = re.sub(r"[\u3000]+", " ", text)
    text = re.sub(r"[_\s]+", " ", text)
    return text


def tag_names(tags):
    names = set()
    for tag in tags or ():
        if isinstance(tag, dict):
            values = (tag.get("name"), tag.get("translated_name"))
        else:
            values = (tag["name"], tag["translated_name"])
        names.update(
            normalized
            for normalized in (normalize_tag_name(value) for value in values)
            if normalized
        )
    return names


def is_r18_tags(tags):
    """Match the gallery rule: exact R18 tags only; R-18G is excluded."""
    return bool(tag_names(tags) & R18_TAG_NAMES)


def is_ai_generated(ai_type, tags):
    """Match Shaft/Pixiv behavior: AI means the official AI flag is 2.

    Ordinary user tags are deliberately ignored. They are not equivalent to
    Pixiv's official AI-generated marker and can produce false positives.
    """
    return normalize_ai_type(ai_type) == 2


def ai_classification(ai_type, tags):
    """Return ``AI``, ``Human`` or ``Unknown``.

    Official value 2 is always AI and value 1 is Human. Missing/unknown
    values use only explicit AI markers as a compatibility fallback.
    """
    value = normalize_ai_type(ai_type)
    if value == 2:
        return "AI"
    if value == 1:
        return "Human"
    names = tag_names(tags)
    if names & {normalize_tag_name(name) for name in AI_TAG_NAMES}:
        return "AI"
    return "Unknown"
