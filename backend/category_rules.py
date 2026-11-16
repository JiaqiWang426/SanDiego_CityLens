"""Compile the approved CSV mapping for deterministic, dependency-free lookup.

Exact normalized detail conditions override the service default. Unseen details
use that default; unseen service names raise an error instead of guessing.
The independent pandas audit remains in category_analysis.py.
"""
import csv
import hashlib
import io
from pathlib import Path

MAPPING = Path(__file__).resolve().with_name('category_mapping.csv')
FIELDS = {'service_name', 'main_category', 'sub_category', 'detail_match', 'detail_values', 'notes'}


def normalize_text(value):
    return ' '.join((value or '').split()) or None


class CategoryRules:
    def __init__(self, rows, sha256):
        self.sha256 = sha256
        self._defaults = {}
        self._conditions = {}
        default_types = {}
        services = set()
        hierarchy = {}
        parents = {}
        for row in rows:
            if None in row or not FIELDS.issubset(row) or any(value is None for value in row.values()):
                raise ValueError('Malformed category mapping row')
            for field in ('service_name', 'main_category', 'sub_category'):
                if row[field] != (normalize_text(row[field]) or ''):
                    raise ValueError(f'Non-normalized mapping label: {field}')
            service, main, sub = row['service_name'], row['main_category'], row['sub_category']
            if not main or not sub:
                raise ValueError('Empty destination category')
            if sub in parents and parents[sub] != main:
                raise ValueError(f'Subcategory has multiple main categories: {sub}')
            parents[sub] = main
            hierarchy.setdefault(main, {}).setdefault(sub, None)
            services.add(service)
            kind, values = row['detail_match'], row['detail_values']
            if kind == 'in':
                for detail in values.split('|'):
                    if not detail or detail != normalize_text(detail):
                        raise ValueError(f'Invalid detail condition for {service!r}')
                    key = (service, detail)
                    if key in self._conditions:
                        raise ValueError(f'Overlapping detail rules for {service!r}: {detail}')
                    self._conditions[key] = (main, sub)
            elif kind in ('any', 'fallback'):
                if values or service in self._defaults:
                    raise ValueError(f'Exactly one compatible default required for {service!r}')
                self._defaults[service] = (main, sub)
                default_types[service] = kind
            else:
                raise ValueError(f'Unsupported detail_match: {kind}')
        if not services or services != set(self._defaults):
            raise ValueError('Every mapped service must have exactly one default')
        for service, _ in self._conditions:
            if default_types[service] != 'fallback':
                raise ValueError(f'Conditional service requires a fallback default: {service!r}')
        self._hierarchy = tuple((main, tuple(subs)) for main, subs in hierarchy.items())

    def classify(self, service_name, service_name_detail):
        service = normalize_text(service_name) or ''
        detail = normalize_text(service_name_detail) or ''
        if service not in self._defaults:
            raise ValueError(f'Unmapped service_name: {service!r}')
        condition = self._conditions.get((service, detail))
        main, sub = condition if condition is not None else self._defaults[service]
        method = 'detail_match' if condition is not None else 'service_default'
        if main == 'Needs Review':
            method = 'needs_review'
        return main, sub, method

    def hierarchy(self):
        """Return all designed categories in first-appearance CSV order."""
        return [{'main_category': main, 'subcategories': list(subs)} for main, subs in self._hierarchy]


def load_category_rules(path=MAPPING):
    content = Path(path).read_bytes()
    reader = csv.DictReader(io.StringIO(content.decode('utf-8-sig'), newline=''))
    headers = reader.fieldnames or []
    if len(headers) != len(set(headers)) or not FIELDS.issubset(headers):
        raise ValueError('Missing or duplicate category mapping columns')
    return CategoryRules(reader, hashlib.sha256(content).hexdigest())
