"""
Central observability for the Serotonin MCP fleet — Axiom sink.

Source of truth. Copied verbatim into each server alongside `logging_config.py`
(fleet convention). Only `SERVICE_NAME` differs per server, and that comes from
the environment, not from editing this file.

Design constraints, in priority order:

1.  Never block a tool call. Events land in a bounded in-memory deque and are
    drained by a background task. At capacity the oldest event is dropped.
2.  Never raise. A dead or misconfigured Axiom degrades to stderr-only, silently.
3.  Never write to stdout. These servers also run `--stdio`, where stdout *is*
    the MCP protocol channel; a stray write corrupts the session. The network
    handler is off by default under `--stdio`.
4.  Never log argument values. Gmail, Drive, Snowflake, LinkedIn and
    client-graph-memory carry client PII in their arguments. We record argument
    *keys* and value *types* only, plus a narrow per-server allowlist of
    non-sensitive identifiers.

Usage in a server's main.py, replacing the inline audit block:

    from observability import AuditLoggingMiddleware, set_outcome, shutdown_observability

    mcp.add_middleware(AuditLoggingMiddleware(arg_allowlist={"publication_id"}))

and inside `query()`'s error branches:

    set_outcome(ok=False, error_type="upstream_4xx", upstream_status=429)

Docs: https://axiom.co/docs/restapi/ingest
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import sys
import time
from collections import deque
from contextvars import ContextVar
from typing import Any, Iterable, Optional

import httpx

from fastmcp.server.middleware import Middleware, MiddlewareContext

logger = logging.getLogger("mcp.observability")

# The audit logger stays a real logging channel so events still reach stderr
# (and therefore Railway) whether or not Axiom is configured.
#
# It needs its own stderr handler: the fleet's `logging_config.py` only
# configures the per-service logger, so "mcp.audit" would otherwise propagate to
# a root logger with no handler, where logging's lastResort handler drops
# anything below WARNING. Every audit line is INFO, so without this they are
# silently discarded.
audit_logger = logging.getLogger("mcp.audit")
if not audit_logger.handlers:
    _audit_handler = logging.StreamHandler(sys.stderr)
    _audit_handler.setFormatter(logging.Formatter("%(message)s"))
    audit_logger.addHandler(_audit_handler)
    audit_logger.setLevel(logging.INFO)
    audit_logger.propagate = False


# ──────────────────────────────────────────────────────────────────────────
# Resource attributes — stamped once at import, attached to every event
# ──────────────────────────────────────────────────────────────────────────

_STDIO = "--stdio" in sys.argv


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


SERVICE_NAME = os.getenv("SERVICE_NAME", "unknown-mcp")

RESOURCE: dict[str, Any] = {
    "service": SERVICE_NAME,
    "env": os.getenv("ENV", "development"),
    "version": (os.getenv("RAILWAY_GIT_COMMIT_SHA") or "")[:12] or None,
    "railway_deployment_id": os.getenv("RAILWAY_DEPLOYMENT_ID") or None,
    "railway_replica_id": os.getenv("RAILWAY_REPLICA_ID") or None,
}
RESOURCE = {k: v for k, v in RESOURCE.items() if v is not None}


# ──────────────────────────────────────────────────────────────────────────
# Axiom shipper
# ──────────────────────────────────────────────────────────────────────────

_AXIOM_URL = os.getenv("AXIOM_URL", "https://api.axiom.co")
_AXIOM_TOKEN = os.getenv("AXIOM_TOKEN", "").strip()
_DATASET_AUDIT = os.getenv("AXIOM_DATASET_AUDIT", "mcp-audit")
_DATASET_LOGS = os.getenv("AXIOM_DATASET_LOGS", "mcp-logs")

# Off under --stdio unless explicitly forced, per constraint 3.
_AXIOM_ENABLED = _env_flag("AXIOM_ENABLED", not _STDIO) and bool(_AXIOM_TOKEN)

_QUEUE_MAXLEN = int(os.getenv("AXIOM_QUEUE_MAXLEN", "10000"))
_BATCH_SIZE = int(os.getenv("AXIOM_BATCH_SIZE", "100"))
_FLUSH_SECONDS = float(os.getenv("AXIOM_FLUSH_SECONDS", "2"))


class _AxiomShipper:
    """Bounded drop-oldest buffer drained by a background task."""

    def __init__(self) -> None:
        # deque(maxlen=...) gives drop-oldest for free and never blocks.
        self._buf: deque[tuple[str, dict]] = deque(maxlen=_QUEUE_MAXLEN)
        self._task: Optional[asyncio.Task] = None
        self._client: Optional[httpx.AsyncClient] = None
        self._stopping = False
        self.dropped = 0

    def emit(self, dataset: str, event: dict) -> None:
        """Non-blocking, non-raising. Safe to call from anywhere."""
        if not _AXIOM_ENABLED:
            return
        try:
            if len(self._buf) == self._buf.maxlen:
                self.dropped += 1
            self._buf.append((dataset, event))
            self._ensure_task()
        except Exception:  # pragma: no cover - observability must never break callers
            pass

    def _ensure_task(self) -> None:
        # There is no running loop at import time, so the drain task is started
        # lazily on the first emit that happens inside the event loop.
        if self._task is not None and not self._task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._task = loop.create_task(self._drain_forever())

    async def _drain_forever(self) -> None:
        self._client = httpx.AsyncClient(timeout=10.0)
        try:
            while not self._stopping:
                # Poll rather than wait on an asyncio.Event: the shipper is a
                # module-level singleton created at import, before any loop
                # exists, and an Event would bind to the wrong loop.
                await asyncio.sleep(_FLUSH_SECONDS)
                await self._flush_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover
            logger.warning("axiom drain loop stopped: %s", exc)
        finally:
            await self._flush_once()
            if self._client is not None:
                await self._client.aclose()
                self._client = None

    async def _flush_once(self) -> None:
        if not self._buf or self._client is None:
            return
        # Group by dataset; Axiom ingest is one dataset per request.
        batches: dict[str, list[dict]] = {}
        for _ in range(min(len(self._buf), _BATCH_SIZE * 4)):
            try:
                dataset, event = self._buf.popleft()
            except IndexError:
                break
            batches.setdefault(dataset, []).append(event)

        for dataset, events in batches.items():
            for start in range(0, len(events), _BATCH_SIZE):
                chunk = events[start:start + _BATCH_SIZE]
                await self._post(dataset, chunk)

    async def _post(self, dataset: str, events: list[dict]) -> None:
        assert self._client is not None
        body = "\n".join(json.dumps(e, default=str) for e in events)
        try:
            response = await self._client.post(
                f"{_AXIOM_URL}/v1/ingest/{dataset}",
                content=body.encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {_AXIOM_TOKEN}",
                    "Content-Type": "application/x-ndjson",
                },
            )
            if response.status_code >= 400:
                # Deliberately not re-queued: a rejected batch is usually a
                # schema or auth problem and retrying it forever costs more
                # than the events are worth.
                logger.warning(
                    "axiom ingest %s -> %s: %s",
                    dataset, response.status_code, response.text[:300],
                )
        except Exception as exc:
            logger.warning("axiom ingest %s failed: %s", dataset, exc)

    async def shutdown(self) -> None:
        self._stopping = True
        task = self._task
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                task.cancel()
            except Exception:
                pass


_shipper = _AxiomShipper()


def audit(**fields: Any) -> None:
    """Emit one structured audit event: stderr always, Axiom when configured."""
    event = {**RESOURCE, **{k: v for k, v in fields.items() if v is not None}}
    event.setdefault("_time", time.time())
    try:
        audit_logger.info(json.dumps(event, default=str))
    except Exception:
        pass
    _shipper.emit(_DATASET_AUDIT, event)


def log_event(level: str, message: str, **fields: Any) -> None:
    """Emit an app-log event to the `mcp-logs` dataset.

    Messages are scrubbed on the same terms as `error_message`: a stack trace or
    an upstream failure line is free text we did not author, so it gets the same
    treatment.
    """
    event = {**RESOURCE, "level": level, "message": scrub_log(message),
             **{k: v for k, v in fields.items() if v is not None}}
    event.setdefault("_time", time.time())
    _shipper.emit(_DATASET_LOGS, event)


async def shutdown_observability() -> None:
    """Flush pending events. Call from the app's shutdown/lifespan hook."""
    await _shipper.shutdown()



