from __future__ import annotations

import base64
import json
import struct
import zlib

import executed_pdf


def _png_rgba_base64() -> str:
    width, height = 2, 2
    raw = b"".join(
        b"\x00" + b"\x00\x00\x00\xff" * width
        for _ in range(height)
    )

    def chunk(kind: bytes, data: bytes) -> bytes:
        import binascii

        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", binascii.crc32(body) & 0xFFFFFFFF)

    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )
    return base64.b64encode(png).decode()


def test_png_signature_image_converts_to_pdf_rgb():
    image = executed_pdf._png_to_pdf_rgb(_png_rgba_base64())

    assert image is not None
    width, height, compressed = image
    assert (width, height) == (2, 2)
    assert len(zlib.decompress(compressed)) == 12


def test_finalize_embeds_signature_images(tmp_path, monkeypatch):
    neg_dir = tmp_path / "neg"
    neg_dir.mkdir()
    founder_cfg = neg_dir / "founder.json"
    investor_cfg = neg_dir / "investor.json"
    founder_cfg.write_text(json.dumps({
        "name": "Juan Figuera",
        "title": "CEO",
        "company_name": "Avocado",
    }))
    investor_cfg.write_text(json.dumps({
        "name": "Nora Vassileva",
        "investor_firm": "SD Capital",
    }))
    (tmp_path / "mint.json").write_text(json.dumps({
        "negotiation_id": "neg_test",
        "founder_config_path": str(founder_cfg),
        "investor_config_path": str(investor_cfg),
    }))
    (tmp_path / "config.json").write_text(json.dumps({"constraints": {}}))
    (tmp_path / "neg_test_founder_pending.txt").write_text("pnd_f")
    (tmp_path / "neg_test_investor_pending.txt").write_text("pnd_i")

    monkeypatch.setattr(executed_pdf, "ssh_history", lambda *a, **k: [
        {
            "round": 0,
            "from": "founder",
            "type": "accept",
            "metadata": json.dumps({
                "valuation_cap": 24_000_000,
                "investment_amount": 600_000,
                "discount_rate": 0,
                "pro_rata": True,
                "mfn": False,
            }),
            "immudb_tx": 1,
            "created_at": "2026-05-07T00:00:00Z",
        },
    ])

    png = _png_rgba_base64()
    monkeypatch.setattr(executed_pdf, "_run_ssh_json", lambda _host, args, runner=None: {
        "status": "approved",
        "key_id": "ak_test",
        "envelope_hash": "a" * 64,
        "envelope": {
            "signature_image": png,
            "image_hash": "b" * 64,
            "captured_at": "2026-05-07T00:00:01Z",
        },
    })

    pdf = executed_pdf.finalize_executed_pdf(tmp_path, "pnd_f", "sshsign.test")

    assert pdf is not None
    data = pdf.read_bytes()
    assert b"/Subtype /Image" in data
    assert b"Signatures" in data
