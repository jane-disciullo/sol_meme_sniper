"""Six on-chain / market safety checks used by the sniper.

The functions are extracted from the original notebook and exposed as
callable Python functions so the notebook becomes a thin orchestration layer.
"""

from __future__ import annotations

import base64
import math
import os
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Optional

import requests
from solders.pubkey import Pubkey

TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"

TOKEN_2022_PROGRAM_ID = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"

DEFAULT_RPC_URL = "https://api.mainnet-beta.solana.com"

DEXSCREENER_TOKEN_URL = (
    "https://api.dexscreener.com/tokens/v1/solana/{mint}"
)

SYSTEM_PROGRAM_ID = (
    "11111111111111111111111111111111"
)

KNOWN_BURN_ADDRESSES = {
    "1nc1nerator11111111111111111111111111111111"
}

HTTP_TIMEOUT = 20

PAGE_SIZE = 1000

MAX_PAGES = 1000

REQUEST_DELAY = 0.20

RUGCHECK_BASE = "https://api.rugcheck.xyz"

RUGCHECK_TIMEOUT = 20

RUGCHECK_RETRIES = 2

RUGCHECK_DELAY_SECONDS = 1.0

CHECK4_SUPPLY_TOLERANCE_PCT = 0.10

CHECK4_CONCENTRATION_TOP1_BLOCK_PCT = 50.0

CHECK4_CONCENTRATION_TOP5_BLOCK_PCT = 80.0

CHECK4_CONCENTRATION_TOP10_BLOCK_PCT = 70.0

CHECK4_RPC_TIMEOUT_SECONDS = 20

CHECK5_SPL_TOKEN_PROGRAM = (
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
)

CHECK5_TOKEN_2022_PROGRAM = (
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
)

CHECK5_MAX_TRANSFER_FEE_BPS = 100

CHECK5_BLOCK_TRANSFER_HOOK = True

CHECK5_BLOCK_PERMANENT_DELEGATE = True

CHECK5_BLOCK_FROZEN_DEFAULT_STATE = True

CHECK5_BLOCK_PAUSABLE = True

CHECK5_BLOCK_PERMISSIONED_BURN = True

CHECK5_BLOCK_NON_TRANSFERABLE = True

CHECK5_BLOCK_ACTIVE_FEE_AUTHORITY = True

CHECK5_BLOCK_UNKNOWN_EXTENSIONS = True

CHECK5_RPC_TIMEOUT = 20

CHECK5_RPC_RETRIES = 3

CHECK5_RPC_RETRY_DELAY = 1.0

CHECK6_CREATOR_BLOCK_PCT = 10.0

CHECK6_CREATOR_WARN_PCT = 5.0

CHECK6_CREATOR_LINKED_BLOCK_PCT = 20.0

CHECK6_COMMON_FUNDER_BLOCK_PCT = 15.0

CHECK6_LARGE_OUTFLOW_BLOCK_PCT = 15.0

CHECK6_OUTFLOW_WARN_PCT = 5.0

CHECK6_MAX_EARLY_WALLETS = 4

CHECK6_MAX_OUTFLOW_SCAN_WALLETS = 2

CHECK6_RECENT_SIGNATURE_LIMIT = 8

CHECK6_HISTORY_PAGE_SIZE = 1000

CHECK6_MAX_HISTORY_PAGES = 3

CHECK6_RPC_DELAY = 0.15

class SolanaRPC:
    """
    Read-only JSON-RPC client.

    The authority check uses one getMultipleAccounts call with
    finalized commitment. We request jsonParsed first because it
    is easy to inspect, but we validate that the returned account
    is actually a Mint. When parsed data is unavailable, we fall
    back to decoding the raw SPL Mint bytes ourselves.
    """

    def __init__(
        self,
        rpc_url=DEFAULT_RPC_URL,
        request_delay=0.5,
        timeout=15,
        max_retries=3,
    ):
        self.rpc_url = rpc_url
        self.request_delay = request_delay
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "SolanaMemeSniper/9.0",
        })
        self._last_request = 0.0

    def _rate_limit(self):
        elapsed = time.monotonic() - self._last_request
        remaining = self.request_delay - elapsed
        if remaining > 0:
            time.sleep(remaining)
        self._last_request = time.monotonic()

    def _call(self, method, params):
        payload = {
            "jsonrpc": "2.0",
            "id": int(time.time() * 1000),
            "method": method,
            "params": params,
        }

        last_error = None

        for attempt in range(1, self.max_retries + 1):
            self._rate_limit()

            try:
                response = self.session.post(
                    self.rpc_url,
                    json=payload,
                    timeout=self.timeout,
                )

                # Retry temporary HTTP failures.
                if response.status_code in {408, 425, 429, 500, 502, 503, 504}:
                    last_error = (
                        f"HTTP {response.status_code}: "
                        f"{response.text[:300]}"
                    )

                    if attempt < self.max_retries:
                        retry_after = response.headers.get("Retry-After")

                        try:
                            wait = (
                                max(1.0, float(retry_after))
                                if retry_after
                                else 2 ** (attempt - 1)
                            )
                        except ValueError:
                            wait = 2 ** (attempt - 1)

                        time.sleep(min(wait, 10))
                        continue

                response.raise_for_status()
                body = response.json()

                if "error" in body:
                    err = body["error"]
                    raise RuntimeError(
                        f"RPC error {err.get('code', 'unknown')}: "
                        f"{err.get('message', 'unknown RPC error')}"
                    )

                if "result" not in body:
                    raise RuntimeError("RPC response had no 'result' field")

                return body["result"]

            except (requests.RequestException, ValueError, RuntimeError) as exc:
                last_error = str(exc)

                if attempt < self.max_retries:
                    time.sleep(min(2 ** (attempt - 1), 10))
                    continue

                break

        raise RuntimeError(
            f"{method} failed after {self.max_retries} attempts: {last_error}"
        )

    def get_health(self):
        return self._call("getHealth", [])

    def get_multiple_accounts(self, addresses):
        if not addresses:
            return {"context_slot": None, "value": []}

        if len(addresses) > 100:
            raise ValueError(
                "getMultipleAccounts supports at most 100 addresses per request"
            )

        normalized = []

        for address in addresses:
            address = str(address).strip()

            # Validate Solana base58 public key locally before RPC.
            Pubkey.from_string(address)

            normalized.append(address)

        result = self._call(
            "getMultipleAccounts",
            [
                normalized,
                {
                    "encoding": "jsonParsed",
                    "commitment": "finalized",
                },
            ],
        )

        return {
            "context_slot": result.get("context", {}).get("slot"),
            "value": result.get("value") or [],
        }

def _read_coption_pubkey(raw, offset):
    """
    SPL Token COption<Pubkey>:
      4-byte little-endian option tag
      0 = None
      1 = Some + 32-byte public key
    """
    if len(raw) < offset + 36:
        raise ValueError("Mint data is too short for COption<Pubkey>")

    tag = int.from_bytes(
        raw[offset:offset + 4],
        "little",
    )

    if tag == 0:
        return None

    if tag != 1:
        raise ValueError(
            f"Invalid COption tag {tag} at byte offset {offset}"
        )

    return str(
        Pubkey.from_bytes(
            raw[offset + 4:offset + 36]
        )
    )

def _parse_raw_mint(raw, program):
    """
    Decode the 82-byte SPL Mint base layout.

    Offsets:
      0-35   mint authority COption<Pubkey>
      36-43  supply u64 LE
      44     decimals
      45     initialized flag
      46-81  freeze authority COption<Pubkey>

    Token-2022 appends extensions after the base Mint layout, so
    the first 82 bytes still contain these base fields.
    """
    if len(raw) < 82:
        raise ValueError(
            f"Account data is {len(raw)} bytes; a Mint needs at least 82"
        )

    mint_authority = _read_coption_pubkey(raw, 0)
    supply = int.from_bytes(raw[36:44], "little")
    decimals = raw[44]
    is_initialized_byte = raw[45]
    freeze_authority = _read_coption_pubkey(raw, 46)

    if is_initialized_byte not in (0, 1):
        raise ValueError(
            f"Invalid initialized flag: {is_initialized_byte}"
        )

    # Legacy SPL Token mints are exactly 82 bytes.
    # Token-2022 mints have additional account-type/extensions data.
    if program == "SPL Token" and len(raw) != 82:
        raise ValueError(
            f"Legacy SPL Token account length is {len(raw)}, expected 82"
        )

    if program == "Token-2022":
        # Token-2022 uses account type discriminator at byte 165:
        # 1 = Mint, 2 = TokenAccount.
        if len(raw) <= 165:
            raise ValueError(
                "Token-2022 account is too short to identify Mint account type"
            )

        if raw[165] != 1:
            raise ValueError(
                f"Token-2022 account type byte is {raw[165]}, not Mint (1)"
            )

    return {
        "mint_authority_raw": mint_authority,
        "freeze_authority_raw": freeze_authority,
        "supply": supply,
        "decimals": decimals,
        "is_initialized": bool(is_initialized_byte),
        "data_len": len(raw),
        "parser": "RAW BASE64 DECODE",
    }

def _parse_account(account, mint_address):
    if account is None:
        return {
            "status": "❌ ACCOUNT NOT FOUND",
            "message": "RPC returned value=null for this mint address",
            "mint_authority_raw": None,
            "freeze_authority_raw": None,
            "program": "UNKNOWN",
            "owner": None,
            "supply": None,
            "decimals": None,
            "is_initialized": None,
            "data_len": 0,
            "parser": "RPC",
            "mint_address": mint_address,
        }

    owner = account.get("owner")

    if owner == TOKEN_PROGRAM_ID:
        program = "SPL Token"
    elif owner == TOKEN_2022_PROGRAM_ID:
        program = "Token-2022"
    else:
        return {
            "status": "⚠️ NOT A TOKEN MINT",
            "message": (
                f"Account owner is {owner}, not a supported Solana token program"
            ),
            "mint_authority_raw": None,
            "freeze_authority_raw": None,
            "program": owner or "UNKNOWN",
            "owner": owner,
            "supply": None,
            "decimals": None,
            "is_initialized": None,
            "data_len": 0,
            "parser": "RPC",
            "mint_address": mint_address,
        }

    data = account.get("data")

    # ========================================================
    # First choice: strict jsonParsed Mint response
    # ========================================================

    if isinstance(data, dict) and "parsed" in data:
        parsed = data.get("parsed") or {}
        parsed_type = parsed.get("type")
        info = parsed.get("info") or {}

        # IMPORTANT:
        # Never assume missing fields mean None/disabled.
        # We require the account to explicitly be a Mint and
        # the authority fields to be present.
        if parsed_type == "mint":
            required = {
                "mintAuthority",
                "freezeAuthority",
                "supply",
                "decimals",
                "isInitialized",
            }

            if required.issubset(info.keys()):
                return {
                    "status": "✅ SUCCESS",
                    "message": "Parsed Mint account from RPC",
                    "mint_authority_raw": info["mintAuthority"],
                    "freeze_authority_raw": info["freezeAuthority"],
                    "program": program,
                    "owner": owner,
                    "supply": int(info["supply"]),
                    "decimals": int(info["decimals"]),
                    "is_initialized": bool(info["isInitialized"]),
                    "data_len": account.get("space"),
                    "parser": "RPC jsonParsed",
                    "mint_address": mint_address,
                }

    # ========================================================
    # Fallback: raw base64 account data from the SAME RPC
    # ========================================================

    if isinstance(data, list) and len(data) >= 2:
        encoded, encoding = data[0], data[1]

        if encoding != "base64":
            raise ValueError(
                f"RPC fallback encoding is {encoding}, expected base64"
            )

        raw = base64.b64decode(encoded)
        parsed_raw = _parse_raw_mint(raw, program)

        parsed_raw.update({
            "status": "✅ SUCCESS",
            "message": "Decoded Mint account from raw RPC bytes",
            "program": program,
            "owner": owner,
            "mint_address": mint_address,
        })

        return parsed_raw

    raise ValueError(
        "RPC returned an unrecognized account-data structure; "
        "authority status is UNKNOWN"
    )

def _format_authority(value):
    """
    Human-readable value.

    None is kept internally as the raw on-chain representation,
    but output is 'DISABLED (on-chain null)' so it cannot be
    confused with an RPC failure.
    """
    if value is None:
        return "DISABLED (on-chain null)"
    return f"ACTIVE → {value}"

def _authority_risk(mint_authority, freeze_authority):
    risk_score = 0
    flags = []

    if mint_authority is None:
        flags.append("✅ Mint authority: DISABLED")
    else:
        risk_score += 50
        flags.append(
            f"🔴 Mint authority: ACTIVE → {mint_authority}"
        )

    if freeze_authority is None:
        flags.append("✅ Freeze authority: DISABLED")
    else:
        risk_score += 30
        flags.append(
            f"🔴 Freeze authority: ACTIVE → {freeze_authority}"
        )

    if risk_score == 0:
        assessment = "🟢 BOTH AUTHORITIES DISABLED"
    elif risk_score == 30:
        assessment = "🟡 FREEZE AUTHORITY ACTIVE"
    elif risk_score == 50:
        assessment = "🟠 MINT AUTHORITY ACTIVE"
    else:
        assessment = "🔴 BOTH AUTHORITIES ACTIVE"

    return min(risk_score, 100), flags, assessment

