"""Solana token discovery and initial market-risk screening.

This module contains the public-data scanner from the original Colab notebook.
It does not contain private keys, wallet signing, or live trade execution.
"""

from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

import requests


CONFIG = {
    "scan_limit": 15,
    "safe_only": True,
    "request_delay": 1.5,
    "max_retries": 2,
    "timeout": 15,
    "demo_fallback": True,
    "debug_http": True,
}

DEXSCREENER_BASE = "https://api.dexscreener.com"
GECKOTERMINAL_BASE = "https://api.geckoterminal.com/api/v2"

def utc_now():
    return datetime.now(timezone.utc)

def safe_float(value, default=0.0):
    """
    Safely convert a value to float.
    """

    try:
        if value is None or value == "":
            return default

        return float(value)

    except (TypeError, ValueError):
        return default

def parse_timestamp(value):
    """
    Accept:
    - Unix timestamp in seconds
    - Unix timestamp in milliseconds
    - ISO datetime string
    """

    if value is None or value == "":
        return None

    # Numeric timestamp
    if isinstance(value, (int, float)):

        ts = float(value)

        # Milliseconds -> seconds
        if ts > 10_000_000_000:
            ts /= 1000.0

        return ts

    # String timestamp
    if isinstance(value, str):

        value = value.strip()

        # Numeric string
        try:

            ts = float(value)

            if ts > 10_000_000_000:
                ts /= 1000.0

            return ts

        except ValueError:
            pass

        # ISO timestamp
        try:

            return datetime.fromisoformat(
                value.replace("Z", "+00:00")
            ).timestamp()

        except ValueError:

            return None

    return None

def format_liquidity(value):
    """
    Format liquidity safely.

    None -> N/A
    0    -> $0
    """

    if value is None:
        return "N/A"

    return f"${value:,.0f}"

