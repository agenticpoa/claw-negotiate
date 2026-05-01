"""Deterministic planning helpers for APOA token minting."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


AI_FLAG_SUFFIXES = {
    "CAP_MIN": "cap-min",
    "CAP_MAX": "cap-max",
    "INVESTMENT_AMOUNT_MIN": "investment-amount-min",
    "INVESTMENT_AMOUNT_MAX": "investment-amount-max",
    "DISCOUNT_MIN": "discount-min",
    "DISCOUNT_MAX": "discount-max",
    "PRO_RATA_REQUIRED": "pro-rata-required",
    "MFN_REQUIRED": "mfn-required",
}


@dataclass(frozen=True)
class RolePlan:
    user_role: str
    user_flag_prefix: str
    ai_flag_prefix: str
    ai_env_prefix: str


@dataclass(frozen=True)
class MintIdentity:
    founder_name: str
    founder_title: str
    investor_name: str
    investor_firm: str
    company: str


_PLACEHOLDER_VALUES = {
    "alex chen",
    "angel ventures",
    "apoa founder ai agent",
    "apoa investor ai agent",
    "company",
    "demo capital",
    "demo founder",
    "demo investor",
    "founder",
    "investor",
    "investor firm",
    "jane doe",
}


def identity_value(value: object, *, field: str = "", drop_placeholders: bool = True) -> str:
    """Return a user-facing identity value, dropping known demo/bot placeholders."""
    text = str(value or "").strip()
    if not text:
        return ""
    lowered = " ".join(text.lower().split())
    if drop_placeholders and lowered in _PLACEHOLDER_VALUES:
        return ""
    if "ai agent" in lowered:
        return ""
    if drop_placeholders and field == "company" and lowered == "apoa":
        # APOA is the demo/bot brand in this skill, not the user's startup.
        return ""
    return text


def normalize_user_role(role: object) -> str:
    """Return a supported negotiation role, defaulting safely to founder."""
    normalized = str(role or "founder").strip().lower()
    if normalized not in ("founder", "investor"):
        return "founder"
    return normalized


def role_plan(role: object) -> RolePlan:
    """Map the user's role to create_tokens.py flags and AI env defaults."""
    user_role = normalize_user_role(role)
    if user_role == "investor":
        return RolePlan(
            user_role=user_role,
            user_flag_prefix="--investor-",
            ai_flag_prefix="--founder-",
            ai_env_prefix="FOUNDER_",
        )
    return RolePlan(
        user_role=user_role,
        user_flag_prefix="--founder-",
        ai_flag_prefix="--investor-",
        ai_env_prefix="INVESTOR_",
    )


def resolve_mint_identity(
    constraints: Mapping[str, object],
    *,
    env: Mapping[str, str] | None = None,
) -> MintIdentity:
    """Resolve party display fields for upstream token/config generation."""
    environ = env if env is not None else os.environ
    user_role = normalize_user_role(constraints.get("role"))
    mode = str(constraints.get("mode") or "demo").strip().lower()
    is_demo = mode == "demo"

    demo_founder_name = "Demo Founder" if is_demo and user_role == "investor" else ""
    demo_founder_title = "CEO" if is_demo and user_role == "investor" else ""
    demo_investor_name = "Demo Investor" if is_demo and user_role == "founder" else ""
    demo_investor_firm = "Demo Capital" if is_demo and user_role == "founder" else ""

    return MintIdentity(
        founder_name=(
            identity_value(constraints.get("founder_name"), drop_placeholders=False)
            or identity_value(environ.get("FOUNDER_NAME"))
            or demo_founder_name
            or "Founder"
        ),
        founder_title=(
            identity_value(constraints.get("founder_title"), drop_placeholders=False)
            or identity_value(environ.get("FOUNDER_TITLE"))
            or demo_founder_title
            or "CEO"
        ),
        investor_name=(
            identity_value(constraints.get("investor_name"), drop_placeholders=False)
            or identity_value(environ.get("INVESTOR_NAME"))
            or demo_investor_name
            or "Investor"
        ),
        investor_firm=(
            identity_value(constraints.get("investor_firm"), drop_placeholders=False)
            or identity_value(environ.get("INVESTOR_FIRM"))
            or demo_investor_firm
            or "Investor Firm"
        ),
        company=(
            identity_value(constraints.get("company_name"), field="company", drop_placeholders=False)
            or identity_value(environ.get("COMPANY_NAME"), field="company")
            or "Company"
        ),
    )


