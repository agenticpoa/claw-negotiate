from __future__ import annotations

import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).parent.parent
SCRIPT = REPO_ROOT / "scripts" / "build_clawhub_package.py"


def test_build_clawhub_package_is_lean(tmp_path):
    out = tmp_path / "pkg"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--out", str(out)],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert (out / "SKILL.md").exists()
    assert (out / "README.md").exists()
    assert (out / "requirements.txt").exists()
    assert (out / "negotiate_safe" / "run_safe.py").exists()
    assert (out / "negotiate_safe" / "documents" / "fonts_text" / "Inter-Regular.ttf.txt").exists()
    assert not (out / "tests").exists()
    assert not (out / "hooks").exists()
    assert not (out / "assets").exists()
    assert not (out / "docs").exists()
