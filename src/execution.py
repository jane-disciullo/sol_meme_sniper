"""
Live execution layer for the Solana Meme Coin Sniper.

This module is written for the exact runtime objects produced by the
Google Colab notebook in this project:

    results               -> list[TokenRisk]
    authority_checks      -> Check 1 list[dict]
    holder_checks         -> Check 2 list[dict]
    lp_checks             -> Check 3 list[dict]
    check4_results        -> Check 4 list[dict]
    check5_results        -> Check 5 list[dict]
    check6_results        -> Check 6 list[dict]
    final_safety_results  -> final gate list[dict]
    rpc                   -> existing SolanaRPC instance from Check 1

The notebook's TokenRisk fields used here are:
    mint, symbol, name, pair_address, is_demo, is_safe

The final gate dynamically adds:
    final_safety_pass, final_safety_failed_checks,
    final_safety_reasons, final_safety_status

Safety behavior:
    - Final gate must say safe and all six check flags must be True.
    - Demo/fallback data is blocked.
    - Check 3's exact pair_address is re-checked with DEX Screener.
    - Mint/freeze authorities are re-checked with the existing SolanaRPC.
    - Jupiter must return a Metis route whose AMM keys are exactly the
      Check-3 verified pair. Other Jupiter routers are blocked because the
      notebook's LP check is tied to one specific on-chain pool.
    - A fresh buy order and quote-only sell order are required immediately
      before execution.
    - LIVE_TRADING=False is the default.
    - Live mode requires an explicit typed confirmation.
    - A failed/ambiguous /execute HTTP request is never blindly retried.

Jupiter uses the current Swap API V2 Meta-Aggregator:
    GET  https://api.jup.ag/swap/v2/order
    POST https://api.jup.ag/swap/v2/execute
"""

from __future__ import annotations

import base64
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

import requests
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction


# ============================================================
# CONFIG
# ============================================================

SOL_MINT = "So11111111111111111111111111111111111111112"
LAMPORTS_PER_SOL = 1_000_000_000

JUPITER_BASE = "https://api.jup.ag/swap/v2"

# Safe by default. The module never trades merely by being imported.
LIVE_TRADING = False

BUY_AMOUNT_SOL = 0.01
MAX_TRADES_PER_RUN = 3
MAX_TOTAL_SOL_PER_RUN = 0.05
MIN_WALLET_RESERVE_SOL = 0.02

MAX_GATE_AGE_MINUTES = 10.0
MIN_LIQUIDITY_USD = 5000.0
MAX_PRICE_IMPACT_PCT = 3.0
MAX_ROUNDTRIP_LOSS_PCT = 15.0

HTTP_TIMEOUT_SECONDS = 20
TRADE_LOG_FILE = Path("trade_log.json")
LIVE_CONFIRM_PHRASE = "BUY LIVE"

TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM_ID = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"


class ExecutionUnknown(RuntimeError):
    """The Jupiter execute request may have reached the service, outcome unknown."""


# ============================================================
# HELPERS FOR THE NOTEBOOK'S TokenRisk STRUCTURE
# ============================================================

def token_get(token: Any, field: str, default: Any = None) -> Any:
    if isinstance(token, dict):
        return token.get(field, default)
    return getattr(token, field, default)


def token_mint(token: Any) -> str:
    return str(token_get(token, "mint", "") or "").strip()


def token_symbol(token: Any) -> str:
    return str(token_get(token, "symbol", "UNKNOWN") or "UNKNOWN").upper()


def token_pair_address(token: Any) -> str:
    return str(token_get(token, "pair_address", "") or "").strip()


# ============================================================
# COLAB SECRETS / WALLET
# ============================================================

def get_secret(name: str, required: bool = True) -> Optional[str]:
    value = None

    try:
        from google.colab import userdata  # type: ignore
        value = userdata.get(name)
    except Exception:
        value = None

    if not value:
        value = os.environ.get(name)

    if not value:
        if required:
            raise RuntimeError(f"Required secret '{name}' was not found.")
        return None

    return str(value).strip()


