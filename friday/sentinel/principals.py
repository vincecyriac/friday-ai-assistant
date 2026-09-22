"""Who is asking? A dashboard user (session cookie) or a node (bearer token)."""

from __future__ import annotations

from aiohttp import web

from friday.sentinel.auth import SESSION_COOKIE, Principal, User
from friday.sentinel.services import SERVICES


def unauthorized() -> web.Response:
    return web.json_response({"error": "authentication required"}, status=401)


def _raise_unauthorized() -> web.HTTPUnauthorized:
    return web.HTTPUnauthorized(text='{"error": "authentication required"}',
                                content_type="application/json")


async def resolve_principal(request: web.Request, *, allow_query: bool = False) -> Principal | None:
    services = request.app[SERVICES]
    cookie = request.cookies.get(SESSION_COOKIE)
    if cookie:
        user = await services.sessions.resolve(cookie)
        if user is not None:
            return user
    header = request.headers.get("Authorization", "")
    token = header[7:].strip() if header.startswith("Bearer ") else ""
    if not token and allow_query:
        token = request.query.get("token", "")
    if token:
        return await services.node_tokens.resolve(token)
    return None


async def require_principal(request: web.Request, *, allow_query: bool = False) -> Principal:
    principal = await resolve_principal(request, allow_query=allow_query)
    if principal is None:
        raise _raise_unauthorized()
    return principal


async def require_user(request: web.Request) -> User:
    principal = await resolve_principal(request)
    if not isinstance(principal, User):
        raise _raise_unauthorized()
    return principal
