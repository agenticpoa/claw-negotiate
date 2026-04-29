from __future__ import annotations

import links


class TestBuildInviteUrl:
    def test_empty_without_base(self):
        assert links.build_invite_url("INV-7K3X9", base_url="") == ""

    def test_base_url(self):
        assert (
            links.build_invite_url("INV-X", base_url="https://staging.example.com/")
            == "https://staging.example.com/join/INV-X"
        )

    def test_empty_code_returns_empty(self):
        assert links.build_invite_url("", base_url="https://x/") == ""
        assert links.build_invite_url("   ", base_url="https://x/") == ""


class TestExtractBindCode:
    def test_extracts_from_plain_bind(self):
        assert links.extract_bind_code("/bind INV-7K3X9") == "INV-7K3X9"

    def test_extracts_with_bot_suffix(self):
        assert links.extract_bind_code("/bind@AgenticPOA_bot INV-ABC1") == "INV-ABC1"

    def test_lowercase_input_uppercased(self):
        assert links.extract_bind_code("/bind inv-zzz9") == "INV-ZZZ9"

    def test_standalone_code(self):
        assert links.extract_bind_code("INV-FOO") == "INV-FOO"

    def test_returns_none_on_no_code(self):
        assert links.extract_bind_code("/bind") is None
        assert links.extract_bind_code("") is None
        assert links.extract_bind_code("random text") is None
