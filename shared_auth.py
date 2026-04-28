from __future__ import annotations

from typing import Optional

from fastapi import FastAPI, Request, Response


def _is_truthy(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def auth_required(env_name: str = "development", require_auth_value: str | None = None) -> bool:
    raw = (require_auth_value or "").strip()
    if raw:
        return _is_truthy(raw)
    return (env_name or "development").strip().lower() != "development"


def enforce_auth_enabled(
    enabled: bool,
    *,
    service_name: str,
    env_name: str = "development",
    require_auth_value: str | None = None,
    logger: Optional[object] = None,
) -> None:
    if not auth_required(env_name=env_name, require_auth_value=require_auth_value):
        return
    if enabled:
        return

    message = (
        f"{service_name}: inbound auth is required outside development. "
        "Configure OAuth and/or API_KEY, or set REQUIRE_AUTH=false explicitly."
    )
    if logger is not None:
        try:
            logger.error(message)
        except Exception:
            pass
    raise RuntimeError(message)


def install_mcp_bearer_auth(
    app: FastAPI,
    api_key: str,
    path_prefix: str = "/mcp",
    logger: Optional[object] = None,
) -> bool:
    token = (api_key or "").strip()
    if not token:
        return False

    prefix = path_prefix if path_prefix.startswith("/") else f"/{path_prefix}"

    @app.middleware("http")
    async def _auth_middleware(request: Request, call_next):
        if request.url.path.startswith(prefix):
            auth = request.headers.get("Authorization", "")
            if not auth.startswith("Bearer ") or auth[7:] != token:
                return Response(status_code=401, content="Unauthorized")
        return await call_next(request)

    if logger is not None:
        try:
            logger.info("Inbound MCP Bearer auth enabled for prefix '%s'", prefix)
        except Exception:
            pass

    return True
