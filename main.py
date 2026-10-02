"""Solstice API backend.

This API wraps the existing Solstice scanner safety pipeline:
    1. Resolve a Solana mint to a current DEX Screener pair.
    2. Build the lightweight token object expected by checks.py.
    3. Run the existing Checks 1-6 through checks.run_all_checks().
    4. Apply the existing fail-closed safety gate.

Live trading is intentionally NOT exposed by this API.
"""

from __future__ import annotations

import math
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from solders.pubkey import Pubkey

# Render starts uvicorn from the repository root. This also makes local
# execution work when this file is run from api/ directly.
REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"
for import_path in (REPO_ROOT, SRC_DIR):
    if import_path.exists() and str(import_path) not in sys.path:
        sys.path.insert(0, str(import_path))

from checks import SolanaRPC, run_all_checks  # noqa: E402
from safety_gate import run_final_safety_gate  # noqa: E402


APP_NAME = "Solstice API"
APP_VERSION = "1.0.0"
DEXSCREENER_TOKEN_URL = "https://api.dexscreener.com/tokens/v1/solana/{mint}"
DEFAULT_HELIUS_RPC_URL = "https://mainnet.helius-rpc.com/?api-key={key}"
HTTP_TIMEOUT_SECONDS = 20

app = FastAPI(
    title=APP_NAME,
    version=APP_VERSION,
    description=(
        "Backend API for the Solstice Solana meme coin safety scanner. "
        "Runs the existing six-check, fail-closed pipeline."
    ),
)

frontend_origin = os.getenv("FRONTEND_ORIGIN", "*").strip() or "*"
allow_origins = [frontend_origin] if frontend_origin != "*" else ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_credentials=frontend_origin != "*",
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


class ScanRequest(BaseModel):
    mint: str = Field(
        ...,
        min_length=32,
        max_length=50,
        description="Solana token mint address",
    )


def jsonable(value: Any) -> Any:
    """Convert check output into strict JSON-safe values."""
    if value is None or isinstance(value, (str, bool, int)):
        return value

    if isinstance(value, float):
        return value if math.isfinite(value) else None

    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}

    if isinstance(value, (list, tuple, set)):
        return [jsonable(v) for v in value]

    # Handles occasional enum-like / numeric values without failing the API.
    try:
        return jsonable(value.item())
    except Exception:
        return str(value)


def validate_mint(mint: str) -> str:
    mint = str(mint or "").strip()
    if not mint:
        raise HTTPException(status_code=400, detail="Mint address is required.")

    try:
        Pubkey.from_string(mint)
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid Solana mint address: {exc}",
        ) from exc

    return mint


def get_helius_key() -> str:
    key = os.getenv("HELIUS_API_KEY", "").strip()
    if not key:
        raise HTTPException(
            status_code=503,
            detail=(
                "HELIUS_API_KEY is not configured on the backend. "
                "Add it as a Render environment variable."
            ),
        )
    return key


def get_rpc_url(helius_key: str) -> str:
    explicit = os.getenv("SOLANA_RPC_URL", "").strip()
    if explicit:
        return explicit
    return DEFAULT_HELIUS_RPC_URL.format(key=helius_key)


def safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def fetch_current_pair(mint: str) -> dict[str, Any]:
    """Resolve the current Solana pair used to seed the existing checks."""
    try:
        response = requests.get(
            DEXSCREENER_TOKEN_URL.format(mint=mint),
            timeout=HTTP_TIMEOUT_SECONDS,
            headers={
                "Accept": "application/json",
                "User-Agent": "Solstice/1.0",
            },
        )
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        raise HTTPException(
            status_code=502,
            detail=f"DEX Screener request failed: {exc}",
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=502,
            detail="DEX Screener returned invalid JSON.",
        ) from exc

    if not isinstance(payload, list):
        raise HTTPException(
            status_code=502,
            detail="DEX Screener returned an unexpected response format.",
        )

    candidates: list[dict[str, Any]] = []

    for pair in payload:
        if not isinstance(pair, dict):
            continue

        if str(pair.get("chainId") or "").lower() != "solana":
            continue

        base_address = str(
            (pair.get("baseToken") or {}).get("address") or ""
        ).strip()
        if base_address != mint:
            continue

        pair_address = str(pair.get("pairAddress") or "").strip()
        if not pair_address:
            continue

        liquidity_raw = (pair.get("liquidity") or {}).get("usd")
        liquidity_usd = safe_float(liquidity_raw)

        candidates.append(
            {
                "pair_address": pair_address,
                "symbol": str(
                    (pair.get("baseToken") or {}).get("symbol") or "UNKNOWN"
                ).strip(),
                "name": str(
                    (pair.get("baseToken") or {}).get("name") or "Unknown token"
                ).strip(),
                "liquidity_usd": liquidity_usd,
                "dex_id": str(pair.get("dexId") or "").strip(),
                "pair_url": str(pair.get("url") or "").strip(),
                "price_usd": safe_float(pair.get("priceUsd")),
                "volume_24h_usd": safe_float(
                    ((pair.get("volume") or {}).get("h24"))
                ),
            }
        )

    if not candidates:
        raise HTTPException(
            status_code=404,
            detail=(
                "No current Solana DEX Screener pair was found for this mint. "
                "The token cannot enter the six-check pipeline without a verified pair."
            ),
        )

    # Prefer the most liquid current pair. Missing liquidity stays unknown.
    candidates.sort(
        key=lambda item: item["liquidity_usd"]
        if item["liquidity_usd"] is not None
        else -1.0,
        reverse=True,
    )
    return candidates[0]


