from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


REPO_ROOT = Path(__file__).parent.parent
SCRIPT = REPO_ROOT / "scripts" / "smoke_install.py"


def test_smoke_install_script_passes_without_openclaw_requirement():
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--skip-openclaw"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert "smoke check passed" in result.stdout


def test_smoke_install_reports_missing_skill_dir():
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--skill-dir",
            str(REPO_ROOT / "does-not-exist"),
            "--skip-openclaw",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 1
    assert "skill dir missing" in result.stderr


def test_check_bins_can_skip_openclaw():
    import importlib.util

    spec = importlib.util.spec_from_file_location("smoke_install", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    manifest = {"required_bins": ["python3", "openclaw"]}
    with patch.object(module.shutil, "which", side_effect=lambda name: None):
        assert module._check_bins(manifest, skip_openclaw=True) == [
            "missing command: python3"
        ]


@pytest.mark.parametrize("run_doctor", [False])
def test_main_function_success(run_doctor, capsys):
    import importlib.util

    spec = importlib.util.spec_from_file_location("smoke_install_main", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    rc = module.main(["--skip-openclaw"] + (["--run-doctor"] if run_doctor else []))

    assert rc == 0
    assert "smoke check passed" in capsys.readouterr().out
