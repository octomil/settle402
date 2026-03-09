# settle402

Batch settlement for [x402](https://www.x402.org/) micro-payments via Multicall3.

## The Problem

x402 micro-payments ($0.001/call) are uneconomical to settle individually — gas costs exceed the payment value, and hosted facilitators charge per-settlement fees that eat 100% of revenue at sub-$0.01 prices.

## The Solution

settle402 batches hundreds of signed EIP-3009 `transferWithAuthorization` payloads and submits them on-chain in a single transaction via [Multicall3](https://www.multicall3.com/), amortizing gas across the entire batch.

**Economics:** 1,000 authorizations ($1 revenue) in one tx costs ~$0.005 in gas = **99.5% margin**.

## Quick Start

```bash
# Clone and run
git clone https://github.com/octomil/settle402.git
cd settle402

# Configure
cp .env.example .env
# Edit .env with your RPC URL and private key

# Run with Docker
docker compose up -d

# Or run directly
pip install -e .
uvicorn settler.main:app --host 0.0.0.0 --port 8002
```

## API

### `POST /settle`

Submit a batch of EIP-3009 authorizations for on-chain settlement.

```bash
curl -X POST http://localhost:8002/settle \
  -H "Content-Type: application/json" \
  -H "X-Settler-Token: your-api-key" \
  -d '{
    "network": "base",
    "chainId": 8453,
    "tokenContract": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    "authorizations": [
      {
        "authorization": {
          "from": "0x...",
          "to": "0x...",
          "value": 1000,
          "validAfter": 0,
          "validBefore": 9999999999,
          "nonce": "0x..."
        },
        "signature": "0x...",
        "payer": "0x...",
        "amount": 1000
      }
    ]
  }'
```

**Response:**

```json
{
  "status": "settled",
  "total_submitted": 1000,
  "total_succeeded": 998,
  "total_failed": 2,
  "total_gas_used": 65000000,
  "total_gas_cost_eth": "0.00500000",
  "sub_batches": [...],
  "results": [
    {"index": 0, "nonce": "0x...", "success": true, "tx_hash": "0x..."},
    {"index": 1, "nonce": "0x...", "success": false, "error": "authorization expired"}
  ]
}
```

### `GET /health`

Liveness check.

### `GET /status`

Settler address, ETH balance, chain ID, and lifetime stats.

## Configuration

| Environment Variable | Default | Description |
|---------------------|---------|-------------|
| `SETTLER_RPC_URL` | — | RPC endpoint (required) |
| `SETTLER_PRIVATE_KEY` | — | Settler EOA private key (required) |
| `SETTLER_CHAIN_ID` | `8453` | Target chain (Base) |
| `SETTLER_API_KEYS` | — | Comma-separated API keys |
| `SETTLER_MAX_CALLS_PER_TX` | `400` | Max authorizations per transaction |
| `SETTLER_GAS_MULTIPLIER` | `1.1` | Gas estimate safety margin |
| `SETTLER_LOG_LEVEL` | `info` | Log level |

## Supported Chains

| Chain | ID | USDC Address |
|-------|-----|-------------|
| Base | 8453 | `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913` |
| Base Sepolia | 84532 | `0x036CbD53842c5426634e7929541eC2318f3dCF7e` |
| Ethereum | 1 | `0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48` |
| Polygon | 137 | `0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359` |
| Arbitrum | 42161 | `0xaf88d065e77c8cC2239327C5EDb3A432268e5831` |
| Optimism | 10 | `0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85` |

## How It Works

1. Receive batch of signed EIP-3009 `transferWithAuthorization` payloads
2. Split into sub-batches of 400 (Base gas limit ~30M, each auth ~65K gas)
3. Encode each sub-batch as a `Multicall3.aggregate3()` call with `allowFailure=true`
4. Sign and submit as EIP-1559 transaction
5. Decode per-authorization results from Multicall3 return data
6. Return per-auth success/failure with revert reasons

No custom smart contracts — uses pre-deployed [Multicall3](https://www.multicall3.com/) (audited, deployed on all EVM chains).

## License

MIT