def load_wallet() -> Keypair:
    secret = get_secret("SOLANA_PRIVATE_KEY", required=True)

    try:
        if secret.startswith("["):
            return Keypair.from_bytes(bytes(json.loads(secret)))
        return Keypair.from_base58_string(secret)
    except Exception:
        raise RuntimeError(
            "SOLANA_PRIVATE_KEY could not be parsed. "
            "Use a base58 private key or JSON byte array."
        ) from None


# ============================================================
# EXISTING SolanaRPC CLIENT FROM CHECK 1
# ============================================================

def rpc_call(rpc: Any, method: str, params: list[Any]) -> Any:
    """Use the exact SolanaRPC object created by the notebook."""
    call = getattr(rpc, "_call", None)
    if not callable(call):
        raise RuntimeError(
            "The supplied rpc object is not the notebook's SolanaRPC client."
        )
    return call(method, params)


def wallet_sol_balance(rpc: Any, pubkey: str) -> float:
    result = rpc_call(
        rpc,
        "getBalance",
        [pubkey, {"commitment": "confirmed"}],
    )
    return int(result["value"]) / LAMPORTS_PER_SOL


def fresh_mint_authorities(rpc: Any, mint: str) -> tuple[Optional[str], Optional[str]]:
    """Re-check mint/freeze authorities using the notebook's finalized RPC client."""
    batch = rpc.get_multiple_accounts([mint])
    values = batch.get("value") or []

    if not values or values[0] is None:
        raise RuntimeError("Mint account was not returned by RPC.")

    account = values[0]
    owner = str(account.get("owner") or "")

    if owner not in {TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID}:
        raise RuntimeError("Mint account is not owned by an SPL token program.")

    data = account.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("Mint account data is not jsonParsed.")

    parsed = data.get("parsed")
    if not isinstance(parsed, dict):
        raise RuntimeError("Mint account parsed data is unavailable.")

    if parsed.get("type") != "mint":
        raise RuntimeError("Supplied address is not a parsed token mint.")

    info = parsed.get("info") or {}
    if not isinstance(info, dict):
        raise RuntimeError("Mint account info is invalid.")

    return info.get("mintAuthority"), info.get("freezeAuthority")


# ============================================================
# FINAL GATE INTEGRATION
# ============================================================

def find_final_gate_record(
    final_safety_results: Iterable[dict[str, Any]],
    mint: str,
) -> Optional[dict[str, Any]]:
    target = str(mint or "").strip()
    for item in final_safety_results or []:
        if isinstance(item, dict) and str(item.get("mint") or "").strip() == target:
            return item
    return None


def validate_final_gate(
    token: Any,
    final_safety_results: list[dict[str, Any]],
) -> tuple[bool, list[str]]:
    """Require the notebook's dynamic TokenRisk flags AND final_safety_results flags."""
    reasons: list[str] = []
    mint = token_mint(token)

    if not mint:
        reasons.append("Token mint is missing.")

    if token_get(token, "is_demo", None) is True:
        reasons.append("Token is marked as demo/fallback data.")

    if token_get(token, "is_safe", False) is not True:
        reasons.append("TokenRisk.is_safe is not True.")

    if token_get(token, "final_safety_pass", False) is not True:
        reasons.append("TokenRisk.final_safety_pass is not True.")

    gate = find_final_gate_record(final_safety_results, mint)
    if gate is None:
        reasons.append("No final_safety_results record exists for this mint.")
        return False, reasons

    if gate.get("safe") is not True:
        reasons.append("Final safety record does not say safe=True.")

    for n in range(1, 7):
        if gate.get(f"check{n}_pass") is not True:
            reasons.append(f"Final safety record has check{n}_pass != True.")

    return not reasons, reasons


def validate_gate_freshness(
    gate_completed_at: Optional[float],
    max_age_minutes: float = MAX_GATE_AGE_MINUTES,
) -> None:
    """
    The uploaded notebook does not currently stamp final-gate completion time.
    The Colab notebook should do this immediately after the Final Safety Gate:

        FINAL_GATE_COMPLETED_AT = time.time()
    """
    if gate_completed_at is None:
        raise RuntimeError(
            "FINAL_GATE_COMPLETED_AT is missing. Add `FINAL_GATE_COMPLETED_AT = time.time()` "
            "immediately after the Final Safety Gate and pass that value to run_live_trading()."
        )

    age_minutes = (time.time() - float(gate_completed_at)) / 60.0

    if age_minutes < 0:
        raise RuntimeError("FINAL_GATE_COMPLETED_AT is in the future.")

    if age_minutes > max_age_minutes:
        raise RuntimeError(
            f"Final Safety Gate result is {age_minutes:.1f} minutes old; "
            f"maximum allowed is {max_age_minutes:.1f}."
        )


