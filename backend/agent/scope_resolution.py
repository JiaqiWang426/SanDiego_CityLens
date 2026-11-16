"""Deterministic explicit scope precedence, independent of model/UI defaults."""
from datetime import datetime
import re
from zoneinfo import ZoneInfo
from .models import TimeRange

LA = ZoneInfo('America/Los_Angeles')

# A year phrase only denotes the whole calendar year when the message has no
# finer-grained interval ("January to March in 2026", "Q1 2026", "2026-03-01",
# "past six months"). Those intervals stay with the validated interpretation.
FINER_INTERVAL = re.compile(
    r'\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|'
    r'sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b'
    r'|\bq[1-4]\b|\bquarter\b|\b(?:first|second|third|fourth|last|past|previous|next)\s+(?:\w+\s+)?'
    r'(?:days?|weeks?|months?|quarters?|half)\b|\bh[12]\b|\bhalf\b|\b(?:spring|summer|fall|autumn|winter)\b'
    r'|\b20\d{2}-\d{1,2}\b|\b\d{1,2}/\d{1,2}\b|\bbetween\b|\bfrom\b.*\b(?:to|through|until)\b|\bsince\b|\buntil\b'
    r'|\bthrough\b|\bthis\s+(?:month|week)\b|月|季度|上半年|下半年', re.I)


def explicit_scope(message):
    matches = re.findall(r'\b(?:in|during|for(?:\s+the\s+year)?|calendar\s+year)\s+(20\d{2})(?![-\d])|(?<!\d)(20\d{2})年', message or '', re.I)
    years = {int(part) for match in matches for part in match if part}
    if years and FINER_INTERVAL.search(message or ''):
        years = set()
    if len(years) > 1:
        raise ValueError('Multiple calendar years require an explicit analysis interval.')
    interval = None
    if years:
        year = years.pop()
        interval = TimeRange(start=datetime(year, 1, 1, tzinfo=LA), end=datetime(year+1, 1, 1, tzinfo=LA))
    radii = set()
    pattern = r'(?:within\s+|radius(?:\s+of)?\s*|半径\s*)([\d,]+(?:\.\d+)?)\s*(kilomet(?:er|re)s?|km|met(?:er|re)s?|miles?|m\b|公里|米)'
    for value, unit in re.findall(pattern, message or '', re.I):
        amount = float(value.replace(',', ''))
        amount *= 1000 if unit.lower().startswith(('kilo', 'km')) or unit == '公里' else 1609.344 if unit.lower().startswith('mile') else 1
        radii.add(amount)
    if len(radii) > 1:
        raise ValueError('Conflicting radii require confirmation.')
    radius = next(iter(radii), None)
    if radius is not None and not 0 <= radius <= 50000:
        raise ValueError('Radius must be between 0 and 50,000 meters.')
    return interval, radius


def resolve_scope(message, interval=None, radius=800, *, system_defaults=False, reference_time=None):
    explicit_time, explicit_radius = explicit_scope(message)
    interval = explicit_time or interval
    radius = explicit_radius if explicit_radius is not None else radius
    if interval is None and system_defaults:
        now = reference_time or datetime.now(LA)
        interval = TimeRange(start=datetime(now.year, 1, 1, tzinfo=LA), end=datetime(now.year+1, 1, 1, tzinfo=LA))
    return interval, radius
