"""PostgreSQL conversation boundary around the existing verified analysis graph.

Only ordinary JSON application state enters the official LangGraph serializer.
Live locks, clients, credentials and database connections never enter checkpoints.
"""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import TypedDict, get_args
from uuid import uuid4

from fastapi import HTTPException
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, START, StateGraph
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from pydantic_core import to_jsonable_python

from .location_flow import PendingRequest
from .location_models import AddressProposal, ChatResponse, ChatTurn, GeocodedCandidate
from .models import Plan, QueryRequest, TimeRange, ServiceRequestAggregation, CollisionFilters
from .session_models import FollowupOutput, PlanChangeField
from .workflow import validated_calls
from .service_categories import road_clarification
from .dimensions import exclusive_dimensions, mentioned_dimensions


CHECKPOINT_OPTIONS = '-c search_path=citylens_checkpoints -c statement_timeout=10000 -c lock_timeout=2000'
PLAN_LEAVES = set(get_args(PlanChangeField))
FILTER_LEAVES = {key for key in PLAN_LEAVES if key.startswith('collision_filters.')} | {
    'service_request_aggregation.service_name', 'service_request_aggregation.service_names',
    'service_request_aggregation.status', 'exclude_linked_duplicates'}


def restore_pending(data):
    """Revalidate saved application fields; the lock is always newly allocated."""
    values = dict(data)
    values['locations'] = [AddressProposal.model_validate(a) for a in data['locations']]
    values['plan'] = Plan.model_validate(data['plan']) if data['plan'] else None
    values['time_range'] = TimeRange.model_validate(data['time_range']) if data['time_range'] else None
    values['reply'] = ChatResponse.model_validate(data['reply']) if data['reply'] else None
    values['candidates'] = {key: [GeocodedCandidate.model_validate(c) for c in choices]
                            for key, choices in data['candidates'].items()}
    values['resolved'] = {key: GeocodedCandidate.model_validate(c) for key, c in data['resolved'].items()}
    return PendingRequest(**values)


def leaf(data, path):
    for key in path.split('.'):
        data = data[key]
    return data


def filter_sources(plan, quote):
    data = plan.model_dump()
    return {path: quote for path in FILTER_LEAVES if leaf(data, path) not in (None, False)}


def apply_followup(context, message, patch, settings):
    """Patch only named validated fields; inherited scope cannot drift by default."""
    patch = FollowupOutput.model_validate(patch.model_dump())
    if patch.mode != 'followup' or patch.clarification:
        raise ValueError(patch.clarification or 'Please specify the intended query conditions.')
    request = QueryRequest.model_validate(context['request'])
    plan = Plan.model_validate(context['plan']).model_dump()
    proposed = Plan.model_validate(patch.plan.model_dump()).model_dump()
    paths = set(patch.changed_plan_fields)
    if not paths <= PLAN_LEAVES:
        raise ValueError('Unsupported parameter change; please clarify the request.')
    if paths and (not patch.change_quote or patch.change_quote not in message):
        raise ValueError('Please state the filter or analysis change explicitly.')
    sources = dict(context.get('filter_sources', {}))
    for path in paths:
        target = plan
        keys = path.split('.')
        for key in keys[:-1]:
            target = target[key]
        target[keys[-1]] = leaf(proposed, path)
        if path in FILTER_LEAVES:
            sources[path] = patch.change_quote
    exclusive = exclusive_dimensions(message)
    if exclusive is not None:
        # The user's explicit dataset replacement is authoritative, even if the
        # model forgets tools/intent in its diff. Its proposed tools still must
        # pass the strict requested-dimension and count/compare validators.
        plan['tools'] = proposed['tools']
        plan['intent'] = proposed['intent']
        paths.add('tools')
        if 'service_requests' not in exclusive:
            plan['service_request_aggregation'] = ServiceRequestAggregation().model_dump()
            plan['exclude_linked_duplicates'] = False
            sources = {key:value for key,value in sources.items() if not key.startswith('service_request_aggregation.') and key!='exclude_linked_duplicates'}
        if 'traffic_collisions' not in exclusive:
            plan['collision_filters'] = CollisionFilters().model_dump()
            sources = {key:value for key,value in sources.items() if not key.startswith('collision_filters.')}
    if any(path.startswith('service_request_aggregation.') for path in paths):
        # Filtering/grouping requires the existing grouped tool. This is a
        # validated dependency of the user's change, even if the model omits
        # the tool switch from its diff. Never run an all-category substitute.
        plan['tools'] = ['aggregate_service_requests' if name=='count_service_requests' else name
                         for name in plan['tools']]
    for value, quote in ((patch.radius_meters, patch.radius_quote), (patch.time_range, patch.time_quote)):
        if value is not None and (not quote or quote not in message):
            raise ValueError('Please state the radius or interval change explicitly.')
    active = filter_sources(Plan.model_validate(plan), None)
    quotes = list(dict.fromkeys(sources.get(path) for path in sorted(active) if sources.get(path)))
    plan['filter_quote'] = '\n'.join(quotes) or None
    # Keep only active filter instructions. Old removed filters cannot force the
    # existing validator to execute a filter the user has just cleared.
    question = message + '\nEstablished operation: ' + plan['intent'] + '; ' + ', '.join(plan['tools'])
    if quotes:
        question += '\nRetained filter instructions: ' + plan['filter_quote']
    data = request.model_dump()
    data['question'] = question
    if patch.radius_meters is not None:
        data['radius_meters'] = patch.radius_meters
    if patch.time_range is not None:
        data['time_range'] = patch.time_range.model_dump()
    # Explicit domain changes also update required-domain validation. Radius or
    # filter changes preserve the original dataset selection/preferences.
    if 'tools' in paths:
        data['preferences'] = exclusive if exclusive is not None else list(dict.fromkeys(mentioned_dimensions(message)+[domain for domain, names in (
            ('service_requests', {'count_service_requests', 'aggregate_service_requests'}),
            ('traffic_collisions', {'get_traffic_collisions', 'aggregate_traffic_collisions'}))
            if names.intersection(plan['tools'])]))
    request, plan = QueryRequest.model_validate(data), Plan.model_validate(plan)
    validated_calls(request, plan, settings)
    return request, plan, {path: sources[path] for path in active if path in sources}