# ============================================================
# DEX SCREENER EXACT-POOL RECHECK
# ============================================================

def exact_pool_liquidity(mint: str, pair_address: str) -> Optional[float]:
    """
    Re-check the exact pair_address stored by the scanner / Check 3.
    Missing or invalid liquidity stays unknown rather than becoming zero.
    """
    response = requests.get(
        f"https://api.dexscreener.com/tokens/v1/solana/{mint}",
        timeout=HTTP_TIMEOUT_SECONDS,
        headers={
            "Accept": "application/json",
            "User-Agent": "SolanaMemeSniper/1.0",
        },
    )
    response.raise_for_status()

    pairs = response.json()
    if not isinstance(pairs, list):
        raise RuntimeError("Unexpected DEX Screener response.")

    for pair in pairs:
        if str(pair.get("chainId") or "").lower() != "solana":
            continue

        if str(pair.get("pairAddress") or "").strip() != pair_address:
            continue

        base = str((pair.get("baseToken") or {}).get("address") or "").strip()
        if base != mint:
            continue

        liquidity_raw = (pair.get("liquidity") or {}).get("usd")
        if liquidity_raw is None:
            return None

        try:
            return float(liquidity_raw)
        except (TypeError, ValueError):
            return None

    return None


# ============================================================
# JUPITER SWAP API V2
# ============================================================

def jupiter_headers(api_key: str) -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "x-api-key": api_key,
    }


def jupiter_order(
    *,
    input_mint: str,
    output_mint: str,
    amount_raw: int,
    api_key: str,
    taker: Optional[str] = None,
) -> dict[str, Any]:
    params: dict[str, str] = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": str(int(amount_raw)),
    }

    if taker:
        params["taker"] = taker

    response = requests.get(
        f"{JUPITER_BASE}/order",
        params=params,
        headers=jupiter_headers(api_key),
        timeout=HTTP_TIMEOUT_SECONDS,
    )
    response.raise_for_status()

    data = response.json()
    if not isinstance(data, dict):
        raise RuntimeError("Jupiter returned an invalid /order response.")

    return data


def route_amm_keys(order: dict[str, Any]) -> list[str]:
    keys: list[str] = []

    for step in order.get("routePlan") or []:
        info = step.get("swapInfo") or {}
        amm_key = info.get("ammKey")
        if amm_key:
            keys.append(str(amm_key))

    return keys


def price_impact_pct(order: dict[str, Any]) -> float:
    try:
        return abs(float(order.get("priceImpactPct") or 0.0)) * 100.0
    except (TypeError, ValueError):
        raise RuntimeError("Jupiter returned an invalid priceImpactPct.") from None


def out_amount_raw(order: dict[str, Any]) -> int:
    try:
        amount = int(order.get("outAmount") or 0)
    except (TypeError, ValueError):
        amount = 0

    if amount <= 0:
        raise RuntimeError("Jupiter returned zero or invalid outAmount.")

    return amount


def validate_exact_metis_route(
    order: dict[str, Any],
    pair_address: str,
) -> tuple[bool, list[str]]:
    reasons: list[str] = []

    router = str(order.get("router") or "").lower()
    if router != "metis":
        reasons.append(
            f"Jupiter selected router '{router or 'UNKNOWN'}'; this repo requires Metis "
            "so Check 3 can be tied to a specific AMM pool."
        )

    pools = route_amm_keys(order)
    if not pools:
        reasons.append("Jupiter order did not expose an AMM routePlan.")

    if not pair_address:
        reasons.append("Check 3 pair_address is missing.")

    if pools and pair_address and any(pool != pair_address for pool in pools):
        reasons.append(
            "Jupiter route does not use only the exact Check 3 verified pool."
        )

    return not reasons, reasons