def build_token(mint: str, pair: dict[str, Any]) -> SimpleNamespace:
    """Create the small runtime object expected by the existing checks."""
    return SimpleNamespace(
        mint=mint,
        symbol=pair.get("symbol") or "UNKNOWN",
        name=pair.get("name") or "Unknown token",
        pair_address=pair.get("pair_address") or "",
        liquidity_usd=pair.get("liquidity_usd"),
        is_demo=False,
        is_safe=False,
        risks=[],
        risk_score=100.0,
    )


def find_by_mint(records: list[dict[str, Any]] | None, mint: str) -> dict[str, Any] | None:
    for record in records or []:
        if not isinstance(record, dict):
            continue
        record_mint = str(
            record.get("mint", record.get("mint_address", "")) or ""
        ).strip()
        if record_mint == mint:
            return record
    return None


def build_check_summary(
    mint: str,
    authority_checks: list[dict[str, Any]],
    holder_checks: list[dict[str, Any]],
    lp_checks: list[dict[str, Any]],
    check4_results: list[dict[str, Any]],
    check5_results: list[dict[str, Any]],
    check6_results: list[dict[str, Any]],
) -> dict[str, Any]:
    c1 = find_by_mint(authority_checks, mint) or {}
    c2 = find_by_mint(holder_checks, mint) or {}
    c3 = find_by_mint(lp_checks, mint) or {}
    c4 = find_by_mint(check4_results, mint) or {}
    c5 = find_by_mint(check5_results, mint) or {}
    c6 = find_by_mint(check6_results, mint) or {}

    c1_pass = (
        c1.get("status") == "✅ SUCCESS"
        and c1.get("mint_authority_raw") is None
        and c1.get("freeze_authority_raw") is None
    )
    c2_raw = c2.get("raw") or {}
    c2_adjusted = c2.get("adjusted") or {}
    c2_pass = all(
        c2.get("mint") == mint
        for _ in [0]
    ) and all(
        c2_raw.get(key) is not None
        for key in ("owner_count",)
    ) and all(
        c2_adjusted.get(key) is not None
        for key in ("top_1_pct", "top_5_pct", "top_10_pct")
    )
    c3_pass = bool(c3.get("passes", False) and c3.get("pair_match", False))
    c4_pass = bool(c4.get("passes", False))
    c5_pass = bool(c5.get("passes", False))
    c6_pass = bool(c6.get("passes", False))

    return {
        "check_1": {
            "passed": c1_pass,
            "status": c1.get("status"),
            "risk_score": c1.get("risk_score"),
            "details": {
                "program": c1.get("program"),
                "parser": c1.get("parser"),
                "mint_authority": c1.get("mint_authority_raw"),
                "freeze_authority": c1.get("freeze_authority_raw"),
                "context_slot": c1.get("context_slot"),
            },
        },
        "check_2": {
            "passed": c2_pass,
            "status": c2.get("status"),
            "risk_score": c2.get("risk_score"),
            "details": {
                "raw": c2_raw,
                "adjusted": c2_adjusted,
                "unknown_owners": c2.get("unknown_owners", []),
                "excluded_owners": c2.get("excluded_owners", []),
            },
        },
        "check_3": {
            "passed": c3_pass,
            "status": c3.get("status"),
            "risk_score": c3.get("risk_score"),
            "details": {
                "pair_address": c3.get("pair_address"),
                "pair_match": c3.get("pair_match"),
                "lp_locked_pct": c3.get("lp_locked_pct"),
                "lp_unlocked_pct": c3.get("lp_unlocked_pct"),
                "lock_duration_days": c3.get("lock_duration_days"),
                "permanent_lock": c3.get("permanent_lock"),
                "market_type": c3.get("market_type"),
                "locked_usd": c3.get("locked_usd"),
                "locker_count": c3.get("locker_count"),
                "findings": c3.get("findings", []),
            },
        },
        "check_4": {
            "passed": c4_pass,
            "status": c4.get("status"),
            "risk_score": c4.get("risk_score"),
            "details": {
                "onchain_supply": c4.get("onchain_supply"),
                "holder_total_raw": c4.get("holder_total_raw"),
                "holder_coverage_pct": c4.get("holder_coverage_pct"),
                "supply_difference_pct": c4.get("supply_difference_pct"),
                "adjusted_top_1_pct": c4.get("adjusted_top_1_pct"),
                "adjusted_top_5_pct": c4.get("adjusted_top_5_pct"),
                "adjusted_top_10_pct": c4.get("adjusted_top_10_pct"),
                "supply_reconciles": c4.get("supply_reconciles"),
                "distribution_available": c4.get("distribution_available"),
                "findings": c4.get("findings", []),
            },
        },
        "check_5": {
            "passed": c5_pass,
            "status": c5.get("status"),
            "risk_score": c5.get("risk_score"),
            "details": {
                "program": c5.get("program"),
                "extension_names": c5.get("extension_names", []),
                "blocking_reasons": c5.get("blocking_reasons", []),
                "warnings": c5.get("warnings", []),
            },
        },
        "check_6": {
            "passed": c6_pass,
            "status": c6.get("status"),
            "risk_score": c6.get("risk_score"),
            "details": {
                "creator": c6.get("creator"),
                "creator_pct": c6.get("creator_pct"),
                "linked_pct": c6.get("linked_pct"),
                "history_complete": c6.get("history_complete"),
                "common_funders": c6.get("common_funders", []),
                "outflows": c6.get("outflows", []),
                "blocking_reasons": c6.get("blocking_reasons", []),
                "warnings": c6.get("warnings", []),
            },
        },
    }


