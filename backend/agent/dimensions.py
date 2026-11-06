"""Canonical requested dimensions; chat-form selections are fallback scope."""
import re


SUPPORTED_DIMENSIONS = {'traffic_collisions', 'service_requests'}
COLLISION_PATTERN = r'\b(?:(?:traffic|car|vehicle|motor vehicle)[ -]+(?:accidents?|collisions?|crash(?:es)?)|collisions?|crash(?:es)?)\b'
SERVICE_PATTERN = r'\b(?:get[ -]it[ -]done|service[ -]requests?|complaints?|potholes?|streetlights?|sidewalks?|graffiti|trash)\b'
PATTERNS = {'traffic_collisions': COLLISION_PATTERN, 'service_requests': SERVICE_PATTERN}
_NEGATED = re.compile(r'\b(?:no|not|without|exclude|excluding)(?:\s+(?:the|any|all|considering|including|include|counting|count|comparing|compare)){0,3}\s*$',re.I)


def mentioned_dimensions(text):
    """Recognize supported requests, not incidental pin labels or negated domains."""
    normalized = text.replace('_', ' ')
    return [domain for domain, pattern in PATTERNS.items() if any(
        not _NEGATED.search(normalized[max(0,match.start()-70):match.start()])
        for match in re.finditer(pattern, normalized, re.I))]


def exclusive_dimensions(text):
    """Return an explicit 'only/just <supported domain>' replacement, if present."""
    markers = list(re.finditer(r'\b(?:only|just)\b',text,re.I))
    if not markers:
        return None
    suffix = text[markers[-1].end():]
    selected = mentioned_dimensions(suffix)
    if selected:
        return selected
    if not suffix.strip(' .!?'):
        return mentioned_dimensions(text[:markers[-1].start()]) or None
    return None


def chat_preferences(question, fallbacks):
    """Question domains override sticky chat UI defaults, not coordinate contracts."""
    exclusive = exclusive_dimensions(question)
    if exclusive is not None:
        return exclusive
    explicit = mentioned_dimensions(question)
    if not explicit:
        return list(fallbacks)
    return explicit + [name for name in fallbacks if name not in SUPPORTED_DIMENSIONS]
