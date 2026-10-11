"""Message templates: "[name]" and any CSV column header in square brackets."""

import re

_PLACEHOLDER = re.compile(r"\[([^\[\]\n]{1,40})\]")

# GSM 03.38 default alphabet; characters in _GSM_EXT cost two septets.
_GSM = set(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
_GSM_EXT = set("^{}\\[~]|€")


def values(name, fields, first_name_only=False):
    """The lookup table for one recipient; keys are lower-cased."""
    out = {str(k).strip().lower(): str(v).strip() for k, v in (fields or {}).items()}
    name = (name or "").strip()
    if first_name_only and name:
        name = name.split()[0]
    out["name"] = name
    return out


def keys(template):
    return [m.group(1).strip().lower() for m in _PLACEHOLDER.finditer(template or "")]


def render(template, vals):
    def swap(match):
        key = match.group(1).strip().lower()
        return vals[key] if key in vals else match.group(0)

    return _PLACEHOLDER.sub(swap, template or "")


def empty_keys(template, vals):
    """Placeholders this recipient has no value for."""
    return sorted({k for k in keys(template) if k in vals and vals[k] == ""})


def unknown_keys(template, known):
    """Placeholders that match nothing and would be sent literally."""
    known = {k.lower() for k in known} | {"name"}
    return sorted({k for k in keys(template) if k not in known})


def segments(text):
    """(characters, parts, encoding) the way a handset counts them."""
    if all(ch in _GSM or ch in _GSM_EXT for ch in text):
        units = sum(2 if ch in _GSM_EXT else 1 for ch in text)
        parts = 1 if units <= 160 else -(-units // 153)
        return units, (parts if units else 0), "GSM-7"
    units = len(text.encode("utf-16-le")) // 2
    parts = 1 if units <= 70 else -(-units // 67)
    return units, parts, "Unicode"