def slugify_company(company: str) -> str:
    return "".join(
        c.lower() if c.isalnum() else "-" for c in company
    ).strip("-")


def build_service_name(company: str, negotiation_id: str) -> str:
    return f"safe:{slugify_company(company)}:{negotiation_id}"


def resolve_investment_amount(constraints: Mapping[str, object]) -> float:
    """Return the legacy single check size for upstream SAFE generation."""
    amount = (
        constraints.get("investment_amount")
        or constraints.get("investment_amount_min")
        or 500_000.0
    )
    return float(amount)


def resolve_investment_amount_bounds(constraints: Mapping[str, object]) -> tuple[float, float]:
    amount = resolve_investment_amount(constraints)
    amount_min = float(constraints.get("investment_amount_min") or amount)
    amount_max = float(constraints.get("investment_amount_max") or amount)
    return amount_min, amount_max


def build_create_tokens_cmd(
    *,
    repo: str | Path,
    negotiation_id: str,
    constraints: Mapping[str, object],
    neg_dir: str | Path,
    expires_str: str,
    service: str,
    shared_session: bool = False,
    env: Mapping[str, str] | None = None,
    python_executable: str | None = None,
) -> tuple[list[str], str]:
    """Build the upstream create_tokens.py command and normalized user role."""
    environ = env if env is not None else os.environ
    repo_path = Path(repo)
    neg_path = Path(neg_dir)
    plan = role_plan(constraints.get("role"))
    identity = resolve_mint_identity(constraints, env=environ)

    pro_rata_required = constraints.get("pro_rata") == "required"
    mfn_required = constraints.get("mfn") == "required"
    discount_min = float(constraints.get("discount_min", 0.20))
    discount_max = float(constraints.get("discount_max", discount_min))
    amount = resolve_investment_amount(constraints)
    amount_min, amount_max = resolve_investment_amount_bounds(constraints)

    cmd = [
        python_executable or sys.executable,
        str(repo_path / "create_tokens.py"),
        "--negotiation-id", negotiation_id,
        "--principal-id", environ.get("USER_DID") or "did:apoa:default",
        "--expires", expires_str,
        "--service", service,
        "--company-name", identity.company,
        "--founder-name", identity.founder_name,
        "--founder-title", identity.founder_title,
        "--investor-name", identity.investor_name,
        "--investor-firm", identity.investor_firm,
        "--investment-amount", str(amount),
        f"{plan.user_flag_prefix}cap-min", str(constraints["valuation_cap_min"]),
        f"{plan.user_flag_prefix}cap-max", str(constraints["valuation_cap_max"]),
        f"{plan.user_flag_prefix}investment-amount-min", str(amount_min),
        f"{plan.user_flag_prefix}investment-amount-max", str(amount_max),
        f"{plan.user_flag_prefix}discount-min", str(constraints["discount_min"]),
        f"{plan.user_flag_prefix}discount-max", str(discount_max),
        f"{plan.user_flag_prefix}pro-rata-required",
        "true" if pro_rata_required else "false",
        f"{plan.user_flag_prefix}mfn-required",
        "true" if mfn_required else "false",
        "--keys-dir", str(neg_path / "keys"),
        "--tokens-dir", str(neg_path / "tokens"),
        "--config-dir", str(neg_path),
        "--create-keys",
    ]

    if shared_session:
        cmd.extend(["--role", plan.user_role])
    else:
        for env_key_suffix, flag_suffix in AI_FLAG_SUFFIXES.items():
            val = environ.get(f"{plan.ai_env_prefix}{env_key_suffix}")
            if val:
                cmd.extend([f"{plan.ai_flag_prefix}{flag_suffix}", val])

    return cmd, plan.user_role