def attach_lifespan(app: Any) -> None:
    """Start the shipper when the ASGI app boots, flush when it shuts down.

    The startup half matters as much as the shutdown half: anything logged
    before the event loop exists (module import, `run_http_server`'s "Starting
    ..." line) is buffered but undrainable, because the background task cannot
    be created without a running loop. Kicking the shipper on lifespan startup
    drains that backlog.

    FastMCP's `http_app()` returns a `StarletteWithLifespan`, which does not
    expose `add_event_handler`, so the app's existing lifespan is wrapped rather
    than added to. Degrades to a no-op if the app exposes neither.
    """
    try:
        router = getattr(app, "router", None)
        base = getattr(router, "lifespan_context", None)
        if base is None:
            raise AttributeError("no lifespan_context")

        @contextlib.asynccontextmanager
        async def _lifespan(scoped_app):
            _shipper._ensure_task()
            async with base(scoped_app):
                try:
                    yield
                finally:
                    await shutdown_observability()

        router.lifespan_context = _lifespan
        return
    except Exception as exc:
        logger.warning("could not attach observability lifespan hook: %s", exc)

def observability_status() -> dict:
    """Small dict for /health, so a misconfigured sink is visible."""
    return {
        "axiom_enabled": _AXIOM_ENABLED,
        "axiom_dataset_audit": _DATASET_AUDIT if _AXIOM_ENABLED else None,
        "buffered": len(_shipper._buf),
        "dropped": _shipper.dropped,
    }


