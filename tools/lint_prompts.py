"""Lint the versioned prompts in prompts/.

  P001 frontmatter missing or invalid          P009 outputs outside the allowed roots
  P002 required key missing                    P010 guardrails incomplete
  P003 id or area malformed / folder mismatch  P011 body lacks plan-first, a pull request or a no-remote-change statement
  P004 file name does not match the id         P012 placeholder not declared in inputs (unused input is a warning)
  P005 duplicate id                            P013 CHANGELOG.md has no line for this id and version
  P006 version is not semver                   P014 changed against --base without a version bump
  P007 unknown stage                           P015 base ref missing, bump check skipped (warning)
  P008 skill not listed in skills.lock
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

from .promptlib import PLACEHOLDER, ROOT, SEMVER, Prompt, PromptError, content_hash, load_prompts, parse, semver_key

REQUIRED = ("id", "title", "version", "area", "stage", "skills", "inputs", "outputs", "guardrails")
STAGES = {"inspect", "build", "enrich", "consume", "author", "change"}
GUARDRAILS = {"pr-only", "preview-before-deploy", "synthetic-data-only"}
OUTPUT_ROOTS = ("analysis/", "docs/", "fabric/", "kit/model/")
ID_RE = re.compile(r"^([a-z][a-z0-9-]*)\.([a-z][a-z0-9-]*)$")
PROHIBITION = re.compile(r"\bdo not (deploy|publish|create|change|connect)\b", re.I)


@dataclass(frozen=True)
class Finding:
    code: str
    path: str
    message: str
    level: str = "error"


def _skills_used(root: Path) -> set[str]:
    lock = yaml.safe_load((root / "skills.lock").read_text(encoding="utf-8")) or {}
    return set(lock.get("skills_used") or [])


def _base_prompt(root: Path, ref: str, rel: str) -> Optional[Prompt]:
    r = subprocess.run(["git", "-C", str(root), "show", f"{ref}:{rel}"], capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0:
        return None
    try:
        return parse(r.stdout, rel)
    except PromptError:
        return None


def _ref_exists(root: Path, ref: str) -> bool:
    return subprocess.run(["git", "-C", str(root), "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
                          capture_output=True, text=True).returncode == 0


def lint(root: Path = ROOT, base: Optional[str] = None) -> list[Finding]:
    out: list[Finding] = []
    prompts, parse_errors = load_prompts(root)
    for rel, msg in parse_errors:
        out.append(Finding("P001", rel, msg))
    allowed_skills = _skills_used(root)
    changelog_path = root / "CHANGELOG.md"
    changelog = changelog_path.read_text(encoding="utf-8") if changelog_path.exists() else ""
    seen: dict[str, str] = {}
    base_ok = bool(base) and _ref_exists(root, base)
    if base and not base_ok:
        out.append(Finding("P015", "prompts", f"base ref {base!r} not found; version-bump check skipped", "warning"))

    for p in prompts:
        missing = [k for k in REQUIRED if k not in p.meta]
        for k in missing:
            out.append(Finding("P002", p.path, f"missing frontmatter key '{k}'"))
        if missing:
            continue

        m = ID_RE.match(p.id)
        folder = Path(p.path).parent.name
        if not m:
            out.append(Finding("P003", p.path, f"id {p.id!r} must look like area.slug (lower case, digits, hyphens)"))
        else:
            if p.meta["area"] != m.group(1) or folder != m.group(1):
                out.append(Finding("P003", p.path, f"id area {m.group(1)!r}, area key {p.meta['area']!r} and folder {folder!r} must all match"))
            stem = re.sub(r"^\d+-", "", Path(p.path).stem)
            if stem != m.group(2):
                out.append(Finding("P004", p.path, f"file name {stem!r} must match the id slug {m.group(2)!r} (a leading NN- is allowed)"))
        if p.id in seen:
            out.append(Finding("P005", p.path, f"duplicate id {p.id!r} (also in {seen[p.id]})"))
        seen[p.id] = p.path

        if not SEMVER.match(p.version):
            out.append(Finding("P006", p.path, f"version {p.version!r} is not semver (1.2.3)"))
        if p.meta["stage"] not in STAGES:
            out.append(Finding("P007", p.path, f"stage {p.meta['stage']!r} must be one of {sorted(STAGES)}"))

        for s in p.meta["skills"] or []:
            if s not in allowed_skills:
                out.append(Finding("P008", p.path, f"skill {s!r} is not listed in skills.lock"))

        outputs = p.meta["outputs"]
        if not isinstance(outputs, list) or not outputs:
            out.append(Finding("P009", p.path, "outputs must be a non-empty list of repo-relative globs"))
        else:
            for o in outputs:
                o = str(o)
                if o.startswith("/") or ".." in o or "\\" in o or not o.startswith(OUTPUT_ROOTS):
                    out.append(Finding("P009", p.path, f"output {o!r} must stay inside {', '.join(OUTPUT_ROOTS)}"))

        missing_g = GUARDRAILS - set(p.meta["guardrails"] or [])
        if missing_g:
            out.append(Finding("P010", p.path, f"guardrails missing: {sorted(missing_g)}"))

        lower = p.body.lower()
        if "plan first" not in lower:
            out.append(Finding("P011", p.path, "body must ask the agent to plan first"))
        if "pull request" not in lower:
            out.append(Finding("P011", p.path, "body must end in a pull request, never a deployment"))
        if not PROHIBITION.search(p.body):
            out.append(Finding("P011", p.path, "body must say what the agent must not do (do not deploy / publish / create / change / connect)"))

        declared = set((p.meta["inputs"] or {}).keys())
        used = set(PLACEHOLDER.findall(p.body))
        for name in sorted(used - declared):
            out.append(Finding("P012", p.path, f"placeholder {{{{{name}}}}} is not declared in inputs"))
        for name in sorted(declared - used):
            out.append(Finding("P012", p.path, f"input {name!r} is declared but never used", "warning"))

        if m and SEMVER.match(p.version) and not re.search(rf"`{re.escape(p.id)}`\s+{re.escape(p.version)}\b", changelog):
            out.append(Finding("P013", p.path, f"CHANGELOG.md has no line `{p.id}` {p.version}"))

        if base_ok and SEMVER.match(p.version):
            old = _base_prompt(root, base, p.path)
            if old is not None and SEMVER.match(old.version):
                new_v, old_v = semver_key(p.version), semver_key(old.version)
                if new_v < old_v:
                    out.append(Finding("P014", p.path, f"version went backwards: {old.version} on {base}, {p.version} here"))
                elif new_v == old_v and content_hash(old) != content_hash(p):
                    out.append(Finding("P014", p.path, f"prompt changed since {base} but version is still {p.version}; bump it"))
    return out


def report(findings: list[Finding]) -> int:
    gha = os.environ.get("GITHUB_ACTIONS") == "true"
    for f in findings:
        print(f"{f.path}: {f.code} {f.level}: {f.message}")
        if gha:
            print(f"::{f.level} file={f.path}::{f.code} {f.message}")
    errors = sum(1 for f in findings if f.level == "error")
    print(f"lint_prompts: {errors} error(s), {len(findings) - errors} warning(s)")
    return 1 if errors else 0


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Lint the versioned prompts in prompts/")
    ap.add_argument("--root", default=str(ROOT))
    ap.add_argument("--base", help="git ref to compare against (for example origin/main): a changed prompt needs a higher version")
    a = ap.parse_args(argv)
    return report(lint(Path(a.root), a.base))


if __name__ == "__main__":
    sys.exit(main())
