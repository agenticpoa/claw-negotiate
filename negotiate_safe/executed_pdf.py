"""Local executed SAFE PDF generation.

This module keeps the OpenClaw skill self-contained. It deliberately uses only
the Python standard library so a fresh skill install can produce an executed
artifact without cloning the separate negotiate engine.
"""
from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from minting import identity_value
from upstream import ssh_history, synthesize_offer_event


def _money(value: object, *, decimals: bool = False) -> str:
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return str(value or "")
    if decimals:
        return f"${amount:,.2f}"
    return f"${amount:,.0f}"


def _percent(value: object) -> str:
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return str(value or "")
    if amount.is_integer():
        return f"{int(amount)}%"
    return f"{amount:g}%"


def _yes_no(value: object) -> str:
    return "Yes" if bool(value) else "No"


def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _maybe_load_cfg(mint: dict, role: str) -> dict:
    path = mint.get(f"{role}_config_path", "")
    return _load_json(Path(path)) if path else {}


def _pick(
    *,
    configs: list[tuple[dict, str]],
    constraints: dict,
    constraint_key: str,
    env_key: str,
    default: str,
    field: str = "",
) -> str:
    for cfg, key in configs:
        value = identity_value(cfg.get(key), field=field)
        if value:
            return value
    value = identity_value(
        constraints.get(constraint_key),
        field=field,
        drop_placeholders=False,
    )
    if value:
        return value
    return identity_value(os.environ.get(env_key), field=field) or default


def _run_ssh_json(host: str, args: list[str], runner=subprocess.run) -> dict:
    remote_cmd = " ".join(shlex.quote(a) for a in args)
    result = runner(
        ["ssh", host, remote_cmd],
        capture_output=True,
        text=True,
        timeout=20,
    )
    if result.returncode != 0:
        return {"error": (result.stderr or result.stdout or "ssh command failed").strip()}
    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return {"error": "invalid sshsign JSON response"}
    return data if isinstance(data, dict) else {"value": data}


def _history(output_dir: Path, negotiation_id: str, sshsign_host: str) -> list[dict]:
    rows = ssh_history(negotiation_id, sshsign_host=sshsign_host) or []
    events: list[dict] = []
    for row in rows:
        event = synthesize_offer_event(row)
        if not event:
            continue
        event = dict(event)
        event["audit_tx"] = row.get("immudb_tx") or row.get("audit_tx_id")
        event["created_at"] = row.get("created_at") or row.get("timestamp")
        events.append(event)
    return events


def _agreed_terms(events: list[dict]) -> dict:
    for event in reversed(events):
        if event.get("type") == "accept" and isinstance(event.get("terms"), dict):
            return dict(event["terms"])
    return dict(events[-1].get("terms") or {}) if events else {}


def _history_elapsed_seconds(events: list[dict]) -> float | None:
    parsed = []
    for event in events:
        raw = event.get("created_at")
        if not raw:
            continue
        text = str(raw).replace("Z", "+00:00")
        try:
            parsed.append(datetime.fromisoformat(text))
        except ValueError:
            continue
    if len(parsed) < 2:
        return None
    return max(0.0, (max(parsed) - min(parsed)).total_seconds())


def _pending_ids(output_dir: Path, negotiation_id: str) -> dict[str, str]:
    ids: dict[str, str] = {}
    for role in ("founder", "investor"):
        path = output_dir / f"{negotiation_id}_{role}_pending.txt"
        try:
            pending_id = path.read_text(encoding="utf-8").strip()
        except OSError:
            pending_id = ""
        if pending_id:
            ids[role] = pending_id
    return ids


def _collect_signers(
    output_dir: Path,
    negotiation_id: str,
    parties: dict,
    sshsign_host: str,
) -> list[dict]:
    signers: list[dict] = []
    for role, pending_id in _pending_ids(output_dir, negotiation_id).items():
        envelope = _run_ssh_json(sshsign_host, ["get-envelope", "--id", pending_id])
        env = envelope.get("envelope") if isinstance(envelope.get("envelope"), dict) else {}
        if role == "founder":
            label = parties["founder"]["name"]
            org = parties["founder"]["company"]
            title = parties["founder"]["title"]
        else:
            label = parties["investor"]["name"]
            org = parties["investor"]["firm"]
            title = "Investor"
        signers.append({
            "role": role.capitalize(),
            "name": label,
            "title": title,
            "organization": org,
            "pending_id": pending_id,
            "status": envelope.get("status", ""),
            "key_id": envelope.get("key_id", ""),
            "signature": envelope.get("signature", ""),
            "envelope_hash": envelope.get("envelope_hash", ""),
            "image_hash": env.get("image_hash", ""),
            "captured_at": env.get("captured_at", ""),
        })
    return signers


