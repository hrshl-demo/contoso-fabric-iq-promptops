"""Synthetic-data and secret guard.

Fails when the repo contains something that looks like real personal data or a credential:
  EMAIL     an e-mail address whose domain is not on the allow list (tools/pii_allowlist.yaml)
  PAN       an Indian PAN number            AADHAAR  a 12-digit number that passes the Verhoeff check digit
  PHONE     a +91 mobile number             CARD     a 13-19 digit number that passes the Luhn check
  SECRET    a private key, cloud or GitHub token, storage key, SAS signature or JWT
  DENIED-ID a tenant or subscription id from the deny list (stored as SHA-256 hashes, so the list does not leak them)

Digit runs inside longer alphanumeric tokens (hashes, ids) are ignored. Matches are masked in the output.
  python -m tools.pii_guard [--root PATH]
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from pathlib import Path
from typing import Iterator, Optional

import yaml

from .promptlib import ROOT

ALLOWLIST = Path(__file__).with_name("pii_allowlist.yaml")
DENYLIST = Path(__file__).with_name("denied_ids.sha256")
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", "out", "_preview", ".cache"}
SKIP_PREFIXES = ("kit/tests/fixtures/",)
SKIP_FILES = {"manifest.lock.json"}
MAX_BYTES = 5_000_000

B = r"(?<![0-9A-Za-z])"
E = r"(?![0-9A-Za-z])"
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})")
PAN = re.compile(B + r"[A-Z]{5}[0-9]{4}[A-Z]" + E)
AADHAAR = re.compile(r"(?<![0-9A-Za-z-])[2-9][0-9]{3}(?: ?[0-9]{4}){2}(?![0-9A-Za-z-])")
PHONE = re.compile(r"(?<![0-9])(?:\+91|0091)[ -]?[6-9][0-9]{9}" + E)
CARD = re.compile(B + r"[0-9]{13,19}" + E)
GUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
SECRETS = [
    ("private key", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----")),
    ("AWS access key", re.compile(B + r"AKIA[0-9A-Z]{16}" + E)),
    ("GitHub token", re.compile(B + r"(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}" + E)),
    ("GitHub fine-grained token", re.compile(r"github_pat_[A-Za-z0-9_]{50,}")),
    ("storage account key", re.compile(r"AccountKey=[A-Za-z0-9+/=]{40,}")),
    ("SAS signature", re.compile(r"[?&]sig=[A-Za-z0-9%]{30,}")),
    ("JWT", re.compile(B + r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("client secret", re.compile(r"(?i)client_?secret\s*[:=]\s*[\"']?[A-Za-z0-9~._-]{12,}")),
]

_VERHOEFF_D = [[0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 2, 3, 4, 0, 6, 7, 8, 9, 5], [2, 3, 4, 0, 1, 7, 8, 9, 5, 6], [3, 4, 0, 1, 2, 8, 9, 5, 6, 7],
               [4, 0, 1, 2, 3, 9, 5, 6, 7, 8], [5, 9, 8, 7, 6, 0, 4, 3, 2, 1], [6, 5, 9, 8, 7, 1, 0, 4, 3, 2], [7, 6, 5, 9, 8, 2, 1, 0, 4, 3],
               [8, 7, 6, 5, 9, 3, 2, 1, 0, 4], [9, 8, 7, 6, 5, 4, 3, 2, 1, 0]]
_VERHOEFF_P = [[0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 5, 7, 6, 2, 8, 3, 0, 9, 4], [5, 8, 0, 3, 7, 9, 6, 1, 4, 2], [8, 9, 1, 6, 0, 4, 3, 5, 2, 7],
               [9, 4, 5, 3, 1, 2, 6, 8, 7, 0], [4, 2, 8, 6, 5, 7, 3, 9, 0, 1], [2, 7, 9, 3, 8, 0, 6, 4, 1, 5], [7, 0, 4, 6, 9, 1, 3, 2, 5, 8]]


def verhoeff_ok(digits: str) -> bool:
    c = 0
    for i, ch in enumerate(reversed(digits)):
        c = _VERHOEFF_D[c][_VERHOEFF_P[i % 8][int(ch)]]
    return c == 0


def luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def mask(value: str) -> str:
    return value if len(value) <= 6 else value[:2] + "*" * (len(value) - 4) + value[-2:]


def load_allowlist() -> dict:
    data = yaml.safe_load(ALLOWLIST.read_text(encoding="utf-8")) if ALLOWLIST.exists() else {}
    return data or {}


def load_denied() -> set[str]:
    if not DENYLIST.exists():
        return set()
    return {ln.split("#")[0].strip().lower() for ln in DENYLIST.read_text(encoding="utf-8").splitlines() if ln.split("#")[0].strip()}


def domain_allowed(domain: str, allowed: list[str]) -> bool:
    d = domain.lower()
    return any(d == a or d.endswith("." + a) for a in (x.lower() for x in allowed))


def iter_files(root: Path) -> Iterator[Path]:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            p = Path(dirpath) / name
            rel = p.relative_to(root).as_posix()
            if name in SKIP_FILES or rel.startswith(SKIP_PREFIXES) or p.stat().st_size > MAX_BYTES:
                continue
            yield p


def scan_text(text: str, rel: str, cfg: dict, denied: set[str]) -> list[tuple[int, str, str]]:
    """Return (line, code, detail) findings for one file's text."""
    allowed_domains = cfg.get("allowed_email_domains") or []
    ignore_values = {str(v) for v in cfg.get("ignore_values") or []}
    found: list[tuple[int, str, str]] = []
    for n, line in enumerate(text.splitlines(), 1):
        for m in EMAIL.finditer(line):
            if not domain_allowed(m.group(1), allowed_domains) and m.group(0) not in ignore_values:
                found.append((n, "EMAIL", f"{mask(m.group(0))} (domain not allowed)"))
        for m in PAN.finditer(line):
            if m.group(0) not in ignore_values:
                found.append((n, "PAN", mask(m.group(0))))
        for m in AADHAAR.finditer(line):
            digits = m.group(0).replace(" ", "")
            if verhoeff_ok(digits) and digits not in ignore_values:
                found.append((n, "AADHAAR", mask(digits)))
        for m in PHONE.finditer(line):
            if m.group(0) not in ignore_values:
                found.append((n, "PHONE", mask(m.group(0))))
        for m in CARD.finditer(line):
            if luhn_ok(m.group(0)) and m.group(0) not in ignore_values:
                found.append((n, "CARD", mask(m.group(0))))
        for label, rx in SECRETS:
            if rx.search(line):
                found.append((n, "SECRET", label))
        if denied:
            for m in GUID.finditer(line):
                if hashlib.sha256(m.group(0).lower().encode()).hexdigest() in denied:
                    found.append((n, "DENIED-ID", f"{mask(m.group(0))} is a tenant or subscription id; use an Actions variable"))
    return found


