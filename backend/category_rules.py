"""Compile the approved CSV mapping for deterministic, dependency-free lookup.

Ported unchanged from the Service Request Explorer reference implementation
(feature/modify-detail). Exact normalized detail conditions override the service
default. Unseen details use that default; unseen service names raise an error in
``classify`` instead of guessing. The independent pandas audit remains in
category_analysis.py.

CityLens additions (no change to the rules above):

* ``classify_or_unmapped`` returns an explicit ``Needs Review / Unmapped service
  name / unmapped`` triple instead of raising. PostgreSQL holds every historical
  record, so a future source label must not abort an import; it is labelled and
  counted instead of silently guessed.
* ``rule_rows`` exports the validated rules for ``citylens.service_category_rules``.
  The SQL classifier in sql/migrations/001_service_request_classification.sql
  evaluates exactly these rows, so Python and PostgreSQL share one mapping.
"""
import csv
import hashlib
import io
from pathlib import Path

MAPPING = Path(__file__).resolve().with_name('category_mapping.csv')
FIELDS = {'service_name', 'main_category', 'sub_category', 'detail_match', 'detail_values', 'notes'}
METHODS = ('detail_match', 'service_default', 'needs_review', 'unmapped')
UNMAPPED = ('Needs Review', 'Unmapped service name', 'unmapped')


def normalize_text(value):
    return ' '.join((value or '').split()) or None


class CategoryRules:
    def __init__(self, rows, sha256):
        self.sha256 = sha256
        self._defaults = {}
        self._conditions = {}
        self._rows = []
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
                    self._rows.append((service, detail, main, sub))
            elif kind in ('any', 'fallback'):
                if values or service in self._defaults:
                    raise ValueError(f'Exactly one compatible default required for {service!r}')
                self._defaults[service] = (main, sub)
                default_types[service] = kind
                self._rows.append((service, None, main, sub))
            else:
                raise ValueError(f'Unsupported detail_match: {kind}')
        if not services or services != set(self._defaults):
            raise ValueError('Every mapped service must have exactly one default')
        for service, _ in self._conditions:
            if default_types[service] != 'fallback':
                raise ValueError(f'Conditional service requires a fallback default: {service!r}')
        if UNMAPPED[1] in parents:
            raise ValueError(f'Reserved subcategory name: {UNMAPPED[1]}')
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

    def classify_or_unmapped(self, service_name, service_name_detail):
        """Same result as classify for mapped services; explicit label otherwise."""
        try:
            return self.classify(service_name, service_name_detail)
        except ValueError:
            return UNMAPPED

    def hierarchy(self, include_unmapped=False):
        """Return all designed categories in first-appearance CSV order.

        ``include_unmapped`` appends the system-defined unmapped subcategory to
        Needs Review; it is not part of the approved mapping itself.
        """
        result = [{'main_category': main, 'subcategories': list(subs)} for main, subs in self._hierarchy]
        if include_unmapped:
            review = next((row for row in result if row['main_category'] == UNMAPPED[0]), None)
            if review is None:
                result.append({'main_category': UNMAPPED[0], 'subcategories': [UNMAPPED[1]]})
            else:
                review['subcategories'].append(UNMAPPED[1])
        return result

    def rule_rows(self):
        """(ordinal, service_name, detail or None, main, sub) in CSV order."""
        return [(ordinal, *row) for ordinal, row in enumerate(self._rows, 1)]


def load_category_rules(path=MAPPING):
    content = Path(path).read_bytes()
    reader = csv.DictReader(io.StringIO(content.decode('utf-8-sig'), newline=''))
    headers = reader.fieldnames or []
    if len(headers) != len(set(headers)) or not FIELDS.issubset(headers):
        raise ValueError('Missing or duplicate category mapping columns')
    return CategoryRules(reader, hashlib.sha256(content).hexdigest())
