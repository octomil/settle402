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


# EIP-712 (name, version) of the USDC token per chain, for off-chain signer recovery
USDC_EIP712_DOMAINS: dict[int, tuple[str, str]] = {
    8453: ("USD Coin", "2"),
    84532: ("USDC", "2"),
    1: ("USD Coin", "2"),
    137: ("USD Coin", "2"),
    42161: ("USD Coin", "2"),
    10: ("USD Coin", "2"),
}


def _read_private_key() -> str:
    key = os.environ.get("SETTLER_PRIVATE_KEY", "")
    path = os.environ.get("SETTLER_PRIVATE_KEY_FILE", "")
    if not key and path:
        with open(path) as f:
            key = f.read().strip()
    return key


def _csv(name: str) -> list[str]:
    return [v.strip() for v in os.environ.get(name, "").split(",") if v.strip()]


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
    admin_token: str = ""
    max_keys: int = 1000
    keys_file: str = "/data/keys.json"
    token_allowlist: list[str] = field(default_factory=list)  # extra tokens beyond USDC
    payee_allowlist: list[str] = field(default_factory=list)  # empty = any recipient
    min_value: int = 1  # base units
    max_fee_per_gas_gwei: float = 50.0

    @classmethod
    def from_env(cls) -> SettlerConfig:
        raw_keys = os.environ.get("SETTLER_API_KEYS", "")
        api_keys = [k.strip() for k in raw_keys.split(",") if k.strip()]
        return cls(
            rpc_url=os.environ.get("SETTLER_RPC_URL", ""),
            chain_id=int(os.environ.get("SETTLER_CHAIN_ID", "8453")),
            private_key=_read_private_key(),
            api_keys=api_keys,
            max_calls_per_tx=int(os.environ.get("SETTLER_MAX_CALLS_PER_TX", "400")),
            gas_price_multiplier=float(os.environ.get("SETTLER_GAS_MULTIPLIER", "1.1")),
            rate_limit=int(os.environ.get("SETTLER_RATE_LIMIT", "10")),
            fee_enabled=os.environ.get("SETTLER_FEE_ENABLED", "").lower() in ("true", "1", "yes"),
            fee_amount=int(os.environ.get("SETTLER_FEE_AMOUNT", "100000")),
            port=int(os.environ.get("SETTLER_PORT", "8002")),
            log_level=os.environ.get("SETTLER_LOG_LEVEL", "info"),
            admin_token=os.environ.get("ADMIN_TOKEN", ""),
            max_keys=int(os.environ.get("SETTLER_MAX_KEYS", "1000")),
            keys_file=os.environ.get("SETTLER_KEYS_FILE", "/data/keys.json"),
            token_allowlist=_csv("SETTLER_TOKEN_ALLOWLIST"),
            payee_allowlist=_csv("SETTLER_PAYEE_ALLOWLIST"),
            min_value=int(os.environ.get("SETTLER_MIN_VALUE", "1")),
            max_fee_per_gas_gwei=float(os.environ.get("SETTLER_MAX_FEE_GWEI", "50")),
        )

    @property
    def network_name(self) -> str:
        return CHAIN_NAMES.get(self.chain_id, f"chain-{self.chain_id}")
