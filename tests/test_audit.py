"""Tool-call audit events go through the shared observability module.

Runs the server in-memory with a fastmcp Client and captures what the audit
middleware emits. No upstream API is called: discover is local and the query
targets an operation that does not exist.
"""
import asyncio
import json
import os
import sys
from pathlib import Path

os.environ.setdefault("ENV", "development")
os.environ.setdefault("REQUIRE_AUTH", "false")
for _k in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "AXIOM_TOKEN"):
    os.environ.pop(_k, None)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import observability  # noqa: E402
import main  # noqa: E402
from fastmcp import Client  # noqa: E402

DISCOVER_TOOL = "discover"
QUERY_TOOL = "query"
ALLOWED_KEY = "chain"  # None when the server has an empty allowlist
SENSITIVE = "do-not-log-this-value-7f3a"


def _run(calls):
    events = []

    async def go():
        async with Client(main.mcp) as client:
            for name, args in calls:
                await client.call_tool(name, args, raise_on_error=False)

    original = observability.audit
    observability.audit = lambda **fields: events.append(fields)
    try:
        asyncio.run(go())
    finally:
        observability.audit = original
    return [e for e in events if e.get("event") == "tool_call"]


def test_one_event_per_call_with_op_and_outcome():
    inner = {"note": SENSITIVE, "nested": {"deep": SENSITIVE}}
    if ALLOWED_KEY:
        inner[ALLOWED_KEY] = "allowed-id-123"
    events = _run([
        (DISCOVER_TOOL, {}),
        (QUERY_TOOL, {"tool": "definitely_not_an_operation", "arguments": inner}),
    ])
    assert len(events) == 2

    disc, bad = events
    assert disc["tool"] == DISCOVER_TOOL
    assert disc["ok"] is True

    assert bad["tool"] == QUERY_TOOL
    assert bad["op"] == "definitely_not_an_operation"
    assert bad["ok"] is False
    assert "arguments.note" in bad["arg_keys"]

    dumped = json.dumps(bad, default=str)
    assert SENSITIVE not in dumped
    values = bad.get("arg_values", {})
    if ALLOWED_KEY:
        assert values.get(ALLOWED_KEY) == "allowed-id-123"
    # Only the operation name and allowlisted identifiers may carry values.
    assert set(values) <= {ALLOWED_KEY} - {None}


def test_single_audit_path():
    source = (Path(__file__).resolve().parents[1] / "main.py").read_text()
    assert 'getLogger("mcp.audit")' not in source
    assert "class AuditLoggingMiddleware" not in source
    assert source.count("AuditLoggingMiddleware(") == 1


def test_health_reports_observability():
    import httpx

    async def get_health():
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get("/health")

    response = asyncio.run(get_health())
    assert response.status_code == 200
    assert {"axiom_enabled", "buffered", "dropped"} <= set(response.json()["observability"])