# ──────────────────────────────────────────────────────────────────────────
# Error-message scrubbing
# ──────────────────────────────────────────────────────────────────────────

# Upstream error bodies are the one uncontrolled channel into the audit log: a
# 400 from beehiiv or Vanta routinely echoes the caller's own data back
# ("subscriber sarah@acme.com already exists"), and some vendors include the
# request's credentials in the error envelope. Everything else in an audit event
# is a field we chose; this is a field they chose. So it is scrubbed at the
# single choke point in set_outcome(), never at the call sites.
_SCRUB_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Emails first — otherwise the opaque-token rule below mangles them into
    # something that still leaks the domain.
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "<email>"),
    # "api_key": "...", token=..., secret: '...'
    (re.compile(
        r"""(?i)(['"]?\b[\w-]*(?:key|token|secret|password|passwd|auth|credential)[\w-]*['"]?\s*[:=]\s*)"""
        r"""(['"]?)[^\s,'"}\]]+\2"""),
     r"\1<redacted>"),
    (re.compile(r"(?i)\bbearer\s+[\w.\-]+"), "Bearer <redacted>"),
    # Strip query strings — vendors echo the full request URL, params included.
    (re.compile(r"(https?://[^\s'\"]+)\?[^\s'\"]*"), r"\1?<redacted>"),
    # Long opaque runs: API keys, JWTs, base64, hex digests, record ids.
    # 20 chars is past the longest ordinary English word, so false positives
    # are rare and erring toward redaction is the right bias here.
    (re.compile(r"\b[A-Za-z0-9_\-]{20,}\b"), "<redacted>"),
]

_LOG_MESSAGE_MAXLEN = int(os.getenv("AXIOM_LOG_MESSAGE_MAXLEN", "4000"))
_ERROR_MESSAGE_MODE = os.getenv("AXIOM_ERROR_MESSAGES", "scrubbed").strip().lower()
_ERROR_MESSAGE_MAXLEN = int(os.getenv("AXIOM_ERROR_MESSAGE_MAXLEN", "300"))


def scrub(text: Optional[str]) -> Optional[str]:
    """Redact credentials and personal data out of free-text error output.

    Set AXIOM_ERROR_MESSAGES=off to drop error bodies entirely and keep only
    `error_type` and `upstream_status`.
    """
    if not text:
        return None
    if _ERROR_MESSAGE_MODE == "off":
        return None
    try:
        out = str(text)
        for pattern, replacement in _SCRUB_PATTERNS:
            out = pattern.sub(replacement, out)
        return out[:_ERROR_MESSAGE_MAXLEN] or None
    except Exception:
        # Never let a scrubbing bug ship the unscrubbed original.
        return "<scrub-failed>"