def check_mint_authorities_batch(rpc, tokens):
    if not tokens:
        return []

    addresses = list(
        dict.fromkeys(
            str(token.mint).strip()
            for token in tokens
        )
    )

    print(
        f"\n🔍 Querying {len(addresses)} token mint(s) "
        f"from Solana Mainnet RPC..."
    )
    print("   Method: getMultipleAccounts")
    print("   Encoding: jsonParsed (raw-base64 fallback enabled)")
    print("   Commitment: finalized\n")

    batch = rpc.get_multiple_accounts(addresses)
    context_slot = batch["context_slot"]
    account_values = batch["value"]

    if len(account_values) != len(addresses):
        raise RuntimeError(
            "RPC returned a different number of accounts than requested"
        )

    by_address = dict(
        zip(addresses, account_values)
    )

    checks = []

    for idx, token in enumerate(tokens, 1):
        mint = str(token.mint).strip()

        print(
            f"[{idx}/{len(tokens)}] "
            f"{token.symbol.upper()} ({token.name})"
        )
        print(f"      Mint: {mint}")

        try:
            check = _parse_account(
                by_address.get(mint),
                mint,
            )

        except Exception as exc:
            check = {
                "status": "❌ PARSE ERROR",
                "message": str(exc),
                "mint_authority_raw": None,
                "freeze_authority_raw": None,
                "program": "UNKNOWN",
                "owner": None,
                "supply": None,
                "decimals": None,
                "is_initialized": None,
                "data_len": 0,
                "parser": "ERROR",
                "mint_address": mint,
            }

        check["context_slot"] = context_slot

        print(f"      Status: {check['status']}")
        print(f"      Program: {check['program']}")
        print(f"      Parser: {check.get('parser', 'UNKNOWN')}")
        print(f"      Finalized RPC slot: {context_slot}")

        if check["status"] == "✅ SUCCESS":
            mint_auth = check["mint_authority_raw"]
            freeze_auth = check["freeze_authority_raw"]

            risk_score, flags, assessment = _authority_risk(
                mint_auth,
                freeze_auth,
            )

            print(f"      Supply: {check['supply']:,}")
            print(f"      Decimals: {check['decimals']}")
            print(
                "      Mint Authority: "
                + _format_authority(mint_auth)
            )
            print(
                "      Freeze Authority: "
                + _format_authority(freeze_auth)
            )
            print(f"      Risk Score: {risk_score}/100")
            print(f"      Assessment: {assessment}")

            check["risk_score"] = risk_score
            check["flags"] = flags
            check["assessment"] = assessment

        else:
            check["risk_score"] = None
            check["flags"] = []
            check["assessment"] = "UNKNOWN — DO NOT MARK SAFE"

            print(f"      Message: {check['message']}")
            print(
                "      ⚠️ Authority status is UNKNOWN; "
                "this is NOT treated as safe."
            )

        print()
        checks.append(check)

    return checks

def safe_int(value):

    if value is None:
        return None

    try:
        return int(value)

    except (TypeError, ValueError):

        try:
            return int(float(value))

        except (TypeError, ValueError):

            return None

def pct(numerator, denominator):

    if denominator <= 0:
        return None

    return (
        numerator
        / denominator
        * 100.0
    )

def fmt_pct(value):

    if value is None:
        return "N/A"

    return f"{value:.2f}%"

def fmt_liquidity(value):

    if value is None:
        return "N/A"

    try:
        return f"${float(value):,.0f}"

    except (TypeError, ValueError):

        return "N/A"

class HeliusRPC:

    def __init__(self, url):

        self.url = url

        self.session = requests.Session()

        self.session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": (
                "SolanaMemeSniper-Check2/1.0"
            ),
        })

        self.last_request = 0.0


    def call(
        self,
        method,
        params,
        retries=3,
    ):

        payload = {
            "jsonrpc": "2.0",
            "id": int(
                time.time() * 1000
            ),
            "method": method,
            "params": params,
        }

        last_error = None


        for attempt in range(
            1,
            retries + 1,
        ):

            wait = (
                REQUEST_DELAY
                - (
                    time.monotonic()
                    - self.last_request
                )
            )

            if wait > 0:
                time.sleep(wait)


            try:

                response = self.session.post(
                    self.url,
                    json=payload,
                    timeout=HTTP_TIMEOUT,
                )


                self.last_request = (
                    time.monotonic()
                )


                if response.status_code in {
                    408,
                    425,
                    429,
                    500,
                    502,
                    503,
                    504,
                }:

                    last_error = (
                        f"HTTP "
                        f"{response.status_code}: "
                        f"{response.text[:300]}"
                    )


                    if attempt < retries:

                        time.sleep(
                            min(
                                2 ** (
                                    attempt - 1
                                ),
                                10,
                            )
                        )

                        continue


                response.raise_for_status()

                body = response.json()


                if "error" in body:

                    error = (
                        body.get("error")
                        or {}
                    )

                    raise RuntimeError(
                        f"RPC error "
                        f"{error.get('code', 'unknown')}: "
                        f"{error.get('message', 'unknown error')}"
                    )


                if "result" not in body:

                    raise RuntimeError(
                        "RPC response did not "
                        "contain result."
                    )


                return body["result"]


            except (
                requests.RequestException,
                ValueError,
                RuntimeError,
            ) as exc:

                last_error = str(exc)


                if attempt < retries:

                    time.sleep(
                        min(
                            2 ** (
                                attempt - 1
                            ),
                            10,
                        )
                    )


        raise RuntimeError(
            f"{method} failed after "
            f"{retries} attempts: "
            f"{last_error}"
        )


    def get_token_accounts(
        self,
        mint,
        page,
    ):

        return self.call(
            "getTokenAccounts",
            {
                "page": page,
                "limit": PAGE_SIZE,
                "displayOptions": {},
                "mint": mint,
            },
        )


    def get_multiple_accounts(
        self,
        addresses,
    ):

        all_values = []


        for start in range(
            0,
            len(addresses),
            100,
        ):

            batch = addresses[
                start:start + 100
            ]


            result = self.call(
                "getMultipleAccounts",
                [
                    batch,
                    {
                        "encoding": "jsonParsed",
                        "commitment": "finalized",
                    },
                ],
            )


            all_values.extend(
                result.get("value")
                or []
            )


        return all_values

def get_dex_pair_addresses(mint):

    try:

        response = requests.get(
            DEXSCREENER_TOKEN_URL.format(
                mint=mint
            ),
            timeout=HTTP_TIMEOUT,
            headers={
                "Accept": "application/json",
                "User-Agent": (
                    "SolanaMemeSniper-Check2/1.0"
                ),
            },
        )


        response.raise_for_status()

        data = response.json()


        if not isinstance(
            data,
            list,
        ):

            return set()


        pairs = set()


        for pair in data:

            if not isinstance(
                pair,
                dict,
            ):

                continue


            chain_id = str(
                pair.get(
                    "chainId"
                )
                or ""
            ).lower()


            if (
                chain_id
                and chain_id != "solana"
            ):

                continue


            pair_address = str(
                pair.get(
                    "pairAddress"
                )
                or ""
            ).strip()


            if pair_address:

                pairs.add(
                    pair_address
                )


        return pairs


    except Exception as exc:

        print(
            "      ⚠️ DEX Screener pool "
            f"lookup failed: {exc}"
        )

        return set()

def fetch_all_token_accounts(
    mint
):

    accounts = []

    page = 1


    while page <= MAX_PAGES:

        result = (
            helius.get_token_accounts(
                mint,
                page,
            )
        )


        page_accounts = (
            result.get(
                "token_accounts"
            )
            or []
        )


        print(
            f"      Helius page {page}: "
            f"{len(page_accounts):,} "
            "token accounts"
        )


        if not page_accounts:

            break


        accounts.extend(
            page_accounts
        )


        if (
            len(page_accounts)
            < PAGE_SIZE
        ):

            break


        page += 1


    if page > MAX_PAGES:

        raise RuntimeError(
            "Reached MAX_PAGES. "
            "Pagination stopped for safety."
        )


    return accounts

def extract_owner_amount(
    account
):

    if not isinstance(
        account,
        dict,
    ):

        return None, None


    owner = str(
        account.get(
            "owner"
        )
        or ""
    ).strip()


    if not owner:

        return None, None


    amount = safe_int(
        account.get(
            "amount"
        )
    )


    if amount is None:

        token_amount = (
            account.get(
                "tokenAmount"
            )
            or {}
        )


        if isinstance(
            token_amount,
            dict,
        ):

            amount = safe_int(
                token_amount.get(
                    "amount"
                )
            )


    return owner, amount

def aggregate_by_owner(
    token_accounts
):

    balances = defaultdict(int)

    nonzero_accounts = 0

    skipped = 0


    for account in token_accounts:

        owner, amount = (
            extract_owner_amount(
                account
            )
        )


        if (
            owner is None
            or amount is None
        ):

            skipped += 1

            continue


        if amount <= 0:

            continue


        balances[owner] += amount

        nonzero_accounts += 1


    return (
        dict(balances),
        nonzero_accounts,
        skipped,
    )

def concentration_stats(
    owner_balances
):

    if not owner_balances:

        return None


    ordered = sorted(
        owner_balances.items(),
        key=lambda item: item[1],
        reverse=True,
    )


    total = sum(
        owner_balances.values()
    )


    if total <= 0:

        return None


    def top_n_pct(n):

        amount = sum(
            balance
            for _, balance
            in ordered[:n]
        )


        return pct(
            amount,
            total,
        )


    return {
        "sorted_balances": ordered,
        "total": total,
        "top_1_pct": top_n_pct(1),
        "top_5_pct": top_n_pct(5),
        "top_10_pct": top_n_pct(10),
        "owner_count": len(
            owner_balances
        ),
    }

def classify_owners(
    owner_balances,
    pair_addresses,
):

    classification = {}


    # --------------------------------------------------------
    # Positively identified addresses
    # --------------------------------------------------------

    for owner in owner_balances:

        if owner in KNOWN_BURN_ADDRESSES:

            classification[owner] = {
                "kind": "burn",
                "reason": (
                    "Known Solana "
                    "incinerator address"
                ),
                "program_owner": None,
            }


        elif owner in pair_addresses:

            classification[owner] = {
                "kind": "dex_pool",
                "reason": (
                    "Current DEX Screener "
                    "pair/pool address"
                ),
                "program_owner": None,
            }


    unresolved = [
        owner
        for owner in owner_balances
        if owner not in classification
    ]


    if not unresolved:

        return classification


    infos = helius.get_multiple_accounts(
        unresolved
    )


    if len(infos) != len(
        unresolved
    ):

        raise RuntimeError(
            "getMultipleAccounts returned "
            "an unexpected number of accounts."
        )


    for owner, account in zip(
        unresolved,
        infos,
    ):

        if account is None:

            classification[owner] = {
                "kind": "unknown",
                "reason": (
                    "Owner account not found "
                    "by RPC"
                ),
                "program_owner": None,
            }

            continue


        program_owner = str(
            account.get(
                "owner"
            )
            or ""
        ).strip()


        executable = bool(
            account.get(
                "executable",
                False,
            )
        )


        if executable:

            classification[owner] = {
                "kind": "program",
                "reason": (
                    "Owner address is an "
                    "executable Solana program"
                ),
                "program_owner": (
                    program_owner
                    or None
                ),
            }


        elif (
            program_owner
            and program_owner
            != SYSTEM_PROGRAM_ID
        ):

            classification[owner] = {
                "kind": "contract_controlled",
                "reason": (
                    "Owner address is controlled "
                    "by a non-System program"
                ),
                "program_owner": (
                    program_owner
                ),
            }


        else:

            classification[owner] = {
                "kind": "wallet",
                "reason": (
                    "Owner account is "
                    "System-owned"
                ),
                "program_owner": (
                    program_owner
                    or None
                ),
            }


    return classification

def holder_status(
    top_1,
    top_5,
    top_10,
):

    if (
        top_1 is None
        or top_5 is None
        or top_10 is None
    ):

        return (
            None,
            "⚪ UNKNOWN — DO NOT MARK SAFE",
        )


    # HIGH

    if (
        top_1 > 50.0
        or top_5 > 80.0
    ):

        return (
            100,
            "🔴 HIGH CONCENTRATION",
        )


    # MODERATE

    if top_10 > 70.0:

        return (
            50,
            "🟡 MODERATE CONCENTRATION",
        )


    if top_1 >= 30.0:

        return (
            25,
            "🟡 MODERATE CONCENTRATION",
        )


    # LOWER

    return (
        0,
        "🟢 LOWER CONCENTRATION",
    )

def rc_float(value):
    if value is None or value == "":
        return None

    try:
        return float(value)
    except (TypeError, ValueError):
        return None

def rc_int(value):
    if value is None or value == "":
        return None

    try:
        return int(value)

    except (TypeError, ValueError):

        try:
            return int(float(value))

        except (TypeError, ValueError):
            return None

