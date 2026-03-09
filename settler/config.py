"""Environment-driven configuration for settle402."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

# Pre-deployed Multicall3 address (same on all EVM chains)
MULTICALL3_ADDRESS = "0xcA11bde05977b3631167028862bE2a173976CA11"

# Well-known USDC contract addresses
USDC_CONTRACTS: dict[int, str] = {
    8453: "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",  # Base
    84532: "0x036CbD53842c5426634e7929541eC2318f3dCF7e",  # Base Sepolia
    1: "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",  # Ethereum
    137: "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359",  # Polygon
    42161: "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",  # Arbitrum
    10: "0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85",  # Optimism
}

CHAIN_NAMES: dict[int, str] = {
    8453: "base",
    84532: "base-sepolia",
    1: "ethereum",
    137: "polygon",
    42161: "arbitrum",
    10: "optimism",
}


@dataclass
class SettlerConfig:
    """Configuration loaded from environment variables."""

    rpc_url: str = ""
    chain_id: int = 8453
    private_key: str = ""
    api_keys: list[str] = field(default_factory=list)
    max_calls_per_tx: int = 400
    gas_price_multiplier: float = 1.1
    rate_limit: int = 10  # max batches per minute per API key
    fee_enabled: bool = False
    fee_amount: int = 100_000  # $0.10 USDC (6 decimals)
    port: int = 8002
    log_level: str = "info"

    @classmethod
    def from_env(cls) -> SettlerConfig:
        raw_keys = os.environ.get("SETTLER_API_KEYS", "")
        api_keys = [k.strip() for k in raw_keys.split(",") if k.strip()]
        return cls(
            rpc_url=os.environ.get("SETTLER_RPC_URL", ""),
            chain_id=int(os.environ.get("SETTLER_CHAIN_ID", "8453")),
            private_key=os.environ.get("SETTLER_PRIVATE_KEY", ""),
            api_keys=api_keys,
            max_calls_per_tx=int(os.environ.get("SETTLER_MAX_CALLS_PER_TX", "400")),
            gas_price_multiplier=float(os.environ.get("SETTLER_GAS_MULTIPLIER", "1.1")),
            rate_limit=int(os.environ.get("SETTLER_RATE_LIMIT", "10")),
            fee_enabled=os.environ.get("SETTLER_FEE_ENABLED", "").lower() in ("true", "1", "yes"),
            fee_amount=int(os.environ.get("SETTLER_FEE_AMOUNT", "100000")),
            port=int(os.environ.get("SETTLER_PORT", "8002")),
            log_level=os.environ.get("SETTLER_LOG_LEVEL", "info"),
        )

    @property
    def network_name(self) -> str:
        return CHAIN_NAMES.get(self.chain_id, f"chain-{self.chain_id}")
