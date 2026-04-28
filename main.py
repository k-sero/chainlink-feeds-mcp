import json
import os
import sys
import time
import warnings
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse

warnings.filterwarnings("ignore", message=".*authlib\.jose module is deprecated.*")

from fastmcp import FastMCP
from fastmcp.server.auth.providers.google import GoogleProvider
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware.logging import StructuredLoggingMiddleware
from fastmcp.server.middleware.timing import TimingMiddleware
from web3 import Web3

from config import settings
from shared_auth import enforce_auth_enabled, install_mcp_bearer_auth


PRICE_FEED_ABI = [
    {
        "inputs": [],
        "name": "decimals",
        "outputs": [{"internalType": "uint8", "name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "latestRoundData",
        "outputs": [
            {"internalType": "uint80", "name": "roundId", "type": "uint80"},
            {"internalType": "int256", "name": "answer", "type": "int256"},
            {"internalType": "uint256", "name": "startedAt", "type": "uint256"},
            {"internalType": "uint256", "name": "updatedAt", "type": "uint256"},
            {"internalType": "uint80", "name": "answeredInRound", "type": "uint80"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "uint80", "name": "_roundId", "type": "uint80"}],
        "name": "getRoundData",
        "outputs": [
            {"internalType": "uint80", "name": "roundId", "type": "uint80"},
            {"internalType": "int256", "name": "answer", "type": "int256"},
            {"internalType": "uint256", "name": "startedAt", "type": "uint256"},
            {"internalType": "uint256", "name": "updatedAt", "type": "uint256"},
            {"internalType": "uint80", "name": "answeredInRound", "type": "uint80"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
]

DATA_PATH = Path(__file__).resolve().parent / "feeds.json"
FEEDS_DATA = json.loads(DATA_PATH.read_text())


def _normalize_chain(chain: str) -> str:
    key = (chain or "").strip().lower()
    if key not in FEEDS_DATA:
        raise ValueError(f"Unsupported chain '{chain}'.")
    return key


def _normalize_pair(pair: str) -> str:
    value = (pair or "").strip()
    if not value:
        raise ValueError("pair is required.")
    return value


def _find_feed(chain: str, pair: str) -> dict[str, Any]:
    chain_data = FEEDS_DATA[chain]
    for feed in chain_data["feeds"]:
        if feed["name"].lower() == pair.lower():
            return feed
    raise ValueError(f"Pair '{pair}' not found on chain '{chain}'.")


def _build_rpc_url(chain: str) -> str:
    override = os.environ.get(f"RPC_URL_{chain.upper()}", "").strip()
    if override:
        return override

    base = FEEDS_DATA[chain].get("baseUrl", "").strip().rstrip("/")
    if not base:
        raise ValueError(f"No RPC base URL configured for chain '{chain}'.")

    if "infura.io" in base:
        if not settings.infura_api_key:
            raise ValueError(
                "INFURA_API_KEY is required for this chain. Set INFURA_API_KEY or RPC_URL_<CHAIN>."
            )
        return f"{base}/{settings.infura_api_key}"

    return base


def _to_iso(ts: int) -> Optional[str]:
    if ts <= 0:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _read_feed(chain: str, pair: str, round_id: Optional[int] = None) -> dict[str, Any]:
    feed = _find_feed(chain, pair)
    rpc_url = _build_rpc_url(chain)
    web3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 20}))
    if not web3.is_connected():
        raise RuntimeError(f"Unable to connect to RPC endpoint for chain '{chain}'.")

    contract = web3.eth.contract(address=Web3.to_checksum_address(feed["proxyAddress"]), abi=PRICE_FEED_ABI)
    decimals = int(contract.functions.decimals().call())

    if round_id is None:
        round_data = contract.functions.latestRoundData().call()
    else:
        round_data = contract.functions.getRoundData(round_id).call()

    out_round_id = int(round_data[0])
    answer = int(round_data[1])
    started_at = int(round_data[2])
    updated_at = int(round_data[3])
    answered_in_round = int(round_data[4])

    price = answer / (10**decimals)
    return {
        "chain": chain,
        "pair": pair,
        "price": price,
        "answer": answer,
        "decimals": decimals,
        "round_id": out_round_id,
        "started_at": _to_iso(started_at),
        "updated_at": _to_iso(updated_at),
        "answered_in_round": answered_in_round,
        "proxy_address": feed["proxyAddress"],
        "feed_category": feed.get("feedCategory"),
        "rpc_url_source": "override" if os.environ.get(f"RPC_URL_{chain.upper()}", "").strip() else "feeds.json",
    }


async def list_supported_chains() -> dict[str, Any]:
    return {"chains": sorted(FEEDS_DATA.keys())}


async def list_supported_feeds(chain: Optional[str] = None) -> dict[str, Any]:
    if chain:
        key = _normalize_chain(chain)
        return {
            "chain": key,
            "count": len(FEEDS_DATA[key]["feeds"]),
            "feeds": FEEDS_DATA[key]["feeds"],
        }

    items = []
    for key in sorted(FEEDS_DATA.keys()):
        items.append(
            {
                "chain": key,
                "count": len(FEEDS_DATA[key]["feeds"]),
                "feed_names": [f["name"] for f in FEEDS_DATA[key]["feeds"]],
            }
        )
    return {"chains": items}


async def get_latest_price(pair: str, chain: str) -> dict[str, Any]:
    key = _normalize_chain(chain)
    pr = _normalize_pair(pair)
    return _read_feed(key, pr)


async def get_price_by_round(round_id: int, pair: str, chain: str) -> dict[str, Any]:
    if round_id < 0:
        raise ValueError("round_id must be >= 0")
    key = _normalize_chain(chain)
    pr = _normalize_pair(pair)
    return _read_feed(key, pr, round_id=round_id)


TOOL_CATALOG = {
    "list_supported_chains": {
        "category": "metadata",
        "description": "List all supported chains from feeds.json.",
        "params": {},
        "fn": list_supported_chains,
    },
    "list_supported_feeds": {
        "category": "metadata",
        "description": "List feed definitions. Optional chain filter.",
        "params": {"chain": "optional chain key, e.g. ethereum"},
        "fn": list_supported_feeds,
    },
    "get_latest_price": {
        "category": "pricing",
        "description": "Read latest Chainlink price answer for a feed pair.",
        "params": {"chain": "required chain key", "pair": "required feed name, e.g. BTC/USD"},
        "fn": get_latest_price,
    },
    "get_price_by_round": {
        "category": "pricing",
        "description": "Read Chainlink price answer for a specific round id.",
        "params": {
            "chain": "required chain key",
            "pair": "required feed name",
            "round_id": "required integer round id",
        },
        "fn": get_price_by_round,
    },
}


_mcp_auth = None
_base = settings.base_url.rstrip("/")
if _base.endswith("/mcp"):
    _base = _base[:-4]

if settings.google_client_id and settings.google_client_secret:
    _mcp_auth = GoogleProvider(
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        base_url=_base,
        required_scopes=[
            "openid",
            "https://www.googleapis.com/auth/userinfo.email",
        ],
    )

enforce_auth_enabled(
    bool(_mcp_auth) or bool((settings.api_key or "").strip()),
    service_name="chainlink-feeds-mcp",
    env_name=settings.env,
    require_auth_value=os.getenv("REQUIRE_AUTH"),
)

mcp = FastMCP(
    name="chainlink-feeds",
    instructions=(
        "Chainlink on-chain feed reader. Use discover() to browse metadata and pricing operations, "
        "then query(tool, arguments) to run one operation at a time."
    ),
    auth=_mcp_auth,
)

mcp.add_middleware(
    StructuredLoggingMiddleware(
        include_payload_length=True,
        estimate_payload_tokens=True,
    )
)
mcp.add_middleware(TimingMiddleware())


def _require_allowed_email() -> Optional[dict[str, str]]:
    if _mcp_auth is None:
        return None
    allowed_domains = {d.strip().lower() for d in settings.allowed_email_domains.split(",") if d.strip()}
    allowed_emails = {e.strip().lower() for e in settings.allowed_emails.split(",") if e.strip()}
    if not allowed_domains and not allowed_emails:
        return None
    token = get_access_token()
    claims = getattr(token, "claims", {}) or {}
    email = str(claims.get("email", "")).strip().lower()
    if not email:
        return {"status": "error", "message": "No email in token."}
    if not claims.get("email_verified"):
        return {"status": "error", "message": f"Email '{email}' not verified."}
    if allowed_emails and email not in allowed_emails:
        return {"status": "error", "message": f"'{email}' is not allowed."}
    domain = email.split("@")[-1]
    if allowed_domains and domain not in allowed_domains:
        return {"status": "error", "message": f"Domain '{domain}' is not allowed."}
    return None


@mcp.tool()
async def discover(category: Optional[str] = None, search: Optional[str] = None) -> dict[str, Any]:
    auth_error = _require_allowed_email()
    if auth_error:
        return auth_error

    needle = (search or "").strip().lower()
    ops = []
    for name, meta in TOOL_CATALOG.items():
        if category and meta["category"] != category:
            continue
        if needle and needle not in name.lower() and needle not in meta["description"].lower():
            continue
        ops.append(
            {
                "name": name,
                "category": meta["category"],
                "description": meta["description"],
                "params": meta["params"],
            }
        )
    return {
        "status": "ok",
        "count": len(ops),
        "operations": ops,
        "usage": "Call query(tool='<name>', arguments={...}).",
    }


@mcp.tool()
async def query(tool: str, arguments: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    auth_error = _require_allowed_email()
    if auth_error:
        return auth_error

    if tool not in TOOL_CATALOG:
        return {
            "status": "error",
            "message": f"Unknown operation '{tool}'.",
            "available": list(TOOL_CATALOG.keys()),
        }

    fn = TOOL_CATALOG[tool]["fn"]
    start = time.perf_counter()
    try:
        result = await fn(**(arguments or {}))
        duration_ms = round((time.perf_counter() - start) * 1000)
        return {"status": "ok", "duration_ms": duration_ms, **result}
    except TypeError as exc:
        return {
            "status": "error",
            "message": f"Invalid arguments: {exc}",
            "expected_params": TOOL_CATALOG[tool]["params"],
        }
    except Exception as exc:
        return {
            "status": "error",
            "message": str(exc),
        }


def _build_app() -> FastAPI:
    raw_mcp_app = mcp.http_app(path="/")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with raw_mcp_app.lifespan(app):
            yield

    app = FastAPI(
        title="Chainlink Feeds MCP",
        description="Chainlink on-chain feed reader via FastMCP meta-tool pattern.",
        lifespan=lifespan,
    )
    app.state.fastmcp_server = mcp
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

    install_mcp_bearer_auth(app, settings.api_key, path_prefix="/mcp")

    if _mcp_auth is not None:
        for route in _mcp_auth.get_routes():
            app.routes.insert(0, route)

        @app.get("/.well-known/oauth-protected-resource/mcp", include_in_schema=False)
        @app.get("/.well-known/oauth-protected-resource/mcp/", include_in_schema=False)
        async def oauth_protected_resource_metadata_mcp_path():
            base = settings.base_url.rstrip("/").removesuffix("/mcp")
            issuer = base if base.endswith("/") else base + "/"
            return {
                "resource": f"{base}/mcp",
                "authorization_servers": [issuer],
                "scopes_supported": [
                    "openid",
                    "https://www.googleapis.com/auth/userinfo.email",
                ],
                "bearer_methods_supported": ["header"],
            }

    @app.get("/")
    async def root() -> dict[str, str]:
        return {
            "status": "ok",
            "service": "chainlink-feeds-mcp",
            "transport": "streamable-http",
            "mcp": "/mcp/",
            "health": "/health",
        }

    @app.get("/health")
    async def health() -> dict[str, Any]:
        base = settings.base_url.rstrip("/").removesuffix("/mcp")
        return {
            "status": "ok",
            "chains": len(FEEDS_DATA.keys()),
            "feeds": sum(len(v.get("feeds", [])) for v in FEEDS_DATA.values()),
            "infura_api_key_set": bool(settings.infura_api_key),
            "mcp_auth_enabled": _mcp_auth is not None,
            "api_key_auth_enabled": bool((settings.api_key or "").strip()),
            "oauth_issuer": f"{base}/",
            "oauth_authorize_url": f"{base}/authorize",
            "oauth_token_url": f"{base}/token",
            "oauth_expected_redirect_uri": f"{base}/auth/callback",
            "google_client_id_configured": bool(settings.google_client_id),
            "google_client_secret_configured": bool(settings.google_client_secret),
        }

    @app.get("/mcp", include_in_schema=False)
    @app.post("/mcp", include_in_schema=False)
    async def mcp_without_trailing_slash_redirect():
        return RedirectResponse(url="/mcp/", status_code=307)

    app.mount("/mcp", raw_mcp_app)
    return app


def run_stdio_server() -> None:
    mcp.run(transport="stdio")


def run_http_server() -> None:
    app = _build_app()
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_level="warning",
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


def main() -> None:
    if "--stdio" in sys.argv:
        run_stdio_server()
    else:
        if settings.fastmcp_stateless_http:
            os.environ["FASTMCP_STATELESS_HTTP"] = "true"
        run_http_server()


if __name__ == "__main__":
    main()
