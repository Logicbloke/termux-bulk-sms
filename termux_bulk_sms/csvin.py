"""Pasted CSV to recipients.

The paste comes from spreadsheets, web pages and notes, so nothing is assumed:
the delimiter is sniffed, a header row is optional, and the phone column is the
one where most cells are phone numbers.
"""

import csv
import io
import re

from .phone import to_e164

_NAME_HEADERS = ("name", "namn", "first name", "firstname", "förnamn", "fornamn",
                 "contact", "kontakt", "full name")
_PHONE_HEADERS = ("phone", "telefon", "tel", "mobile", "mobil", "mobilnummer",
                  "number", "nummer", "telefonnummer", "phone number")
_PHONE_IN_LINE = re.compile(r"(\+|00)?\d[\d\s\-./()]{6,}\d")


def parse(text, country_code="46"):
    """Return a list of {"name", "phone_raw", "fields"}; fields is {header: cell}."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    if not text.strip():
        return []
    delimiter = _sniff(text)
    if delimiter is None:
        return [_loose(line, country_code) for line in text.split("\n") if line.strip()]

    rows = [[c.strip() for c in row] for row in csv.reader(io.StringIO(text), delimiter=delimiter)]
    rows = [r for r in rows if any(r)]
    if not rows:
        return []
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]

    header = None
    first = [c.lower() for c in rows[0]]
    # A header row has no phone number in it — and no failed attempt at one,
    # or a mistyped first recipient would silently vanish as a "header".
    digitless = not any(sum(ch.isdigit() for ch in c) >= 5 for c in rows[0])
    if digitless and (any(c in _NAME_HEADERS + _PHONE_HEADERS for c in first) or len(rows) > 1):
        header, rows = rows[0], rows[1:]
    if not rows:
        return []

    phone_col = _phone_column(rows, header, country_code)
    name_col = _name_column(rows, header, phone_col)

    out = []
    for row in rows:
        fields = {}
        if header:
            fields = {h: row[i] for i, h in enumerate(header) if h and i not in (phone_col,)}
        out.append({
            "name": row[name_col] if name_col is not None else "",
            "phone_raw": row[phone_col],
            "fields": fields,
        })
    return out


def _sniff(text):
    lines = [l for l in text.split("\n") if l.strip()][:20]
    best, best_score = None, 0
    for d in ("\t", ";", ",", "|"):
        counts = [l.count(d) for l in lines]
        hits = sum(1 for c in counts if c > 0)
        # A comma inside "Andersson, Anna" on one line is not a delimiter.
        if hits * 2 > len(lines) and hits > best_score:
            best, best_score = d, hits
    return best


def _loose(line, country_code):
    """One recipient from a free-form line: the phone-looking part and the rest."""
    match = _PHONE_IN_LINE.search(line)
    if match and to_e164(match.group(0), country_code):
        name = (line[:match.start()] + " " + line[match.end():]).strip(" \t,;:-")
        return {"name": name, "phone_raw": match.group(0).strip(), "fields": {}}
    return {"name": "", "phone_raw": line.strip(), "fields": {}}


def _phone_column(rows, header, country_code):
    width = len(rows[0])
    scores = [sum(1 for r in rows if to_e164(r[i], country_code)) for i in range(width)]
    if max(scores) > 0:
        return scores.index(max(scores))
    if header:
        for i, h in enumerate(header):
            if h.lower() in _PHONE_HEADERS:
                return i
    return width - 1


def _name_column(rows, header, phone_col):
    width = len(rows[0])
    if header:
        for i, h in enumerate(header):
            if i != phone_col and h.lower() in _NAME_HEADERS:
                return i
    best, best_score = None, 0
    for i in range(width):
        if i == phone_col:
            continue
        score = sum(1 for r in rows if r[i] and not any(ch.isdigit() for ch in r[i]) and "@" not in r[i])
        if score > best_score:
            best, best_score = i, score
    return best
