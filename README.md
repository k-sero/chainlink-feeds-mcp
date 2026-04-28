# Chainlink Feeds MCP

FastMCP server for reading Chainlink AggregatorV3 price feeds on-chain.

This fork is rebuilt on the same FastMCP + OAuth pattern used across the Sero MCP services (discover/query meta-tools, `/mcp/` transport, health routes, optional Google OAuth, optional static bearer auth).

## Operations

Exposed via two MCP tools:

- `discover(category?, search?)`
- `query(tool, arguments?)`

Underlying operations:

- `list_supported_chains`
- `list_supported_feeds`
- `get_latest_price`
- `get_price_by_round`

## Environment

Copy `.env.example` to `.env` and set:

- `INFURA_API_KEY` (required for Infura-based chains in `feeds.json`)
- `BASE_URL` (required when using Google OAuth)
- Optional inbound auth:
  - `GOOGLE_CLIENT_ID` + `GOOGLE_CLIENT_SECRET`
  - `API_KEY` (static bearer on `/mcp`)

Per-chain RPC overrides are supported:

- `RPC_URL_ETHEREUM`
- `RPC_URL_BASE`
- etc. (`RPC_URL_<CHAIN_UPPER>`)

## Run

```bash
python main.py          # HTTP
python main.py --stdio  # stdio
```

Default MCP endpoint: `http://localhost:8016/mcp/`

## Docker

```bash
docker build -t chainlink-feeds-mcp .
docker run --rm -p 8016:8016 --env-file .env chainlink-feeds-mcp
```

## Notes

- Feed definitions are loaded from `feeds.json`.
- Round-specific reads use `getRoundData(round_id)` on the aggregator proxy.
- If a chain is not available through your Infura key, provide `RPC_URL_<CHAIN>` override.
