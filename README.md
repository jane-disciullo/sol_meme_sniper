<div align="center">

# ☀️ Solstice

**A Solana meme coin scanner built around on-chain risk analysis and a six-check safety gate.**

*Screen first. Verify again. Trade only when the full gate passes.*

</div>

---

## ✦ Overview

**Solstice** is a Python-based Solana meme coin screening and execution project that combines market data with on-chain risk checks before a token can reach the trading layer.

The system discovers candidates, runs **six independent checks**, applies a **fail-closed final safety gate**, and can optionally pass approved tokens to the execution layer for live trading.

> **Important:** Passing the safety gate does not guarantee that a token is safe or profitable. Solana markets can change quickly, liquidity can disappear, and no automated screening system can detect every failure mode.

---

## ⚡ Pipeline

```text
        DEX Screener
             │
             ▼
      Candidate Discovery
             │
             ▼
     ┌───────────────────┐
     │      Check 1      │
     │ Mint + Freeze Auth│
     └─────────┬─────────┘
               ▼
     ┌───────────────────┐
     │      Check 2      │
     │ Holder Concentr.  │
     └─────────┬─────────┘
               ▼
     ┌───────────────────┐
     │      Check 3      │
     │   LP Ownership    │
     │      / Lock       │
     └─────────┬─────────┘
               ▼
     ┌───────────────────┐
     │      Check 4      │
     │ Supply + Distrib. │
     └─────────┬─────────┘
               ▼
     ┌───────────────────┐
     │      Check 5      │
     │ Token-2022 Risk   │
     └─────────┬─────────┘
               ▼
     ┌───────────────────┐
     │      Check 6      │
     │ Creator / Insider │
     └─────────┬─────────┘
               ▼
        FINAL SAFETY GATE
               │
        ┌──────┴──────┐
        ▼             ▼
      SAFE          BLOCKED
        │
        ▼
   Optional Execution
        │
        ▼
   Live Trading
```

---

## 🔍 The Six Checks

### 01 — Mint & Freeze Authority
Verifies the token mint account and blocks when critical authority status cannot be verified.

### 02 — Holder Concentration
Uses Helius account data to evaluate holder distribution, identify verified pool, burn, and program-controlled accounts, and retain unknown owners in the adjusted concentration calculation.

### 03 — LP Ownership / Lock
Evaluates the token's liquidity pool against the project's LP-lock rules, including lock-duration requirements.

### 04 — Supply & Distribution
Compares on-chain supply information with holder distribution and applies the project's concentration thresholds.

### 05 — Token-2022 / Transfer Controls
Checks for Token-2022 extensions and configured transfer-control risks, including unsupported or unknown extensions.

### 06 — Creator / Insider Behavior
Examines creator holdings, early recipients, funding relationships, and recent outflow behavior.

The result is treated as a **risk signal**, not proof of intent.

---

## 🛡️ Final Safety Gate

Solstice uses a **fail-closed** final gate.

A token reaches the execution layer only when all six checks are present and pass:

```text
Check 1  ✓
Check 2  ✓
Check 3  ✓
Check 4  ✓
Check 5  ✓
Check 6  ✓
─────────────
Final Gate ✓
```

Missing or unknown critical results are not treated as passes.

---

## 🚀 Live Trading

Solstice includes an **optional live-trading layer**.

The execution code is separated from the screening logic so the screening pipeline can be tested independently before live execution is enabled.

The execution layer is designed to:

- accept only final-gate-cleared tokens
- perform fresh pre-trade verification
- re-check current liquidity and routing conditions
- enforce trade-size and run-level limits
- keep wallet credentials in secrets rather than source code
- require explicit confirmation before live execution

**Live trading is optional and experimental.**

Never commit a private key, API key, or wallet credential to GitHub.

---

## 🗂️ Project Structure

```text
solana-meme-coin-sniper/
│
├── README.md
├── requirements.txt
├── .gitignore
│
├── notebooks/
│   └── solana_memecoin_sniper.ipynb
│
├── src/
│   ├── scanner.py
│   ├── checks.py
│   ├── safety_gate.py
│   └── execution.py
│
├── results/
│   └── figures/
│
└── tests/
    └── test_safety_gate.py
```

---

## 🧰 Stack

**Python** · **Solana RPC / Helius** · **DEX Screener** · **RugCheck** · **Jupiter** · **Google Colab** · **pytest**

---

## 🔐 Configuration

For Colab, keep credentials in **Secrets** rather than notebook cells or GitHub.

```text
HELIUS_API_KEY
JUPITER_API_KEY
SOLANA_PRIVATE_KEY
```

The private key should belong to a wallet dedicated to the trading application rather than a primary wallet.

---

## ▶️ Running Solstice

Open:

```text
notebooks/solana_memecoin_sniper.ipynb
```

Run the workflow in order:

```text
Scanner
   ↓
Checks 1–6
   ↓
Final Safety Gate
   ↓
Execution layer
```

For development and testing, keep live execution disabled until the complete pipeline has been validated.

---

## 🧪 Testing

Run the repository tests with:

```bash
pytest -q
```

The test suite focuses on safety-gate behavior and fail-closed logic.

---

## ✧ Design Philosophy

Solstice is built around a simple idea:

> **Don't treat one signal as enough.**

Instead of relying on one score or one data source, the project combines market discovery, token authority checks, holder distribution, liquidity-pool analysis, token extensions, and creator behavior before a candidate can reach execution.

The goal is not to predict every winner.

The goal is to **systematically eliminate as many identifiable risks as possible before a trade is considered.**

---

## ⚠️ Disclaimer

Solstice is a research, educational, and software-engineering project.

It is **not financial advice** and does not guarantee the detection of scams, rug pulls, honeypots, malicious contracts, or other risks.

Live trading can result in the loss of capital, including the entire amount committed to a trade.

---

<div align="center">

**Solstice ☀️**

*On-chain analysis · Risk screening · Automated execution*

</div>