def unlock_days_from_timestamp(value):
    """
    Convert a Unix timestamp to remaining days.

    RugCheck uses:
      0 = locked forever

    Supports seconds and milliseconds.
    """

    ts = rc_float(value)

    if ts is None:
        return None

    if ts == 0:
        return 0.0

    if ts > 10_000_000_000:
        ts /= 1000.0

    now = datetime.now(
        timezone.utc
    ).timestamp()

    return max(
        0.0,
        (ts - now) / 86400.0
    )

def fetch_rugcheck_report(mint):
    url = (
        f"{RUGCHECK_BASE}"
        f"/v1/tokens/{mint}/report"
    )

    last_error = None

    for attempt in range(
        RUGCHECK_RETRIES + 1
    ):

        try:

            response = requests.get(
                url,
                timeout=RUGCHECK_TIMEOUT,
                headers={
                    "Accept": "application/json"
                },
            )

            # Success
            if response.status_code == 200:

                data = response.json()

                if not isinstance(
                    data,
                    dict
                ):
                    return (
                        None,
                        "Unexpected RugCheck response format"
                    )

                return data, None


            # Retry temporary failures
            if (
                response.status_code == 429
                or response.status_code >= 500
            ):

                last_error = (
                    f"HTTP "
                    f"{response.status_code}: "
                    f"{response.text[:200]}"
                )

                if attempt < RUGCHECK_RETRIES:

                    time.sleep(
                        2.0 * (attempt + 1)
                    )

                    continue


            # Not found
            if response.status_code == 404:

                return (
                    None,
                    "RugCheck report not found"
                )


            return (
                None,
                f"HTTP {response.status_code}: "
                f"{response.text[:200]}"
            )


        except requests.RequestException as exc:

            last_error = str(exc)

            if attempt < RUGCHECK_RETRIES:

                time.sleep(
                    2.0 * (attempt + 1)
                )

                continue


    return (
        None,
        last_error
        or
        "Unknown RugCheck request error"
    )

def find_exact_rugcheck_market(
    report,
    token
):

    markets = report.get(
        "markets"
    )

    if not isinstance(
        markets,
        list
    ):
        return None


    pair_address = str(
        getattr(
            token,
            "pair_address",
            ""
        )
        or
        ""
    ).strip()


    if not pair_address:
        return None


    for market in markets:

        if not isinstance(
            market,
            dict
        ):
            continue


        market_pubkey = str(
            market.get(
                "pubkey"
            )
            or
            ""
        ).strip()


        if market_pubkey == pair_address:

            return market


    return None

def extract_rugcheck_lockers(
    report
):

    lockers = report.get(
        "lockers"
    )

    if not isinstance(
        lockers,
        dict
    ):

        return {
            "count": 0,
            "permanent": False,
            "earliest_unlock_days": None,
            "locked_usd": None,
            "has_explicit_lock": False,
        }


    usable_lockers = []

    total_locked_usd = 0.0

    have_locked_usd = False


    for locker_key, locker in (
        lockers.items()
    ):

        if not isinstance(
            locker,
            dict
        ):
            continue


        usdc_locked = rc_float(
            locker.get(
                "usdcLocked"
            )
        )

        unlock_date = rc_int(
            locker.get(
                "unlockDate"
            )
        )


        # Only count meaningful locker records
        if (
            (
                usdc_locked is not None
                and usdc_locked > 0
            )
            or
            unlock_date is not None
        ):

            unlock_days = None


            if unlock_date is not None:

                if unlock_date == 0:
                    unlock_days = 0.0

                else:
                    unlock_days = (
                        unlock_days_from_timestamp(
                            unlock_date
                        )
                    )


            if (
                usdc_locked is not None
                and usdc_locked > 0
            ):

                total_locked_usd += (
                    usdc_locked
                )

                have_locked_usd = True


            usable_lockers.append({
                "id":
                    str(locker_key),

                "unlock_date":
                    unlock_date,

                "unlock_days":
                    unlock_days,

                "usdc_locked":
                    usdc_locked,

                "type":
                    str(
                        locker.get(
                            "type"
                        )
                        or
                        ""
                    ),
            })


    if not usable_lockers:

        return {
            "count": 0,
            "permanent": False,
            "earliest_unlock_days": None,
            "locked_usd": (
                total_locked_usd
                if have_locked_usd
                else
                None
            ),
            "has_explicit_lock": False,
        }


    # A permanent locker is one whose unlockDate is 0.
    # Require ALL usable locker records to be permanent
    # before calling the pool permanently locked.
    permanent = all(
        locker["unlock_date"] == 0
        for locker in usable_lockers
        if locker["unlock_date"] is not None
    )


    timed_unlocks = [
        locker["unlock_days"]
        for locker in usable_lockers
        if (
            locker["unlock_days"] is not None
            and
            locker["unlock_days"] > 0
        )
    ]


    earliest_unlock_days = (
        min(timed_unlocks)
        if timed_unlocks
        else
        None
    )


    return {
        "count":
            len(usable_lockers),

        "permanent":
            permanent,

        "earliest_unlock_days":
            earliest_unlock_days,

        "locked_usd":
            (
                total_locked_usd
                if have_locked_usd
                else
                None
            ),

        "has_explicit_lock":
            True,
    }

def calculate_lp_risk(
    lp_locked_pct,
    locker_info
):

    if lp_locked_pct is None:

        return {
            "risk_score":
                100.0,

            "status":
                "⚪ LP LOCK UNKNOWN",

            "passes":
                False,

            "reason":
                "No usable LP lock percentage was returned.",
        }


    pct = max(
        0.0,
        min(
            100.0,
            float(lp_locked_pct)
        )
    )


    # --------------------------------------------------------
    # ZERO LOCK
    # --------------------------------------------------------

    if pct <= 0.0:

        return {
            "risk_score":
                100.0,

            "status":
                "🔴 LP UNLOCKED",

            "passes":
                False,

            "reason":
                "0% of LP tokens are currently reported as locked.",
        }


    # --------------------------------------------------------
    # PARTIAL LOCK
    # --------------------------------------------------------

    if pct < 50.0:

        return {
            "risk_score":
                75.0,

            "status":
                "🟡 LP MOSTLY UNLOCKED",

            "passes":
                False,

            "reason":
                f"Only {pct:.2f}% of LP is reported as locked.",
        }


    if pct < 95.0:

        return {
            "risk_score":
                50.0,

            "status":
                "🟡 LP PARTIALLY LOCKED",

            "passes":
                False,

            "reason":
                f"Only {pct:.2f}% of LP is reported as locked.",
        }


    # --------------------------------------------------------
    # 95% - 99.98%
    # --------------------------------------------------------

    if pct < 99.99:

        return {
            "risk_score":
                25.0,

            "status":
                "🟡 LP MOSTLY LOCKED",

            "passes":
                False,

            "reason":
                (
                    f"{pct:.2f}% of LP is locked, "
                    "but the strict full-lock condition was not met."
                ),
        }


    # --------------------------------------------------------
    # EFFECTIVELY 100% LOCKED
    # --------------------------------------------------------

    if locker_info["permanent"]:

        return {
            "risk_score":
                0.0,

            "status":
                "🟢 LP LOCKED FOREVER",

            "passes":
                True,

            "reason":
                (
                    "All usable locker records report "
                    "unlockDate=0."
                ),
        }


    days = (
        locker_info[
            "earliest_unlock_days"
        ]
    )


    # We do NOT call 100% locked safe when we don't
    # know how long the lock lasts.
    if days is None:

        return {
            "risk_score":
                25.0,

            "status":
                "🟡 LP 100% LOCKED — DURATION UNKNOWN",

            "passes":
                False,

            "reason":
                (
                    "LP is effectively 100% locked, "
                    "but no usable unlock duration was found."
                ),
        }


    if days < 30.0:

        return {
            "risk_score":
                100.0,

            "status":
                "🔴 LP LOCK EXPIRES <30 DAYS",

            "passes":
                False,

            "reason":
                (
                    f"Earliest reported unlock is "
                    f"in approximately {days:.1f} days."
                ),
        }


    if days < 180.0:

        return {
            "risk_score":
                50.0,

            "status":
                "🟡 LP LOCK <6 MONTHS",

            "passes":
                False,

            "reason":
                (
                    f"Earliest reported unlock is "
                    f"in approximately {days:.1f} days."
                ),
        }


    # 180+ days
    return {
        "risk_score":
            0.0,

        "status":
            "🟢 LP LOCKED ≥6 MONTHS",

        "passes":
            True,

        "reason":
            (
                f"Earliest reported unlock is "
                f"in approximately {days:.1f} days."
            ),
    }

def analyze_lp_ownership(
    token
):

    mint = str(
        getattr(
            token,
            "mint",
            ""
        )
        or
        ""
    ).strip()


    pair_address = str(
        getattr(
            token,
            "pair_address",
            ""
        )
        or
        ""
    ).strip()


    if not mint:

        return {
            "mint":
                "",

            "pair_address":
                pair_address,

            "source":
                "RugCheck",

            "status":
                "⚪ LP LOCK UNKNOWN",

            "risk_score":
                100.0,

            "passes":
                False,

            "lp_locked_pct":
                None,

            "lp_unlocked_pct":
                None,

            "lock_duration_days":
                None,

            "permanent_lock":
                False,

            "market_type":
                None,

            "market_pubkey":
                None,

            "pair_match":
                False,

            "locked_usd":
                None,

            "locker_count":
                0,

            "findings": [
                "Missing token mint."
            ],
        }


    report, error = (
        fetch_rugcheck_report(
            mint
        )
    )


    if report is None:

        return {
            "mint":
                mint,

            "pair_address":
                pair_address,

            "source":
                "RugCheck",

            "status":
                "⚪ LP LOCK UNKNOWN",

            "risk_score":
                100.0,

            "passes":
                False,

            "lp_locked_pct":
                None,

            "lp_unlocked_pct":
                None,

            "lock_duration_days":
                None,

            "permanent_lock":
                False,

            "market_type":
                None,

            "market_pubkey":
                None,

            "pair_match":
                False,

            "locked_usd":
                None,

            "locker_count":
                0,

            "findings": [
                "RugCheck report could not be retrieved.",
                str(
                    error
                    or
                    "Unknown API error"
                ),
                "UNKNOWN is not treated as safe.",
            ],

            "error":
                error,
        }


    # --------------------------------------------------------
    # Match the CURRENT sniper pair exactly
    # --------------------------------------------------------

    market = (
        find_exact_rugcheck_market(
            report,
            token
        )
    )


    if market is None:

        # We may have a top-level percentage,
        # but we DO NOT apply it to the current pair
        # because the pair was not independently matched.
        top_level_pct = rc_float(
            report.get(
                "lpLockedPct"
            )
        )


        return {
            "mint":
                mint,

            "pair_address":
                pair_address,

            "source":
                "RugCheck",

            "status":
                "⚪ LP LOCK UNKNOWN — PAIR NOT VERIFIED",

            "risk_score":
                100.0,

            "passes":
                False,

            "lp_locked_pct":
                top_level_pct,

            "lp_unlocked_pct":
                (
                    100.0 - top_level_pct
                    if top_level_pct is not None
                    else
                    None
                ),

            "lock_duration_days":
                None,

            "permanent_lock":
                False,

            "market_type":
                None,

            "market_pubkey":
                None,

            "pair_match":
                False,

            "locked_usd":
                None,

            "locker_count":
                0,

            "findings": [
                (
                    "Current DEX Screener pair address "
                    "did not exactly match a RugCheck market."
                ),
                (
                    "Top-level LP data was NOT used "
                    "to mark this pair safe."
                ),
                "UNKNOWN is not treated as safe.",
            ],
        }


    lp = market.get(
        "lp"
    ) or {}


    lp_locked_pct = rc_float(
        lp.get(
            "lpLockedPct"
        )
    )


    if lp_locked_pct is None:

        lp_locked_pct = rc_float(
            market.get(
                "lpLockedPct"
            )
        )


    locker_info = (
        extract_rugcheck_lockers(
            report
        )
    )


    lp_result = (
        calculate_lp_risk(
            lp_locked_pct,
            locker_info
        )
    )


    locked_usd = rc_float(
        lp.get(
            "lpLockedUSD"
        )
    )


    if locked_usd is None:

        locked_usd = rc_float(
            lp.get(
                "lpLockedUsd"
            )
        )


    unlocked_pct = (
        100.0 - lp_locked_pct
        if lp_locked_pct is not None
        else
        None
    )


    findings = [
        lp_result["reason"],

        (
            "Market type: "
            f"{market.get('marketType') or 'UNKNOWN'}"
        ),

        (
            "RugCheck market pubkey: "
            f"{market.get('pubkey') or 'UNKNOWN'}"
        ),
    ]


    if locker_info[
        "has_explicit_lock"
    ]:

        findings.append(
            (
                "Explicit locker records: "
                f"{locker_info['count']}"
            )
        )


    if locked_usd is not None:

        findings.append(
            (
                f"Reported locked LP value: "
                f"${locked_usd:,.2f}"
            )
        )


    if not lp_result["passes"]:

        findings.append(
            "LP verification does not pass the strict sniper gate."
        )


    return {
        "mint":
            mint,

        "pair_address":
            pair_address,

        "source":
            "RugCheck",

        "status":
            lp_result["status"],

        "risk_score":
            lp_result["risk_score"],

        "passes":
            lp_result["passes"],

        "lp_locked_pct":
            lp_locked_pct,

        "lp_unlocked_pct":
            unlocked_pct,

        "lock_duration_days":
            (
                0.0
                if locker_info["permanent"]
                else
                locker_info["earliest_unlock_days"]
            ),

        "permanent_lock":
            locker_info["permanent"],

        "market_type":
            str(
                market.get(
                    "marketType"
                )
                or
                ""
            ),

        "market_pubkey":
            str(
                market.get(
                    "pubkey"
                )
                or
                ""
            ),

        "pair_match":
            True,

        "locked_usd":
            locked_usd,

        "locker_count":
            locker_info["count"],

        "findings":
            findings,
    }