class RobustHTTP:

    RETRYABLE_CODES = {
        408,
        425,
        429,
        500,
        502,
        503,
        504,
    }

    def __init__(
        self,
        request_delay=1.5,
        timeout=15,
        max_retries=2,
        debug=True
    ):

        import requests

        self.session = requests.Session()

        self.session.headers.update({
            "User-Agent": "SolanaMemeSniper/8.1",
            "Accept": "application/json",
        })

        self.request_delay = request_delay
        self.timeout = timeout
        self.max_retries = max_retries
        self.debug = debug

        self.last_request_time = 0.0

        self.logs = []

    def _wait(self):

        elapsed = (
            time.monotonic()
            - self.last_request_time
        )

        remaining = (
            self.request_delay
            - elapsed
        )

        if remaining > 0:
            time.sleep(remaining)

    def get_json(
        self,
        url,
        params=None,
        label="HTTP GET"
    ):

        import requests

        for attempt in range(
            self.max_retries + 1
        ):

            self._wait()

            start = time.monotonic()

            try:

                response = self.session.get(
                    url,
                    params=params,
                    timeout=self.timeout
                )

                self.last_request_time = (
                    time.monotonic()
                )

                elapsed = (
                    time.monotonic()
                    - start
                )

                log_entry = {
                    "time_utc":
                        utc_now().isoformat(),

                    "label":
                        label,

                    "url":
                        response.url,

                    "status_code":
                        response.status_code,

                    "elapsed_seconds":
                        round(elapsed, 3),

                    "attempt":
                        attempt + 1,
                }

                self.logs.append(
                    log_entry
                )

                # ------------------------------------------------
                # HTTP STATUS
                # ------------------------------------------------

                if self.debug:

                    print(
                        f"      HTTP "
                        f"{response.status_code} "
                        f"| {elapsed:.2f}s "
                        f"| {response.url}"
                    )

                # ------------------------------------------------
                # SUCCESS
                # ------------------------------------------------

                if 200 <= response.status_code < 300:

                    try:

                        return response.json()

                    except ValueError:

                        print(
                            "      ❌ Server returned "
                            "invalid JSON"
                        )

                        return None

                # ------------------------------------------------
                # ERROR BODY
                # ------------------------------------------------

                error_body = (
                    response.text[:300]
                    .replace("\n", " ")
                )

                print(
                    f"      ↳ Response: "
                    f"{error_body}"
                )

                # ------------------------------------------------
                # RETRY
                # ------------------------------------------------

                if (
                    response.status_code
                    in self.RETRYABLE_CODES
                    and
                    attempt < self.max_retries
                ):

                    retry_after = (
                        response.headers.get(
                            "Retry-After"
                        )
                    )

                    if retry_after:

                        try:

                            wait_seconds = max(
                                1.0,
                                float(retry_after)
                            )

                        except ValueError:

                            wait_seconds = (
                                2 ** attempt
                            )

                    else:

                        wait_seconds = (
                            (2 ** attempt)
                            + random.uniform(
                                0,
                                0.5
                            )
                        )

                    wait_seconds = min(
                        wait_seconds,
                        30
                    )

                    print(
                        f"      ⏳ Retry "
                        f"{attempt + 1}/"
                        f"{self.max_retries} "
                        f"in "
                        f"{wait_seconds:.1f}s..."
                    )

                    time.sleep(
                        wait_seconds
                    )

                    continue

                return None

            except requests.RequestException as e:

                self.last_request_time = (
                    time.monotonic()
                )

                elapsed = (
                    time.monotonic()
                    - start
                )

                self.logs.append({

                    "time_utc":
                        utc_now().isoformat(),

                    "label":
                        label,

                    "url":
                        url,

                    "status_code":
                        None,

                    "elapsed_seconds":
                        round(
                            elapsed,
                            3
                        ),

                    "attempt":
                        attempt + 1,

                    "error":
                        str(e),
                })

                print(
                    f"      ❌ Network error: {e}"
                )

                if attempt < self.max_retries:

                    wait_seconds = min(

                        (2 ** attempt)
                        + random.uniform(
                            0,
                            0.5
                        ),

                        30
                    )

                    print(
                        f"      ⏳ Retry "
                        f"{attempt + 1}/"
                        f"{self.max_retries} "
                        f"in "
                        f"{wait_seconds:.1f}s..."
                    )

                    time.sleep(
                        wait_seconds
                    )

                    continue

                return None

        return None

class TokenRisk:

    mint: str
    symbol: str
    name: str

    risk_score: float
    is_safe: bool

    risks: List[str]
    warnings: List[str]

    # IMPORTANT:
    # Liquidity can now be None when API data is unavailable.
    liquidity_usd: Optional[float]

    age_minutes: float

    source: str
    pair_address: str

    data_quality: str
    is_demo: bool

    url: str

    # Check 2 holder-verification fields.
    # The main scanner starts with holder_check_status="NOT_RUN",
    # so a token cannot be marked safe until Check 2 completes.
    holder_check_status: str = "NOT_RUN"
    holder_risk_score: Optional[float] = None

    def to_dict(self):

        return {

            "mint":
                self.mint,

            "symbol":
                self.symbol,

            "name":
                self.name,

            "risk_score":
                round(
                    self.risk_score,
                    1
                ),

            "is_safe":
                self.is_safe,

            "holder_check_status":
                self.holder_check_status,

            "holder_risk_score":
                self.holder_risk_score,

            "risks":
                self.risks,

            "warnings":
                self.warnings,

            "liquidity_usd":
                (
                    round(
                        self.liquidity_usd,
                        2
                    )
                    if self.liquidity_usd
                    is not None
                    else None
                ),

            "age_minutes":
                round(
                    self.age_minutes,
                    1
                ),

            "source":
                self.source,

            "pair_address":
                self.pair_address,

            "data_quality":
                self.data_quality,

            "is_demo":
                self.is_demo,

            "url":
                self.url,
        }

