from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_prerelease_workflow_publishes_only_alpha_and_rc_tags() -> None:
    workflow = ROOT / ".github" / "workflows" / "publish-prerelease.yml"
    text = workflow.read_text(encoding="utf-8")

    assert '"v*.*.*a*"' in text
    assert '"v*.*.*rc*"' in text
    assert r"v\d+\.\d+\.\d+(a\d+|rc\d+)" in text
    assert "pyproject.toml" in text
    assert "pypa/gh-action-pypi-publish@release/v1" in text


def test_package_version_is_current_prerelease() -> None:
    pyproject = ROOT / "pyproject.toml"
    init = ROOT / "penguiflow" / "__init__.py"

    pyproject_text = pyproject.read_text(encoding="utf-8")
    init_text = init.read_text(encoding="utf-8")

    pyproject_match = re.search(r'^version = "([^"]+)"', pyproject_text, re.MULTILINE)
    init_match = re.search(r'^__version__ = "([^"]+)"', init_text, re.MULTILINE)

    assert pyproject_match is not None
    assert init_match is not None
    assert pyproject_match.group(1) == init_match.group(1)