@app.get("/")
def root() -> dict[str, Any]:
    return {
        "service": APP_NAME,
        "version": APP_VERSION,
        "status": "ok",
        "live_trading": False,
        "endpoints": ["/health", "/scan", "/docs"],
    }


@app.get("/health")
def health() -> dict[str, Any]:
    helius_configured = bool(os.getenv("HELIUS_API_KEY", "").strip())
    return {
        "status": "ok",
        "service": APP_NAME,
        "version": APP_VERSION,
        "helius_configured": helius_configured,
        "live_trading": False,
    }


@app.post("/scan")
def scan_token(request: ScanRequest) -> dict[str, Any]:
    """Run the existing Solstice six-check pipeline for one mint."""
    mint = validate_mint(request.mint)
    helius_key = get_helius_key()
    rpc_url = get_rpc_url(helius_key)

    pair = fetch_current_pair(mint)
    token = build_token(mint, pair)

    try:
        rpc_client = SolanaRPC(
            rpc_url=rpc_url,
            request_delay=float(os.getenv("RPC_REQUEST_DELAY", "0.5")),
            timeout=HTTP_TIMEOUT_SECONDS,
            max_retries=3,
        )

        started = time.time()
        check_outputs = run_all_checks(
            results=[token],
            rpc_client=rpc_client,
            helius_api_key=helius_key,
        )

        gate = run_final_safety_gate(
            results=[token],
            **check_outputs,
        )
        elapsed_seconds = time.time() - started

    except HTTPException:
        raise
    except Exception as exc:
        # Do not turn a check failure into a successful/partial result.
        raise HTTPException(
            status_code=502,
            detail=f"Solstice safety pipeline failed: {exc}",
        ) from exc

    final_record = find_by_mint(gate.get("results"), mint) or {
        "mint": mint,
        "symbol": str(getattr(token, "symbol", "UNKNOWN") or "UNKNOWN").upper(),
        "safe": False,
        "failed_checks": ["DATA"],
        "reasons": ["Final safety result was not returned."],
        "status": "🔴 BLOCKED — FINAL SAFETY RESULT MISSING",
    }

    checks = build_check_summary(
        mint=mint,
        authority_checks=check_outputs.get("authority_checks", []),
        holder_checks=check_outputs.get("holder_checks", []),
        lp_checks=check_outputs.get("lp_checks", []),
        check4_results=check_outputs.get("check4_results", []),
        check5_results=check_outputs.get("check5_results", []),
        check6_results=check_outputs.get("check6_results", []),
    )

    return jsonable(
        {
            "scan_id": f"{mint[:8]}-{int(time.time() * 1000)}",
            "scanned_at": time.time(),
            "elapsed_seconds": round(elapsed_seconds, 3),
            "token": {
                "mint": mint,
                "symbol": getattr(token, "symbol", "UNKNOWN"),
                "name": getattr(token, "name", "Unknown token"),
                "pair_address": getattr(token, "pair_address", None),
                "liquidity_usd": getattr(token, "liquidity_usd", None),
                "dex_id": pair.get("dex_id"),
                "pair_url": pair.get("pair_url"),
                "price_usd": pair.get("price_usd"),
                "volume_24h_usd": pair.get("volume_24h_usd"),
                "is_demo": False,
            },
            "safety_gate": {
                "safe": final_record.get("safe", False),
                "status": final_record.get("status"),
                "failed_checks": final_record.get("failed_checks", []),
                "reasons": final_record.get("reasons", []),
                "completed_at": gate.get("completed_at"),
            },
            "checks": checks,
        }
    )
