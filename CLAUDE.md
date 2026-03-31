# settle402

Batch settlement service for x402 micro-payments via Multicall3. Accepts signed EIP-3009 `transferWithAuthorization` payloads, batches them into Multicall3 `aggregate3` calls, and submits on-chain.

## Commands

```bash
# Install dependencies
uv sync --all-extras

# Run tests
pytest tests/ -v

# Run tests with coverage
pytest tests/ --cov=settler --cov-report=term-missing

# Lint
ruff check .

# Format
ruff format .

# Run server locally (requires SETTLER_RPC_URL and SETTLER_PRIVATE_KEY)
uvicorn settler.main:app --port 8002
```

## Architecture

```
settler/
  main.py       # FastAPI app, endpoints: POST /settle, POST /keys, GET /health, GET /status
  settler.py    # Core engine: Multicall3 encoding, batch splitting, tx submission, result decoding
  config.py     # Env var loading, chain/USDC address mappings, SettlerConfig dataclass
  auth.py       # API key verification, sliding-window rate limiting
  schemas.py    # Pydantic request/response models
tests/
  test_api.py      # Endpoint, auth, rate limiting, fee validation tests
  test_settler.py  # Core encoding/decoding, batch splitting tests
```

## Style

- Python 3.10+, line length 100
- Ruff for linting (E, F, I, W rules) and formatting
- pytest + pytest-asyncio (asyncio_mode = "auto")
- Pydantic v2 for schemas, FastAPI for endpoints, Web3.py for chain interaction
