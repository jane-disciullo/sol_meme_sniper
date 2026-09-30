"""Execution interface intentionally disabled in the public repository.

Live wallet signing/trading will be added separately. This module exists now
so the repository layout remains stable without putting real-money execution in
the public-facing notebook.
"""

from __future__ import annotations


class LiveTradingDisabled(RuntimeError):
    """Raised when a caller attempts to use live execution too early."""


def preview_trade_candidates(tokens, amount_sol=0.01):
    """Return a non-signing preview of tokens that passed the final gate."""
    candidates = [
        token
        for token in tokens
        if getattr(token, "is_safe", False) is True
        and getattr(token, "final_safety_pass", False) is True
        and getattr(token, "is_demo", None) is not True
    ]

    return {
        "amount_sol_each": float(amount_sol),
        "candidate_count": len(candidates),
        "symbols": [getattr(token, "symbol", "?") for token in candidates],
        "mints": [getattr(token, "mint", "") for token in candidates],
        "live_trading_enabled": False,
    }


def execute_live_trade(*args, **kwargs):
    """Placeholder for a future isolated live-execution module."""
    raise LiveTradingDisabled(
        "Live trading is intentionally disabled in the public repository. "
        "Use the final safety gate and preview_trade_candidates() only."
    )


__all__ = [
    "LiveTradingDisabled",
    "preview_trade_candidates",
    "execute_live_trade",
]
