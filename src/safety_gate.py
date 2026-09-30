"""Final fail-closed safety gate for the six-check pipeline."""

from __future__ import annotations

import time
from typing import Any, Iterable, Optional


def final_gate_find_by_mint(check_list, mint):
    """Return the check record matching a token mint, or None."""
    if not isinstance(check_list, list):
        return None

    target = str(mint or "").strip()
    for check in check_list:
        if not isinstance(check, dict):
            continue
        check_mint = str(
            check.get("mint", check.get("mint_address", "")) or ""
        ).strip()
        if check_mint == target:
            return check
    return None


def evaluate_token(
    token,
    authority_checks,
    holder_checks,
    lp_checks,
    check4_results,
    check5_results,
    check6_results,
):
    """Evaluate one token. Missing/unknown data fails closed."""
    mint = str(getattr(token, "mint", "") or "").strip()

    check1 = final_gate_find_by_mint(authority_checks, mint)
    check2 = final_gate_find_by_mint(holder_checks, mint)
    check3 = final_gate_find_by_mint(lp_checks, mint)
    check4 = final_gate_find_by_mint(check4_results, mint)
    check5 = final_gate_find_by_mint(check5_results, mint)
    check6 = final_gate_find_by_mint(check6_results, mint)

    failed_checks = []
    reasons = []

    check1_pass = bool(
        check1 is not None
        and check1.get("status") == "✅ SUCCESS"
        and check1.get("mint_authority_raw") is None
        and check1.get("freeze_authority_raw") is None
    )
    if not check1_pass:
        failed_checks.append("Check 1")
        reasons.append(
            "Mint/freeze authority verification did not confirm both "
            "authorities disabled."
        )

    raw = (check2 or {}).get("raw") or {}
    adjusted = (check2 or {}).get("adjusted") or {}
    check2_pass = bool(
        check2 is not None
        and raw.get("owner_count") is not None
        and adjusted.get("top_1_pct") is not None
        and adjusted.get("top_5_pct") is not None
        and adjusted.get("top_10_pct") is not None
    )
    if not check2_pass:
        failed_checks.append("Check 2")
        reasons.append(
            "Holder concentration/distribution data is missing or incomplete."
        )

    check3_pass = bool(
        check3 is not None
        and check3.get("passes", False)
        and check3.get("pair_match", False)
    )
    if not check3_pass:
        failed_checks.append("Check 3")
        reasons.append("LP ownership/lock verification did not pass.")

    check4_pass = bool(check4 is not None and check4.get("passes", False))
    if not check4_pass:
        failed_checks.append("Check 4")
        reasons.append("On-chain supply/distribution verification did not pass.")

    check5_pass = bool(check5 is not None and check5.get("passes", False))
    if not check5_pass:
        failed_checks.append("Check 5")
        reasons.append(
            "Token-2022/transfer-control verification did not pass."
        )

    check6_pass = bool(check6 is not None and check6.get("passes", False))
    if not check6_pass:
        failed_checks.append("Check 6")
        reasons.append(
            "Creator/insider behavior verification did not pass."
        )

    if getattr(token, "is_demo", None) is True:
        failed_checks.append("DATA")
        reasons.append("Token is marked as demo/fallback data.")

    all_pass = not failed_checks

    token.is_safe = all_pass
    token.final_safety_pass = all_pass
    token.final_safety_status = (
        "🟢 SAFE — ALL 6 CHECKS PASSED"
        if all_pass
        else "🔴 BLOCKED — ONE OR MORE CHECKS FAILED"
    )
    token.final_safety_failed_checks = failed_checks
    token.final_safety_reasons = reasons

    if not all_pass:
        message = "🛑 Final Safety Gate: " + ", ".join(failed_checks)
        if message not in getattr(token, "risks", []):
            token.risks.append(message)

    return {
        "mint": mint,
        "symbol": str(getattr(token, "symbol", "UNKNOWN") or "UNKNOWN").upper(),
        "safe": all_pass,
        "failed_checks": failed_checks,
        "reasons": reasons,
        "status": token.final_safety_status,
    }


def run_final_safety_gate(
    results,
    authority_checks,
    holder_checks,
    lp_checks,
    check4_results,
    check5_results,
    check6_results,
):
    """Apply the final six-check gate and return a report plus timestamp."""
    final_safety_results = []

    for token in results:
        final_safety_results.append(
            evaluate_token(
                token,
                authority_checks,
                holder_checks,
                lp_checks,
                check4_results,
                check5_results,
                check6_results,
            )
        )

    results.sort(
        key=lambda token: (
            not getattr(token, "is_safe", False),
            getattr(token, "risk_score", 100.0),
            -(getattr(token, "liquidity_usd", 0.0) or 0.0),
        )
    )

    completed_at = time.time()

    return {
        "results": final_safety_results,
        "safe_count": sum(1 for x in final_safety_results if x["safe"]),
        "blocked_count": sum(1 for x in final_safety_results if not x["safe"]),
        "completed_at": completed_at,
    }


__all__ = [
    "evaluate_token",
    "final_gate_find_by_mint",
    "run_final_safety_gate",
]