def check4_float(value):
    """
    Safely convert a value to float.

    Returns None when the value is missing,
    invalid, NaN, or infinite.
    """

    if value is None:
        return None

    try:
        result = float(value)

        if not math.isfinite(result):
            return None

        return result

    except (TypeError, ValueError):
        return None

def check4_int(value):
    """
    Safely convert a value to int.
    """

    if value is None:
        return None

    try:
        return int(value)

    except (TypeError, ValueError):
        return None

def check4_pct(part, whole):
    """
    Return part as a percentage of whole.
    """

    part = check4_float(part)
    whole = check4_float(whole)

    if part is None or whole is None or whole <= 0:
        return None

    return (part / whole) * 100.0

def check4_get_rpc_url():
    """
    Reuse an existing RPC URL from the notebook when possible.

    Supports common variable names already used in Colab projects.
    """

    possible_names = [
        "HELIUS_RPC_URL",
        "HELIUS_RPC",
        "SOLANA_RPC_URL",
        "RPC_URL",
        "rpc_url",
    ]

    for name in possible_names:

        if name in globals():

            value = globals().get(name)

            if isinstance(value, str) and value.strip():
                return value.strip()

    # Try an existing Helius API key if the notebook has one.
    possible_key_names = [
        "HELIUS_API_KEY",
        "HELIUS_KEY",
        "helius_api_key",
    ]

    for name in possible_key_names:

        if name in globals():

            api_key = globals().get(name)

            if isinstance(api_key, str) and api_key.strip():

                return (
                    "https://mainnet.helius-rpc.com/"
                    f"?api-key={api_key.strip()}"
                )

    return None

def check4_get_token_supply(
    mint,
    rpc_url,
):
    """
    Query Solana's getTokenSupply RPC method.

    Returns:
        {
            "amount": raw integer supply,
            "decimals": token decimals,
            "ui_amount": human-readable supply,
            "ui_amount_string": RPC string representation
        }

    Raises an exception when supply cannot be verified.
    """

    if not mint:
        raise RuntimeError(
            "Token mint is missing."
        )

    if not rpc_url:
        raise RuntimeError(
            "No Solana RPC URL was found."
        )

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTokenSupply",
        "params": [
            mint,
            {
                "commitment": "finalized"
            },
        ],
    }

    response = requests.post(
        rpc_url,
        json=payload,
        timeout=CHECK4_RPC_TIMEOUT_SECONDS,
    )

    response.raise_for_status()

    data = response.json()

    if not isinstance(data, dict):
        raise RuntimeError(
            "getTokenSupply returned an unexpected response."
        )

    if data.get("error"):
        raise RuntimeError(
            f"getTokenSupply RPC error: {data['error']}"
        )

    result = data.get("result")

    if not isinstance(result, dict):
        raise RuntimeError(
            "getTokenSupply returned an unexpected result."
        )

    value = result.get("value")

    if not isinstance(value, dict):
        raise RuntimeError(
            "getTokenSupply response did not contain value."
        )

    amount = check4_int(
        value.get("amount")
    )

    decimals = check4_int(
        value.get("decimals")
    )

    ui_amount_string = value.get(
        "uiAmountString"
    )

    if amount is None:
        raise RuntimeError(
            "getTokenSupply did not return a usable raw amount."
        )

    if decimals is None:
        raise RuntimeError(
            "getTokenSupply did not return token decimals."
        )

    ui_supply = check4_float(
        ui_amount_string
    )

    if ui_supply is None:

        ui_supply = (
            amount
            / (10 ** decimals)
        )

    return {
        "amount": amount,
        "decimals": decimals,
        "ui_amount": ui_supply,
        "ui_amount_string": str(
            ui_amount_string
            if ui_amount_string is not None
            else ui_supply
        ),
    }

def check4_find_holder_check(
    holder_checks,
    mint,
):
    """
    Find the Check 2 result belonging to the exact same mint.

    Check 4 never silently substitutes another token's
    holder analysis.
    """

    if not isinstance(holder_checks, list):
        return None

    for check in holder_checks:

        if not isinstance(check, dict):
            continue

        check_mint = str(
            check.get("mint") or ""
        ).strip()

        if check_mint == mint:
            return check

    return None

def check4_get_raw_holder_total(holder_check):
    """
    Extract the raw holder balance total from Check 2.

    Check 2 stores the raw holder information under:
        check["raw"]

    and the raw total under:
        raw["total_amount"]
    """

    if not isinstance(holder_check, dict):
        return None

    raw = holder_check.get("raw")

    if not isinstance(raw, dict):
        return None

    total_amount = check4_int(
        raw.get("total_amount")
    )

    return total_amount

def check4_get_adjusted_distribution(holder_check):
    """
    Extract the adjusted concentration data generated by Check 2.

    Check 4 intentionally uses Check 2's existing classification
    instead of rebuilding holder concentration independently.
    """

    if not isinstance(holder_check, dict):
        return None

    adjusted = holder_check.get("adjusted")

    if not isinstance(adjusted, dict):
        return None

    return adjusted

def check4_run_one(
    result,
    holder_checks,
    rpc_url,
):
    """
    Run Check 4 against one current sniper result.
    """

    mint = str(
        getattr(
            result,
            "mint",
            ""
        ) or ""
    ).strip()

    symbol = str(
        getattr(
            result,
            "symbol",
            ""
        ) or ""
    ).strip()

    name = str(
        getattr(
            result,
            "name",
            ""
        ) or ""
    ).strip()

    check = {
        "mint": mint,
        "symbol": symbol,
        "name": name,

        "source": (
            "Solana getTokenSupply + "
            "Check 2 Helius holder analysis"
        ),

        "status": (
            "⚪ UNKNOWN / NOT VERIFIED"
        ),

        "risk_score": None,

        "passes": False,

        "onchain_supply": None,
        "onchain_supply_raw": None,
        "supply_decimals": None,

        "holder_total_raw": None,
        "holder_coverage_pct": None,
        "supply_difference_raw": None,
        "supply_difference_pct": None,

        "adjusted_top_1_pct": None,
        "adjusted_top_5_pct": None,
        "adjusted_top_10_pct": None,

        "supply_reconciles": False,
        "distribution_available": False,

        "concentration_flags": [],

        "findings": [],
        "error": None,
    }


    # --------------------------------------------------------
    # Basic mint validation
    # --------------------------------------------------------

    if not mint:

        check["status"] = (
            "🔴 CHECK FAILED — MINT UNKNOWN"
        )

        check["risk_score"] = 100.0

        check["findings"].append(
            "Token mint is missing."
        )

        return check


    # --------------------------------------------------------
    # Check 2 lookup
    # --------------------------------------------------------

    holder_check = check4_find_holder_check(
        holder_checks,
        mint,
    )

    if holder_check is None:

        check["status"] = (
            "🔴 CHECK FAILED — CHECK 2 DATA MISSING"
        )

        check["risk_score"] = 100.0

        check["findings"].append(
            "No Check 2 holder analysis was found "
            "for this exact mint."
        )

        return check


    # --------------------------------------------------------
    # Raw holder total
    # --------------------------------------------------------

    holder_total = check4_get_raw_holder_total(
        holder_check
    )

    if holder_total is None:

        check["status"] = (
            "🔴 CHECK FAILED — HOLDER SUPPLY UNKNOWN"
        )

        check["risk_score"] = 100.0

        check["findings"].append(
            "Check 2 did not provide a usable raw holder total."
        )

        return check

    check["holder_total_raw"] = holder_total


    # --------------------------------------------------------
    # Adjusted distribution
    # --------------------------------------------------------

    adjusted = check4_get_adjusted_distribution(
        holder_check
    )

    if adjusted is None:

        check["findings"].append(
            "Check 2 did not provide adjusted "
            "holder concentration data."
        )

    else:

        top_1 = check4_float(
            adjusted.get("top_1_pct")
        )

        top_5 = check4_float(
            adjusted.get("top_5_pct")
        )

        top_10 = check4_float(
            adjusted.get("top_10_pct")
        )

        check["adjusted_top_1_pct"] = top_1
        check["adjusted_top_5_pct"] = top_5
        check["adjusted_top_10_pct"] = top_10

        if (
            top_1 is not None
            and top_5 is not None
            and top_10 is not None
        ):

            check["distribution_available"] = True

        else:

            check["findings"].append(
                "Adjusted holder concentration data is incomplete."
            )


    # --------------------------------------------------------
    # On-chain supply
    # --------------------------------------------------------

    try:

        supply = check4_get_token_supply(
            mint,
            rpc_url,
        )

        reported_supply = supply["amount"]

        check["onchain_supply_raw"] = (
            reported_supply
        )

        check["onchain_supply"] = (
            supply["ui_amount"]
        )

        check["supply_decimals"] = (
            supply["decimals"]
        )

    except Exception as exc:

        check["status"] = (
            "🔴 CHECK FAILED — SUPPLY UNKNOWN"
        )

        check["risk_score"] = 100.0

        check["error"] = str(exc)

        check["findings"].append(
            "On-chain token supply could not be verified."
        )

        return check


    # --------------------------------------------------------
    # Supply validation
    # --------------------------------------------------------

    if reported_supply <= 0:

        check["status"] = (
            "🔴 CHECK FAILED — INVALID SUPPLY"
        )

        check["risk_score"] = 100.0

        check["findings"].append(
            "On-chain reported token supply is zero or negative."
        )

        return check


    # --------------------------------------------------------
    # Supply reconciliation
    # --------------------------------------------------------

    holder_coverage_pct = (
        check4_pct(
            holder_total,
            reported_supply,
        )
    )

    supply_difference = (
        reported_supply
        - holder_total
    )

    absolute_difference_pct = (
        abs(supply_difference)
        / reported_supply
        * 100.0
    )

    holder_over_supply = (
        holder_total
        > reported_supply
    )

    supply_reconciles = (
        not holder_over_supply
        and
        absolute_difference_pct
        <= CHECK4_SUPPLY_TOLERANCE_PCT
    )

    check["holder_coverage_pct"] = (
        holder_coverage_pct
    )

    check["supply_difference_raw"] = (
        supply_difference
    )

    check["supply_difference_pct"] = (
        absolute_difference_pct
    )

    check["supply_reconciles"] = (
        supply_reconciles
    )


    # --------------------------------------------------------
    # Concentration rules
    # --------------------------------------------------------

    concentration_flags = []

    top_1 = check["adjusted_top_1_pct"]
    top_5 = check["adjusted_top_5_pct"]
    top_10 = check["adjusted_top_10_pct"]


    if check["distribution_available"]:

        if (
            top_1
            > CHECK4_CONCENTRATION_TOP1_BLOCK_PCT
        ):

            concentration_flags.append(
                "Adjusted Top 1 owner exceeds 50%."
            )


        if (
            top_5
            > CHECK4_CONCENTRATION_TOP5_BLOCK_PCT
        ):

            concentration_flags.append(
                "Adjusted Top 5 owners exceed 80%."
            )


        if (
            top_10
            > CHECK4_CONCENTRATION_TOP10_BLOCK_PCT
        ):

            concentration_flags.append(
                "Adjusted Top 10 owners exceed 70%."
            )


    check["concentration_flags"] = (
        concentration_flags
    )


    # --------------------------------------------------------
    # FINAL DECISION
    # --------------------------------------------------------

    reasons = []


    if not supply_reconciles:

        if holder_over_supply:

            reasons.append(
                "Helius holder balances exceed "
                "the on-chain token supply."
            )

        else:

            reasons.append(
                "Helius holder balances do not reconcile "
                "with the on-chain token supply within "
                "the 0.10% tolerance."
            )


    if not check["distribution_available"]:

        reasons.append(
            "Adjusted holder concentration data is incomplete."
        )


    reasons.extend(
        concentration_flags
    )


    passes = (
        supply_reconciles
        and
        check["distribution_available"]
        and
        not concentration_flags
    )


    if passes:

        check["status"] = (
            "🟢 SUPPLY VERIFIED / "
            "DISTRIBUTION WITHIN LIMITS"
        )

        check["risk_score"] = 0.0
        check["passes"] = True

        check["findings"].append(
            "On-chain token supply reconciles with "
            "the Check 2 holder balance total."
        )

        check["findings"].append(
            "Adjusted holder concentration is "
            "within all Check 4 limits."
        )

    else:

        check["status"] = (
            "🔴 SUPPLY / DISTRIBUTION CHECK FAILED"
        )

        check["risk_score"] = 100.0
        check["passes"] = False

        check["findings"].extend(
            reasons
        )


    return check