def scan(root: Path = ROOT, cfg: Optional[dict] = None, denied: Optional[set[str]] = None) -> list[tuple[str, int, str, str]]:
    cfg = load_allowlist() if cfg is None else cfg
    denied = load_denied() if denied is None else denied
    ignore_paths = tuple(cfg.get("ignore_paths") or [])
    rule_paths = {code: tuple(paths) for code, paths in (cfg.get("ignore_rule_paths") or {}).items()}
    results: list[tuple[str, int, str, str]] = []
    for p in iter_files(root):
        rel = p.relative_to(root).as_posix()
        if ignore_paths and rel.startswith(ignore_paths):
            continue
        data = p.read_bytes()
        if b"\0" in data[:8000]:
            continue
        for line, code, detail in scan_text(data.decode("utf-8", errors="ignore"), rel, cfg, denied):
            if code in rule_paths and rel.startswith(rule_paths[code]):
                continue
            results.append((rel, line, code, detail))
    return results


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=str(ROOT))
    a = ap.parse_args(argv)
    results = scan(Path(a.root))
    gha = os.environ.get("GITHUB_ACTIONS") == "true"
    for rel, line, code, detail in results[:100]:
        print(f"{rel}:{line}: {code}: {detail}")
        if gha:
            print(f"::error file={rel},line={line}::{code} {detail}")
    if len(results) > 100:
        print(f"... and {len(results) - 100} more")
    print(f"pii_guard: {len(results)} finding(s)")
    return 1 if results else 0


if __name__ == "__main__":
    sys.exit(main())