def validate_buy_order(
    order: dict[str, Any],
    pair_address: str,
) -> dict[str, Any]:
    reasons: list[str] = []

    if not order.get("transaction"):
        reasons.append("Jupiter returned no executable transaction.")

    impact = price_impact_pct(order)
    if impact > MAX_PRICE_IMPACT_PCT:
        reasons.append(
            f"Price impact {impact:.2f}% exceeds {MAX_PRICE_IMPACT_PCT:.2f}%."
        )

    route_ok, route_reasons = validate_exact_metis_route(order, pair_address)
    if not route_ok:
        reasons.extend(route_reasons)

    output_raw = out_amount_raw(order)

    return {
        "ok": not reasons,
        "reasons": reasons,
        "price_impact_pct": impact,
        "out_raw": output_raw,
        "route_pools": route_amm_keys(order),
        "router": str(order.get("router") or ""),
    }


# ============================================================
# FRESH PRE-TRADE CHECK
# ============================================================

def fresh_pretrade(
    *,
    token: Any,
    rpc: Any,
    wallet_pubkey: str,
    api_key: str,
    buy_amount_lamports: int,
) -> tuple[bool, list[str], Optional[dict[str, Any]], dict[str, Any]]:
    """Re-check a final-gate-approved token immediately before execution."""
    reasons: list[str] = []
    info: dict[str, Any] = {}

    mint = token_mint(token)
    pair_address = token_pair_address(token)

    if not mint:
        reasons.append("Token mint is missing.")
    if not pair_address:
        reasons.append("Token pair_address is missing.")

    if reasons:
        return False, reasons, None, info

    # Check 1 fresh re-check.
    try:
        mint_authority, freeze_authority = fresh_mint_authorities(rpc, mint)
        if mint_authority is not None:
            reasons.append("Mint authority is active now.")
        if freeze_authority is not None:
            reasons.append("Freeze authority is active now.")
    except Exception as exc:
        reasons.append(f"Fresh mint/freeze authority check failed: {exc}")

    # Check 3 exact-pool liquidity re-check.
    try:
        liquidity = exact_pool_liquidity(mint, pair_address)
        info["fresh_liquidity_usd"] = liquidity

        if liquidity is None:
            reasons.append("Exact Check 3 verified-pool liquidity is unavailable.")
        elif liquidity < MIN_LIQUIDITY_USD:
            reasons.append(
                f"Exact verified-pool liquidity ${liquidity:,.0f} is below "
                f"${MIN_LIQUIDITY_USD:,.0f}."
            )
    except Exception as exc:
        reasons.append(f"Fresh exact-pool liquidity check failed: {exc}")

    if reasons:
        return False, reasons, None, info

    # Fresh buy order. Taker is required to receive an assembled transaction.
    try:
        order = jupiter_order(
            input_mint=SOL_MINT,
            output_mint=mint,
            amount_raw=buy_amount_lamports,
            api_key=api_key,
            taker=wallet_pubkey,
        )

        validation = validate_buy_order(order, pair_address)
        info.update({
            "price_impact_pct": validation["price_impact_pct"],
            "quote_out_raw": validation["out_raw"],
            "route_pools": validation["route_pools"],
            "router": validation["router"],
        })

        if not validation["ok"]:
            return False, validation["reasons"], None, info

    except Exception as exc:
        return False, [f"Jupiter buy order failed: {exc}"], None, info

    # Quote-only sell-side round trip; no taker means no transaction is returned.
    try:
        sell = jupiter_order(
            input_mint=mint,
            output_mint=SOL_MINT,
            amount_raw=validation["out_raw"],
            api_key=api_key,
            taker=None,
        )

        sell_out = out_amount_raw(sell)
        roundtrip_loss_pct = (
            1.0 - (sell_out / buy_amount_lamports)
        ) * 100.0

        info["roundtrip_loss_pct"] = roundtrip_loss_pct

        if roundtrip_loss_pct > MAX_ROUNDTRIP_LOSS_PCT:
            return False, [
                f"Buy/sell quote round-trip loses {roundtrip_loss_pct:.1f}% "
                f"(limit {MAX_ROUNDTRIP_LOSS_PCT:.1f}%)."
            ], None, info

    except Exception as exc:
        return False, [f"Sell-side quote check failed: {exc}"], None, info

    return True, [], order, info