def check5_decode_optional_pubkey(data):

    if data is None or len(data) != 32:

        raise ValueError(
            "Optional pubkey must contain exactly 32 bytes."
        )


    if data == bytes(32):

        return None


    return str(
        Pubkey.from_bytes(
            data
        )
    )

def check5_rpc_request(
    method,
    params,
):

    existing_rpc = globals().get(
        "rpc"
    )


    rpc_url = (
        getattr(
            existing_rpc,
            "rpc_url",
            None,
        )
        or
        "https://api.mainnet-beta.solana.com"
    )


    payload = {

        "jsonrpc": "2.0",

        "id": int(
            time.time() * 1000
        ),

        "method": method,

        "params": params,
    }


    last_error = None


    for attempt in range(
        1,
        CHECK5_RPC_RETRIES + 1,
    ):

        try:

            response = requests.post(

                rpc_url,

                json=payload,

                headers={
                    "Content-Type":
                        "application/json",

                    "Accept":
                        "application/json",
                },

                timeout=CHECK5_RPC_TIMEOUT,
            )


            if response.status_code in {

                408,
                425,
                429,
                500,
                502,
                503,
                504,

            }:

                last_error = (
                    f"HTTP {response.status_code}"
                )


                if (
                    attempt
                    <
                    CHECK5_RPC_RETRIES
                ):

                    time.sleep(
                        CHECK5_RPC_RETRY_DELAY
                        * attempt
                    )

                    continue


            response.raise_for_status()


            body = response.json()


            if "error" in body:

                raise RuntimeError(
                    str(
                        body["error"]
                    )
                )


            if "result" not in body:

                raise RuntimeError(
                    "RPC response did not contain a result."
                )


            return body["result"]


        except Exception as exc:

            last_error = str(exc)


            if (
                attempt
                <
                CHECK5_RPC_RETRIES
            ):

                time.sleep(
                    CHECK5_RPC_RETRY_DELAY
                    * attempt
                )


    raise RuntimeError(
        "RPC request failed after "
        f"{CHECK5_RPC_RETRIES} attempts: "
        f"{last_error}"
    )

def check5_get_mint_account(
    mint,
):

    result = check5_rpc_request(

        "getAccountInfo",

        [
            mint,

            {
                "encoding":
                    "base64",

                "commitment":
                    "finalized",
            },
        ],
    )


    value = (
        result.get(
            "value"
        )
        if isinstance(
            result,
            dict,
        )
        else None
    )


    if value is None:

        raise RuntimeError(
            "Mint account does not exist."
        )


    data = value.get(
        "data"
    )


    if not (

        isinstance(
            data,
            list,
        )

        and

        len(data) >= 2

        and

        data[1] == "base64"

    ):

        raise RuntimeError(
            "Mint account did not return base64 data."
        )


    return {

        "owner":
            value.get(
                "owner"
            ),

        "raw":
            base64.b64decode(
                data[0]
            ),
    }

def check5_parse_extensions(
    raw,
):
    """
    Token-2022 extended mint layout:

        bytes 0..81     = base Mint
        bytes 82..164   = padding
        byte 165        = AccountType
        bytes 166..     = TLV extension data

    AccountType == 1 means Mint.

    A normal 82-byte mint has no extensions.
    """

    if len(raw) == 82:

        return []


    if len(raw) < 166:

        raise RuntimeError(
            "Invalid Token-2022 mint length: "
            f"{len(raw)}"
        )


    if raw[165] != 1:

        raise RuntimeError(
            "Token-2022 account type is not Mint."
        )


    extensions = []

    offset = 166


    while offset < len(raw):

        remaining = len(raw) - offset


        if remaining < 4:

            if any(
                raw[offset:]
            ):

                raise RuntimeError(
                    "Unexpected non-zero "
                    "trailing bytes."
                )

            break


        extension_type = int.from_bytes(

            raw[
                offset:
                offset + 2
            ],

            "little",
        )


        extension_length = int.from_bytes(

            raw[
                offset + 2:
                offset + 4
            ],

            "little",
        )


        offset += 4


        # Uninitialized extension entries are padding.
        if (

            extension_type == 0

            and

            extension_length == 0

        ):

            if not any(
                raw[offset:]
            ):

                break

            continue


        end = (
            offset
            + extension_length
        )


        if end > len(raw):

            raise RuntimeError(
                "Token-2022 extension exceeds "
                "mint account length."
            )


        extensions.append({

            "type_id":
                extension_type,

            "type_name":
                CHECK5_EXTENSION_TYPES.get(

                    extension_type,

                    f"Unknown({extension_type})",

                ),

            "data":
                raw[
                    offset:
                    end
                ],
        })


        offset = end


    return extensions

def check5_decode_extension(
    extension,
):

    name = extension[
        "type_name"
    ]

    data = extension[
        "data"
    ]


    output = {

        "type_id":
            extension[
                "type_id"
            ],

        "type_name":
            name,

        "length":
            len(data),
    }


    # --------------------------------------------------------
    # TRANSFER FEE CONFIG
    # --------------------------------------------------------

    if name == "TransferFeeConfig":

        if len(data) < 108:

            output[
                "decode_error"
            ] = (
                "TransferFeeConfig "
                "data is too short."
            )

            return output


        output[
            "transfer_fee_config_authority"
        ] = check5_decode_optional_pubkey(

            data[0:32]

        )


        output[
            "withdraw_withheld_authority"
        ] = check5_decode_optional_pubkey(

            data[32:64]

        )


        # bytes 64..71 = withheld amount


        output[
            "older_epoch"
        ] = int.from_bytes(

            data[72:80],

            "little",
        )


        output[
            "older_max_fee"
        ] = int.from_bytes(

            data[80:88],

            "little",
        )


        output[
            "older_bps"
        ] = int.from_bytes(

            data[88:90],

            "little",
        )


        output[
            "newer_epoch"
        ] = int.from_bytes(

            data[90:98],

            "little",
        )


        output[
            "newer_max_fee"
        ] = int.from_bytes(

            data[98:106],

            "little",
        )


        output[
            "newer_bps"
        ] = int.from_bytes(

            data[106:108],

            "little",
        )


    # --------------------------------------------------------
    # TRANSFER HOOK
    # --------------------------------------------------------

    elif name == "TransferHook":

        if len(data) < 64:

            output[
                "decode_error"
            ] = (
                "TransferHook "
                "data is too short."
            )

            return output


        output[
            "authority"
        ] = check5_decode_optional_pubkey(

            data[0:32]

        )


        output[
            "program_id"
        ] = check5_decode_optional_pubkey(

            data[32:64]

        )


    # --------------------------------------------------------
    # PERMANENT DELEGATE
    # --------------------------------------------------------

    elif name == "PermanentDelegate":

        if len(data) < 32:

            output[
                "decode_error"
            ] = (
                "PermanentDelegate "
                "data is too short."
            )

            return output


        output[
            "delegate"
        ] = check5_decode_optional_pubkey(

            data[0:32]

        )


    # --------------------------------------------------------
    # DEFAULT ACCOUNT STATE
    # --------------------------------------------------------

    elif name == "DefaultAccountState":

        if len(data) < 1:

            output[
                "decode_error"
            ] = (
                "DefaultAccountState "
                "data is empty."
            )

            return output


        state = data[0]


        output[
            "state"
        ] = state


        output[
            "state_name"
        ] = {

            0:
                "Uninitialized",

            1:
                "Initialized",

            2:
                "Frozen",

        }.get(

            state,

            f"Unknown({state})",

        )


    # --------------------------------------------------------
    # PAUSABLE
    # --------------------------------------------------------

    elif name == "Pausable":

        if len(data) < 33:

            output[
                "decode_error"
            ] = (
                "Pausable "
                "data is too short."
            )

            return output


        output[
            "authority"
        ] = check5_decode_optional_pubkey(

            data[0:32]

        )


        output[
            "paused"
        ] = bool(
            data[32]
        )


    # --------------------------------------------------------
    # PERMISSIONED BURN
    # --------------------------------------------------------

    elif name == "PermissionedBurn":

        if len(data) < 32:

            output[
                "decode_error"
            ] = (
                "PermissionedBurn "
                "data is too short."
            )

            return output


        output[
            "authority"
        ] = check5_decode_optional_pubkey(

            data[0:32]

        )


    # --------------------------------------------------------
    # MINT CLOSE AUTHORITY
    # --------------------------------------------------------

    elif name == "MintCloseAuthority":

        if len(data) < 32:

            output[
                "decode_error"
            ] = (
                "MintCloseAuthority "
                "data is too short."
            )

            return output


        output[
            "authority"
        ] = check5_decode_optional_pubkey(

            data[0:32]

        )


    return output

