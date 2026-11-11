"""API gateway: the single public entry point.

- Serves the web frontend (static files) and /config.js for the frontend's Keycloak settings
- Routes /api/<service>/<path> to the right microservice
- Blocks /internal/* routes from the internet (service-to-service only)
- Redis fixed-window rate limiting per client IP (429 + Retry-After when exceeded)
- Adds security headers to every response
- Propagates the X-Correlation-ID header so one request can be traced across services in the logs
"""
import json
import os
import time
from pathlib import Path

import httpx
import redis
from fastapi import HTTPException, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from common import correlation_id, create_app, log

app = create_app("gateway")

ROUTES = {
    "tickets": os.environ.get("TICKETS_URL", "http://tickets:8000"),
    "sla": os.environ.get("SLA_URL", "http://sla:8000"),
    "notifications": os.environ.get("NOTIFICATIONS_URL", "http://notifications:8000"),
    "chat": os.environ.get("CHATBOT_URL", "http://chatbot:8000"),
}
RATE_LIMIT = int(os.environ.get("RATE_LIMIT_PER_MINUTE", "120"))
FORWARD_HEADERS = ("authorization", "content-type")
KEYCLOAK_PUBLIC_URL = os.environ.get("KEYCLOAK_PUBLIC_URL", "http://localhost:8080/auth")
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self' https://cdn.jsdelivr.net; style-src 'self'; "
        f"connect-src 'self' {KEYCLOAK_PUBLIC_URL.rsplit('/auth', 1)[0]}; "
        f"frame-src {KEYCLOAK_PUBLIC_URL.rsplit('/auth', 1)[0]}; frame-ancestors 'none'"),
}

_redis = redis.Redis.from_url(os.environ.get("REDIS_URL", "redis://redis:6379/0"), socket_timeout=0.5)
client = httpx.AsyncClient(timeout=60, limits=httpx.Limits(max_connections=200, max_keepalive_connections=50))


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.update(SECURITY_HEADERS)
    return response


def is_internal_path(path: str) -> bool:
    return path.split("/")[0] == "internal"


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    return forwarded.split(",")[0].strip() if forwarded else (request.client.host if request.client else "unknown")


def check_rate_limit(key: str) -> int | None:
    """Returns seconds to wait if the limit is exceeded, else None. Fails open if Redis is down."""
    window = int(time.time() // 60)
    redis_key = f"ratelimit:{key}:{window}"
    try:
        pipe = _redis.pipeline()
        pipe.incr(redis_key)
        pipe.expire(redis_key, 61)
        count, _ = pipe.execute()
    except redis.RedisError as exc:
        log.warning(f"rate limiter unavailable, allowing request: {exc}")
        return None
    if count > RATE_LIMIT:
        return 60 - int(time.time() % 60)
    return None


@app.get("/config.js", include_in_schema=False)
def frontend_config():
    cfg = {
        "keycloakUrl": KEYCLOAK_PUBLIC_URL,
        "realm": os.environ.get("KEYCLOAK_REALM", "helpdesk"),
        "clientId": os.environ.get("KEYCLOAK_CLIENT_ID", "helpdesk-web"),
    }
    return PlainTextResponse(f"window.APP_CONFIG = {json.dumps(cfg)};", media_type="application/javascript")


@app.api_route("/api/{service}/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def forward(service: str, path: str, request: Request):
    base = ROUTES.get(service)
    if base is None:
        raise HTTPException(404, "Unknown service")
    if is_internal_path(path):
        raise HTTPException(403, "Internal endpoint")

    wait = check_rate_limit(client_ip(request))
    if wait is not None:
        return JSONResponse({"detail": "Too many requests"}, status_code=429, headers={"Retry-After": str(wait)})

    headers = {h: request.headers[h] for h in FORWARD_HEADERS if h in request.headers}
    headers["X-Correlation-ID"] = correlation_id.get()
    try:
        upstream = await client.request(request.method, f"{base}/{path}", params=request.query_params,
                                        content=await request.body(), headers=headers)
    except httpx.RequestError as exc:
        log.error(f"{service} unavailable: {exc}")
        raise HTTPException(503, f"{service} service unavailable") from exc
    return Response(upstream.content, status_code=upstream.status_code,
                    media_type=upstream.headers.get("content-type"))


static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