# ============================================================
# SIGN + EXECUTE
# ============================================================

def sign_metis_order(order: dict[str, Any], wallet: Keypair) -> bytes:
    """
    Sign a Metis Swap API V2 order.

    Because this module blocks non-Metis routers, the repository expects one
    wallet signature. The fee payer must be the same wallet before signing.
    """
    transaction_b64 = order.get("transaction")
    if not transaction_b64:
        raise RuntimeError("Jupiter order contains no transaction to sign.")

    try:
        raw = VersionedTransaction.from_bytes(
            base64.b64decode(transaction_b64)
        )
    except Exception:
        raise RuntimeError("Jupiter transaction could not be decoded.") from None

    account_keys = list(raw.message.account_keys)
    if not account_keys:
        raise RuntimeError("Jupiter transaction has no account keys.")

    if account_keys[0] != wallet.pubkey():
        raise RuntimeError(
            "Refusing to sign: wallet is not the transaction fee payer."
        )

    required_signers = int(raw.message.header.num_required_signatures)
    if required_signers != 1:
        raise RuntimeError(
            f"Refusing to sign: transaction requires {required_signers} signatures; "
            "this execution module expects one wallet signer for Metis routes."
        )

    signed = VersionedTransaction(
        raw.message,
        [wallet],
    )

    return bytes(signed)


def execute_jupiter_order(
    order: dict[str, Any],
    wallet: Keypair,
    api_key: str,
) -> dict[str, Any]:
    """Submit one freshly signed Metis order to Jupiter /execute."""
    signed_bytes = sign_metis_order(order, wallet)

    request_id = str(order.get("requestId") or "")
    if not request_id:
        raise RuntimeError("Jupiter order is missing requestId.")

    body = {
        "signedTransaction": base64.b64encode(signed_bytes).decode("ascii"),
        "requestId": request_id,
    }

    # Jupiter documents lastValidBlockHeight as an optional execution field.
    if order.get("lastValidBlockHeight") is not None:
        body["lastValidBlockHeight"] = int(
            order["lastValidBlockHeight"]
        )

    try:
        response = requests.post(
            f"{JUPITER_BASE}/execute",
            json=body,
            headers=jupiter_headers(api_key),
            timeout=30,
        )
    except requests.RequestException as exc:
        raise ExecutionUnknown(
            "Jupiter /execute did not return a response; the outcome is "
            f"ambiguous. Do not automatically retry this mint. Details: {exc}"
        ) from None

    response.raise_for_status()

    result = response.json()
    if not isinstance(result, dict):
        raise RuntimeError("Jupiter returned an invalid /execute response.")

    return result


# ============================================================
# TRADE LOG
# ============================================================

def _read_trade_log() -> list[dict[str, Any]]:
    if not TRADE_LOG_FILE.exists():
        return []

    try:
        data = json.loads(
            TRADE_LOG_FILE.read_text(encoding="utf-8")
        )
    except Exception:
        return []

    return data if isinstance(data, list) else []


def _append_trade_log(entry: dict[str, Any]) -> None:
    data = _read_trade_log()
    record = dict(entry)
    record["time_utc"] = datetime.now(timezone.utc).isoformat()
    data.append(record)
    TRADE_LOG_FILE.write_text(
        json.dumps(data, indent=2),
        encoding="utf-8",
    )


def already_bought_mints() -> set[str]:
    return {
        str(item.get("mint")).strip()
        for item in _read_trade_log()
        if isinstance(item, dict)
        and item.get("mode") == "LIVE_BUY"
        and item.get("status") == "SUCCESS"
        and item.get("mint")
    }


# ============================================================
# MAIN ENTRY POINT
# ============================================================

