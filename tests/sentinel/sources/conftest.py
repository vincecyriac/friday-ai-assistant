"""Fake Google and Jira, so no test needs a credential or a network.

Each fake records every request it receives, which is how the allowlist tests
prove a capability never issued a verb it was not supposed to.
"""

from types import SimpleNamespace

import pytest
from aiohttp import web


class RecordingServer:
    def __init__(self):
        self.calls: list[dict] = []
        self.url = ""

    def record(self, request, body=None):
        self.calls.append({"method": request.method, "path": request.path,
                           "query": dict(request.query), "body": body,
                           "headers": dict(request.headers)})

    @property
    def methods(self) -> set[str]:
        return {c["method"] for c in self.calls}


@pytest.fixture
async def google_server(aiohttp_server):
    """Token endpoint + the Gmail and Calendar routes the capabilities use."""
    state = RecordingServer()
    state.token_responses = [{"access_token": "tok-1", "expires_in": 3600}]
    state.gmail = {"messages": [], "byid": {}, "drafts": []}
    state.calendar = {"events": [], "byid": {}, "created": []}
    state.status = {}                       # path suffix -> status to force

    async def token(request):
        body = await request.post()
        state.record(request, dict(body))
        forced = state.status.get("token")
        if forced:
            return web.json_response({"error": "invalid_grant"}, status=forced)
        payload = state.token_responses.pop(0) if len(state.token_responses) > 1 \
            else state.token_responses[0]
        return web.json_response(payload)

    async def messages_list(request):
        state.record(request)
        if state.status.get("gmail"):
            return web.json_response({"error": {"message": "nope"}}, status=state.status["gmail"])
        return web.json_response({"messages": [{"id": m["id"]} for m in state.gmail["messages"]]})

    async def message_get(request):
        state.record(request)
        message = state.gmail["byid"].get(request.match_info["id"])
        if message is None:
            return web.json_response({"error": {"message": "not found"}}, status=404)
        return web.json_response(message)

    async def drafts_create(request):
        body = await request.json()
        state.record(request, body)
        state.gmail["drafts"].append(body)
        return web.json_response({"id": "draft-1"})

    async def events_list(request):
        state.record(request)
        if state.status.get("calendar"):
            return web.json_response({"error": {"message": "nope"}}, status=state.status["calendar"])
        return web.json_response({"items": state.calendar["events"]})

    async def event_get(request):
        state.record(request)
        event = state.calendar["byid"].get(request.match_info["id"])
        if event is None:
            return web.json_response({"error": {"message": "not found"}}, status=404)
        return web.json_response(event)

    async def event_create(request):
        body = await request.json()
        state.record(request, body)
        state.calendar["created"].append(body)
        return web.json_response({**body, "id": "new-event"})

    async def event_patch(request):
        body = await request.json()
        state.record(request, body)
        return web.json_response({**body, "id": request.match_info["id"]})

    async def event_delete(request):
        state.record(request)
        return web.Response(status=204)

    app = web.Application()
    app.add_routes([
        web.post("/token", token),
        web.get("/gmail/v1/users/me/messages", messages_list),
        web.get("/gmail/v1/users/me/messages/{id}", message_get),
        web.post("/gmail/v1/users/me/drafts", drafts_create),
        web.get("/calendar/v3/calendars/primary/events", events_list),
        web.get("/calendar/v3/calendars/primary/events/{id}", event_get),
        web.post("/calendar/v3/calendars/primary/events", event_create),
        web.patch("/calendar/v3/calendars/primary/events/{id}", event_patch),
        web.delete("/calendar/v3/calendars/primary/events/{id}", event_delete),
    ])
    server = await aiohttp_server(app)
    state.url = str(server.make_url("")).rstrip("/")
    return state


@pytest.fixture
async def jira_server(aiohttp_server):
    state = RecordingServer()
    state.issues = []
    state.status = 0
    state.error_body = {"errorMessages": ["Error in the JQL Query: unexpected token 'foo'"]}

    async def search(request):
        state.record(request)
        if state.status:
            return web.json_response(state.error_body, status=state.status)
        return web.json_response({"issues": state.issues})

    async def catch_all(request):
        state.record(request)
        return web.json_response({}, status=405)

    app = web.Application()
    app.add_routes([
        web.get("/rest/api/3/search/jql", search),
        web.route("*", "/{tail:.*}", catch_all),
    ])
    server = await aiohttp_server(app)
    state.url = str(server.make_url("")).rstrip("/")
    return state


def fixed_clock(start: float = 0.0):
    """A monotonic clock the test advances by hand."""
    box = SimpleNamespace(now=start)
    box.tick = lambda seconds: setattr(box, "now", box.now + seconds)
    box.read = lambda: box.now
    return box
