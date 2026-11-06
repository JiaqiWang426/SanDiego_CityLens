"""Regressions for plan_rejected:requested_dimension_omitted on collision questions.

Root cause: the chat form sends both dataset checkboxes as preferences. Those
sticky UI defaults were treated as explicit requests, so a collision-only
question with a correct collision-only plan was rejected for omitting service
requests. Question dimensions now override chat UI defaults before planning;
the validator itself is unchanged and still rejects genuinely omitted domains.
External model, geocoder and database are mocked; numbers come from FakeTools.
"""
import asyncio

import pytest

from backend.agent.dimensions import chat_preferences, exclusive_dimensions, mentioned_dimensions
from backend.agent.evidence import verify_claims
from backend.agent.location_flow import LocationConversations
from backend.agent.location_models import AddressProposal, ChatTurn, GeocodedCandidate
from backend.agent.models import Location, Plan, QueryRequest
from backend.agent.sessions import ConversationRunner, apply_followup
from backend.agent.settings import Settings
from backend.agent.user_geocoding import AddressResolution
from backend.agent.workflow import Agent, validated_calls
from backend.agent_tests.helpers import FakeTools
from backend.agent_tests.test_location_flow import InterpretingModel, follow, interpretation
from backend.agent_tests.test_sessions import patch

UI_DEFAULTS = ['traffic_collisions', 'service_requests']
COLLISIONS = Plan(intent='compare_locations', tools=['aggregate_traffic_collisions'])
SERVICES = Plan(intent='compare_locations', tools=['count_service_requests'])
MIXED = Plan(intent='compare_locations', tools=['aggregate_traffic_collisions', 'count_service_requests'])
# Matches the January bucket FakeTools returns; evidence rejects foreign buckets.
WINDOW = dict(start='2026-01-01T00:00:00-08:00', end='2026-03-01T00:00:00-08:00', calendar_timezone='America/Los_Angeles')
PINS = {'A': (32.873424299545, -117.218238397659), 'B': (32.869772212037226, -117.22182179396124)}


def address(id, number, name, suffix, quote, city='San Diego'):
    return AddressProposal(id=id, source_quote=quote, house_number=number, street_name=name,
                           street_suffix=suffix, direction=None, city=city, state='CA')


REGENTS = address('A', '9237', 'Regents', 'Rd', '9237 Regents Road')
MAHAILA = address('B', '3950', 'Mahaila', 'Ave', '3950 Mahaila Ave')


class PinGeocoder:
    """Returns fixed provider coordinates and a deliberately misleading label."""
    def __init__(self):
        self.calls = []

    def resolve(self, proposal):
        self.calls.append(proposal)
        lat, lon = PINS[proposal.id]
        label = proposal.display + ' (Axiom La Jolla)'
        return AddressResolution('resolved', [GeocodedCandidate(candidate_id='candidate_' + proposal.id,
            matched_address=label, location=Location(id=proposal.id, label=label, latitude=lat, longitude=lon))])


def chat(question, plan, locations=(REGENTS, MAHAILA), preferences=UI_DEFAULTS):
    async def run():
        agent = Agent(InterpretingModel(interpretation(locations=list(locations), plan=plan.model_dump(), time_range=WINDOW)),
                      FakeTools(), Settings(), geocoder=PinGeocoder())
        pending = await agent.chat(ChatTurn(message=question, preferences=list(preferences)))
        assert pending.status == 'needs_confirmation' and agent.tools.calls == []
        return agent, await agent.chat(follow(pending))
    return asyncio.run(run())


def assert_verified(result):
    assert result.status == 'partial' and result.claims
    verify_claims(result.claims, result.evidence)
    ids = {e.evidence_id for e in result.evidence}
    assert all(set(c.evidence_ids) <= ids for c in result.claims)
    assert result.execution_plan and len(result.tool_calls) == len(result.execution_plan)


@pytest.mark.parametrize('text', ['traffic collisions', 'traffic accidents', 'crashes', 'car accidents',
                                  'a car crash', 'vehicle collisions', 'traffic_collisions'])
def test_collision_expressions_map_to_canonical_dimension(text):
    assert mentioned_dimensions(f'Compare {text} near A and B') == ['traffic_collisions']


@pytest.mark.parametrize('text', ['Compare crime near A and B', 'Which is safer?', 'Compare accidental falls',
                                  'Compare traffic congestion', 'Compare A and B, not including crashes'])
