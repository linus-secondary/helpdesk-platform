"""Shared building blocks used by every service.

- create_app(): FastAPI app with JSON logging, correlation IDs, Prometheus metrics and /health
- get_db(): PostgreSQL connection pool (psycopg 3)
- current_user / require_roles: validates Keycloak JWT access tokens (OAuth 2.0 / OIDC)
- require_internal: protects service-to-service endpoints with a shared token
- internal_client(): httpx client that calls other services with the internal token + correlation ID
"""
import contextvars
import json
import logging
import os
import sys
import time
import uuid
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path

import httpx
import jwt
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

SERVICE = os.environ.get("SERVICE", "unknown")
INTERNAL_TOKEN = os.environ.get("INTERNAL_TOKEN", "dev-internal-token")
AUTH_DISABLED = os.environ.get("AUTH_DISABLED", "false").lower() == "true"  # tests only

# ---------------------------------------------------------------- logging
correlation_id: contextvars.ContextVar[str] = contextvars.ContextVar("correlation_id", default="-")


class JsonFormatter(logging.Formatter):
    """One JSON object per line, so Filebeat/Logstash can parse logs without regex."""

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)),
            "level": record.levelname,
            "service": SERVICE,
            "correlation_id": correlation_id.get(),
            "msg": record.getMessage(),
        }
        for key in ("method", "path", "status", "duration_ms", "user"):
            if hasattr(record, key):
                entry[key] = getattr(record, key)
        if record.exc_info:
            entry["error"] = self.formatException(record.exc_info)
        return json.dumps(entry)


def setup_logging() -> logging.Logger:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(os.environ.get("LOG_LEVEL", "INFO"))
    logging.getLogger("uvicorn.access").disabled = True  # we log requests ourselves
    return logging.getLogger(SERVICE)


log = setup_logging()

# ---------------------------------------------------------------- metrics
REQUESTS = Counter("http_requests_total", "HTTP requests", ["service", "method", "route", "status"])
LATENCY = Histogram("http_request_duration_seconds", "HTTP request latency", ["service", "method", "route"],
                    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5))

# ---------------------------------------------------------------- database
_pool = None


def get_pool():
    global _pool
    if _pool is None:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        _pool = ConnectionPool(
            os.environ["DATABASE_URL"],
            min_size=1,
            max_size=int(os.environ.get("DB_POOL_SIZE", "10")),
            kwargs={"row_factory": dict_row, "autocommit": True},
            open=True,
        )
    return _pool


def run_schema(path: Path) -> None:
    """Every service runs the shared schema at start-up; it is idempotent (IF NOT EXISTS)."""
    for attempt in range(30):
        try:
            with get_pool().connection() as conn, conn.transaction():
                conn.execute(path.read_text(encoding="utf-8"))
            return
        except Exception as exc:  # database still starting
            log.warning(f"waiting for database ({attempt + 1}/30): {exc}")
            time.sleep(2)
    raise RuntimeError("database not reachable")


# ---------------------------------------------------------------- auth (Keycloak JWT)
KEYCLOAK_ISSUER = os.environ.get("KEYCLOAK_ISSUER", "http://localhost:8080/auth/realms/helpdesk")
# JWKS is fetched over the internal network; the issuer claim keeps the public URL.
KEYCLOAK_JWKS_URL = os.environ.get(
    "KEYCLOAK_JWKS_URL", "http://keycloak:8080/auth/realms/helpdesk/protocol/openid-connect/certs")


@lru_cache(maxsize=1)
def _jwks_client() -> jwt.PyJWKClient:
    return jwt.PyJWKClient(KEYCLOAK_JWKS_URL, cache_keys=True, lifespan=300)


def decode_token(token: str) -> dict:
    signing_key = _jwks_client().get_signing_key_from_jwt(token)
    return jwt.decode(
        token,
        signing_key.key,
        algorithms=["RS256"],
        issuer=KEYCLOAK_ISSUER,
        options={"verify_aud": False},  # Keycloak puts "account" in aud by default; we check azp instead
    )


def roles_of(claims: dict) -> set[str]:
    return set(claims.get("realm_access", {}).get("roles", []))


def current_user(authorization: str | None = Header(default=None)) -> dict:
    if AUTH_DISABLED:
        return {"sub": "test-user", "preferred_username": "tester", "email": "tester@example.com",
                "roles": {"user", "agent", "admin"}}
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Missing bearer token")
    try:
        claims = decode_token(authorization.split(" ", 1)[1])
    except jwt.PyJWTError as exc:
        raise HTTPException(401, f"Invalid token: {exc}") from exc
    return {
        "sub": claims["sub"],
        "preferred_username": claims.get("preferred_username", ""),
        "email": claims.get("email", ""),
        "roles": roles_of(claims),
    }


def optional_user(authorization: str | None = Header(default=None)) -> dict | None:
    if not authorization:
        return None
    return current_user(authorization)


def require_roles(*allowed: str):
    def checker(user: dict = Depends(current_user)) -> dict:
        if not user["roles"] & set(allowed):
            raise HTTPException(403, f"Requires one of roles: {', '.join(allowed)}")
        return user

    return checker


def require_internal(x_internal_token: str | None = Header(default=None)) -> None:
    """Service-to-service calls only. The gateway also refuses /internal/* from the internet."""
    if x_internal_token != INTERNAL_TOKEN:
        raise HTTPException(403, "Internal endpoint")


def internal_client(timeout: float = 10) -> httpx.Client:
    return httpx.Client(timeout=timeout, headers={
        "X-Internal-Token": INTERNAL_TOKEN,
        "X-Correlation-ID": correlation_id.get(),
    })


# ---------------------------------------------------------------- app factory
def create_app(name: str, schema: Path | None = None, on_startup: list | None = None) -> FastAPI:
    hooks = list(on_startup or [])

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if schema is not None:
            run_schema(schema)
        for hook in hooks:
            hook()
        yield

    app = FastAPI(title=f"{name} service", lifespan=lifespan)

    @app.middleware("http")
    async def observe(request: Request, call_next):
        cid = request.headers.get("x-correlation-id") or uuid.uuid4().hex[:16]
        correlation_id.set(cid)
        start = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
        except Exception:
            log.exception("unhandled error")
            response = JSONResponse({"detail": "Internal server error", "correlation_id": cid}, status_code=500)
        duration = time.perf_counter() - start
        route = request.scope.get("route")
        route_path = getattr(route, "path", "unmatched")
        if route_path not in ("/metrics", "/health"):
            REQUESTS.labels(name, request.method, route_path, str(status)).inc()
            LATENCY.labels(name, request.method, route_path).observe(duration)
            log.info("request", extra={"method": request.method, "path": request.url.path,
                                       "status": status, "duration_ms": round(duration * 1000, 1)})
        response.headers["X-Correlation-ID"] = cid
        return response

    @app.get("/health")
    def health():
        return {"status": "ok", "service": name}

    @app.get("/metrics")
    def metrics():
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app