def check5_analyze_token(
    token,
):

    mint = str(

        getattr(
            token,
            "mint",
            "",
        )

        or ""

    ).strip()


    if not mint:

        return {

            "mint":
                "",

            "program":
                "UNKNOWN",

            "passes":
                False,

            "status":
                "🔴 BLOCK — MISSING MINT",

            "risk_score":
                100.0,

            "extension_names":
                [],

            "extensions":
                [],

            "blocking_reasons":
                [
                    "Mint address is missing."
                ],

            "warnings":
                [],
        }


    account = check5_get_mint_account(
        mint
    )


    owner = account[
        "owner"
    ]

    raw = account[
        "raw"
    ]


    # --------------------------------------------------------
    # LEGACY SPL TOKEN
    # --------------------------------------------------------

    if owner == CHECK5_SPL_TOKEN_PROGRAM:

        return {

            "mint":
                mint,

            "program":
                "SPL Token",

            "passes":
                True,

            "status":
                "✅ PASS — LEGACY SPL TOKEN",

            "risk_score":
                0.0,

            "extension_names":
                [],

            "extensions":
                [],

            "blocking_reasons":
                [],

            "warnings":
                [],
        }


    # --------------------------------------------------------
    # UNKNOWN TOKEN PROGRAM
    # --------------------------------------------------------

    if owner != CHECK5_TOKEN_2022_PROGRAM:

        return {

            "mint":
                mint,

            "program":
                owner
                or
                "UNKNOWN",

            "passes":
                False,

            "status":
                (
                    "🔴 BLOCK — "
                    "UNSUPPORTED TOKEN PROGRAM"
                ),

            "risk_score":
                100.0,

            "extension_names":
                [],

            "extensions":
                [],

            "blocking_reasons":
                [
                    (
                        "Mint is not owned by "
                        "SPL Token or Token-2022."
                    )
                ],

            "warnings":
                [],
        }


    # --------------------------------------------------------
    # TOKEN-2022
    # --------------------------------------------------------

    raw_extensions = (
        check5_parse_extensions(
            raw
        )
    )


    extensions = [

        check5_decode_extension(
            extension
        )

        for extension
        in raw_extensions

    ]


    extension_names = [

        extension[
            "type_name"
        ]

        for extension
        in extensions

    ]


    blocking_reasons = []
    warnings = []


    # --------------------------------------------------------
    # UNKNOWN EXTENSIONS
    # --------------------------------------------------------

    unknown_extensions = [

        extension[
            "type_name"
        ]

        for extension
        in extensions

        if extension[
            "type_name"
        ].startswith(
            "Unknown("
        )

    ]


    if (

        unknown_extensions

        and

        CHECK5_BLOCK_UNKNOWN_EXTENSIONS

    ):

        blocking_reasons.append(

            "Unknown Token-2022 "
            "extension(s): "

            + ", ".join(
                unknown_extensions
            )

        )


    # --------------------------------------------------------
    # NON-TRANSFERABLE
    # --------------------------------------------------------

    if (

        "NonTransferable"
        in
        extension_names

        and

        CHECK5_BLOCK_NON_TRANSFERABLE

    ):

        blocking_reasons.append(

            "Non-Transferable extension "
            "is enabled."

        )


    # --------------------------------------------------------
    # TRANSFER HOOK
    # --------------------------------------------------------

    for extension in extensions:

        if (
            extension[
                "type_name"
            ]
            !=
            "TransferHook"
        ):

            continue


        if "decode_error" in extension:

            blocking_reasons.append(

                "Transfer Hook "
                "could not be decoded."

            )

            continue


        hook_program = extension.get(
            "program_id"
        )


        hook_authority = extension.get(
            "authority"
        )


        if (

            CHECK5_BLOCK_TRANSFER_HOOK

            and

            (

                hook_program
                is not None

                or

                hook_authority
                is not None

            )

        ):

            blocking_reasons.append(

                "Active Transfer Hook detected."

            )


            if hook_program:

                warnings.append(

                    "Transfer Hook program: "
                    f"{hook_program}"

                )


    # --------------------------------------------------------
    # PERMANENT DELEGATE
    # --------------------------------------------------------

    for extension in extensions:

        if (
            extension[
                "type_name"
            ]
            !=
            "PermanentDelegate"
        ):

            continue


        if "decode_error" in extension:

            blocking_reasons.append(

                "Permanent Delegate "
                "could not be decoded."

            )

            continue


        delegate = extension.get(
            "delegate"
        )


        if (

            CHECK5_BLOCK_PERMANENT_DELEGATE

            and

            delegate
            is not None

        ):

            blocking_reasons.append(

                "Permanent Delegate is active."

            )


            warnings.append(

                "Permanent Delegate: "
                f"{delegate}"

            )


    # --------------------------------------------------------
    # DEFAULT ACCOUNT STATE
    # --------------------------------------------------------

    for extension in extensions:

        if (
            extension[
                "type_name"
            ]
            !=
            "DefaultAccountState"
        ):

            continue


        if "decode_error" in extension:

            blocking_reasons.append(

                "Default Account State "
                "could not be decoded."

            )

            continue


        state = extension.get(
            "state"
        )


        if (

            state == 2

            and

            CHECK5_BLOCK_FROZEN_DEFAULT_STATE

        ):

            blocking_reasons.append(

                "Default Account State "
                "is FROZEN."

            )


        elif state == 1:

            warnings.append(

                "Default Account State "
                "is Initialized."

            )


        elif state == 0:

            warnings.append(

                "Default Account State "
                "is Uninitialized."

            )


        else:

            blocking_reasons.append(

                "Unknown Default Account State: "
                f"{state}"

            )


    # --------------------------------------------------------
    # PAUSABLE
    # --------------------------------------------------------

    for extension in extensions:

        if (
            extension[
                "type_name"
            ]
            !=
            "Pausable"
        ):

            continue


        if "decode_error" in extension:

            blocking_reasons.append(

                "Pausable extension "
                "could not be decoded."

            )

            continue


        authority = extension.get(
            "authority"
        )


        paused = extension.get(
            "paused",
            False
        )


        if (

            CHECK5_BLOCK_PAUSABLE

            and

            (

                authority
                is not None

                or

                paused

            )

        ):

            if paused:

                blocking_reasons.append(

                    "Token is currently PAUSED."

                )

            else:

                blocking_reasons.append(

                    "Pausable authority is active."

                )


    # --------------------------------------------------------
    # PERMISSIONED BURN
    # --------------------------------------------------------

    for extension in extensions:

        if (
            extension[
                "type_name"
            ]
            !=
            "PermissionedBurn"
        ):

            continue


        if "decode_error" in extension:

            blocking_reasons.append(

                "Permissioned Burn "
                "could not be decoded."

            )

            continue


        authority = extension.get(
            "authority"
        )


        if (

            CHECK5_BLOCK_PERMISSIONED_BURN

            and

            authority
            is not None

        ):

            blocking_reasons.append(

                "Permissioned Burn authority "
                "is active."

            )


    # --------------------------------------------------------
    # TRANSFER FEE
    # --------------------------------------------------------

    for extension in extensions:

        if (
            extension[
                "type_name"
            ]
            !=
            "TransferFeeConfig"
        ):

            continue


        if "decode_error" in extension:

            blocking_reasons.append(

                "Transfer Fee Config "
                "could not be decoded."

            )

            continue


        fee_authority = extension.get(

            "transfer_fee_config_authority"

        )


        older_bps = extension.get(
            "older_bps",
            0
        )


        newer_bps = extension.get(
            "newer_bps",
            0
        )


        highest_bps = max(

            older_bps,

            newer_bps,

        )


        # If this authority is still active,
        # the configured transfer fee can be changed.
        if (

            CHECK5_BLOCK_ACTIVE_FEE_AUTHORITY

            and

            fee_authority
            is not None

        ):

            blocking_reasons.append(

                "Transfer-fee authority "
                "is active."

            )


        # Block excessive configured fees.
        if (

            highest_bps
            >
            CHECK5_MAX_TRANSFER_FEE_BPS

        ):

            blocking_reasons.append(

                "Transfer fee exceeds "
                f"{CHECK5_MAX_TRANSFER_FEE_BPS / 100:.2f}%: "

                f"older={older_bps / 100:.2f}%, "

                f"newer={newer_bps / 100:.2f}%"

            )


        elif highest_bps > 0:

            warnings.append(

                "Transfer fee configured: "

                f"older={older_bps / 100:.2f}%, "

                f"newer={newer_bps / 100:.2f}%"

            )


    # --------------------------------------------------------
    # MINT CLOSE AUTHORITY
    # --------------------------------------------------------
    #
    # Warning only because this is not itself a transfer
    # restriction.
    # --------------------------------------------------------

    for extension in extensions:

        if (
            extension[
                "type_name"
            ]
            !=
            "MintCloseAuthority"
        ):

            continue


        if "decode_error" in extension:

            continue


        authority = extension.get(
            "authority"
        )


        if authority is not None:

            warnings.append(

                "Mint Close Authority "
                "is active."

            )


    # --------------------------------------------------------
    # FINAL CHECK 5 RESULT
    # --------------------------------------------------------

    if blocking_reasons:

        return {

            "mint":
                mint,

            "program":
                "Token-2022",

            "passes":
                False,

            "status":
                (
                    "🔴 BLOCK — "
                    "TRANSFER CONTROL RISK"
                ),

            "risk_score":
                100.0,

            "extension_names":
                extension_names,

            "extensions":
                extensions,

            "blocking_reasons":
                blocking_reasons,

            "warnings":
                warnings,
        }


    if warnings:

        return {

            "mint":
                mint,

            "program":
                "Token-2022",

            "passes":
                True,

            "status":
                (
                    "🟡 PASS — "
                    "TOKEN-2022 WITH WARNINGS"
                ),

            "risk_score":
                20.0,

            "extension_names":
                extension_names,

            "extensions":
                extensions,

            "blocking_reasons":
                [],

            "warnings":
                warnings,
        }


    return {

        "mint":
            mint,

        "program":
            "Token-2022",

        "passes":
            True,

        "status":
            (
                "✅ PASS — "
                "NO BLOCKING TRANSFER CONTROLS"
            ),

        "risk_score":
            0.0,

        "extension_names":
            extension_names,

        "extensions":
            extensions,

        "blocking_reasons":
            [],

        "warnings":
            [],
    }

def check6_rpc(
    method,
    params,
):

    if "rpc" not in globals():

        raise RuntimeError(
            "Existing Solana RPC client `rpc` was not found."
        )

    time.sleep(
        CHECK6_RPC_DELAY
    )

    return rpc._call(
        method,
        params,
    )

def check6_get_account_key(
    account_key,
):

    if isinstance(
        account_key,
        dict,
    ):

        return str(
            account_key.get(
                "pubkey"
            )
            or
            ""
        )

    return str(
        account_key
        or
        ""
    )

def check6_get_signers(
    transaction,
):

    message = (
        transaction
        .get(
            "transaction",
            {}
        )
        .get(
            "message",
            {}
        )
    )

    account_keys = (
        message
        .get(
            "accountKeys"
        )
        or
        []
    )

    signers = []

    for key in account_keys:

        if isinstance(
            key,
            dict,
        ):

            if key.get(
                "signer"
            ) is True:

                pubkey = key.get(
                    "pubkey"
                )

                if pubkey:

                    signers.append(
                        str(
                            pubkey
                        )
                    )

        else:

            if key:

                signers.append(
                    str(key)
                )

    return signers

def check6_get_token_deltas(
    transaction,
    mint,
):

    meta = (
        transaction
        .get(
            "meta"
        )
        or
        {}
    )

    pre_balances = (
        meta
        .get(
            "preTokenBalances"
        )
        or
        []
    )

    post_balances = (
        meta
        .get(
            "postTokenBalances"
        )
        or
        []
    )

    message = (
        transaction
        .get(
            "transaction",
            {}
        )
        .get(
            "message",
            {}
        )
    )

    account_keys = (
        message
        .get(
            "accountKeys"
        )
        or
        []
    )


    def get_owner(
        balance_entry,
    ):

        owner = balance_entry.get(
            "owner"
        )

        if owner:

            return str(
                owner
            )

        account_index = (
            balance_entry
            .get(
                "accountIndex"
            )
        )

        if account_index is None:

            return None

        if (
            account_index
            >=
            len(account_keys)
        ):

            return None

        return check6_get_account_key(
            account_keys[
                account_index
            ]
        )


    pre = {}

    for entry in pre_balances:

        if entry.get(
            "mint"
        ) != mint:

            continue

        owner = get_owner(
            entry
        )

        if not owner:

            continue

        amount = (
            entry
            .get(
                "uiTokenAmount",
                {}
            )
            .get(
                "amount"
            )
        )

        if amount is None:

            continue

        pre[owner] = (
            pre.get(
                owner,
                0
            )
            +
            int(amount)
        )


    post = {}

    for entry in post_balances:

        if entry.get(
            "mint"
        ) != mint:

            continue

        owner = get_owner(
            entry
        )

        if not owner:

            continue

        amount = (
            entry
            .get(
                "uiTokenAmount",
                {}
            )
            .get(
                "amount"
            )
        )

        if amount is None:

            continue

        post[owner] = (
            post.get(
                owner,
                0
            )
            +
            int(amount)
        )


    owners = (
        set(pre)
        |
        set(post)
    )

    deltas = {}

    for owner in owners:

        delta = (
            post.get(
                owner,
                0
            )
            -
            pre.get(
                owner,
                0
            )
        )

        if delta != 0:

            deltas[
                owner
            ] = delta

    return deltas

def check6_get_oldest_successful_signatures(
    address,
    max_pages=CHECK6_MAX_HISTORY_PAGES,
):

    all_successful = []

    before = None


    for _ in range(
        max_pages
    ):

        config = {

            "limit":
                CHECK6_HISTORY_PAGE_SIZE,

            "commitment":
                "finalized",
        }


        if before:

            config[
                "before"
            ] = before


        batch = check6_rpc(

            "getSignaturesForAddress",

            [
                address,
                config,
            ],

        )


        if not batch:

            break


        for item in batch:

            if item.get(
                "err"
            ) is None:

                all_successful.append(
                    item
                )


        before = (
            batch[
                -1
            ]
            .get(
                "signature"
            )
        )


        if len(batch) < CHECK6_HISTORY_PAGE_SIZE:

            break


    all_successful.sort(

        key=lambda item: (
            item.get(
                "slot",
                0
            ),
            item.get(
                "blockTime"
            )
            or
            0,
        )

    )


    return all_successful

def check6_get_transaction(
    signature,
):

    return check6_rpc(

        "getTransaction",

        [
            signature,

            {
                "encoding":
                    "jsonParsed",

                "commitment":
                    "finalized",

                "maxSupportedTransactionVersion":
                    0,
            },
        ],

    )

def check6_find_creation_data(
    mint,
):

    signatures = (
        check6_get_oldest_successful_signatures(
            mint
        )
    )


    if not signatures:

        raise RuntimeError(
            "No successful history was found "
            "for the mint account."
        )


    creation_signature = (
        signatures[
            0
        ][
            "signature"
        ]
    )


    creation_tx = (
        check6_get_transaction(
            creation_signature
        )
    )


    if creation_tx is None:

        raise RuntimeError(
            "The earliest mint transaction "
            "could not be loaded."
        )


    signers = (
        check6_get_signers(
            creation_tx
        )
    )


    if not signers:

        raise RuntimeError(
            "The mint creation transaction "
            "contained no identifiable signer."
        )


    # First signer is used as the creator candidate.
    creator = signers[0]


    distribution_signature = None
    initial_deltas = {}


    # Search the earliest mint-account transactions
    # for a positive token balance change.
    for item in signatures[:20]:

        signature = item[
            "signature"
        ]


        if signature == creation_signature:

            tx = creation_tx

        else:

            tx = (
                check6_get_transaction(
                    signature
                )
            )


        if tx is None:

            continue


        deltas = (
            check6_get_token_deltas(
                tx,
                mint
            )
        )


        positive = {

            owner: amount

            for owner, amount
            in deltas.items()

            if amount > 0

        }


        if positive:

            distribution_signature = (
                signature
            )

            initial_deltas = positive

            break


    return {

        "creator":
            creator,

        "creator_signers":
            signers,

        "creation_signature":
            creation_signature,

        "distribution_signature":
            distribution_signature,

        "initial_deltas":
            initial_deltas,

        "history_complete":
            len(signatures)
            <
            CHECK6_HISTORY_PAGE_SIZE,
    }

def check6_get_supply(
    mint,
):

    result = check6_rpc(

        "getTokenSupply",

        [
            mint,

            {
                "commitment":
                    "finalized"
            },
        ],

    )


    value = (

        result.get(
            "value"
        )

        if isinstance(
            result,
            dict
        )

        else
        None

    )


    if not value:

        raise RuntimeError(
            "On-chain token supply was unavailable."
        )


    amount = value.get(
        "amount"
    )

    decimals = value.get(
        "decimals"
    )


    if (
        amount is None
        or
        decimals is None
    ):

        raise RuntimeError(
            "Token supply response was incomplete."
        )


    return {

        "raw":
            int(amount),

        "decimals":
            int(decimals),

        "ui":
            float(
                value.get(
                    "uiAmount"
                )
                or
                0.0
            ),
    }

