"""Shared helpers: locate the repo root, parse prompt files, hash them."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
PROMPTS_DIR = "prompts"

_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n(.*)\Z", re.S)
SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


class PromptError(ValueError):
    """A prompt file that cannot be parsed."""


@dataclass(frozen=True)
class Prompt:
    path: str                      # repo-relative, posix separators
    meta: dict[str, Any]
    body: str

    @property
    def id(self) -> str:
        return str(self.meta.get("id", ""))

    @property
    def version(self) -> str:
        return str(self.meta.get("version", ""))


def normalize(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")


def parse(text: str, path: str) -> Prompt:
    m = _FRONTMATTER.match(normalize(text))
    if not m:
        raise PromptError(f"{path}: YAML frontmatter (--- ... ---) is missing")
    try:
        meta = yaml.safe_load(m.group(1))
    except yaml.YAMLError as ex:
        raise PromptError(f"{path}: frontmatter is not valid YAML: {ex}") from ex
    if not isinstance(meta, dict):
        raise PromptError(f"{path}: frontmatter must be a mapping")
    return Prompt(path, meta, m.group(2))


def load_prompts(root: Path = ROOT) -> tuple[list[Prompt], list[tuple[str, str]]]:
    """Every prompt under prompts/ (README.md files excluded) and the parse errors."""
    prompts: list[Prompt] = []
    errors: list[tuple[str, str]] = []
    for p in sorted((root / PROMPTS_DIR).rglob("*.md")):
        if p.name.lower() == "readme.md":
            continue
        rel = p.relative_to(root).as_posix()
        try:
            prompts.append(parse(p.read_text(encoding="utf-8"), rel))
        except PromptError as ex:
            errors.append((rel, str(ex)))
    return prompts, errors


def semver_key(version: str) -> tuple[int, int, int]:
    m = SEMVER.match(version)
    if not m:
        raise ValueError(f"not a semantic version: {version!r}")
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def content_hash(p: Prompt) -> str:
    """Hash of everything that changes what the prompt does: the frontmatter without `version`, plus the body."""
    meta = {k: v for k, v in p.meta.items() if k != "version"}
    canon = yaml.safe_dump(meta, sort_keys=True, allow_unicode=True) + "\n---\n" + p.body.strip() + "\n"
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()