class ConversationState(TypedDict, total=False):
    session_id: str
    kind: str
    payload: dict
    messages: list[dict]
    pending: dict | None
    query_context: dict | None
    reply: dict
    reply_kind: str


class ConversationRunner:
    def __init__(self, agent):
        self.agent = agent
        self.builder = StateGraph(ConversationState)
        self.builder.add_node('conversation_turn', self.run)
        self.builder.add_edge(START, 'conversation_turn')
        self.builder.add_edge('conversation_turn', END)

    async def run(self, state):
        agent, session_id = self.agent, state['session_id']
        context = state.get('query_context')
        pending = restore_pending(state['pending']) if state.get('pending') else None
        query_context = context
        if state['kind'] == 'query':
            # A new full request replaces the active scope. Failed planning must
            # not silently bind its next follow-up to an older investigation.
            query_context = None
            request = QueryRequest.model_validate(state['payload'])
            captured = {}
            reply = await agent.query(request, state_out=captured)
            plan = captured.get('plan')
            if plan and not captured.get('stop'):
                query_context = self.context(request, plan)
            pending = None
            reply_kind, content = 'query', reply.answer
            message = request.question
        else:
            turn = ChatTurn.model_validate(state['payload'])
            message = turn.message or {'confirm': 'Confirm addresses', 'choose_candidates': 'Confirm selected candidates',
                                       'cancel': 'Cancel request'}.get(turn.action, turn.action)
            if context and turn.action == 'cancel' and (not pending or pending.stage in {'complete', 'cancelled'}):
                reply = ChatResponse(status='cancelled', message='Follow-up cancelled. No additional analysis will run.',
                    session_id=session_id, conversation_id=session_id)
                return self.result(state, pending, context, reply, 'chat', message, reply.message)
            if context and turn.action in {'ask', 'clarify'} and (not pending or pending.stage in {'complete', 'cancelled'}):
                reply, query_context, new_locations = await self.followup(session_id, turn, context, pending)
                if new_locations:
                    if pending and pending.locations:
                        # Reuse address correction to preserve unchanged A/B
                        # addresses and require confirmation for revised pins.
                        pending.stage = 'clarify'
                        pending.radius = context['request']['radius_meters']
                        pending.time_range = TimeRange.model_validate(context['request']['time_range'])
                        pending.plan = Plan.model_validate(context['plan'])
                        pending.preferences = context['request']['preferences']
                        pending.analysis_question = context['request']['question']
                        turn = turn.model_copy(update={'action': 'correct', 'revision': pending.revision})
                    else:
                        pending = None
                        turn = turn.model_copy(update={'action': 'ask', 'revision': None,
                            'time_range': TimeRange.model_validate(context['request']['time_range']),
                            'radius_meters': context['request']['radius_meters'],
                            'preferences': context['request']['preferences']})
                else:
                    reply_kind, content = 'chat', reply.result.answer if reply.result else reply.message
                    return self.result(state, pending, query_context, reply, reply_kind, message, content)
            if turn.action == 'ask':
                if pending and pending.stage not in {'complete', 'cancelled'}:
                    raise HTTPException(409, 'Confirm, correct or cancel the pending request first')
                if not turn.message or not turn.message.strip():
                    raise HTTPException(422, 'A question is required')
                pending = PendingRequest(session_id, turn.message, list(turn.preferences),
                    analysis_question=turn.message, source_text=turn.message,
                    radius=turn.radius_meters, time_range=turn.time_range)
            elif pending is None:
                raise HTTPException(409, 'No pending address request to confirm or correct')
            reply = await agent.chat(turn, session=pending)
            # prepare() returns pending replies without assigning session.reply.
            pending.reply = reply
            if reply.result and pending.plan and pending.time_range:
                request = QueryRequest(question=pending.analysis_question,
                    locations=[pending.resolved[a.id].location for a in pending.locations],
                    radius_meters=pending.radius, time_range=pending.time_range,
                    preferences=pending.preferences, conversation_id=session_id, session_id=session_id)
                query_context = self.context(request, pending.plan)
            reply.session_id = session_id
            reply.conversation_id = session_id
            reply_kind, content = 'chat', reply.result.answer if reply.result else reply.message
        return self.result(state, pending, query_context, reply, reply_kind, message, content)

    @staticmethod
    def context(request, plan, sources=None):
        return {'request': request.model_dump(mode='json'), 'plan': plan.model_dump(mode='json'),
                'filter_sources': sources if sources is not None else filter_sources(plan, plan.filter_quote)}

    @staticmethod
    def result(state, pending, context, reply, kind, message, content):
        now = datetime.now(timezone.utc).isoformat()
        messages = state.get('messages', []) + [
            {'role': 'user', 'content': message, 'created_at': now},
            {'role': 'assistant', 'content': content, 'created_at': now,
             'response_kind': kind, 'response': reply.model_dump(mode='json')}]
        return {'messages': messages, 'pending': to_jsonable_python(pending, exclude={'lock', 'updated'}) if pending else None,
                'query_context': context, 'reply': reply.model_dump(mode='json'), 'reply_kind': kind}

    async def followup(self, session_id, turn, context, pending):
        if not turn.message or not turn.message.strip():
            raise HTTPException(422, 'A follow-up question is required')
        request = QueryRequest.model_validate(context['request'])
        # Send validated scope/plan only, never historical numbers or answers.
        clarification = 'Please clarify the parameter change or provide a complete question with locations and dates.'
        try:
            model_context = dict(context)
            catalog_reader = getattr(self.agent.tools, 'service_categories', None)
            service_tools = {'count_service_requests', 'aggregate_service_requests'}
            if catalog_reader and service_tools.intersection(context['plan']['tools']):
                catalog = await asyncio.to_thread(catalog_reader)
                model_context['service_categories'] = catalog
                clarification = road_clarification(turn.message, catalog)
                if clarification:
                    return ChatResponse(status='needs_clarification', message=clarification,
                        session_id=session_id, conversation_id=session_id, radius_meters=request.radius_meters,
                        time_range=request.time_range, diagnostic_codes=['service_category_scope_required']), context, False
                clarification = 'Please select supported service categories or clarify the requested parameter change.'
            output = await asyncio.wait_for(self.agent.model.followup(turn.message, model_context),
                                            self.agent.settings.llm_timeout_seconds)
            output = FollowupOutput.model_validate(output.model_dump())
            if output.mode == 'new_locations':
                return None, context, True
            if output.clarification or output.mode == 'clarify':
                clarification = output.clarification or clarification
            else:
                request, plan, sources = apply_followup(context, turn.message, output, self.agent.settings)
                result = await self.agent.query(request, plan=plan)
                if pending and pending.resolved:
                    result.limitations.append('User locations are provider estimates: Census interpolation or SanGIS property points. Placement labels do not establish verified building or apartment-unit accuracy.')
                resolved = list(pending.resolved.values()) if pending else [GeocodedCandidate(
                    candidate_id='pin_'+loc.id, matched_address=loc.label or loc.id,
                    location=loc, provider='User coordinates', precision='user_supplied') for loc in request.locations]
                reply = ChatResponse(status='error' if result.status == 'error' else 'complete',
                    message='Analysis rerun using the saved locations and updated validated conditions.',
                    session_id=session_id, conversation_id=session_id, original_question=turn.message,
                    resolved_locations=resolved, radius_meters=request.radius_meters,
                    time_range=request.time_range, proposed_plan=plan, result=result)
                return reply, self.context(request, plan, sources), False
        except Exception as exc:
            from .diagnostics import log_failure
            log_failure('followup_rejected', exc, stage='session_followup',
                        secrets=(self.agent.settings.openai_api_key,))
        return ChatResponse(status='needs_clarification', message=clarification,
            session_id=session_id, conversation_id=session_id, radius_meters=request.radius_meters,
            time_range=request.time_range, diagnostic_codes=['followup_clarification_required']), context, False