def run_live_trading(
    *,
    results: list[Any],
    final_safety_results: list[dict[str, Any]],
    rpc: Any,
    gate_completed_at: Optional[float],
    live_trading: bool = LIVE_TRADING,
    buy_amount_sol: float = BUY_AMOUNT_SOL,
    max_trades_per_run: int = MAX_TRADES_PER_RUN,
    max_total_sol_per_run: float = MAX_TOTAL_SOL_PER_RUN,
    min_wallet_reserve_sol: float = MIN_WALLET_RESERVE_SOL,
    max_gate_age_minutes: float = MAX_GATE_AGE_MINUTES,
) -> list[dict[str, Any]]:
    """
    Run buys for tokens that passed the notebook's final six-check gate.

    With live_trading=False this runs the same wallet-independent eligibility,
    liquidity, route and quote checks but never signs or submits a transaction.
    """
    validate_gate_freshness(
        gate_completed_at,
        max_age_minutes=max_gate_age_minutes,
    )

    if not isinstance(results, list) or not results:
        raise RuntimeError("The notebook 'results' list is empty or invalid.")

    if not isinstance(final_safety_results, list):
        raise RuntimeError(
            "The notebook 'final_safety_results' list is invalid."
        )

    api_key = get_secret("JUPITER_API_KEY", required=True)

    wallet = load_wallet()
    wallet_pubkey = str(wallet.pubkey())
    balance = wallet_sol_balance(rpc, wallet_pubkey)

    bought = already_bought_mints()
    candidates: list[Any] = []

    for token in results:
        mint = token_mint(token)
        ok, _ = validate_final_gate(token, final_safety_results)
        if ok and mint not in bought:
            candidates.append(token)

    if not candidates:
        print("No tokens currently pass the Final Safety Gate.")
        return []

    if buy_amount_sol <= 0:
        raise ValueError("buy_amount_sol must be greater than zero.")

    by_config = min(
        int(max_trades_per_run),
        int(max_total_sol_per_run // buy_amount_sol),
    )

    after_reserve = balance - min_wallet_reserve_sol
    by_balance = int(max(0.0, after_reserve) // buy_amount_sol)

    trade_limit = min(
        by_config,
        by_balance,
        len(candidates),
    )

    if trade_limit <= 0:
        raise RuntimeError(
            "Wallet balance after the configured reserve cannot fund a trade."
        )

    candidates = candidates[:trade_limit]
    buy_amount_lamports = int(
        round(buy_amount_sol * LAMPORTS_PER_SOL)
    )

    approved: list[tuple[Any, dict[str, Any], dict[str, Any]]] = []

    print()
    print("=" * 100)
    print("SOLANA MEME COIN SNIPER — LIVE EXECUTION")
    print("=" * 100)
    print(f"Wallet: {wallet_pubkey}")
    print(f"Balance: {balance:.6f} SOL")
    print(f"Gate-cleared candidates: {len(candidates)}")
    print(f"Buy size: {buy_amount_sol:.6f} SOL")

    # Initial fresh checks.
    for token in candidates:
        symbol = token_symbol(token)
        mint = token_mint(token)

        print()
        print(f"🔎 {symbol} | {mint}")

        ok, reasons, order, info = fresh_pretrade(
            token=token,
            rpc=rpc,
            wallet_pubkey=wallet_pubkey,
            api_key=api_key,
            buy_amount_lamports=buy_amount_lamports,
        )

        if not ok:
            print("   🔴 BLOCKED")
            for reason in reasons:
                print(f"      • {reason}")
            continue

        print("   ✅ Fresh pre-trade checks passed")
        print(f"      Liquidity: ${info['fresh_liquidity_usd']:,.0f}")
        print(f"      Price impact: {info['price_impact_pct']:.2f}%")
        print(f"      Round-trip loss: {info['roundtrip_loss_pct']:.1f}%")
        print(f"      Router: {info['router']}")
        print(f"      Route pool(s): {', '.join(info['route_pools'])}")

        approved.append((token, order, info))

    if not approved:
        print("\nNo token passed fresh pre-trade verification.")
        return []

    total_planned = buy_amount_sol * len(approved)

    print()
    print("-" * 100)
    print("TRADE PLAN")
    print("-" * 100)

    for token, _, _ in approved:
        print(f"BUY {buy_amount_sol:.6f} SOL -> {token_symbol(token)}")

    print(f"Total planned spend: {total_planned:.6f} SOL")

    if not live_trading:
        print("🧪 DRY RUN ONLY — no transaction was signed or sent.")
        return [
            {
                "symbol": token_symbol(token),
                "mint": token_mint(token),
                "status": "DRY_RUN_APPROVED",
                "sol": buy_amount_sol,
                "quote_out_raw": info["quote_out_raw"],
                "router": info["router"],
                "route_pools": info["route_pools"],
            }
            for token, _, info in approved
        ]

    print("⚠️ LIVE MODE: real SOL will be spent.")
    confirmation = input(
        f"Type '{LIVE_CONFIRM_PHRASE}' to authorize up to {total_planned:.6f} SOL: "
    ).strip()

    if confirmation != LIVE_CONFIRM_PHRASE:
        print("Cancelled. Nothing was signed or sent.")
        return []

    executed: list[dict[str, Any]] = []

    for token, _, _ in approved:
        symbol = token_symbol(token)
        mint = token_mint(token)

        print()
        print(f"🚀 LIVE BUY: {symbol}")

        # Just-in-time recheck and re-quote. The notebook's market conditions
        # can change quickly after the initial approval.
        ok, reasons, order, info = fresh_pretrade(
            token=token,
            rpc=rpc,
            wallet_pubkey=wallet_pubkey,
            api_key=api_key,
            buy_amount_lamports=buy_amount_lamports,
        )

        if not ok or order is None:
            print("   🔴 SKIPPED — conditions changed.")
            for reason in reasons:
                print(f"      • {reason}")
            continue

        try:
            result = execute_jupiter_order(
                order,
                wallet,
                api_key,
            )

            status = str(result.get("status") or "")
            signature = result.get("signature")

            if status == "Success":
                print("   ✅ BUY SUCCESS")
                if signature:
                    print(f"      Signature: {signature}")
                    print(
                        "      Solscan: "
                        f"https://solscan.io/tx/{signature}"
                    )

                _append_trade_log(
                    {
                        "mode": "LIVE_BUY",
                        "mint": mint,
                        "symbol": symbol,
                        "sol": buy_amount_sol,
                        "quote_out_raw": order.get("outAmount"),
                        "request_id": order.get("requestId"),
                        "router": order.get("router"),
                        "route_pools": info.get("route_pools", []),
                        "signature": signature,
                        "status": "SUCCESS",
                        "error": None,
                    }
                )

                executed.append(
                    {
                        "symbol": symbol,
                        "mint": mint,
                        "status": "SUCCESS",
                        "signature": signature,
                    }
                )

            else:
                error = (
                    result.get("error")
                    or result.get("errorMessage")
                    or str(result)
                )
                print("   🔴 JUPITER FAILED")
                print(f"      {error}")

                _append_trade_log(
                    {
                        "mode": "LIVE_BUY",
                        "mint": mint,
                        "symbol": symbol,
                        "sol": buy_amount_sol,
                        "quote_out_raw": order.get("outAmount"),
                        "request_id": order.get("requestId"),
                        "router": order.get("router"),
                        "route_pools": info.get("route_pools", []),
                        "signature": signature,
                        "status": "FAILED",
                        "error": error,
                    }
                )

                executed.append(
                    {
                        "symbol": symbol,
                        "mint": mint,
                        "status": "FAILED",
                        "signature": signature,
                        "error": error,
                    }
                )

        except ExecutionUnknown as exc:
            print("   🟠 EXECUTION UNKNOWN")
            print("      DO NOT AUTO-RETRY THIS MINT.")
            print(f"      {exc}")

            _append_trade_log(
                {
                    "mode": "LIVE_BUY",
                    "mint": mint,
                    "symbol": symbol,
                    "sol": buy_amount_sol,
                    "quote_out_raw": order.get("outAmount"),
                    "request_id": order.get("requestId"),
                    "router": order.get("router"),
                    "signature": None,
                    "status": "UNKNOWN",
                    "error": str(exc),
                }
            )

            executed.append(
                {
                    "symbol": symbol,
                    "mint": mint,
                    "status": "UNKNOWN",
                    "signature": None,
                    "error": str(exc),
                }
            )

        except Exception as exc:
            print("   🔴 EXECUTION ERROR")
            print(f"      {exc}")

            _append_trade_log(
                {
                    "mode": "LIVE_BUY",
                    "mint": mint,
                    "symbol": symbol,
                    "sol": buy_amount_sol,
                    "quote_out_raw": order.get("outAmount"),
                    "request_id": order.get("requestId"),
                    "router": order.get("router"),
                    "signature": None,
                    "status": "ERROR",
                    "error": str(exc),
                }
            )

            executed.append(
                {
                    "symbol": symbol,
                    "mint": mint,
                    "status": "ERROR",
                    "signature": None,
                    "error": str(exc),
                }
            )

    print()
    print("=" * 100)
    print("LIVE BUY RUN COMPLETE")
    print("=" * 100)

    for item in executed:
        print(
            f"{item['symbol']:12s} | "
            f"{item['status']:8s} | "
            f"{item.get('signature') or 'no signature'}"
        )

    return executed


# ============================================================
# MANUAL SELL
# ============================================================

def token_balance_raw(rpc: Any, owner: str, mint: str) -> int:
    result = rpc_call(
        rpc,
        "getTokenAccountsByOwner",
        [
            owner,
            {"mint": mint},
            {
                "encoding": "jsonParsed",
                "commitment": "confirmed",
            },
        ],
    )

    total = 0
    for item in result.get("value", []):
        info = (
            (
                (
                    item.get("account") or {}
                ).get("data") or {}
            ).get("parsed") or {}
        ).get("info") or {}

        amount = (
            (info.get("tokenAmount") or {}).get("amount")
        )

        if amount is not None:
            total += int(amount)

    return total


def sell_token(
    *,
    mint: str,
    rpc: Any,
    live_trading: bool = LIVE_TRADING,
    percent: float = 100.0,
) -> Optional[dict[str, Any]]:
    """Manual token -> SOL sale using Jupiter V2 /order + /execute."""
    mint = str(mint or "").strip()

    if not mint:
        raise ValueError("mint is required.")
    if not (0 < percent <= 100):
        raise ValueError("percent must be between 0 and 100.")

    api_key = get_secret("JUPITER_API_KEY", required=True)
    wallet = load_wallet()
    owner = str(wallet.pubkey())

    balance_raw = token_balance_raw(rpc, owner, mint)
    if balance_raw <= 0:
        print("No balance of this token was found.")
        return None

    amount_raw = int(balance_raw * percent / 100.0)

    order = jupiter_order(
        input_mint=mint,
        output_mint=SOL_MINT,
        amount_raw=amount_raw,
        api_key=api_key,
        taker=owner,
    )

    estimated_sol = out_amount_raw(order) / LAMPORTS_PER_SOL
    impact = price_impact_pct(order)

    print(
        f"SELL {percent:.1f}% -> ~{estimated_sol:.6f} SOL "
        f"(impact {impact:.2f}%, router {order.get('router', 'UNKNOWN')})"
    )

    if not live_trading:
        print("🧪 Dry run: sale was not signed or sent.")
        return {
            "status": "DRY_RUN",
            "mint": mint,
            "amount_raw": amount_raw,
            "estimated_sol": estimated_sol,
            "price_impact_pct": impact,
        }

    confirmation = input(
        "Type 'SELL LIVE' to authorize this sale: "
    ).strip()

    if confirmation != "SELL LIVE":
        print("Cancelled. Nothing was signed or sent.")
        return None

    result = execute_jupiter_order(
        order,
        wallet,
        api_key,
    )

    status = (
        "SUCCESS"
        if result.get("status") == "Success"
        else "FAILED"
    )

    _append_trade_log(
        {
            "mode": "LIVE_SELL",
            "mint": mint,
            "percent": percent,
            "amount_raw": amount_raw,
            "request_id": order.get("requestId"),
            "router": order.get("router"),
            "signature": result.get("signature"),
            "status": status,
            "error": result.get("error"),
        }
    )

    print(f"Status: {status}")
    if result.get("signature"):
        print(
            "Solscan: "
            f"https://solscan.io/tx/{result['signature']}"
        )

    return result


__all__ = [
    "LIVE_TRADING",
    "ExecutionUnknown",
    "run_live_trading",
    "sell_token",
    "get_secret",
    "load_wallet",
    "validate_final_gate",
    "validate_gate_freshness",
    "fresh_mint_authorities",
    "exact_pool_liquidity",
    "jupiter_order",
    "validate_buy_order",
    "execute_jupiter_order",
    "token_balance_raw",
]