def scrub_log(text: Optional[str]) -> Optional[str]:
    """Same redaction rules as `scrub()`, but keeps a longer message.

    App logs carry stack traces, which are worth reading in full; the value is in
    the frames, and the credential patterns are what matter to strip.
    """
    if not text:
        return None
    try:
        out = str(text)
        for pattern, replacement in _SCRUB_PATTERNS:
            out = pattern.sub(replacement, out)
        return out[:_LOG_MESSAGE_MAXLEN] or None
    except Exception:
        return "<scrub-failed>"


# ──────────────────────────────────────────────────────────────────────────
# Per-call outcome — set from inside query(), read by the middleware
# ──────────────────────────────────────────────────────────────────────────

_outcome: ContextVar[Optional[dict]] = ContextVar("mcp_call_outcome", default=None)


def set_outcome(
    *,
    ok: bool,
    error_type: Optional[str] = None,
    error_message: Optional[str] = None,
    upstream_status: Optional[int] = None,
) -> None:
    """Record the real outcome of a tool call.

    The meta-tool pattern swallows most failures into `{"status": "error"}`
    return dicts, which the middleware would otherwise score as success — so
    error rate reads near zero even when a connector is fully broken. Calling
    this from `query()`'s error branches is what makes `ok` truthful.
    """
    try:
        outcome = {
            "ok": ok,
            "error_type": error_type,
            "error_message": scrub(error_message),
            "upstream_status": upstream_status,
        }
        holder = _outcome.get()
        if holder is None:
            _outcome.set(outcome)
        else:
            # Mutate the middleware's holder rather than rebinding the ContextVar:
            # FastMCP runs sync tools in a worker thread with a copied context, so a
            # .set() there would never be seen by the middleware.
            holder.clear()
            holder.update(outcome)
    except Exception:
        pass


def _result_payload(result: Any) -> Any:
    """The dict a tool returned, unwrapped from FastMCP's ToolResult if needed."""
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict):
        # FastMCP wraps non-object return values as {"result": ...}.
        if set(structured) == {"result"} and isinstance(structured["result"], dict):
            return structured["result"]
        return structured
    return result


def record_result_outcome(result: Any) -> None:
    """Score a returned dict as success or failure.

    The fleet's request helpers return `{"status": "error", "http_status": 429}`
    rather than raising, so the outcome is only knowable by inspecting what
    `query()` is about to hand back.
    """
    if not isinstance(result, dict):
        set_outcome(ok=True)
        return
    if result.get("status") == "error":
        upstream = result.get("http_status")
        set_outcome(
            ok=False,
            error_type=f"upstream_{upstream}" if isinstance(upstream, int) else "operation_error",
            error_message=str(result.get("message") or result.get("error") or "") or None,
            upstream_status=upstream if isinstance(upstream, int) else None,
        )
        return
    set_outcome(ok=True)


# ──────────────────────────────────────────────────────────────────────────
# Argument redaction
# ──────────────────────────────────────────────────────────────────────────

def describe_arguments(arguments: Any, allowlist: Iterable[str] = ()) -> Optional[dict]:
    """Argument *shape*, never argument values.

    Returns `{"keys": {...key: typename...}, "values": {...allowlisted...}}`.
    Only keys named in `allowlist` have their value recorded, and only when that
    value is a short scalar.
    """
    if not isinstance(arguments, dict) or not arguments:
        return None
    allowed = set(allowlist)
    keys: dict[str, str] = {}
    values: dict[str, Any] = {}

    def collect(source: dict, prefix: str) -> None:
        for key, value in source.items():
            keys[f"{prefix}{key}"] = type(value).__name__
            if key in allowed and isinstance(value, (str, int, float, bool)):
                text = str(value)
                if len(text) <= 128:
                    values[str(key)] = value

    collect(arguments, "")
    # Meta-tools carry the operation's own parameters one level down.
    for nested in ("arguments", "params"):
        inner = arguments.get(nested)
        if isinstance(inner, dict):
            collect(inner, f"{nested}.")
    out: dict[str, Any] = {"arg_keys": keys}
    if values:
        out["arg_values"] = values
    return out


# ──────────────────────────────────────────────────────────────────────────
# Middleware
# ──────────────────────────────────────────────────────────────────────────

