# Contributing to settle402

## Quick Start

```bash
git clone https://github.com/octomil/settle402.git
cd settle402
uv sync --all-extras        # or: pip install -e ".[dev]"
pytest tests/ -v             # run tests
ruff check . && ruff format . # lint
```

## Architecture

settle402 is a batch settlement engine for x402 micro-payments. It takes signed EIP-3009 `transferWithAuthorization` payloads and submits them on-chain via [Multicall3](https://www.multicall3.com/) in a single transaction.

```
POST /settle (batch of EIP-3009 auths)
  → settler.py: encode Multicall3 calldata
  → settler.py: split into sub-batches of 400 (gas limit)
  → settler.py: sign + submit EIP-1559 tx per sub-batch
  → settler.py: decode per-auth results from Multicall3 return data
  → response: per-auth success/failure with tx hashes
```

**Key files:**
- `settler/settler.py` — core engine: encoding, batching, submission, result decoding
- `settler/config.py` — chain configs, USDC addresses, environment-driven settings
- `settler/schemas.py` — Pydantic request/response models
- `settler/main.py` — FastAPI endpoints
- `settler/auth.py` — API key verification + rate limiting

## Integration

settle402 is designed as infrastructure — a settlement primitive that other services call. If you're building a facilitator, payment platform, or agent framework that needs efficient batch settlement:

1. Collect EIP-3009 signed authorizations from your users/agents
2. POST them to `/settle` with your API key
3. Get back per-auth success/failure with on-chain tx hashes

The API is stateless (aside from nonce management). No accounts, no subscriptions, no state to manage on your side.

## Running Locally

```bash
export SETTLER_RPC_URL="https://mainnet.base.org"  # or your RPC
export SETTLER_PRIVATE_KEY="0x..."                   # funded EOA
uvicorn settler.main:app --port 8002
```

## Tests

```bash
pytest tests/ -v
pytest tests/ --cov=settler --cov-report=term-missing  # with coverage
```

All tests are unit tests — no live blockchain calls. Safe to run without an RPC endpoint.

## Pull Requests

- Fork the repo, create a branch, open a PR
- Tests must pass
- Run `ruff check . --fix && ruff format .` before committing