DEMO_TOKENS = [

    {
        "mint":
            "DemoMint111111111111111111111111111111111111",

        "symbol":
            "DEMO1",

        "name":
            "Demo Meme One",

        "liquidity":
            18500,

        "createdAt":
            120,
    },

    {
        "mint":
            "DemoMint222222222222222222222222222222222222",

        "symbol":
            "DEMO2",

        "name":
            "Demo Meme Two",

        "liquidity":
            620,

        "createdAt":
            18,
    },

    {
        "mint":
            "DemoMint333333333333333333333333333333333333",

        "symbol":
            "DEMO3",

        "name":
            "Demo Meme Three",

        "liquidity":
            45,

        "createdAt":
            3,
    },

    {
        "mint":
            "DemoMint444444444444444444444444444444444444",

        "symbol":
            "DEMO4",

        "name":
            "Demo Meme Four",

        "liquidity":
            3900,

        "createdAt":
            250,
    },
]

class SolanaSniper:

    def __init__(self, config=None):

        self.config = dict(
            config or CONFIG
        )

        self.http = RobustHTTP(

            request_delay=
                self.config[
                    "request_delay"
                ],

            timeout=
                self.config[
                    "timeout"
                ],

            max_retries=
                self.config[
                    "max_retries"
                ],

            debug=
                self.config[
                    "debug_http"
                ],
        )

        self.results = []

        self.source_used = None

        self.demo_mode = False


    # ========================================================
    # DEX SCREENER
    # ========================================================

    def fetch_dexscreener(self):

        print(
            "  🔹 DEX Screener..."
        )

        # ----------------------------------------------------
        # 1. Latest token profiles
        # ----------------------------------------------------

        profiles = self.http.get_json(

            f"{DEXSCREENER_BASE}/"
            "token-profiles/latest/v1",

            label=
                "DEX Screener profiles",
        )

        if not isinstance(
            profiles,
            list
        ):

            print(
                "    ❌ Profiles request failed\n"
            )

            return []

        # ----------------------------------------------------
        # 2. Keep Solana profiles
        # ----------------------------------------------------

        solana_profiles = []

        seen = set()

        for profile in profiles:

            chain = str(
                profile.get(
                    "chainId",
                    ""
                )
            ).lower()

            if chain != "solana":
                continue

            mint = profile.get(
                "tokenAddress"
            )

            if not mint:
                continue

            if mint in seen:
                continue

            seen.add(mint)

            solana_profiles.append(
                profile
            )

            if (
                len(solana_profiles)
                >= self.config[
                    "scan_limit"
                ]
            ):
                break

        if not solana_profiles:

            print(
                "    ❌ No Solana token "
                "profiles found\n"
            )

            return []

        # ----------------------------------------------------
        # 3. Batched pair lookup
        # ----------------------------------------------------

        addresses = [

            p["tokenAddress"]

            for p in solana_profiles

        ]

        # DEX Screener supports up to 30 addresses
        addresses = addresses[:30]

        pairs = self.http.get_json(

            f"{DEXSCREENER_BASE}/"
            "tokens/v1/solana/"
            +
            ",".join(addresses),

            label=
                "DEX Screener batched "
                "Solana pairs",
        )

        if not isinstance(
            pairs,
            list
        ):

            print(
                "    ❌ Pair lookup failed\n"
            )

            return []

        # ----------------------------------------------------
        # 4. Map profiles
        # ----------------------------------------------------

        profile_map = {

            p["tokenAddress"]:
                p

            for p in solana_profiles

        }

        # ----------------------------------------------------
        # 5. Find highest-liquidity pair
        # ----------------------------------------------------

        best_pair = {}

        for pair in pairs:

            if str(
                pair.get(
                    "chainId",
                    ""
                )
            ).lower() != "solana":

                continue

            base = (
                pair.get(
                    "baseToken"
                )
                or {}
            )

            quote = (
                pair.get(
                    "quoteToken"
                )
                or {}
            )

            base_address = (
                base.get(
                    "address"
                )
            )

            quote_address = (
                quote.get(
                    "address"
                )
            )

            token_mint = None

            if (
                base_address
                in profile_map
            ):

                token_mint = (
                    base_address
                )

            elif (
                quote_address
                in profile_map
            ):

                token_mint = (
                    quote_address
                )

            if not token_mint:
                continue

            # ------------------------------------------------
            # IMPORTANT LIQUIDITY FIX
            #
            # Missing liquidity stays None.
            # It is NOT converted to $0.
            # ------------------------------------------------

            liquidity_raw = (
                pair.get(
                    "liquidity"
                )
                or {}
            ).get(
                "usd"
            )

            if liquidity_raw is None:

                liquidity = None

            else:

                liquidity = safe_float(
                    liquidity_raw
                )

            existing = best_pair.get(
                token_mint
            )

            if existing is None:

                best_pair[
                    token_mint
                ] = pair

            else:

                existing_raw = (
                    existing.get(
                        "liquidity"
                    )
                    or {}
                ).get(
                    "usd"
                )

                if existing_raw is None:

                    existing_liquidity = 0.0

                else:

                    existing_liquidity = (
                        safe_float(
                            existing_raw
                        )
                    )

                current_comparison = (
                    0.0
                    if liquidity is None
                    else liquidity
                )

                if (
                    current_comparison
                    > existing_liquidity
                ):

                    best_pair[
                        token_mint
                    ] = pair

        # ----------------------------------------------------
        # 6. Convert to scanner format
        # ----------------------------------------------------

        tokens = []

        for mint, pair in best_pair.items():

            base = (
                pair.get(
                    "baseToken"
                )
                or {}
            )

            profile = profile_map.get(
                mint,
                {}
            )

            name = (
                base.get("name")
                or
                profile.get("description")
                or
                "Unknown"
            )

            symbol = (
                base.get("symbol")
                or
                "???"
            )

            # ------------------------------------------------
            # IMPORTANT LIQUIDITY FIX
            # ------------------------------------------------

            liquidity_raw = (
                pair.get(
                    "liquidity"
                )
                or {}
            ).get(
                "usd"
            )

            if liquidity_raw is None:

                liquidity = None

            else:

                liquidity = safe_float(
                    liquidity_raw
                )

            tokens.append({

                "mint":
                    mint,

                "symbol":
                    symbol,

                "name":
                    name,

                "liquidity":
                    liquidity,

                "createdAt":
                    pair.get(
                        "pairCreatedAt"
                    ),

                "pairAddress":
                    pair.get(
                        "pairAddress",
                        ""
                    ),

                "source":
                    "DEX Screener",

                "is_demo":
                    False,
            })

        # ----------------------------------------------------
        # Highest liquidity first
        #
        # None values go to the bottom.
        # ----------------------------------------------------

        tokens.sort(

            key=lambda x: (
                x["liquidity"]
                is not None,

                x["liquidity"]
                if x["liquidity"]
                is not None
                else -1
            ),

            reverse=True
        )

        tokens = tokens[
            :self.config[
                "scan_limit"
            ]
        ]

        print(
            f"    ✅ Got "
            f"{len(tokens)} "
            f"Solana tokens "
            f"with liquidity data\n"
        )

        return tokens


    # ========================================================
    # GECKOTERMINAL
    # ========================================================

    def fetch_geckoterminal(
        self,
        endpoint_name
    ):

        print(
            f"  🔹 GeckoTerminal "
            f"({endpoint_name})..."
        )

        payload = self.http.get_json(

            f"{GECKOTERMINAL_BASE}/"
            f"networks/solana/"
            f"{endpoint_name}",

            params={
                "page": 1
            },

            label=
                f"GeckoTerminal "
                f"{endpoint_name}",
        )

        if not isinstance(
            payload,
            dict
        ):

            print(
                "    ❌ Endpoint failed\n"
            )

            return []

        data = (
            payload.get("data")
            or []
        )

        included = (
            payload.get("included")
            or []
        )

        # ----------------------------------------------------
        # Included token lookup
        # ----------------------------------------------------

        included_map = {

            item.get("id"):
                item

            for item in included

            if isinstance(
                item,
                dict
            )

            and item.get("id")
        }

        tokens = []

        seen = set()

        for pool in data:

            if not isinstance(
                pool,
                dict
            ):
                continue

            attrs = (
                pool.get(
                    "attributes"
                )
                or {}
            )

            relationships = (
                pool.get(
                    "relationships"
                )
                or {}
            )

            base_relationship = (
                relationships.get(
                    "base_token"
                )
                or {}
            )

            base_data = (
                base_relationship.get(
                    "data"
                )
                or {}
            )

            base_id = (
                base_data.get(
                    "id",
                    ""
                )
            )

            base_resource = (
                included_map.get(
                    base_id,
                    {}
                )
            )

            base_attrs = (
                base_resource.get(
                    "attributes"
                )
                or {}
            )

            mint = base_attrs.get(
                "address"
            )

            # Fallback
            if (
                not mint
                and "_"
                in base_id
            ):

                mint = (
                    base_id.split(
                        "_",
                        1
                    )[1]
                )

            # Ignore native SOL
            if (
                not mint
                or
                mint
                ==
                "So11111111111111111111111111111111111111112"
            ):

                continue

            if mint in seen:
                continue

            pool_name = str(
                attrs.get(
                    "name"
                )
                or
                "Unknown / SOL"
            )

            fallback_symbol = (
                pool_name
                .split("/")[0]
                .strip()
                [:12]
                or
                "???"
            )

            # ------------------------------------------------
            # Gecko liquidity
            # ------------------------------------------------

            liquidity_raw = (
                attrs.get(
                    "reserve_in_usd"
                )
            )

            if liquidity_raw is None:

                liquidity = None

            else:

                liquidity = safe_float(
                    liquidity_raw
                )

            created_at = (
                attrs.get(
                    "pool_created_at"
                )
                or
                attrs.get(
                    "created_at"
                )
            )

            tokens.append({

                "mint":
                    mint,

                "symbol":
                    base_attrs.get(
                        "symbol"
                    )
                    or
                    fallback_symbol,

                "name":
                    base_attrs.get(
                        "name"
                    )
                    or
                    fallback_symbol,

                "liquidity":
                    liquidity,

                "createdAt":
                    created_at,

                "pairAddress":
                    attrs.get(
                        "address",
                        ""
                    ),

                "source":
                    f"GeckoTerminal "
                    f"{endpoint_name}",

                "is_demo":
                    False,
            })

            seen.add(mint)

            if (
                len(tokens)
                >= self.config[
                    "scan_limit"
                ]
            ):

                break

        if tokens:

            print(
                f"    ✅ Got "
                f"{len(tokens)} "
                f"Solana pool tokens\n"
            )

        else:

            print(
                "    ❌ No usable "
                "token data\n"
            )

        return tokens


    # ========================================================
    # DEMO FALLBACK
    # ========================================================

    def fetch_demo(self):

        print(
            "  🧪 All live providers failed."
        )

        print(
            "     Switching to DEMO DATA."
        )

        print(
            "     ⚠️ Demo tokens are "
            "placeholders only.\n"
        )

        demo = []

        for token in DEMO_TOKENS[
            :self.config[
                "scan_limit"
            ]
        ]:

            item = dict(token)

            item["source"] = "DEMO"

            item["is_demo"] = True

            demo.append(
                item
            )

        return demo


    # ========================================================
    # RISK CALCULATION
    # ========================================================

    def calculate_risk(
        self,
        token
    ):

        try:

            mint = str(
                token.get(
                    "mint"
                )
                or
                "unknown"
            )

            symbol = str(
                token.get(
                    "symbol"
                )
                or
                "???"
            )

            name = str(
                token.get(
                    "name"
                )
                or
                "Unknown"
            )

            # ------------------------------------------------
            # IMPORTANT:
            # Do NOT convert missing liquidity to 0.
            # ------------------------------------------------

            liquidity_raw = (
                token.get(
                    "liquidity"
                )
            )

            if liquidity_raw is None:

                liquidity = None

            else:

                liquidity = safe_float(
                    liquidity_raw
                )

            source = str(
                token.get(
                    "source"
                )
                or
                "Unknown"
            )

            pair_address = str(
                token.get(
                    "pairAddress"
                )
                or
                ""
            )

            is_demo = bool(
                token.get(
                    "is_demo",
                    False
                )
            )

            # ------------------------------------------------
            # AGE
            # ------------------------------------------------

            created_timestamp = (
                parse_timestamp(
                    token.get(
                        "createdAt"
                    )
                )
            )

            if created_timestamp is not None:

                age_minutes = max(

                    0,

                    (
                        utc_now().timestamp()
                        -
                        created_timestamp
                    )
                    / 60
                )

            else:

                # Unknown age gets a neutral placeholder
                age_minutes = 1440

            risks = []

            warnings = []

            risk_score = 0.0

            data_quality = "GOOD"

            # ------------------------------------------------
            # LIQUIDITY RISK
            # ------------------------------------------------

            if liquidity is None:

                warnings.append(
                    "💧 Liquidity data unavailable"
                )

                # Missing data should lower confidence,
                # but shouldn't pretend liquidity is $0.
                data_quality = "PARTIAL"

                risk_score += 20

            elif liquidity <= 0:

                risks.append(
                    "🔴 No reported liquidity"
                )

                risk_score += 50

            elif liquidity < 100:

                risks.append(
                    f"🔴 CRITICAL: "
                    f"Very low liquidity "
                    f"(${liquidity:,.0f})"
                )

                risk_score += 45

            elif liquidity < 500:

                warnings.append(
                    f"💧 Low liquidity: "
                    f"${liquidity:,.0f}"
                )

                risk_score += 20

            elif liquidity < 1000:

                warnings.append(
                    f"💧 Limited liquidity: "
                    f"${liquidity:,.0f}"
                )

                risk_score += 8

            # ------------------------------------------------
            # AGE RISK
            # ------------------------------------------------

            if created_timestamp is None:

                warnings.append(
                    "⏱️ Pool age unavailable"
                )

                data_quality = "PARTIAL"

            elif age_minutes < 5:

                risks.append(
                    "🔴 CRITICAL: < 5 min old"
                )

                risk_score += 40

            elif age_minutes < 30:

                warnings.append(
                    f"⚠️ Very new "
                    f"({age_minutes:.0f}m)"
                )

                risk_score += 20

            elif age_minutes < 120:

                warnings.append(
                    f"⚠️ New "
                    f"({age_minutes:.0f}m)"
                )

                risk_score += 8

            # ------------------------------------------------
            # SAFE SCREEN
            # ------------------------------------------------
            #
            # FAIL-CLOSED HOLDER GATE:
            # Check 2 has not run yet, so the token CANNOT
            # be marked safe by the main scanner alone.
            # Final safety is established by the FINAL SAFETY
            # GATE cell after Check 2 completes.
            # ------------------------------------------------

            holder_check_status = "NOT_RUN"
            holder_risk_score = None

            is_safe = False

            return TokenRisk(

                mint=mint,

                symbol=
                    symbol.upper()[:10],

                name=
                    name[:30],

                risk_score=
                    min(
                        100,
                        risk_score
                    ),

                is_safe=
                    is_safe,

                holder_check_status=
                    holder_check_status,

                holder_risk_score=
                    holder_risk_score,

                risks=
                    risks,

                warnings=
                    warnings,

                liquidity_usd=
                    liquidity,

                age_minutes=
                    age_minutes,

                source=
                    source,

                pair_address=
                    pair_address,

                data_quality=
                    data_quality,

                is_demo=
                    is_demo,

                url=
                    f"https://solscan.io/token/{mint}",
            )

        except Exception as e:

            print(
                f"❌ Risk calculation "
                f"error: {e}"
            )

            return None


    # ========================================================
    # SCAN
    # ========================================================

    def scan_tokens(self):

        print(
            "🔍 Fetching REAL token data "
            "(not NFTs)...\n"
        )

        # ----------------------------------------------------
        # Provider order
        # ----------------------------------------------------

        providers = [

            (
                "DEX Screener",
                self.fetch_dexscreener
            ),

            (
                "GeckoTerminal new pools",
                lambda:
                    self.fetch_geckoterminal(
                        "new_pools"
                    )
            ),

            (
                "GeckoTerminal trending pools",
                lambda:
                    self.fetch_geckoterminal(
                        "trending_pools"
                    )
            ),
        ]

        tokens = []

        # ----------------------------------------------------
        # Try live providers
        # ----------------------------------------------------

        for provider_name, function in providers:

            try:

                tokens = function()

            except Exception as e:

                print(
                    f"    ❌ "
                    f"{provider_name} "
                    f"unexpected error: "
                    f"{e}\n"
                )

                tokens = []

            if tokens:

                self.source_used = (
                    provider_name
                )

                break

        # ----------------------------------------------------
        # Demo fallback
        # ----------------------------------------------------

        if not tokens:

            if not self.config[
                "demo_fallback"
            ]:

                print(
                    "❌ All APIs failed "
                    "and demo fallback "
                    "is disabled.\n"
                )

                return []

            tokens = self.fetch_demo()

            self.source_used = "DEMO"

            self.demo_mode = True

        # ----------------------------------------------------
        # Analyze tokens
        # ----------------------------------------------------

        print(
            f"📊 Analyzing "
            f"{len(tokens)} tokens "
            f"from: "
            f"{self.source_used}\n"
        )

        self.results = []

        for index, token in enumerate(
            tokens,
            1
        ):

            print(
                f"[{index:2d}/"
                f"{len(tokens)}] ",
                end="",
                flush=True
            )

            result = (
                self.calculate_risk(
                    token
                )
            )

            if result is None:

                print("❌")

                continue

            self.results.append(
                result
            )

            status = (
                "✅"
                if result.is_safe
                else
                "🚨"
            )

            liquidity_display = (
                format_liquidity(
                    result.liquidity_usd
                )
            )

            demo_tag = (
                " | DEMO"
                if result.is_demo
                else ""
            )

            print(

                f"{result.symbol:10s} "
                f"{status} "
                f"{result.risk_score:5.0f}/100  "
                f"{liquidity_display:>15s}  "
                f"{result.age_minutes:7.0f}m"
                f"{demo_tag}"
            )

        # ----------------------------------------------------
        # Sort results
        #
        # Safe first
        # Lower risk first
        # Higher liquidity first
        #
        # None liquidity goes last.
        # ----------------------------------------------------

        self.results.sort(

            key=lambda x: (

                not x.is_safe,

                x.risk_score,

                (
                    -x.liquidity_usd
                    if x.liquidity_usd
                    is not None
                    else float("inf")
                )
            )
        )

        return self.results


    # ========================================================
    # PRINT RESULTS
    # ========================================================

    def print_results(
        self,
        safe_only=True
    ):

        if not self.results:

            print(
                "No results."
            )

            return

        safe = [

            r
            for r in self.results
            if r.is_safe

        ]

        risky = [

            r
            for r in self.results
            if not r.is_safe

        ]

        show = (
            safe
            if safe_only
            else self.results
        )

        print(
            "\n"
            + "=" * 110
        )

        print(
            f"{'SOLANA MEMECOIN SNIPER v8.1':^110}"
        )

        print(
            f"Source: "
            f"{self.source_used}"
        )

        print(
            f"Demo mode: "
            f"{self.demo_mode}"
        )

        print(
            f"Total: "
            f"{len(self.results)} | "
            f"Safe-screen pass ✅: "
            f"{len(safe)} | "
            f"Flagged 🚨: "
            f"{len(risky)}"
        )

        print(
            "=" * 110
        )

        # ----------------------------------------------------
        # Demo warning
        # ----------------------------------------------------

        if self.demo_mode:

            print(
                "\n"
                "⚠️ DEMO MODE ACTIVE\n"
            )

            print(
                "These are placeholder records "
                "for testing only."
            )

            print(
                "DO NOT trade them.\n"
            )

        # ----------------------------------------------------
        # No safe tokens
        # ----------------------------------------------------

        if not show:

            print(
                "\n⚠️ NO TOKENS PASSED "
                "THE CURRENT SAFE SCREEN\n"
            )

            for i, result in enumerate(
                self.results[:5],
                1
            ):

                if result.risks:

                    reason = (
                        result.risks[0]
                    )

                elif result.warnings:

                    reason = (
                        result.warnings[0]
                    )

                else:

                    reason = (
                        "No reason recorded"
                    )

                print(

                    f"{i}. "
                    f"{result.symbol:10s} "
                    f"Risk: "
                    f"{result.risk_score:.0f}/100 "
                    f"| Liq: "
                    f"{format_liquidity(result.liquidity_usd)}"
                )

                print(
                    f"   {reason}"
                )

            return

        # ----------------------------------------------------
        # Results
        # ----------------------------------------------------

        title = (

            "🧪 DEMO TOKENS"

            if self.demo_mode

            else
            "✅ TOKENS PASSING CURRENT SCREEN"
        )

        print(
            f"\n{title} "
            f"({len(show)}):\n"
        )

        for i, result in enumerate(
            show,
            1
        ):

            liquidity_display = (
                format_liquidity(
                    result.liquidity_usd
                )
            )

            print(

                f"{i}. "
                f"{result.symbol:10s} "
                f"({result.name}) "
                f"| "
                f"{liquidity_display}"
            )

            print(

                f"   Age: "
                f"{result.age_minutes:.0f}m "
                f"| Risk: "
                f"{result.risk_score:.0f}/100 "
                f"| Data: "
                f"{result.data_quality}"
            )

            print(
                f"   Source: "
                f"{result.source}"
            )

            if result.risks:

                for risk in result.risks:

                    print(
                        f"   {risk}"
                    )

            if result.warnings:

                for warning in (
                    result.warnings
                ):

                    print(
                        f"   {warning}"
                    )

            print(
                f"   {result.url}"
            )

            print()

        # ----------------------------------------------------
        # Important warning
        # ----------------------------------------------------

        print(
            "⚠️ IMPORTANT:"
        )

        print(
            "Passing this screen does NOT "
            "guarantee that a token is safe."
        )

        print(
            "Current checks:"
        )

        print(
            "  • Pool age"
        )

        print(
            "  • Reported liquidity"
        )

        print(
            "  • Data quality"
        )

        print()

        print(
            "Not yet checked:"
        )

        print(
            "  • Mint authority"
        )

        print(
            "  • Freeze authority"
        )

        print(
            "  • Holder concentration"
        )

        print(
            "  • LP ownership / locks"
        )

        print(
            "  • Token distribution"
        )

        print(
            "  • Contract/program behavior"
        )


    # ========================================================
    # EXPORT JSON
    # ========================================================

    def export_json(
        self,
        filename="sniper_results.json"
    ):

        data = {

            "scan_utc":
                utc_now().isoformat(),

            "source_used":
                self.source_used,

            "demo_mode":
                self.demo_mode,

            "total":
                len(self.results),

            "safe":
                len([
                    r
                    for r in self.results
                    if r.is_safe
                ]),

            "risky":
                len([
                    r
                    for r in self.results
                    if not r.is_safe
                ]),

            "http_logs":
                self.http.logs,

            "tokens":
                [
                    r.to_dict()
                    for r in self.results
                ],
        }

        with open(
            filename,
            "w",
            encoding="utf-8"
        ) as f:

            json.dump(
                data,
                f,
                indent=2
            )

        print(
            f"✅ Saved to {filename}"
        )

__all__ = [
    "CONFIG",
    "DEXSCREENER_BASE",
    "GECKOTERMINAL_BASE",
    "TokenRisk",
    "SolanaSniper",
]