class AuditLoggingMiddleware(Middleware):
    """One audit event per tool call: who, what, when, ok, how long.

    Two corrections over the inline version this replaces:

    * `tool` is the outer MCP tool (always "query" or "discover" under the
      meta-tool pattern), so the operation the caller actually ran is lifted out
      of `arguments["tool"]` into `op`. Without this the audit log can say a
      client used beehiiv but never what they did.
    * `ok` comes from `set_outcome()` when `query()` reported one, falling back
      to exception-or-not. Without this, swallowed errors read as successes.
    """

    def __init__(self, arg_allowlist: Iterable[str] = ()) -> None:
        self.arg_allowlist = set(arg_allowlist)

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        started = time.perf_counter()
        message = getattr(context, "message", None)
        tool = getattr(message, "name", None) or "?"
        arguments = getattr(message, "arguments", None)

        op = None
        if isinstance(arguments, dict):
            # discover/query servers name the operation `tool`; the read/write
            # split servers (query_read/query_write) name it `operation`.
            for key in ("tool", "operation"):
                candidate = arguments.get(key)
                if isinstance(candidate, str):
                    op = candidate
                    break

        holder: dict = {}
        token = _outcome.set(holder)
        raised = False
        try:
            result = await call_next(context)
            if not holder:
                # The server didn't report an outcome, so score what it returned:
                # a `{"status": "error"}` dict, including an access denial, is a failure.
                record_result_outcome(_result_payload(result))
            return result
        except Exception as exc:
            raised = True
            set_outcome(ok=False, error_type=type(exc).__name__, error_message=str(exc))
            raise
        finally:
            outcome = dict(holder)
            _outcome.reset(token)

            caller = None
            access_token = None
            try:
                from fastmcp.server.dependencies import get_access_token
                access_token = get_access_token()
            except Exception:
                access_token = None

            if access_token is None:
                # No inbound auth configured at all. Kept distinct from
                # "api_key" so monitor #6 can alarm on unauthenticated traffic
                # rather than quietly counting it as a static-token caller.
                auth_mode = "off"
            else:
                claims = getattr(access_token, "claims", None) or {}
                caller = claims.get("email")
                auth_mode = "google" if caller else "api_key"

            caller_domain = caller.rsplit("@", 1)[-1] if caller and "@" in caller else None

            fields: dict[str, Any] = {
                "event": "tool_call",
                "tool": tool,
                "op": op,
                "caller": caller,
                "caller_domain": caller_domain,
                "auth_mode": auth_mode,
                "ok": outcome.get("ok", not raised),
                "duration_ms": round((time.perf_counter() - started) * 1000),
                "error_type": outcome.get("error_type"),
                "error_message": outcome.get("error_message"),
                "upstream_status": outcome.get("upstream_status"),
            }
            described = describe_arguments(arguments, self.arg_allowlist)
            if described:
                fields.update(described)

            audit(**fields)


# ──────────────────────────────────────────────────────────────────────────
# App logs — stderr stays as-is, Axiom gets a copy
# ──────────────────────────────────────────────────────────────────────────

# Loggers whose records must never be shipped. `mcp.observability` is the
# shipper's own channel: a failing Axiom POST logs a warning, and shipping that
# warning would produce another failing POST, forever. `mcp.audit` already has
# its own path to the audit dataset and must not be duplicated into logs.
_NO_SHIP_LOGGERS = ("mcp.observability", "mcp.audit", "httpx", "httpcore")


class AxiomLogHandler(logging.Handler):
    """Mirror app log records into the `mcp-logs` dataset.

    Additive: the existing stderr handler is left untouched, so Railway's console
    keeps working exactly as before whether or not Axiom is configured.
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if record.name.startswith(_NO_SHIP_LOGGERS):
                return
            fields: dict[str, Any] = {
                "logger": record.name,
                "func": record.funcName,
                "line": record.lineno,
            }
            if record.exc_info:
                fields["exception"] = scrub_log(self.format_exception(record))
            log_event(record.levelname.lower(), record.getMessage(), **fields)
        except Exception:
            # A logging handler that raises breaks the caller's log call.
            pass

    def format_exception(self, record: logging.LogRecord) -> str:
        import traceback
        return "".join(traceback.format_exception(*record.exc_info))


def install_log_handler(app_logger: logging.Logger, level: int = logging.INFO) -> None:
    """Attach the Axiom mirror to a server's logger. Safe to call twice."""
    if any(isinstance(h, AxiomLogHandler) for h in app_logger.handlers):
        return
    handler = AxiomLogHandler()
    handler.setLevel(level)
    app_logger.addHandler(handler)
