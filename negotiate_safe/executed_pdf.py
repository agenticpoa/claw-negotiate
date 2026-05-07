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
import base64
import binascii
import struct
import zlib
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


def _wrap(text: str, width: int = 86) -> list[str]:
    words = str(text or "").split()
    if not words:
        return [""]
    lines: list[str] = []
    current = ""
    for word in words:
        if len(word) > width:
            if current:
                lines.append(current)
                current = ""
            for i in range(0, len(word), width):
                lines.append(word[i : i + width])
            continue
        candidate = f"{current} {word}".strip()
        if len(candidate) <= width:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


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
            "signature_image": env.get("signature_image", ""),
            "envelope_hash": envelope.get("envelope_hash", ""),
            "image_hash": env.get("image_hash", ""),
            "captured_at": env.get("captured_at", ""),
        })
    return signers


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa = abs(p - a)
    pb = abs(p - b)
    pc = abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def _png_to_pdf_rgb(png_b64: str) -> tuple[int, int, bytes] | None:
    """Return (width, height, zlib-compressed RGB bytes) for common PNGs.

    The sshsign signature pad emits RGBA PNG data URLs. We flatten alpha onto
    white and embed the result directly as a PDF Image XObject, avoiding heavy
    runtime dependencies for public OpenClaw skill installs.
    """
    if not png_b64:
        return None
    if "," in png_b64 and png_b64.startswith("data:"):
        png_b64 = png_b64.split(",", 1)[1]
    try:
        data = base64.b64decode(png_b64, validate=True)
    except (ValueError, binascii.Error):  # type: ignore[name-defined]
        return None
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return None

    pos = 8
    width = height = bit_depth = color_type = 0
    idat = bytearray()
    while pos + 8 <= len(data):
        length = struct.unpack(">I", data[pos : pos + 4])[0]
        chunk_type = data[pos + 4 : pos + 8]
        chunk = data[pos + 8 : pos + 8 + length]
        pos += 12 + length
        if chunk_type == b"IHDR" and len(chunk) >= 13:
            width, height, bit_depth, color_type = struct.unpack(">IIBB", chunk[:10])
        elif chunk_type == b"IDAT":
            idat.extend(chunk)
        elif chunk_type == b"IEND":
            break

    channels_by_type = {0: 1, 2: 3, 6: 4}
    channels = channels_by_type.get(color_type)
    if not width or not height or bit_depth != 8 or not channels:
        return None
    row_len = width * channels
    try:
        raw = zlib.decompress(bytes(idat))
    except zlib.error:
        return None
    expected = (row_len + 1) * height
    if len(raw) < expected:
        return None

    rows: list[bytearray] = []
    offset = 0
    previous = bytearray(row_len)
    for _ in range(height):
        filter_type = raw[offset]
        offset += 1
        row = bytearray(raw[offset : offset + row_len])
        offset += row_len
        for i in range(row_len):
            left = row[i - channels] if i >= channels else 0
            up = previous[i]
            up_left = previous[i - channels] if i >= channels else 0
            if filter_type == 1:
                row[i] = (row[i] + left) & 0xFF
            elif filter_type == 2:
                row[i] = (row[i] + up) & 0xFF
            elif filter_type == 3:
                row[i] = (row[i] + ((left + up) // 2)) & 0xFF
            elif filter_type == 4:
                row[i] = (row[i] + _paeth(left, up, up_left)) & 0xFF
            elif filter_type != 0:
                return None
        rows.append(row)
        previous = row

    rgb = bytearray()
    for row in rows:
        for x in range(width):
            i = x * channels
            if color_type == 0:
                gray = row[i]
                rgb.extend((gray, gray, gray))
            elif color_type == 2:
                rgb.extend(row[i : i + 3])
            else:
                r, g, b, a = row[i], row[i + 1], row[i + 2], row[i + 3]
                inv = 255 - a
                rgb.extend((
                    (r * a + 255 * inv) // 255,
                    (g * a + 255 * inv) // 255,
                    (b * a + 255 * inv) // 255,
                ))
    return width, height, zlib.compress(bytes(rgb), level=9)


class _SimplePDF:
    def __init__(self) -> None:
        self.pages: list[list[str]] = []
        self.images: list[tuple[int, int, bytes]] = []
        self._new_page()

    def _new_page(self) -> None:
        self.pages.append([])

    def new_page(self) -> None:
        self._new_page()

    @staticmethod
    def _escape(text: str) -> str:
        text = text.encode("latin-1", "replace").decode("latin-1")
        return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    def command(self, text: str) -> None:
        self.pages[-1].append(text)

    def text(self, x: float, y: float, text: str = "", *, size: int = 10, bold: bool = False, font: str = "") -> None:
        page = self.pages[-1]
        font_name = font or ("F2" if bold else "F1")
        page.append(f"BT /{font_name} {size} Tf {x:.1f} {y:.1f} Td ({self._escape(text)}) Tj ET")

    def rect(self, x: float, y: float, w: float, h: float, *, gray: float = 0.96) -> None:
        self.command(f"{gray:.3f} g {x:.1f} {y:.1f} {w:.1f} {h:.1f} re f 0 g")

    def stroke_rect(self, x: float, y: float, w: float, h: float, *, gray: float = 0.82) -> None:
        self.command(f"{gray:.3f} G {x:.1f} {y:.1f} {w:.1f} {h:.1f} re S 0 G")

    def rule(self, x: float, y: float, w: float, *, gray: float = 0.65) -> None:
        self.command(f"{gray:.3f} G {x:.1f} {y:.1f} m {x + w:.1f} {y:.1f} l S 0 G")

    def line(self, text: str = "", *, size: int = 10, bold: bool = False) -> None:
        page = self.pages[-1]
        content_lines = len([cmd for cmd in page if cmd.startswith("BT ")])
        if content_lines >= 44:
            self._new_page()
            page = self.pages[-1]
            content_lines = 0
        self.text(54, 760 - content_lines * 16, text, size=size, bold=bold)

    def gap(self) -> None:
        self.line("")

    def add_image(self, png_b64: str, *, x: float, y: float, w: float, h: float) -> bool:
        image = _png_to_pdf_rgb(png_b64)
        if not image:
            return False
        self.images.append(image)
        name = f"Im{len(self.images)}"
        self.command(f"q {w:.1f} 0 0 {h:.1f} {x:.1f} {y:.1f} cm /{name} Do Q")
        return True

    def write(self, path: Path) -> None:
        objects: list[bytes] = [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [%s] /Count %d >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>",
        ]
        image_obj_ids: list[int] = []
        for width, height, compressed in self.images:
            image_obj_ids.append(len(objects) + 1)
            objects.append(
                b"<< /Type /XObject /Subtype /Image "
                b"/Width " + str(width).encode() + b" /Height " + str(height).encode() + b" "
                b"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode "
                b"/Length " + str(len(compressed)).encode() + b" >>\nstream\n"
                + compressed
                + b"\nendstream"
            )
        page_obj_ids: list[int] = []
        for page in self.pages:
            content = ("\n".join(page) + "\n").encode("latin-1", "replace")
            content_obj_id = len(objects) + 1
            objects.append(b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"endstream")
            page_obj_id = len(objects) + 1
            xobject = b" ".join(
                f"/Im{i} {obj_id} 0 R".encode()
                for i, obj_id in enumerate(image_obj_ids, start=1)
            )
            objects.append(
                b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                b"/Resources << /Font << /F1 3 0 R /F2 4 0 R /F3 5 0 R >> "
                b"/XObject << " + xobject + b" >> >> "
                b"/Contents " + str(content_obj_id).encode() + b" 0 R >>"
            )
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

    def page_header(title: str, subtitle: str = "") -> None:
        pdf.text(54, 744, title, size=24, bold=True)
        if subtitle:
            pdf.text(54, 724, subtitle, size=10)
        pdf.rule(54, 708, 504)

    def label_value(y: float, label: str, value: str, *, x: float = 78, w: float = 456) -> float:
        pdf.text(x, y, label, size=9, bold=True)
        for line in _wrap(value, 70):
            pdf.text(x + 148, y, line, size=9)
            y -= 13
        return y

    investor_line = parties["investor"]["name"]
    if parties["investor"]["firm"]:
        investor_line += f" at {parties['investor']['firm']}"

    page_header("SAFE Agreement", f"Negotiation {negotiation_id} · Executed {terms['date']}")
    pdf.rect(54, 548, 504, 138, gray=0.965)
    y = 660
    y = label_value(y, "Company", parties["founder"]["company"])
    y = label_value(y, "Founder", f"{parties['founder']['name']}, {parties['founder']['title']}".rstrip(", "))
    y = label_value(y, "Investor", investor_line)
    y = label_value(y, "Execution date", terms["date"])

    pdf.text(54, 506, "Final Terms", size=16, bold=True)
    pdf.rect(54, 342, 504, 142, gray=0.975)
    y = 456
    y = label_value(y, "Purchase amount", _money(terms.get("investment_amount")))
    y = label_value(y, "Valuation cap", _money(terms.get("valuation_cap")))
    y = label_value(y, "Discount", _percent(terms.get("discount_rate")))
    y = label_value(y, "Pro-rata rights", _yes_no(terms.get("pro_rata")))
    y = label_value(y, "MFN", _yes_no(terms.get("mfn")))

    pdf.text(54, 294, "Certificate of Execution", size=16, bold=True)
    certificate = (
        "This certificate attests that the preceding SAFE agreement has been "
        "approved by all listed parties and cryptographically signed via sshsign."
    )
    y = 272
    for line in _wrap(certificate, 82):
        pdf.text(54, y, line, size=10)
        y -= 14
    pdf.rect(54, 112, 504, 104, gray=0.965)
    y = 190
    y = label_value(y, "Document SHA-256", doc_hash, x=72)
    y = label_value(y, "Negotiation ID", negotiation_id, x=72)
    if elapsed is not None:
        y = label_value(y, "Duration", f"{elapsed:.1f} seconds", x=72)
    label_value(y, "Signatories", str(len(signers)), x=72)

    pdf.new_page()
    page_header("Signatures", "Handwritten signatures are sealed in sshsign evidence envelopes.")
    y = 650
    for signer in signers:
        if y < 260:
            pdf.new_page()
            page_header("Signatures")
            y = 650
        pdf.rect(54, y - 174, 504, 184, gray=0.975)
        pdf.stroke_rect(54, y - 174, 504, 184)
        pdf.text(72, y, f"{signer['role']}: {signer['name']}", size=13, bold=True)
        org_line = signer["organization"]
        if signer["title"]:
            org_line = f"{signer['title']} · {org_line}".strip(" ·")
        pdf.text(72, y - 18, org_line, size=10)
        image_drawn = pdf.add_image(signer.get("signature_image", ""), x=342, y=y - 126, w=178, h=66)
        if not image_drawn:
            pdf.text(362, y - 96, "[signature image unavailable]", size=9)
        pdf.rule(334, y - 136, 198)
        pdf.text(374, y - 150, "Drawn signature", size=8)
        meta_y = y - 50
        meta_y = label_value(meta_y, "Pending ID", signer["pending_id"], x=72)
        meta_y = label_value(meta_y, "Key ID", signer["key_id"], x=72)
        if signer["captured_at"]:
            meta_y = label_value(meta_y, "Signed at", signer["captured_at"], x=72)
        if signer["envelope_hash"]:
            meta_y = label_value(meta_y, "Envelope SHA-256", signer["envelope_hash"], x=72)
        if signer["image_hash"]:
            label_value(meta_y, "Image SHA-256", signer["image_hash"], x=72)
        y -= 210

    pdf.new_page()
    page_header("Negotiation Audit Trail", "Every offer was recorded to sshsign before display.")
    y = 670
    for event in events:
        if y < 96:
            pdf.new_page()
            page_header("Negotiation Audit Trail")
            y = 670
        terms_text = event.get("terms") or {}
        title = (
            f"Offer {int(event.get('round', 0)) + 1}: "
            f"{str(event.get('party', '')).capitalize()} · {event.get('type')}"
        )
        pdf.text(54, y, title, size=11, bold=True)
        y -= 16
        summary = (
            f"Cap {_money(terms_text.get('valuation_cap'))}; "
            f"check {_money(terms_text.get('investment_amount'))}; "
            f"discount {_percent(terms_text.get('discount_rate'))}; "
            f"pro-rata {_yes_no(terms_text.get('pro_rata'))}; "
            f"MFN {_yes_no(terms_text.get('mfn'))}"
        )
        for line in _wrap(summary, 92):
            pdf.text(72, y, line, size=9)
            y -= 13
        if event.get("audit_tx"):
            pdf.text(72, y, f"Audit tx: {event['audit_tx']}", size=8, font="F3")
            y -= 13
        y -= 8
    pdf.write(pdf_path)
    return pdf_path if pdf_path.exists() else None