class SessionStore:
    def __init__(self, url):
        self.pool = AsyncConnectionPool(url, open=False, min_size=1, max_size=8, timeout=5,
            kwargs={'autocommit': True, 'prepare_threshold': 0, 'row_factory': dict_row,
                    'connect_timeout': 3, 'options': CHECKPOINT_OPTIONS})

    async def open(self):
        await self.pool.open(wait=True, timeout=5)
        async with self.pool.connection() as conn:
            await conn.execute('SELECT session_id FROM citylens.sessions LIMIT 0')
            await conn.execute('SELECT thread_id FROM checkpoints LIMIT 0')

    async def close(self):
        await self.pool.close()

    async def create(self, title='New conversation'):
        async with self.pool.connection() as conn:
            cursor = await conn.execute('INSERT INTO citylens.sessions(session_id,title) VALUES (%s,%s) RETURNING *',
                                        (uuid4().hex, title.strip() or 'New conversation'))
            return await cursor.fetchone()

    async def list(self, limit=50, offset=0):
        async with self.pool.connection() as conn:
            cursor = await conn.execute('SELECT * FROM citylens.sessions ORDER BY updated_at DESC, session_id LIMIT %s OFFSET %s',
                                        (limit, offset))
            return await cursor.fetchall()

    @asynccontextmanager
    async def locked(self, session_id):
        async with self.pool.connection() as conn:
            # Session-level lock spans model/tool work without a long transaction.
            # The same connection serves the saver, avoiding nested pool borrowing.
            cursor = await conn.execute('SELECT pg_try_advisory_lock(hashtextextended(%s, 1303)) AS acquired', (session_id,))
            if not (await cursor.fetchone())['acquired']:
                raise HTTPException(409, 'This conversation is busy; retry after the current turn finishes')
            try:
                cursor = await conn.execute('SELECT * FROM citylens.sessions WHERE session_id=%s', (session_id,))
                metadata = await cursor.fetchone()
                if metadata is None:
                    raise HTTPException(404, 'Conversation not found')
                yield conn, metadata
            finally:
                await asyncio.shield(conn.execute('SELECT pg_advisory_unlock(hashtextextended(%s, 1303))', (session_id,)))

    async def retrieve(self, session_id, agent):
        async with self.locked(session_id) as (conn, metadata):
            graph = ConversationRunner(agent).builder.compile(checkpointer=AsyncPostgresSaver(conn))
            state = (await graph.aget_state({'configurable': {'thread_id': session_id}})).values
            return {**metadata, 'messages': state.get('messages', []), 'query_context': state.get('query_context'),
                    'latest_response': state.get('reply'), 'response_kind': state.get('reply_kind')}

    async def turn(self, session_id, kind, payload, agent):
        if kind == 'chat' and payload.conversation_id not in (None, session_id):
            raise HTTPException(422, 'conversation_id and session_id must identify the same conversation')
        async with self.locked(session_id) as (conn, metadata):
            graph = ConversationRunner(agent).builder.compile(checkpointer=AsyncPostgresSaver(conn))
            config = {'configurable': {'thread_id': session_id}, 'recursion_limit': 4}
            # ainvoke awaits the saver tasks on graph exit. Async step durability
            # also avoids inheriting sync writes into the request-local subgraph.
            state = await graph.ainvoke({'session_id': session_id, 'kind': kind,
                                        'payload': payload.model_dump(mode='json')}, config, durability='async')
            messages = state['messages']
            title = ' '.join(messages[0]['content'].split())[:100] or 'New conversation'
            await conn.execute('''UPDATE citylens.sessions SET updated_at=clock_timestamp(),
                title=CASE WHEN title='New conversation' THEN %s ELSE title END WHERE session_id=%s''', (title, session_id))
            return state['reply']

    async def delete(self, session_id):
        async with self.locked(session_id) as (conn, _):
            async with conn.transaction():
                await AsyncPostgresSaver(conn).adelete_thread(session_id)
                await conn.execute('DELETE FROM citylens.sessions WHERE session_id=%s', (session_id,))
