"""Exact stored vocabulary and explicit, nonautomatic road-scope suggestions."""
import re


class UnsupportedServiceCategory(ValueError):
    pass


def normalize_category(value):
    return ' '.join(value.upper().split()) or None


def service_categories(conn):
    rows = conn.execute('''SELECT category FROM (
        SELECT DISTINCT citylens.normalize_request_category(service_name) AS category
        FROM citylens.service_requests) labels ORDER BY category COLLATE "C" NULLS FIRST''').fetchall()
    return [row[0] for row in rows]


def validate_service_categories(conn, selection):
    names = selection.service_names
    if names is None:
        names = [selection.service_name] if selection.service_name is not None else []
    if names and not {normalize_category(name) for name in names} <= set(service_categories(conn)):
        raise UnsupportedServiceCategory('Select a category present in the stored Get It Done vocabulary')


# Proposed scopes are application policy suggestions, not an official taxonomy
# and never automatically stand for every meaning of "road-related".
ROAD_SCOPE_SUGGESTIONS = {
    'road_surface': ('POTHOLE', 'PAVEMENT MAINTENANCE'),
    'traffic_infrastructure': ('TRAFFIC SIGN MAINTENANCE', 'TRAFFIC SIGNAL ISSUE',
        'TRAFFIC SIGNAL TIMING', 'TRAFFIC ENGINEERING', 'STREET LIGHT MAINTENANCE'),
    'optional_street_and_pedestrian_maintenance': ('SIDEWALK REPAIR ISSUE', 'ROW MAINTENANCE',
        'RIGHT-OF-WAY CODE ENFORCEMENT', 'STREET SWEEPING', 'STREET FLOODED'),
}


def road_scope_suggestions(catalog):
    known = set(catalog)
    return {key: [name for name in names if name in known] for key, names in ROAD_SCOPE_SUGGESTIONS.items()}


def road_clarification(message, catalog):
    text = ' '.join(message.upper().split())
    if not re.search(r'\bROAD[- ]RELATED\b', text):
        return None
    if any(name and name in text for name in catalog):
        return None
    suggestions = road_scope_suggestions(catalog)
    choices = '; '.join(key.replace('_', ' ')+': '+', '.join(names)
                        for key, names in suggestions.items() if names)
    return ('Road-related has no single validated category mapping in these records. '
        'Available interpretations using stored service labels are '+(choices or 'not available')+'. '
        'Reply with the exact service labels to include. Parking, vegetation and other potentially '
        'related categories are not included automatically. Your locations, radius and dates are preserved; no analytical SQL ran.')
