#!/usr/bin/env python3
"""Static page gate: required files, local links, forbidden strings, image size, img alt.

Stdlib only (Python 3.11+). Exit 0 if clean, 1 if any finding.
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlparse

RULE_REQUIRED = "required-files"
RULE_BROKEN_LINK = "broken-link"
RULE_FORBIDDEN = "forbidden-string"
RULE_IMAGE_SIZE = "image-size"
RULE_IMG_ALT = "img-alt"

SKIP_SCHEMES = ("http:", "https:", "mailto:", "tel:", "data:", "javascript:")


@dataclass(frozen=True)
class Finding:
    path: Path
    line: int
    rule: str
    message: str

    def format(self, root: Path) -> str:
        try:
            rel = self.path.relative_to(root)
        except ValueError:
            rel = self.path
        return f"{rel}:{self.line} · {self.rule} · {self.message}"


@dataclass(frozen=True)
class Config:
    required_files: tuple[str, ...]
    forbidden_patterns: tuple[str, ...]
    max_image_bytes: int


def default_config_path() -> Path:
    env = Path(__file__).resolve().parents[1] / "validate.toml"
    return env


def load_config(path: Path) -> Config:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    required = tuple(data.get("required_files") or ["index.html"])
    forbidden = tuple(
        str(p) for p in (data.get("forbidden") or {}).get("patterns") or []
    )
    max_bytes = int((data.get("images") or {}).get("max_bytes") or 150_000)
    return Config(
        required_files=required,
        forbidden_patterns=forbidden,
        max_image_bytes=max_bytes,
    )


def is_local_ref(ref: str) -> bool:
    value = ref.strip()
    if not value or value.startswith("#"):
        return False
    lower = value.lower()
    if lower.startswith(SKIP_SCHEMES):
        return False
    if lower.startswith("//"):
        return False
    parsed = urlparse(value)
    if parsed.scheme or parsed.netloc:
        return False
    return True


def resolve_local(page_dir: Path, ref: str) -> Path | None:
    """Return absolute path for a local URL, or None if not a file we can check."""
    raw = unquote(ref.split("?", 1)[0].split("#", 1)[0]).strip()
    if not raw or raw.endswith("/"):
        # Directory indexes are out of scope for this gate.
        return None
    path = (page_dir / raw).resolve()
    try:
        path.relative_to(page_dir.resolve())
    except ValueError:
        # Escaped outside the page root — treat as broken.
        return page_dir.resolve() / "__outside__"
    return path


class PageScanner(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.refs: list[tuple[int, str, str]] = []  # line, attr, value
        self.imgs: list[tuple[int, dict[str, str | None]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        line, _ = self.getpos()
        attr_map = {k: v for k, v in attrs}
        if tag == "img":
            self.imgs.append((line, attr_map))
        for key in ("src", "href"):
            value = attr_map.get(key)
            if value:
                self.refs.append((line, key, value))


def check_required(page_dir: Path, config: Config) -> list[Finding]:
    findings: list[Finding] = []
    for name in config.required_files:
        target = page_dir / name
        if not target.is_file():
            findings.append(
                Finding(
                    path=page_dir / "index.html",
                    line=1,
                    rule=RULE_REQUIRED,
                    message=f"missing required file `{name}`",
                )
            )
    return findings


def check_links(page_dir: Path, html_path: Path, html: str) -> list[Finding]:
    scanner = PageScanner()
    try:
        scanner.feed(html)
    except Exception as exc:  # noqa: BLE001 — surface parse issues as findings
        return [
            Finding(
                path=html_path,
                line=1,
                rule=RULE_BROKEN_LINK,
                message=f"HTML parse error: {exc}",
            )
        ]

    findings: list[Finding] = []
    seen: set[tuple[int, str]] = set()
    for line, attr, value in scanner.refs:
        if not is_local_ref(value):
            continue
        key = (line, value)
        if key in seen:
            continue
        seen.add(key)
        resolved = resolve_local(page_dir, value)
        if resolved is None:
            continue
        if not resolved.is_file():
            findings.append(
                Finding(
                    path=html_path,
                    line=line,
                    rule=RULE_BROKEN_LINK,
                    message=f"{attr}=\"{value}\" does not resolve to an existing file",
                )
            )
    return findings


def check_forbidden(
    page_dir: Path, config: Config
) -> list[Finding]:
    findings: list[Finding] = []
    if not config.forbidden_patterns:
        return findings

    patterns = [
        (raw, re.compile(re.escape(raw), re.IGNORECASE))
        for raw in config.forbidden_patterns
    ]

    for path in sorted(page_dir.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() in {
            ".png",
            ".jpg",
            ".jpeg",
            ".gif",
            ".webp",
            ".ico",
            ".woff",
            ".woff2",
            ".ttf",
            ".eot",
            ".pdf",
            ".zip",
        }:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for raw, regex in patterns:
            for match in regex.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                findings.append(
                    Finding(
                        path=path,
                        line=line,
                        rule=RULE_FORBIDDEN,
                        message=f"forbidden string `{raw}`",
                    )
                )
    return findings


def check_image_sizes(
    page_dir: Path, html_path: Path, html: str, config: Config
) -> list[Finding]:
    scanner = PageScanner()
    scanner.feed(html)
    findings: list[Finding] = []
    for line, attrs in scanner.imgs:
        src = attrs.get("src")
        if not src or not is_local_ref(src):
            continue
        resolved = resolve_local(page_dir, src)
        if resolved is None or not resolved.is_file():
            continue
        size = resolved.stat().st_size
        if size > config.max_image_bytes:
            findings.append(
                Finding(
                    path=html_path,
                    line=line,
                    rule=RULE_IMAGE_SIZE,
                    message=(
                        f"image `{src}` is {size} bytes "
                        f"(limit {config.max_image_bytes})"
                    ),
                )
            )
    return findings


def check_img_alt(html_path: Path, html: str) -> list[Finding]:
    scanner = PageScanner()
    scanner.feed(html)
    findings: list[Finding] = []
    for line, attrs in scanner.imgs:
        if "alt" not in attrs:
            src = attrs.get("src") or "?"
            findings.append(
                Finding(
                    path=html_path,
                    line=line,
                    rule=RULE_IMG_ALT,
                    message=f"<img src=\"{src}\"> is missing alt",
                )
            )
    return findings


def validate_page(page_dir: Path, config: Config) -> list[Finding]:
    page_dir = page_dir.resolve()
    findings: list[Finding] = []
    findings.extend(check_required(page_dir, config))

    html_path = page_dir / "index.html"
    if html_path.is_file():
        html = html_path.read_text(encoding="utf-8")
        findings.extend(check_links(page_dir, html_path, html))
        findings.extend(check_forbidden(page_dir, config))
        findings.extend(check_image_sizes(page_dir, html_path, html, config))
        findings.extend(check_img_alt(html_path, html))
    elif "index.html" not in config.required_files:
        # Still scan text files for forbidden strings if index is optional.
        findings.extend(check_forbidden(page_dir, config))

    findings.sort(key=lambda f: (str(f.path), f.line, f.rule, f.message))
    return findings


def print_findings(findings: Iterable[Finding], root: Path) -> None:
    for item in findings:
        print(item.format(root))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate a static page folder before packaging."
    )
    parser.add_argument(
        "page_dir",
        type=Path,
        help="Path to the page folder (must contain index.html unless configured otherwise)",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to validate.toml (default: repo-root validate.toml)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    import os

    parser = build_parser()
    args = parser.parse_args(argv)
    page_dir = args.page_dir
    if not page_dir.is_dir():
        print(f"{page_dir}:1 · required-files · not a directory", file=sys.stderr)
        return 1

    if args.config is not None:
        config_path = args.config
    elif os.environ.get("VALIDATE_TOML"):
        config_path = Path(os.environ["VALIDATE_TOML"])
    else:
        config_path = default_config_path()

    if not config_path.is_file():
        print(
            f"config not found: {config_path}",
            file=sys.stderr,
        )
        return 1

    config = load_config(config_path)
    findings = validate_page(page_dir, config)
    print_findings(findings, page_dir.resolve())
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