def test_unrelated_or_negated_text_is_not_mapped_to_collisions(text):
    assert 'traffic_collisions' not in mentioned_dimensions(text)


def test_question_dimensions_override_sticky_chat_defaults():
    assert chat_preferences('Compare traffic collisions near A and B', UI_DEFAULTS) == ['traffic_collisions']
    assert chat_preferences('Compare Get It Done requests', UI_DEFAULTS) == ['service_requests']
    assert sorted(chat_preferences('Compare crashes and Get It Done requests', UI_DEFAULTS)) == sorted(UI_DEFAULTS)
    # No dataset named in the question: the UI selection remains the scope.
    assert chat_preferences('Compare A and B', UI_DEFAULTS) == UI_DEFAULTS
    assert exclusive_dimensions('Now compare only traffic collisions') == ['traffic_collisions']


def test_root_cause_validator_strictness_is_preserved():
    """The captured failing request: UI defaults as preferences, collision-only plan."""
    question = 'Compare the number of traffic collisions between 9237 Regents Road and 3950 Mahaila Ave'
    pins = [dict(id=k, latitude=v[0], longitude=v[1]) for k, v in PINS.items()]
    scope = dict(question=question, locations=pins, radius_meters=800,
                 time_range=dict(start='2026-01-01T00:00:00-08:00', end='2026-03-01T00:00:00-08:00'))
    # Before: sticky defaults reached the validator as explicit requirements.
    with pytest.raises(ValueError, match='omitted an explicitly requested supported dimension'):
        validated_calls(QueryRequest(**scope, preferences=UI_DEFAULTS), COLLISIONS, Settings())
    # After: the chat flow narrows preferences from the question first.
    narrowed = QueryRequest(**scope, preferences=chat_preferences(question, UI_DEFAULTS))
    assert [c.name for c in validated_calls(narrowed, COLLISIONS, Settings())] == ['aggregate_traffic_collisions'] * 2
    # A question that names collisions still cannot be answered without them.
    with pytest.raises(ValueError, match='omitted'):
        validated_calls(narrowed, SERVICES, Settings())


def test_two_location_collision_comparison_end_to_end():
    question = 'Compare the number of traffic collisions between 9237 Regents Road and 3950 Mahaila Ave in 2026, in a radius of 1km'
    agent, reply = chat(question, COLLISIONS)
    assert reply.status == 'complete', reply.result.limitation_codes
    result = reply.result
    assert_verified(result)
    assert [c.name for c in agent.tools.calls] == ['aggregate_traffic_collisions'] * 2
    assert not any(code.startswith('plan_rejected') for code in result.limitation_codes)
    metric, = result.comparison.metrics
    assert (metric.metric, metric.value_a, metric.value_b, metric.difference_b_minus_a) == \
        ('geocoded_collision_report_count', 7, 11, 4)
    # Spatial scope comes from provider coordinates, not from any city label.
    for call in agent.tools.calls:
        assert (call.location.latitude, call.location.longitude) == PINS[call.location.id]


@pytest.mark.parametrize('phrase', ['traffic accidents', 'crashes', 'car accidents'])
def test_single_location_collision_count(phrase):
    question = f'How many {phrase} were there near 3950 Mahaila Ave?'
    agent, reply = chat(question, Plan(intent='location_summary', tools=['aggregate_traffic_collisions']),
                        locations=[address('A', '3950', 'Mahaila', 'Ave', '3950 Mahaila Ave')])
    assert reply.status == 'complete', reply.result.limitation_codes
    assert_verified(reply.result)
    assert [c.name for c in agent.tools.calls] == ['aggregate_traffic_collisions']
    assert reply.result.evidence[0].value == 7 and reply.result.evidence[0].domain == 'traffic_collisions'


def test_get_it_done_comparison():
    agent, reply = chat('Compare Get It Done requests between 9237 Regents Road and 3950 Mahaila Ave', SERVICES)
    assert reply.status == 'complete', reply.result.limitation_codes
    assert_verified(reply.result)
    assert [c.name for c in agent.tools.calls] == ['count_service_requests'] * 2
    assert all(e.domain == 'service_requests' for e in reply.result.evidence)


