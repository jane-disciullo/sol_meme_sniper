from types import SimpleNamespace

from src.safety_gate import evaluate_token


def make_token(is_demo=False):
    return SimpleNamespace(
        mint="MintA",
        symbol="TEST",
        is_demo=is_demo,
        is_safe=False,
        risks=[],
    )


def passing_checks():
    return (
        [{"mint": "MintA", "status": "✅ SUCCESS",
          "mint_authority_raw": None, "freeze_authority_raw": None}],
        [{"mint": "MintA", "raw": {"owner_count": 10},
          "adjusted": {"top_1_pct": 10.0, "top_5_pct": 40.0,
                       "top_10_pct": 55.0}}],
        [{"mint": "MintA", "passes": True, "pair_match": True}],
        [{"mint": "MintA", "passes": True}],
        [{"mint": "MintA", "passes": True}],
        [{"mint": "MintA", "passes": True}],
    )


def test_all_six_checks_pass():
    token = make_token()
    checks = passing_checks()

    result = evaluate_token(token, *checks)

    assert result["safe"] is True
    assert token.is_safe is True
    assert token.final_safety_pass is True


def test_missing_check_fails_closed():
    token = make_token()
    checks = passing_checks()
    checks = list(checks)
    checks[2] = []

    result = evaluate_token(token, *checks)

    assert result["safe"] is False
    assert "Check 3" in result["failed_checks"]
    assert token.is_safe is False


def test_demo_data_can_never_pass():
    token = make_token(is_demo=True)
    checks = passing_checks()

    result = evaluate_token(token, *checks)

    assert result["safe"] is False
    assert "DATA" in result["failed_checks"]


def test_check_2_requires_complete_distribution_data():
    token = make_token()
    checks = passing_checks()
    checks = list(checks)
    checks[1] = [{
        "mint": "MintA",
        "raw": {"owner_count": 10},
        "adjusted": {"top_1_pct": 10.0, "top_5_pct": None, "top_10_pct": 55.0},
    }]

    result = evaluate_token(token, *checks)

    assert result["safe"] is False
    assert "Check 2" in result["failed_checks"]
