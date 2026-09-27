"""Tests for scripts/validate.py — stdlib gate over example pages."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
VALIDATE = REPO / "scripts" / "validate.py"
GOOD = REPO / "examples" / "pages" / "good"
BROKEN = REPO / "examples" / "pages" / "broken"
PYTHON = sys.executable

RULES = (
    "required-files",
    "broken-link",
    "forbidden-string",
    "image-size",
    "img-alt",
)


def run_validate(page: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [PYTHON, str(VALIDATE), str(page)],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )


def test_good_page_exits_zero() -> None:
    result = run_validate(GOOD)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == ""


def test_broken_page_exits_one() -> None:
    result = run_validate(BROKEN)
    assert result.returncode == 1, result.stdout + result.stderr


def test_broken_page_hits_each_rule_once() -> None:
    result = run_validate(BROKEN)
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(lines) == len(RULES), lines

    by_rule: dict[str, list[str]] = {rule: [] for rule in RULES}
    for line in lines:
        # format: path:line · rule · message
        parts = [p.strip() for p in line.split("·")]
        assert len(parts) == 3, line
        rule = parts[1]
        assert rule in by_rule, line
        by_rule[rule].append(line)

    for rule in RULES:
        assert len(by_rule[rule]) == 1, (rule, by_rule[rule])