def check6_get_current_owner_balances(
    mint,
):

    fetch_fn = globals().get(
        "fetch_all_token_accounts"
    )

    aggregate_fn = globals().get(
        "aggregate_by_owner"
    )


    if not callable(
        fetch_fn
    ):

        raise RuntimeError(
            "Check 2 function "
            "`fetch_all_token_accounts` "
            "was not found."
        )


    if not callable(
        aggregate_fn
    ):

        raise RuntimeError(
            "Check 2 function "
            "`aggregate_by_owner` "
            "was not found."
        )


    token_accounts = (
        fetch_fn(
            mint
        )
    )


    owner_balances, _, _ = (
        aggregate_fn(
            token_accounts
        )
    )


    if not owner_balances:

        raise RuntimeError(
            "No current owner balances "
            "were returned."
        )


    return owner_balances

def check6_classify_early_wallets(
    mint,
    owner_balances,
):

    classify_fn = globals().get(
        "classify_owners"
    )

    pair_fn = globals().get(
        "get_dex_pair_addresses"
    )


    if not callable(
        classify_fn
    ):

        return {}


    pair_addresses = set()


    if callable(
        pair_fn
    ):

        try:

            pair_addresses = set(
                pair_fn(
                    mint
                )
            )

        except Exception:

            pair_addresses = set()


    try:

        return classify_fn(

            owner_balances,

            pair_addresses,

        )

    except Exception:

        return {}

def check6_find_funding_source(
    wallet,
):

    signatures = (
        check6_get_oldest_successful_signatures(

            wallet,

            max_pages=1,

        )
    )


    if not signatures:

        return {

            "source":
                None,

            "complete":
                False,
        }


    oldest = signatures[
        0
    ]


    transaction = (
        check6_get_transaction(

            oldest[
                "signature"
            ]

        )
    )


    if transaction is None:

        return {

            "source":
                None,

            "complete":
                False,
        }


    message = (
        transaction
        .get(
            "transaction",
            {}
        )
        .get(
            "message",
            {}
        )
    )


    instructions = (
        message
        .get(
            "instructions"
        )
        or
        []
    )


    for instruction in instructions:

        if not isinstance(
            instruction,
            dict,
        ):

            continue


        if instruction.get(
            "program"
        ) != "system":

            continue


        parsed = instruction.get(
            "parsed"
        )


        if not isinstance(
            parsed,
            dict,
        ):

            continue


        if parsed.get(
            "type"
        ) != "transfer":

            continue


        info = (
            parsed
            .get(
                "info"
            )
            or
            {}
        )


        source = info.get(
            "source"
        )

        destination = info.get(
            "destination"
        )


        if (

            source

            and

            destination == wallet

        ):

            return {

                "source":
                    str(source),

                "complete":
                    True,
            }


    return {

        "source":
            None,

        "complete":
            len(signatures)
            <
            CHECK6_HISTORY_PAGE_SIZE,
    }

def check6_scan_recent_outflow(
    wallet,
    mint,
    supply_raw,
):

    signatures = check6_rpc(

        "getSignaturesForAddress",

        [
            wallet,

            {
                "limit":
                    CHECK6_RECENT_SIGNATURE_LIMIT,

                "commitment":
                    "finalized",
            },
        ],

    )


    observed_outflow_raw = 0
    outflow_transactions = 0
    checked_transactions = 0


    for item in signatures or []:

        if item.get(
            "err"
        ) is not None:

            continue


        signature = item.get(
            "signature"
        )


        if not signature:

            continue


        transaction = (
            check6_get_transaction(
                signature
            )
        )


        if transaction is None:

            continue


        checked_transactions += 1


        deltas = (
            check6_get_token_deltas(
                transaction,
                mint
            )
        )


        wallet_delta = deltas.get(
            wallet,
            0
        )


        if wallet_delta < 0:

            observed_outflow_raw += (
                abs(
                    wallet_delta
                )
            )

            outflow_transactions += 1


    outflow_pct = (

        observed_outflow_raw
        /
        supply_raw
        *
        100.0

        if supply_raw > 0

        else
        None

    )


    return {

        "wallet":
            wallet,

        "outflow_raw":
            observed_outflow_raw,

        "outflow_pct":
            outflow_pct,

        "outflow_transactions":
            outflow_transactions,

        "checked_transactions":
            checked_transactions,
    }

def check6_analyze_token(
    token,
):

    mint = str(

        getattr(
            token,
            "mint",
            "",
        )
        or
        ""

    ).strip()


    if not mint:

        return {

            "mint":
                "",

            "passes":
                False,

            "status":
                "🔴 BLOCK — MISSING MINT",

            "risk_score":
                100.0,

            "creator":
                None,

            "creator_pct":
                None,

            "initial_recipients":
                [],

            "linked_pct":
                None,

            "common_funders":
                [],

            "outflows":
                [],

            "blocking_reasons":
                [
                    "Mint address is missing."
                ],

            "warnings":
                [],

        }


    supply = (
        check6_get_supply(
            mint
        )
    )


    creation = (
        check6_find_creation_data(
            mint
        )
    )


    creator = creation[
        "creator"
    ]


    owner_balances = (
        check6_get_current_owner_balances(
            mint
        )
    )


    creator_raw = int(
        owner_balances.get(
            creator,
            0
        )
    )


    creator_pct = (

        creator_raw
        /
        supply[
            "raw"
        ]
        *
        100.0

        if supply[
            "raw"
        ] > 0

        else
        None

    )


    classifications = (
        check6_classify_early_wallets(

            mint,

            owner_balances,

        )
    )


    # --------------------------------------------------------
    # INITIAL RECIPIENTS
    # --------------------------------------------------------

    initial_recipients = []


    sorted_initial = sorted(

        creation[
            "initial_deltas"
        ].items(),

        key=lambda item: item[1],

        reverse=True,

    )


    for wallet, initial_raw in (
        sorted_initial[
            :CHECK6_MAX_EARLY_WALLETS
        ]
    ):

        current_raw = int(
            owner_balances.get(
                wallet,
                0
            )
        )


        initial_pct = (

            initial_raw
            /
            supply[
                "raw"
            ]
            *
            100.0

            if supply[
                "raw"
            ] > 0

            else
            0.0

        )


        current_pct = (

            current_raw
            /
            supply[
                "raw"
            ]
            *
            100.0

            if supply[
                "raw"
            ] > 0

            else
            0.0

        )


        kind = (

            classifications
            .get(
                wallet,
                {}
            )
            .get(
                "kind",
                "unknown"
            )

        )


        initial_recipients.append({

            "wallet":
                wallet,

            "initial_raw":
                int(
                    initial_raw
                ),

            "initial_pct":
                initial_pct,

            "current_raw":
                current_raw,

            "current_pct":
                current_pct,

            "kind":
                kind,

        })


    # --------------------------------------------------------
    # CREATOR + IDENTIFIED EARLY WALLETS
    # --------------------------------------------------------

    insider_wallets = []


    insider_wallets.append({

        "wallet":
            creator,

        "role":
            "creator",

        "current_raw":
            creator_raw,

        "current_pct":
            creator_pct,

    })


    for item in initial_recipients:

        wallet = item[
            "wallet"
        ]


        if wallet == creator:

            continue


        if item[
            "kind"
        ] in {

            "dex_pool",
            "burn",
            "program",
            "contract_controlled",

        }:

            continue


        insider_wallets.append({

            "wallet":
                wallet,

            "role":
                "initial_recipient",

            "current_raw":
                item[
                    "current_raw"
                ],

            "current_pct":
                item[
                    "current_pct"
                ],

        })


    linked_total_raw = sum(

        item[
            "current_raw"
        ]

        for item
        in insider_wallets

    )


    linked_total_pct = (

        linked_total_raw
        /
        supply[
            "raw"
        ]
        *
        100.0

        if supply[
            "raw"
        ] > 0

        else
        None

    )


    # --------------------------------------------------------
    # COMMON FUNDING
    # --------------------------------------------------------

    funding_sources = defaultdict(
        list
    )


    for item in initial_recipients:

        wallet = item[
            "wallet"
        ]


        if wallet == creator:

            continue


        if item[
            "kind"
        ] in {

            "dex_pool",
            "burn",
            "program",
            "contract_controlled",

        }:

            continue


        funding = (
            check6_find_funding_source(
                wallet
            )
        )


        source = funding.get(
            "source"
        )


        if source:

            funding_sources[
                source
            ].append(
                item
            )


    common_funders = []


    for source, members in (
        funding_sources.items()
    ):

        if len(members) < 2:

            continue


        combined_raw = sum(

            member[
                "current_raw"
            ]

            for member
            in members

        )


        combined_pct = (

            combined_raw
            /
            supply[
                "raw"
            ]
            *
            100.0

            if supply[
                "raw"
            ] > 0

            else
            None

        )


        common_funders.append({

            "source":
                source,

            "wallets":
                [
                    member[
                        "wallet"
                    ]

                    for member
                    in members
                ],

            "wallet_count":
                len(
                    members
                ),

            "current_pct":
                combined_pct,

        })


    # --------------------------------------------------------
    # RECENT OUTFLOWS
    # --------------------------------------------------------

    wallets_to_scan = [
        creator
    ]


    for item in initial_recipients:

        if len(
            wallets_to_scan
        ) >= (
            1
            +
            CHECK6_MAX_OUTFLOW_SCAN_WALLETS
        ):

            break


        wallet = item[
            "wallet"
        ]


        if wallet == creator:

            continue


        if item[
            "kind"
        ] in {

            "dex_pool",
            "burn",
            "program",
            "contract_controlled",

        }:

            continue


        wallets_to_scan.append(
            wallet
        )


    outflows = []


    for wallet in wallets_to_scan:

        outflow = (
            check6_scan_recent_outflow(

                wallet,

                mint,

                supply[
                    "raw"
                ],

            )
        )


        outflows.append(
            outflow
        )


    # --------------------------------------------------------
    # DECISION
    # --------------------------------------------------------

    blocking_reasons = []
    warnings = []


    if (
        creation[
            "distribution_signature"
        ]
        is
        None
    ):

        blocking_reasons.append(

            "No initial token-distribution "
            "transaction could be identified."

        )


    if creator_pct is None:

        blocking_reasons.append(

            "Creator ownership percentage "
            "could not be calculated."

        )


    elif (
        creator_pct
        >
        CHECK6_CREATOR_BLOCK_PCT
    ):

        blocking_reasons.append(

            f"Creator currently holds "
            f"{creator_pct:.2f}% of supply, "
            f"above the "
            f"{CHECK6_CREATOR_BLOCK_PCT:.2f}% "
            "block limit."

        )


    elif (
        creator_pct
        >=
        CHECK6_CREATOR_WARN_PCT
    ):

        warnings.append(

            f"Creator currently holds "
            f"{creator_pct:.2f}% of supply."

        )


    if (
        linked_total_pct is not None
        and
        linked_total_pct
        >
        CHECK6_CREATOR_LINKED_BLOCK_PCT
    ):

        blocking_reasons.append(

            f"Creator + identified early wallets "
            f"control {linked_total_pct:.2f}% "
            f"of supply, above the "
            f"{CHECK6_CREATOR_LINKED_BLOCK_PCT:.2f}% "
            "block limit."

        )


    for group in common_funders:

        if (
            group[
                "current_pct"
            ]
            is not None

            and

            group[
                "current_pct"
            ]
            >
            CHECK6_COMMON_FUNDER_BLOCK_PCT

        ):

            blocking_reasons.append(

                f"{group['wallet_count']} early "
                "wallets share a common funding "
                "source and currently control "
                f"{group['current_pct']:.2f}% "
                "of supply."

            )

        else:

            warnings.append(

                f"{group['wallet_count']} early "
                "wallets share a common "
                "funding source."

            )


    for outflow in outflows:

        pct = outflow.get(
            "outflow_pct"
        )


        if pct is None:

            continue


        if (
            pct
            >
            CHECK6_LARGE_OUTFLOW_BLOCK_PCT
        ):

            blocking_reasons.append(

                f"Large recent token outflow "
                f"observed from "
                f"{outflow['wallet']}: "
                f"{pct:.2f}% of supply "
                "(this does not by itself prove "
                "a sale)."

            )

        elif (
            pct
            >=
            CHECK6_OUTFLOW_WARN_PCT
        ):

            warnings.append(

                f"Recent token outflow observed "
                f"from {outflow['wallet']}: "
                f"{pct:.2f}% of supply."

            )


    if blocking_reasons:

        return {

            "mint":
                mint,

            "passes":
                False,

            "status":
                (
                    "🔴 BLOCK — "
                    "CREATOR / INSIDER RISK"
                ),

            "risk_score":
                100.0,

            "creator":
                creator,

            "creator_pct":
                creator_pct,

            "creation_signature":
                creation[
                    "creation_signature"
                ],

            "distribution_signature":
                creation[
                    "distribution_signature"
                ],

            "history_complete":
                creation[
                    "history_complete"
                ],

            "initial_recipients":
                initial_recipients,

            "linked_pct":
                linked_total_pct,

            "common_funders":
                common_funders,

            "outflows":
                outflows,

            "blocking_reasons":
                blocking_reasons,

            "warnings":
                warnings,

        }


    if warnings:

        return {

            "mint":
                mint,

            "passes":
                True,

            "status":
                (
                    "🟡 PASS — "
                    "CREATOR / INSIDER WARNINGS"
                ),

            "risk_score":
                20.0,

            "creator":
                creator,

            "creator_pct":
                creator_pct,

            "creation_signature":
                creation[
                    "creation_signature"
                ],

            "distribution_signature":
                creation[
                    "distribution_signature"
                ],

            "history_complete":
                creation[
                    "history_complete"
                ],

            "initial_recipients":
                initial_recipients,

            "linked_pct":
                linked_total_pct,

            "common_funders":
                common_funders,

            "outflows":
                outflows,

            "blocking_reasons":
                [],

            "warnings":
                warnings,

        }


    return {

        "mint":
            mint,

        "passes":
            True,

        "status":
            (
                "✅ PASS — "
                "NO MAJOR CREATOR / INSIDER FLAGS"
            ),

        "risk_score":
            0.0,

        "creator":
            creator,

        "creator_pct":
            creator_pct,

        "creation_signature":
            creation[
                "creation_signature"
            ],

        "distribution_signature":
            creation[
                "distribution_signature"
            ],

        "history_complete":
            creation[
                "history_complete"
            ],

        "initial_recipients":
            initial_recipients,

        "linked_pct":
            linked_total_pct,

        "common_funders":
            common_funders,

        "outflows":
            outflows,

        "blocking_reasons":
            [],

        "warnings":
            [],

    }

