import json
from types import SimpleNamespace

from local_protocol import _unb64url, create_local_token, load_apoa_token
from negotiate_safe.minting import (
    build_service_name,
    normalize_user_role,
    resolve_investment_amount,
    resolve_investment_amount_bounds,
    resolve_mint_identity,
    role_plan,
    write_local_mint_files,
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


def test_resolve_investment_amount_uses_check_range_floor():
    assert resolve_investment_amount(_constraints(
        investment_amount=None,
        investment_amount_min=250_000.0,
        investment_amount_max=750_000.0,
    )) == 250_000.0


def test_resolve_investment_amount_bounds_uses_check_range():
    assert resolve_investment_amount_bounds(
        _constraints(
            investment_amount=None,
            investment_amount_min=250_000.0,
            investment_amount_max=750_000.0,
        )
    ) == (250_000.0, 750_000.0)


def test_write_local_mint_files_uses_nested_apoa_definition(tmp_path):
    def runner(*_args, **_kwargs):
        return SimpleNamespace(returncode=0, stdout=json.dumps({"key_id": "signing-key"}), stderr="")

    write_local_mint_files(
        negotiation_id="neg_test",
        constraints=_constraints(investment_amount=500_000),
        neg_dir=tmp_path,
        expires_at_epoch=1_800_000_000,
        expires_str="2027-01-15",
        service="safe:acme:neg_test",
        env={"USER_DID": "did:apoa:founder"},
        runner=runner,
    )

    token_path = tmp_path / "tokens" / "founder.jwt"
    _token, constraints = load_apoa_token(token_path, tmp_path / "keys" / "founder_public.pem")
    payload = json.loads(_unb64url(token_path.read_text().split(".")[1]).decode("utf-8"))

    assert payload["aud"] == ["safe:acme:neg_test"]
    assert "constraints" not in payload
    assert payload["definition"]["principal"]["id"] == "did:apoa:founder"
    assert payload["definition"]["services"][0]["scopes"] == [
        "offer:submit",
        "offer:accept",
        "document:sign",
    ]
    assert payload["definition"]["services"][0]["constraints"] == constraints
    assert constraints["valuation_cap_min"] == 10_000_000


def test_load_apoa_token_accepts_legacy_flat_constraints(tmp_path):
    secret = "test-secret"
    token_path = tmp_path / "legacy.jwt"
    key_path = tmp_path / "legacy.pem"
    key_path.write_text(secret, encoding="utf-8")
    token_path.write_text(
        create_local_token(
            payload={"constraints": {"valuation_cap": {"min": 1, "max": 2}}},
            secret=secret,
        ),
        encoding="utf-8",
    )

    _token, constraints = load_apoa_token(token_path, key_path)

    assert constraints == {"valuation_cap": {"min": 1, "max": 2}}