class _SimplePDF:
    def __init__(self) -> None:
        self.pages: list[list[str]] = []
        self._new_page()

    def _new_page(self) -> None:
        self.pages.append([])

    @staticmethod
    def _escape(text: str) -> str:
        text = text.encode("latin-1", "replace").decode("latin-1")
        return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    def line(self, text: str = "", *, size: int = 10, bold: bool = False) -> None:
        page = self.pages[-1]
        if len(page) >= 44:
            self._new_page()
            page = self.pages[-1]
        font = "F2" if bold else "F1"
        page.append(f"BT /{font} {size} Tf 54 {760 - len(page) * 16} Td ({self._escape(text)}) Tj ET")

    def gap(self) -> None:
        self.line("")

    def write(self, path: Path) -> None:
        objects: list[bytes] = [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [%s] /Count %d >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>",
        ]
        page_obj_ids: list[int] = []
        content_obj_ids: list[int] = []
        for page in self.pages:
            content = ("\n".join(page) + "\n").encode("latin-1", "replace")
            content_obj_id = len(objects) + 1
            objects.append(b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"endstream")
            page_obj_id = len(objects) + 1
            objects.append(
                b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                b"/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> "
                b"/Contents " + str(content_obj_id).encode() + b" 0 R >>"
            )
            content_obj_ids.append(content_obj_id)
            page_obj_ids.append(page_obj_id)
        kids = b" ".join(f"{obj_id} 0 R".encode() for obj_id in page_obj_ids)
        objects[1] = objects[1] % (kids, len(page_obj_ids))

        out = bytearray(b"%PDF-1.4\n")
        offsets = [0]
        for index, obj in enumerate(objects, start=1):
            offsets.append(len(out))
            out.extend(f"{index} 0 obj\n".encode())
            out.extend(obj)
            out.extend(b"\nendobj\n")
        xref = len(out)
        out.extend(f"xref\n0 {len(objects) + 1}\n".encode())
        out.extend(b"0000000000 65535 f \n")
        for offset in offsets[1:]:
            out.extend(f"{offset:010d} 00000 n \n".encode())
        out.extend(
            f"trailer << /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n".encode()
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(bytes(out))


def _document_hash(negotiation_id: str, terms: dict, parties: dict, events: list[dict]) -> str:
    payload = {
        "negotiation_id": negotiation_id,
        "terms": terms,
        "parties": parties,
        "events": events,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def finalize_executed_pdf(
    output_dir: Path,
    pending_id: str,
    sshsign_host: str,
) -> Path | None:
    mint = _load_json(output_dir / "mint.json")
    config = _load_json(output_dir / "config.json")
    constraints = config.get("constraints") or {}
    negotiation_id = mint.get("negotiation_id") or ""
    if not negotiation_id:
        return None

    founder_cfg = _maybe_load_cfg(mint, "founder")
    investor_cfg = _maybe_load_cfg(mint, "investor")
    parties = {
        "founder": {
            "company": _pick(
                configs=[(founder_cfg, "company_name")],
                constraints=constraints,
                constraint_key="company_name",
                env_key="COMPANY_NAME",
                default="Company",
                field="company",
            ),
            "name": _pick(
                configs=[(founder_cfg, "name"), (founder_cfg, "founder_name")],
                constraints=constraints,
                constraint_key="founder_name",
                env_key="FOUNDER_NAME",
                default="Founder",
            ),
            "title": _pick(
                configs=[(founder_cfg, "title")],
                constraints=constraints,
                constraint_key="founder_title",
                env_key="FOUNDER_TITLE",
                default="",
            ),
        },
        "investor": {
            "name": _pick(
                configs=[(investor_cfg, "name"), (investor_cfg, "investor_name")],
                constraints=constraints,
                constraint_key="investor_name",
                env_key="INVESTOR_NAME",
                default="Investor",
            ),
            "firm": _pick(
                configs=[(investor_cfg, "firm"), (investor_cfg, "investor_firm")],
                constraints=constraints,
                constraint_key="investor_firm",
                env_key="INVESTOR_FIRM",
                default="",
            ),
        },
    }

    events = _history(output_dir, negotiation_id, sshsign_host)
    terms = _agreed_terms(events)
    if not terms:
        return None
    terms.setdefault("investment_amount", founder_cfg.get("investment_amount") or investor_cfg.get("investment_amount") or 0)
    terms["date"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    signers = _collect_signers(output_dir, negotiation_id, parties, sshsign_host)
    if not signers:
        return None
    doc_hash = _document_hash(negotiation_id, terms, parties, events)
    elapsed = _history_elapsed_seconds(events)

    config_anchor = mint.get("founder_config_path") or mint.get("investor_config_path") or ""
    neg_dir = Path(config_anchor).parent if config_anchor else output_dir
    pdf_path = neg_dir / "output" / f"{negotiation_id}_executed.pdf"

    pdf = _SimplePDF()
    pdf.line("SAFE Agreement", size=22, bold=True)
    pdf.line(f"Negotiation ID: {negotiation_id}")
    pdf.line(f"Date: {terms['date']}")
    pdf.gap()
    pdf.line("Parties", size=14, bold=True)
    pdf.line(f"Company: {parties['founder']['company']}")
    pdf.line(f"Founder: {parties['founder']['name']}, {parties['founder']['title']}".rstrip(", "))
    investor_line = parties["investor"]["name"]
    if parties["investor"]["firm"]:
        investor_line += f" at {parties['investor']['firm']}"
    pdf.line(f"Investor: {investor_line}")
    pdf.gap()
    pdf.line("Final Terms", size=14, bold=True)
    pdf.line(f"Purchase amount: {_money(terms.get('investment_amount'))}")
    pdf.line(f"Valuation cap: {_money(terms.get('valuation_cap'))}")
    pdf.line(f"Discount: {_percent(terms.get('discount_rate'))}")
    pdf.line(f"Pro-rata rights: {_yes_no(terms.get('pro_rata'))}")
    pdf.line(f"MFN: {_yes_no(terms.get('mfn'))}")
    pdf.gap()
    pdf.line("Execution Certificate", size=14, bold=True)
    pdf.line("This certificate attests that the preceding SAFE agreement was approved")
    pdf.line("by all listed parties and cryptographically signed via sshsign.")
    pdf.line(f"Document SHA-256: {doc_hash}")
    if elapsed is not None:
        pdf.line(f"Negotiation duration: {elapsed:.1f} seconds")
    pdf.line(f"Signatories: {len(signers)}")
    pdf.gap()
    pdf.line("Signatures", size=14, bold=True)
    for signer in signers:
        pdf.line(f"{signer['role']}: {signer['name']} ({signer['organization']})", bold=True)
        if signer["title"]:
            pdf.line(f"Title: {signer['title']}")
        pdf.line(f"Pending ID: {signer['pending_id']}")
        pdf.line(f"Key ID: {signer['key_id']}")
        if signer["captured_at"]:
            pdf.line(f"Signed at: {signer['captured_at']}")
        if signer["envelope_hash"]:
            pdf.line(f"Envelope SHA-256: {signer['envelope_hash']}")
        if signer["image_hash"]:
            pdf.line(f"Signature image SHA-256: {signer['image_hash']}")
        pdf.gap()
    pdf.line("Negotiation Audit Trail", size=14, bold=True)
    for event in events:
        terms_text = event.get("terms") or {}
        pdf.line(
            f"Offer {int(event.get('round', 0)) + 1}: "
            f"{str(event.get('party', '')).capitalize()} - {event.get('type')}"
        )
        pdf.line(
            "Terms: "
            f"cap {_money(terms_text.get('valuation_cap'))}, "
            f"check {_money(terms_text.get('investment_amount'))}, "
            f"discount {_percent(terms_text.get('discount_rate'))}, "
            f"pro-rata {_yes_no(terms_text.get('pro_rata'))}"
        )
        if event.get("audit_tx"):
            pdf.line(f"Audit tx: {event['audit_tx']}")
    pdf.write(pdf_path)
    return pdf_path if pdf_path.exists() else None
