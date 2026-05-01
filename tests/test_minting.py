from pathlib import Path

from negotiate_safe.minting import (
    build_create_tokens_cmd,
    build_service_name,
    normalize_user_role,
    resolve_investment_amount,
    resolve_mint_identity,
    role_plan,
)


def _constraints(**overrides):
    data = {
        "role": "founder",
        "mode": "demo",
        "valuation_cap_min": 10_000_000,
        "valuation_cap_max": 20_000_000,
        "discount_min": 0.15,
        "discount_max": 0.15,
        "pro_rata": "required",
        "mfn": "preferred",
        "company_name": "Acme Labs",
        "investment_amount": None,
    }
    data.update(overrides)
    return data


def test_normalize_user_role_defaults_unknown_to_founder():
    assert normalize_user_role(None) == "founder"
    assert normalize_user_role("shareholder") == "founder"
    assert normalize_user_role(" INVESTOR ") == "investor"


def test_role_plan_maps_user_and_ai_prefixes():
    founder = role_plan("founder")
    assert founder.user_flag_prefix == "--founder-"
    assert founder.ai_flag_prefix == "--investor-"
    assert founder.ai_env_prefix == "INVESTOR_"

    investor = role_plan("investor")
    assert investor.user_flag_prefix == "--investor-"
    assert investor.ai_flag_prefix == "--founder-"
    assert investor.ai_env_prefix == "FOUNDER_"


def test_resolve_mint_identity_uses_demo_counterparty_presets():
    identity = resolve_mint_identity(
        _constraints(
            founder_name=None,
            founder_title=None,
            investor_name=None,
            investor_firm=None,
            company_name=None,
        ),
        env={},
    )
    assert identity.founder_name == "Founder"
    assert identity.investor_name == "Demo Investor"
    assert identity.investor_firm == "Demo Capital"


def test_resolve_mint_identity_uses_env_and_nl_precedence():
    identity = resolve_mint_identity(
        _constraints(founder_name="Nora", company_name=None),
        env={
            "FOUNDER_NAME": "Env Founder",
            "INVESTOR_NAME": "Ivy",
            "INVESTOR_FIRM": "Babes Fund",
            "COMPANY_NAME": "EnvCo",
        },
    )
    assert identity.founder_name == "Nora"
    assert identity.investor_name == "Ivy"
    assert identity.investor_firm == "Babes Fund"
    assert identity.company == "EnvCo"


def test_resolve_mint_identity_drops_demo_bot_placeholders():
    identity = resolve_mint_identity(
        _constraints(
            mode="two_party",
            founder_name=None,
            founder_title=None,
            investor_name=None,
            investor_firm=None,
            company_name=None,
        ),
        env={
            "FOUNDER_NAME": "APOA Founder AI Agent",
            "INVESTOR_NAME": "Alex Chen",
            "INVESTOR_FIRM": "Investor Firm",
            "COMPANY_NAME": "APOA",
        },
    )
    assert identity.founder_name == "Founder"
    assert identity.investor_name == "Investor"
    assert identity.investor_firm == "Investor Firm"
    assert identity.company == "Company"


def test_build_service_name_slugifies_company():
    assert build_service_name("Acme Labs, Inc.", "neg_1") == "safe:acme-labs--inc:neg_1"


def test_build_create_tokens_cmd_binds_founder_and_ai_env_flags(tmp_path):
    cmd, user_role = build_create_tokens_cmd(
        repo=tmp_path,
        negotiation_id="neg_1",
        constraints=_constraints(),
        neg_dir=tmp_path / "negotiations" / "neg_1",
        expires_str="2026-04-27T00:00:00Z",
        service="safe:acme:neg_1",
        env={"USER_DID": "did:apoa:u1", "INVESTOR_CAP_MIN": "30000000"},
        python_executable="/py",
    )
    assert user_role == "founder"
    assert cmd[:2] == ["/py", str(Path(tmp_path) / "create_tokens.py")]
    assert cmd[cmd.index("--principal-id") + 1] == "did:apoa:u1"
    assert cmd[cmd.index("--investment-amount") + 1] == "500000.0"
    assert cmd[cmd.index("--founder-cap-min") + 1] == "10000000"
    assert cmd[cmd.index("--founder-investment-amount-min") + 1] == "500000.0"
    assert cmd[cmd.index("--founder-investment-amount-max") + 1] == "500000.0"
    assert cmd[cmd.index("--founder-discount-min") + 1] == "0.15"
    assert cmd[cmd.index("--founder-discount-max") + 1] == "0.15"
    assert cmd[cmd.index("--investor-cap-min") + 1] == "30000000"


def test_resolve_investment_amount_uses_check_range_floor():
    assert resolve_investment_amount(_constraints(
        investment_amount=None,
        investment_amount_min=250_000.0,
        investment_amount_max=750_000.0,
    )) == 250_000.0


def test_build_create_tokens_cmd_binds_check_size_range(tmp_path):
    cmd, _ = build_create_tokens_cmd(
        repo=tmp_path,
        negotiation_id="neg_1",
        constraints=_constraints(
            investment_amount=None,
            investment_amount_min=250_000.0,
            investment_amount_max=750_000.0,
        ),
        neg_dir=tmp_path / "negotiations" / "neg_1",
        expires_str="2026-04-27T00:00:00Z",
        service="safe:acme:neg_1",
        env={},
        python_executable="/py",
    )
    assert cmd[cmd.index("--investment-amount") + 1] == "250000.0"
    assert cmd[cmd.index("--founder-investment-amount-min") + 1] == "250000.0"
    assert cmd[cmd.index("--founder-investment-amount-max") + 1] == "750000.0"


def test_build_create_tokens_cmd_join_mode_skips_ai_env_flags(tmp_path):
    cmd, user_role = build_create_tokens_cmd(
        repo=tmp_path,
        negotiation_id="neg_1",
        constraints=_constraints(role="investor"),
        neg_dir=tmp_path / "negotiations" / "neg_1",
        expires_str="2026-04-27T00:00:00Z",
        service="safe:acme:neg_1",
        shared_session=True,
        env={"FOUNDER_CAP_MIN": "30000000"},
        python_executable="/py",
    )
    assert user_role == "investor"
    assert cmd[cmd.index("--role") + 1] == "investor"
    assert "--founder-cap-min" not in cmd
    assert cmd[cmd.index("--investor-cap-min") + 1] == "10000000"