# ---------------------------------------------------------------------------
# Runtime configuration
# ---------------------------------------------------------------------------

rpc = None
helius = None
HELIUS_API_KEY = None
HELIUS_RPC_URL = None


def configure_check_environment(
    rpc_client,
    helius_api_key: Optional[str] = None,
) -> None:
    """Configure shared RPC/Helius state used by the extracted checks."""
    global rpc, helius, HELIUS_API_KEY, HELIUS_RPC_URL

    rpc = rpc_client
    HELIUS_API_KEY = (helius_api_key or "").strip() or None

    if HELIUS_API_KEY:
        HELIUS_RPC_URL = (
            "https://mainnet.helius-rpc.com/"
            f"?api-key={HELIUS_API_KEY}"
        )
        helius = HeliusRPC(HELIUS_RPC_URL)
    else:
        HELIUS_RPC_URL = None
        helius = None


def _require_environment() -> None:
    if rpc is None:
        raise RuntimeError("Solana RPC client is not configured.")
    if helius is None:
        raise RuntimeError(
            "Helius is not configured. Supply HELIUS_API_KEY to "
            "configure_check_environment()."
        )


def run_check1(results):
    """Run Check 1 and return its result records."""
    _require_environment()
    return check_mint_authorities_batch(rpc, results)


def run_check2(results):
    """Run Check 2 and return holder-concentration records."""
    _require_environment()
    holder_checks = []

    for token in results:
        symbol = str(getattr(token, "symbol", "?") or "?").upper()
        name = str(getattr(token, "name", "Unknown") or "Unknown")
        mint = str(getattr(token, "mint", "") or "").strip()
        liquidity_usd = getattr(token, "liquidity_usd", None)

        check = {
            "symbol": symbol,
            "name": name,
            "mint": mint,
            "status": "⚪ UNKNOWN — DO NOT MARK SAFE",
            "risk_score": None,
            "holder_provider": "Helius",
            "method": "getTokenAccounts",
            "raw": {},
            "adjusted": {},
            "excluded_owners": [],
            "unknown_owners": [],
            "pair_addresses": [],
        }

        try:
            if not mint:
                raise ValueError("Token mint is empty.")

            token_accounts = fetch_all_token_accounts(mint)
            owner_balances, nonzero_accounts, skipped = aggregate_by_owner(
                token_accounts
            )
            raw = concentration_stats(owner_balances)
            if raw is None:
                raise RuntimeError(
                    "Helius returned no usable nonzero owner balances."
                )

            pair_addresses = get_dex_pair_addresses(mint)

            excluded_kinds = {
                "burn",
                "dex_pool",
                "program",
                "contract_controlled",
            }

            classifications = classify_owners(
                owner_balances,
                pair_addresses,
            )

            adjusted_balances = {
                owner: amount
                for owner, amount in owner_balances.items()
                if classifications.get(owner, {}).get("kind") not in excluded_kinds
            }

            unknown_owners = {
                owner: amount
                for owner, amount in owner_balances.items()
                if classifications.get(owner, {}).get("kind") == "unknown"
            }

            excluded_owners = []
            for owner, amount in sorted(
                owner_balances.items(),
                key=lambda item: item[1],
                reverse=True,
            ):
                info = classifications.get(owner, {})
                if info.get("kind") in excluded_kinds:
                    excluded_owners.append({
                        "owner": owner,
                        "amount": amount,
                        "kind": info.get("kind"),
                        "reason": info.get("reason"),
                        "program_owner": info.get("program_owner"),
                    })

            adjusted = concentration_stats(adjusted_balances)
            if adjusted is None:
                status = "⚪ NO NON-EXCLUDED OWNERS"
                risk_score = None
                adjusted = {
                    "top_1_pct": None,
                    "top_5_pct": None,
                    "top_10_pct": None,
                    "owner_count": 0,
                    "total": 0,
                }
            else:
                risk_score, status = holder_status(
                    adjusted["top_1_pct"],
                    adjusted["top_5_pct"],
                    adjusted["top_10_pct"],
                )

            check["raw"] = {
                "top_1_pct": raw["top_1_pct"],
                "top_5_pct": raw["top_5_pct"],
                "top_10_pct": raw["top_10_pct"],
                "owner_count": raw["owner_count"],
                "total_amount": raw["total"],
            }
            check["adjusted"] = {
                "top_1_pct": adjusted["top_1_pct"],
                "top_5_pct": adjusted["top_5_pct"],
                "top_10_pct": adjusted["top_10_pct"],
                "owner_count": adjusted["owner_count"],
                "total_amount": adjusted["total"],
            }
            check["status"] = status
            check["risk_score"] = risk_score
            check["excluded_owners"] = excluded_owners
            check["unknown_owners"] = [
                {
                    "owner": owner,
                    "amount": amount,
                    "pct_raw": pct(amount, raw["total"]),
                }
                for owner, amount in sorted(
                    unknown_owners.items(),
                    key=lambda item: item[1],
                    reverse=True,
                )
            ]
            check["pair_addresses"] = sorted(pair_addresses)
            check["token_accounts_returned"] = len(token_accounts)
            check["nonzero_token_accounts"] = nonzero_accounts
            check["skipped_without_amount"] = skipped
            check["classification_counts"] = {}
            for info in classifications.values():
                kind = info.get("kind", "unknown")
                check["classification_counts"][kind] = (
                    check["classification_counts"].get(kind, 0) + 1
                )

        except Exception as exc:
            check["status"] = "❌ UNKNOWN / RPC ERROR"
            check["risk_score"] = None
            check["error"] = str(exc)

        holder_checks.append(check)

    return holder_checks


def run_check3(results):
    """Run Check 3 and attach LP-lock fields to each TokenRisk record."""
    checks = []

    for token in results:
        token.is_safe = False
        check = analyze_lp_ownership(token)
        checks.append(check)

        token.lp_check_status = check["status"]
        token.lp_risk_score = check["risk_score"]
        token.lp_locked_pct = check["lp_locked_pct"]
        token.lp_unlocked_pct = check["lp_unlocked_pct"]
        token.lp_lock_duration_days = check["lock_duration_days"]
        token.lp_permanent_lock = check["permanent_lock"]
        token.lp_market_type = check["market_type"]
        token.lp_market_pubkey = check["market_pubkey"]
        token.lp_pair_verified = check["pair_match"]
        token.lp_locked_usd = check["locked_usd"]
        token.lp_locker_count = check["locker_count"]

    return checks


def run_check4(results, holder_checks):
    """Run Check 4 and attach supply/distribution fields."""
    rpc_url = check4_get_rpc_url()
    if not rpc_url:
        raise RuntimeError(
            "No Solana RPC URL is configured for Check 4."
        )

    checks = []
    for result in results:
        mint = str(getattr(result, "mint", "") or "").strip()
        symbol = str(getattr(result, "symbol", "") or "")
        name = str(getattr(result, "name", "") or "")
        try:
            check = check4_run_one(result, holder_checks, rpc_url)
        except Exception as exc:
            check = {
                "mint": mint,
                "symbol": symbol,
                "name": name,
                "source": (
                    "Solana getTokenSupply + "
                    "Check 2 Helius holder analysis"
                ),
                "status": "🔴 CHECK ERROR",
                "risk_score": 100.0,
                "passes": False,
                "onchain_supply": None,
                "onchain_supply_raw": None,
                "supply_decimals": None,
                "holder_total_raw": None,
                "holder_coverage_pct": None,
                "supply_difference_raw": None,
                "supply_difference_pct": None,
                "adjusted_top_1_pct": None,
                "adjusted_top_5_pct": None,
                "adjusted_top_10_pct": None,
                "supply_reconciles": False,
                "distribution_available": False,
                "concentration_flags": [],
                "findings": [
                    "Check 4 failed with an unexpected error."
                ],
                "error": str(exc),
            }

        checks.append(check)

        result.check4_status = check.get("status")
        result.check4_risk_score = check.get("risk_score")
        result.check4_passes = bool(check.get("passes"))
        result.check4_supply = check.get("onchain_supply")
        result.check4_supply_difference_pct = check.get(
            "supply_difference_pct"
        )
        result.check4_top1_pct = check.get("adjusted_top_1_pct")
        result.check4_top5_pct = check.get("adjusted_top_5_pct")
        result.check4_top10_pct = check.get("adjusted_top_10_pct")

        if not check.get("passes"):
            result.is_safe = False
            for finding in check.get("findings", []):
                message = f"🛑 Check 4: {finding}"
                if message not in result.risks:
                    result.risks.append(message)

    return checks


def run_check5(results):
    """Run Check 5 and attach Token-2022 analysis."""
    checks = []

    for token in results:
        mint = str(getattr(token, "mint", "") or "").strip()
        try:
            check = check5_analyze_token(token)
        except Exception as exc:
            check = {
                "mint": mint,
                "program": "UNKNOWN",
                "passes": False,
                "status": "🔴 BLOCK — CHECK ERROR / UNKNOWN",
                "risk_score": 100.0,
                "extension_names": [],
                "extensions": [],
                "blocking_reasons": [f"Check 5 failed: {exc}"],
                "warnings": [],
            }

        checks.append(check)

        token.check5_status = check["status"]
        token.check5_passes = bool(check["passes"])
        token.check5_risk_score = check["risk_score"]
        token.check5_program = check["program"]
        token.check5_extensions = check["extension_names"]
        token.check5_blocking_reasons = check["blocking_reasons"]
        token.check5_warnings = check["warnings"]

        if not check["passes"]:
            token.is_safe = False
            for reason in check["blocking_reasons"]:
                message = f"🛑 Check 5: {reason}"
                if message not in token.risks:
                    token.risks.append(message)

    return checks


def run_check6(results, holder_checks):
    """Run Check 6 and attach creator/insider behavior analysis."""
    checks = []

    # check6 uses the shared rpc global.
    _require_environment()

    for token in results:
        mint = str(getattr(token, "mint", "") or "").strip()
        try:
            check = check6_analyze_token(token)
        except Exception as exc:
            check = {
                "mint": mint,
                "passes": False,
                "status": "🔴 BLOCK — CHECK ERROR / UNKNOWN",
                "risk_score": 100.0,
                "creator": None,
                "creator_pct": None,
                "creation_signature": None,
                "distribution_signature": None,
                "history_complete": False,
                "initial_recipients": [],
                "linked_pct": None,
                "common_funders": [],
                "outflows": [],
                "blocking_reasons": [f"Check 6 failed: {exc}"],
                "warnings": [],
            }

        checks.append(check)

        token.check6_status = check["status"]
        token.check6_passes = bool(check["passes"])
        token.check6_risk_score = check["risk_score"]
        token.check6_creator = check.get("creator")
        token.check6_creator_pct = check.get("creator_pct")
        token.check6_linked_pct = check.get("linked_pct")
        token.check6_initial_recipients = check.get("initial_recipients", [])
        token.check6_common_funders = check.get("common_funders", [])
        token.check6_outflows = check.get("outflows", [])
        token.check6_blocking_reasons = check.get("blocking_reasons", [])
        token.check6_warnings = check.get("warnings", [])

        if not check["passes"]:
            token.is_safe = False
            for reason in check["blocking_reasons"]:
                message = f"🛑 Check 6: {reason}"
                if message not in token.risks:
                    token.risks.append(message)

    return checks


def run_all_checks(results, rpc_client, helius_api_key):
    """Run Checks 1-6 in the same order used by the original notebook."""
    configure_check_environment(rpc_client, helius_api_key)

    authority_checks = run_check1(results)
    holder_checks = run_check2(results)
    lp_checks = run_check3(results)
    check4_results = run_check4(results, holder_checks)
    check5_results = run_check5(results)
    check6_results = run_check6(results, holder_checks)

    return {
        "authority_checks": authority_checks,
        "holder_checks": holder_checks,
        "lp_checks": lp_checks,
        "check4_results": check4_results,
        "check5_results": check5_results,
        "check6_results": check6_results,
    }
