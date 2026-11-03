"""Cut a release: prepare the changelog, check the rules, write the notes, collect the assets.

A release is a reviewed CHANGELOG section plus a button press. Nothing here talks to GitHub or Azure; the release workflow does.

  python -m tools.release prepare --version v1.0.0 --date 2026-11-04    turn "## Unreleased" into the release section (open a pull request)
  python -m tools.release gate    --version v1.0.0                       the rules a release must satisfy (exit 1 with the reasons)
  python -m tools.release notes   --version v1.0.0 --out notes.md       release notes: changelog section, prompt versions, provenance
  python -m tools.release notes   --unreleased --out notes.md           the same for the pending changes (pull request preview)
  python -m tools.release assets  --version v1.0.0 --brief monthly-brief.pdf --out dist     the files attached to the release
  python -m tools.release lint                                           structure of CHANGELOG.md

CHANGELOG.md convention: newest first; `## Unreleased` first, then `## vX.Y.Z — YYYY-MM-DD` sections in descending order,
each with at least one `- ` entry.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import re
import shutil
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .promptlib import ROOT

VERSION_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
LABEL_RE = re.compile(r"^(v\d+\.\d+\.\d+|preview)$")
HEADING_RE = re.compile(r"^## (?P<title>.+?)[ \t]*$", re.M)
RELEASE_TITLE_RE = re.compile(r"^(?P<version>v\d+\.\d+\.\d+)\s+[—–-]\s+(?P<date>\d{4}-\d{2}-\d{2})$")
BULLET_RE = re.compile(r"^[ \t]*[-*] \S", re.M)
UNRELEASED = "Unreleased"
EMPTY_UNRELEASED = "## Unreleased\n\n(nothing yet)\n\n"
ZIP_TIME = (1980, 1, 1, 0, 0, 0)


class ReleaseError(ValueError):
    """The request is not something a release can do; the message says what to change."""


@dataclass
class Section:
    title: str
    heading_start: int
    heading_end: int
    end: int
    body: str

    @property
    def entries(self) -> int:
        return len(BULLET_RE.findall(self.body))


def parse_version(version: str) -> tuple[int, int, int]:
    m = VERSION_RE.match(version or "")
    if not m:
        raise ReleaseError(f"version '{version}' must look like v1.2.3")
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def split_sections(text: str) -> list[Section]:
    text = text.replace("\r\n", "\n")
    heads = list(HEADING_RE.finditer(text))
    out = []
    for i, m in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        out.append(Section(m.group("title"), m.start(), m.end(), end, text[m.end():end].strip("\n")))
    return out


def release_sections(text: str) -> list[tuple[str, Optional[str], Section]]:
    """(version, date, section) for every `## vX.Y.Z — date` section, in file order."""
    found = []
    for s in split_sections(text):
        m = RELEASE_TITLE_RE.match(s.title)
        if m:
            found.append((m.group("version"), m.group("date"), s))
    return found


def find_section(text: str, version: str) -> Optional[Section]:
    for v, _date, s in release_sections(text):
        if v == version:
            return s
    return None


def unreleased(text: str) -> Optional[Section]:
    return next((s for s in split_sections(text) if s.title == UNRELEASED), None)


def lint(text: str) -> list[str]:
    """Structural problems in CHANGELOG.md."""
    text = text.replace("\r\n", "\n")
    problems = []
    secs = split_sections(text)
    seen: list[tuple[int, int, int]] = []
    for i, s in enumerate(secs):
        if s.title == UNRELEASED:
            if i != 0:
                problems.append("'## Unreleased' must be the first section")
            continue
        m = RELEASE_TITLE_RE.match(s.title)
        if not m:
            problems.append(f"section '## {s.title}' must be '## Unreleased' or '## vX.Y.Z — YYYY-MM-DD'")
            continue
        try:
            datetime.date.fromisoformat(m.group("date"))
        except ValueError:
            problems.append(f"section '## {s.title}' has an impossible date")
        v = parse_version(m.group("version"))
        if v in seen:
            problems.append(f"release {m.group('version')} appears twice")
        elif seen and v > min(seen):
            problems.append(f"release {m.group('version')} is listed after an older release; newest first")
        seen.append(v)
        if s.entries == 0:
            problems.append(f"release {m.group('version')} has no '- ' entries")
    if sum(1 for s in secs if s.title == UNRELEASED) > 1:
        problems.append("more than one '## Unreleased' section")
    return problems


def prepare(text: str, version: str, date: str) -> str:
    """The changelog with `## Unreleased` turned into the release section and a fresh empty Unreleased on top."""
    parse_version(version)
    try:
        datetime.date.fromisoformat(date)
    except ValueError:
        raise ReleaseError(f"date '{date}' must look like 2026-11-04") from None
    text = text.replace("\r\n", "\n")
    sec = unreleased(text)
    if sec is None:
        raise ReleaseError("CHANGELOG.md has no '## Unreleased' section")
    if sec.entries == 0:
        raise ReleaseError("'## Unreleased' has no '- ' entries, so there is nothing to release")
    if find_section(text, version):
        raise ReleaseError(f"CHANGELOG.md already has a section for {version}")
    newest = max((parse_version(v) for v, _d, _s in release_sections(text)), default=None)
    if newest and parse_version(version) <= newest:
        raise ReleaseError(f"{version} is not newer than the newest release in CHANGELOG.md (v{'.'.join(map(str, newest))})")
    return text[:sec.heading_start] + EMPTY_UNRELEASED + f"## {version} — {date}" + text[sec.heading_end:]


def git_tags(root: Path) -> list[str]:
    p = subprocess.run(["git", "-C", str(root), "tag", "--list", "v*"], capture_output=True, text=True)
    return [t.strip() for t in p.stdout.splitlines() if t.strip()] if p.returncode == 0 else []


def gate(root: Path, version: str, tags: list[str]) -> list[str]:
    """Why this release must not go out (empty list: it may)."""
    try:
        want = parse_version(version)
    except ReleaseError as ex:
        return [str(ex)]
    problems = []
    existing = [parse_version(t) for t in tags if VERSION_RE.match(t)]
    if version in tags:
        problems.append(f"tag {version} already exists")
    elif existing and want < max(existing):
        problems.append(f"{version} is older than the newest tag v{'.'.join(map(str, max(existing)))}")
    text = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    sec = find_section(text, version)
    if sec is None:
        problems.append(f"CHANGELOG.md has no '## {version} — YYYY-MM-DD' section; run `python -m tools.release prepare --version {version} --date <today>` and merge that change first")
    elif sec.entries == 0:
        problems.append(f"the {version} section of CHANGELOG.md has no '- ' entries")
    pending = unreleased(text)
    if pending is not None and pending.entries:
        problems.append(f"'## Unreleased' still has {pending.entries} entries that would be missing from the {version} notes; "
                        f"merge them into the release section (run prepare again) before releasing")
    problems += [f"CHANGELOG.md: {p}" for p in lint(text)]
    if not (root / "manifest.lock.json").exists():
        problems.append("manifest.lock.json is missing")
    return problems


def read_manifest(root: Path) -> dict:
    p = root / "manifest.lock.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def notes(root: Path, version: Optional[str], commit: str = "", run_url: str = "") -> str:
    """Release notes: the changelog section, the prompt versions at this release, and where the build came from."""
    text = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    if version is None:
        sec = unreleased(text)
        heading, body = "Pending changes (preview, not released)", sec.body if sec else ""
    else:
        sec = find_section(text, version)
        if sec is None:
            raise ReleaseError(f"CHANGELOG.md has no section for {version}")
        heading, body = f"{version}", sec.body
        m = RELEASE_TITLE_RE.match(sec.title)
        if m:
            heading = f"{version} — {m.group('date')}"
    lines = [f"## {heading}", "", body.strip() or "(nothing yet)", "", "## Prompt versions", "",
             "| Prompt | Version | Prompt hash | Output files |", "| --- | --- | --- | --- |"]
    for pid, e in sorted((read_manifest(root).get("prompts") or {}).items()):
        lines.append(f"| `{pid}` | {e.get('version')} | `{str(e.get('promptSha256', ''))[:12]}` | {(e.get('outputs') or {}).get('files', 0)} |")
    lines += ["", "Each prompt is kept in `prompts/`; `manifest.lock.json` ties its version to the files it produced.", ""]
    if version is not None:
        lines += ["## Attached files", "",
                  f"- `monthly-business-brief-{version}.pdf`: the Monthly Business Brief (six pages), built from the seed data at this commit.",
                  f"- `ontology-brief-{version}.md`: every entity, synonym and PII flag of the ontology, for review.",
                  f"- `fabric-items-{version}.zip`: the ontology and the semantic model as Fabric item definitions, exactly what the deployment pipeline sends.",
                  f"- `prompts-{version}.zip`: the prompt library with `skills.lock` and `manifest.lock.json`.",
                  "- `SHA256SUMS`: a checksum for every file above.", ""]
    if commit or run_url:
        lines += ["## Provenance", ""]
        if commit:
            lines.append(f"- Commit: `{commit}`")
        if run_url:
            lines.append(f"- Built by: {run_url}")
        lines.append("")
    return "\n".join(lines)


def read_normalised(path: Path) -> bytes:
    data = path.read_bytes()
    return data if b"\0" in data[:8000] else data.replace(b"\r\n", b"\n")


def tree_files(root: Path, folder: str) -> list[str]:
    base = root / folder
    return sorted(p.relative_to(root).as_posix() for p in base.rglob("*")
                  if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc")


def write_zip(dest: Path, root: Path, files: list[str]) -> None:
    """Same bytes for the same files: sorted entries, a fixed timestamp, fixed permissions, normalised line endings."""
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for rel in sorted(files):
            info = zipfile.ZipInfo(rel, date_time=ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o644 << 16
            z.writestr(info, read_normalised(root / rel), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def assets(root: Path, version: str, brief: Path, out: Path) -> list[Path]:
    """Copy and build the files a release carries, plus SHA256SUMS. Returns them in the order they are listed."""
    if not LABEL_RE.match(version):
        raise ReleaseError(f"version '{version}' must look like v1.2.3 (or 'preview')")
    if not brief.is_file() or brief.read_bytes()[:5] != b"%PDF-":
        raise ReleaseError(f"{brief} is not a PDF; build it first with python -m analysis.monthly_brief.build_brief")
    ontology_brief = root / "docs" / "generated" / "ontology-brief.md"
    if not ontology_brief.is_file():
        raise ReleaseError("docs/generated/ontology-brief.md is missing; run python -m tools.export_fabric --write")
    fabric = tree_files(root, "fabric")
    if not fabric:
        raise ReleaseError("fabric/ has no files to release")
    out.mkdir(parents=True, exist_ok=True)
    pdf = out / f"monthly-business-brief-{version}.pdf"
    md = out / f"ontology-brief-{version}.md"
    fz = out / f"fabric-items-{version}.zip"
    pz = out / f"prompts-{version}.zip"
    shutil.copyfile(brief, pdf)
    md.write_bytes(read_normalised(ontology_brief))
    write_zip(fz, root, fabric)
    write_zip(pz, root, tree_files(root, "prompts") + [f for f in ("skills.lock", "manifest.lock.json") if (root / f).exists()])
    files = [pdf, md, fz, pz]
    sums = out / "SHA256SUMS"
    sums.write_text("".join(f"{sha256_file(f)}  {f.name}\n" for f in files), encoding="utf-8", newline="\n")
    return files + [sums]


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=str(ROOT))
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--version", required=True)
    p.add_argument("--date", default=datetime.date.today().isoformat())
    p = sub.add_parser("gate")
    p.add_argument("--version", required=True)
    p.add_argument("--tags", help="comma separated existing tags (default: git tag --list 'v*')")
    p = sub.add_parser("notes")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--version")
    g.add_argument("--unreleased", action="store_true")
    p.add_argument("--out", required=True)
    p.add_argument("--commit", default="")
    p.add_argument("--run-url", default="")
    p = sub.add_parser("assets")
    p.add_argument("--version", required=True)
    p.add_argument("--brief", required=True)
    p.add_argument("--out", required=True)
    sub.add_parser("lint")
    a = ap.parse_args(argv)
    root = Path(a.root)
    try:
        if a.cmd == "prepare":
            target = root / "CHANGELOG.md"
            target.write_text(prepare(target.read_text(encoding="utf-8"), a.version, a.date), encoding="utf-8", newline="\n")
            print(f"release: CHANGELOG.md now has '## {a.version} — {a.date}'. Next: open a pull request with this change, then run the release workflow.")
            return 0
        if a.cmd == "gate":
            tags = [t.strip() for t in a.tags.split(",") if t.strip()] if a.tags is not None else git_tags(root)
            problems = gate(root, a.version, tags)
            for line in problems:
                print(f"  - {line}")
            print(f"release gate {a.version}: " + (f"{len(problems)} problem(s)" if problems else "ok"))
            return 1 if problems else 0
        if a.cmd == "notes":
            text = notes(root, None if a.unreleased else a.version, a.commit, a.run_url)
            Path(a.out).parent.mkdir(parents=True, exist_ok=True)
            Path(a.out).write_text(text, encoding="utf-8", newline="\n")
            print(f"release: wrote {a.out}")
            return 0
        if a.cmd == "assets":
            for f in assets(root, a.version, Path(a.brief), Path(a.out)):
                print(f"  {f.name}  {f.stat().st_size:,} bytes")
            return 0
        problems = lint((root / "CHANGELOG.md").read_text(encoding="utf-8"))
        for line in problems:
            print(f"  - {line}")
        print(f"release lint: {len(problems)} problem(s)")
        return 1 if problems else 0
    except ReleaseError as ex:
        print(f"release: {ex}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