def test_mixed_get_it_done_and_collision_comparison():
    question = 'Compare traffic collisions and Get It Done requests between 9237 Regents Road and 3950 Mahaila Ave'
    agent, reply = chat(question, MIXED)
    assert reply.status == 'complete', reply.result.limitation_codes
    assert_verified(reply.result)
    assert sorted(m.metric for m in reply.result.comparison.metrics) == ['geocoded_collision_report_count', 'request_id_count']
    # Dropping a requested dimension is still rejected, with no tools executed.
    agent, reply = chat(question, COLLISIONS)
    assert reply.status == 'error' and agent.tools.calls == [] and not reply.result.claims
    assert 'plan_rejected:requested_dimension_omitted' in reply.result.limitation_codes


def test_followup_compare_only_traffic_collisions():
    request = QueryRequest(question='Compare traffic collisions and Get It Done requests',
        locations=[dict(id=k, latitude=v[0], longitude=v[1]) for k, v in PINS.items()], radius_meters=800,
        time_range=dict(start='2026-01-01T00:00:00-08:00', end='2026-03-01T00:00:00-08:00'), preferences=UI_DEFAULTS)
    context = ConversationRunner.context(request, MIXED)
    message = 'Now compare only traffic collisions'
    for changed in (['tools'], []):  # the model may also forget the tools diff
        updated, plan, _ = apply_followup(context, message,
            patch(COLLISIONS, changed_plan_fields=changed, change_quote='only traffic collisions'), Settings())
        assert updated.preferences == ['traffic_collisions'] and plan.tools == ['aggregate_traffic_collisions']
        assert updated.locations == request.locations and updated.radius_meters == 800
        assert updated.time_range == request.time_range
    tools = FakeTools()
    result = asyncio.run(Agent(InterpretingModel(), tools, Settings()).query(updated, plan=plan))
    assert_verified(result)
    assert [c.name for c in tools.calls] == ['aggregate_traffic_collisions'] * 2
    # Service data is never substituted for the explicitly requested collisions.
    with pytest.raises(ValueError):
        apply_followup(context, message, patch(SERVICES, changed_plan_fields=['tools'],
                                               change_quote='only traffic collisions'), Settings())


@pytest.mark.parametrize('question', ['Compare crime between A and B', 'Which location is safer?'])
def test_unsupported_dimensions_are_still_rejected(question):
    pins = [dict(id=k, latitude=v[0], longitude=v[1]) for k, v in PINS.items()]
    request = QueryRequest(question=question, locations=pins, radius_meters=800,
        time_range=dict(start='2026-01-01T00:00:00-08:00', end='2026-03-01T00:00:00-08:00'))
    tools = FakeTools()
    model = InterpretingModel()
    model.plan_value = Plan(intent='unsupported_dimension', tools=[], unsupported_dimensions=['crime'])
    result = asyncio.run(Agent(model, tools, Settings()).query(request))
    assert result.status == 'unsupported' and tools.calls == [] and not result.claims
    with pytest.raises(ValueError):
        validated_calls(request.model_copy(update={'preferences': ['crime']}),
                        Plan(intent='unsupported_dimension', tools=['aggregate_traffic_collisions']), Settings())


def test_unsupported_question_in_chat_never_executes_default_datasets():
    agent, reply = chat('Compare crime between 9237 Regents Road and 3950 Mahaila Ave',
                        Plan(intent='unsupported_dimension', tools=[], unsupported_dimensions=['crime']))
    assert agent.tools.calls == [] and not reply.result.claims


def test_model_cannot_rename_default_city_to_la_jolla():
    flow = LocationConversations(PinGeocoder())
    source = 'How many crashes near 3950 MAHAILA AVE, SAN DIEGO, CA 92122 (Axiom La Jolla)?'
    quote = '3950 MAHAILA AVE'
    assert flow.check_addresses([address('A', '3950', 'MAHAILA', 'AVE', quote)], source)
    # "La Jolla" here is only apartment branding; the user's postal city is San Diego.
    branded = 'How many crashes near 3950 MAHAILA AVE, SAN DIEGO, CA 92122? It is the Axiom apartments.'
    with pytest.raises(ValueError, match='postal city'):
        flow.check_addresses([address('A', '3950', 'MAHAILA', 'AVE', quote, city='La Jolla')], branded)
    # An explicitly supplied La Jolla postal city remains allowed.
    assert flow.check_addresses([address('A', '3950', 'MAHAILA', 'AVE', quote, city='La Jolla')],
                                'How many crashes near 3950 MAHAILA AVE, La Jolla, CA?')
