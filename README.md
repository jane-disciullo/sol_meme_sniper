# Solana Meme Coin Sniper

A Python-based Solana token screening framework that combines public market discovery with six independent safety checks and a fail-closed final safety gate.

> **Status:** research / portfolio project. The public repository does **not** enable live wallet signing or real-money trading.

## Pipeline

```text
DEX Screener / GeckoTerminal
            │
            ▼
     Initial market screen
            │
            ▼
       Check 1
 Mint + Freeze Authority
            │
            ▼
       Check 2
 Holder concentration
            │
            ▼
       Check 3
    LP ownership / lock
            │
            ▼
       Check 4
 Supply + distribution
            │
            ▼
       Check 5
 Token-2022 transfer controls
            │
            ▼
       Check 6
 Creator / insider behavior
            │
            ▼
     FINAL SAFETY GATE
            │
      ┌─────┴─────┐
      ▼           ▼
    SAFE        BLOCKED
```

## What the project checks

**Scanner:** discovers recent Solana token/pool candidates from DEX Screener, with GeckoTerminal fallbacks and safe handling of missing liquidity data.

**Check 1 — Mint / Freeze Authority:** verifies the token mint account and fails closed when authority status cannot be verified.

**Check 2 — Holder Concentration:** retrieves holder/account data through Helius, aggregates balances by owner, identifies positively verified pool/burn/program-controlled addresses, and retains unknown owners in the adjusted concentration calculation.

**Check 3 — LP Ownership / Lock:** matches the current scanner pair against RugCheck market data and applies the notebook's LP-lock rules, including lock-duration requirements.

**Check 4 — Supply / Distribution:** compares on-chain token supply with the holder distribution from Check 2 and applies the notebook's concentration thresholds.

**Check 5 — Token-2022 / Transfer Controls:** detects Token-2022 extensions and blocks configured transfer-control features and unknown extensions.

**Check 6 — Creator / Insider Behavior:** examines creator holdings, early recipients, common funding relationships, and recent outflow behavior. The result is treated as a risk signal, not proof of malicious intent.

**Final Safety Gate:** a token is marked safe only when all six checks are present and pass. Missing or unknown critical results are blocked.

## Repository structure

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

## Setup

```bash
git clone <your-repository-url>
cd solana-meme-coin-sniper
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

The notebook can also be opened in Google Colab.

### Helius

Checks 2, 4, and 6 require a Helius API key. Store it as an environment variable or a Colab Secret named:

```text
HELIUS_API_KEY
```

Do **not** place the real key in the notebook or commit it to GitHub.

## Run the notebook

Open:

```text
notebooks/solana_memecoin_sniper.ipynb
```

The notebook:

1. discovers current candidates,
2. runs Checks 1–6,
3. applies the final safety gate,
4. shows the resulting SAFE/BLOCKED classification,
5. writes an optional local scan JSON file.

## Safety notes

Passing the final gate is **not** a guarantee that a token is safe or profitable. Solana tokens can change rapidly, liquidity can disappear, and off-chain or protocol behavior can change after a scan.

The public repository intentionally keeps live wallet signing disabled. A separate execution layer can be added later after the screening pipeline is independently tested.

## Disclaimer

This project is for research, educational, and software-engineering purposes. It is not financial advice and does not guarantee the detection of scams, rug pulls, honeypots, or other malicious behavior.
