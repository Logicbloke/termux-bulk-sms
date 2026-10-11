"""Phone numbers to E.164.

A number is whatever someone typed into a spreadsheet: "070-123 45 67",
"+46 (0)70 123 45 67", "0046701234567". Everything that compares numbers
(duplicates, block list, history, the remote lookup) compares this form.
"""

import re

_SEPARATORS = re.compile(r"[\s\-./ ]")


def to_e164(raw, country_code="46"):
    """Return "+<digits>" or None when the input cannot be a phone number.

    A leading 0 means a national number in `country_code`. A number with
    neither "+", "00" nor a leading 0 is refused rather than guessed.
    """
    clean = _SEPARATORS.sub("", (raw or "").strip())
    clean = clean.replace("(0)", "").replace("(", "").replace(")", "")
    if not clean:
        return None
    if clean.startswith("00"):
        clean = "+" + clean[2:]
    if clean.startswith("+"):
        digits = clean[1:]
        if not digits.isascii() or not digits.isdigit():
            return None
        # "+46 070 ..." — the trunk zero typed after the country code.
        if digits.startswith(country_code + "0"):
            digits = country_code + digits[len(country_code) + 1:]
        return "+" + digits if _plausible(digits) else None
    if not clean.isascii() or not clean.isdigit() or not clean.startswith("0"):
        return None
    digits = country_code + clean[1:]
    return "+" + digits if _plausible(digits) else None


def _plausible(digits):
    return 9 <= len(digits) <= 15 and not digits.startswith("0")
