"""Read-only access to the source repo.

The kit never writes inside the repo. Ways to point it at the data:
  --repo PATH   an existing checkout (read-only use)
  --clone       fetch exactly the pinned commit into a scratch cache outside the repo
  (neither)     operator commands look for ~/contoso-retail-superapp4 at the pinned commit, else fetch it as above

Fetching the pinned commit never touches the operator's own checkout. Sources are tried cheapest first: the checkout's own
object store through a file:// URL (no network, read only), then the remote and SSH command that checkout itself uses (so
whatever deploy key lets that checkout fetch also lets this kit fetch), then the public HTTPS URL.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Optional, Sequence

from . import REPO_BRANCH, REPO_COMMIT, REPO_URL

Source = tuple[str, Optional[str]]                      # (git URL, GIT_SSH_COMMAND or None)
_CRED_RE = re.compile(r"(//)[^/@\s]+@")


def mask(text: str) -> str:
    """Hide credentials embedded in URLs before anything is printed."""
    return _CRED_RE.sub(r"\1<credentials>@", text or "")


def _run(cmd: list[str], env: Optional[dict] = None, timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)


def git_head(repo: Path) -> Optional[str]:
    """HEAD of a checkout whose own top level is `repo`. A vendored copy inside another repository must not report that repository's
    HEAD, so such a folder is identified by its `.source-commit` marker instead (None when it has neither)."""
    repo = Path(repo)
    try:
        top = _run(["git", "-C", str(repo), "rev-parse", "--show-toplevel"], timeout=30)
        if top.returncode == 0 and Path(top.stdout.strip()).resolve() == repo.resolve():
            out = _run(["git", "-C", str(repo), "rev-parse", "HEAD"], timeout=30)
            if out.returncode == 0:
                return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    marker = repo / ".source-commit"
    return marker.read_text(encoding="utf-8").strip() if marker.is_file() else None


def has_commit(repo: Path, commit: str) -> bool:
    try:
        return _run(["git", "-C", str(repo), "cat-file", "-e", f"{commit}^{{commit}}"], timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def checkout_remote(repo: Path) -> tuple[Optional[str], Optional[str]]:
    """How the operator's own checkout reaches GitHub: its origin URL and its core.sshCommand, if any (git config reads only)."""
    def get(key: str) -> Optional[str]:
        try:
            r = _run(["git", "-C", str(repo), "config", "--get", key], timeout=30)
        except (OSError, subprocess.SubprocessError):
            return None
        return r.stdout.strip() or None if r.returncode == 0 else None
    return get("remote.origin.url"), get("core.sshCommand")


def pinned_sources(checkout: Optional[Path], commit: str = REPO_COMMIT, public_url: str = REPO_URL, env: Optional[dict] = None) -> list[Source]:
    e = os.environ if env is None else env
    out: list[Source] = []
    if e.get("CONTOSO_REPO_URL"):                                          # explicit override wins
        out.append((e["CONTOSO_REPO_URL"], e.get("CONTOSO_REPO_SSH_COMMAND")))
    if checkout is not None and (checkout / ".git").exists():
        if has_commit(checkout, commit):
            out.append((checkout.resolve().as_uri(), None))
        url, ssh = checkout_remote(checkout)
        if url:
            out.append((url, e.get("CONTOSO_REPO_SSH_COMMAND") or ssh))
    out.append((public_url, None))
    seen: set[Source] = set()
    return [s for s in out if not (s in seen or seen.add(s))]


def clone_pinned(cache_dir: Path, url: str = REPO_URL, branch: str = REPO_BRANCH, commit: str = REPO_COMMIT,
                 sources: Optional[Sequence[Source]] = None) -> Path:
    """Fetch exactly `commit` (depth 1, by SHA) into cache_dir/contoso-retail-superapp4. Reuses a matching existing copy.
    `branch` is kept for compatibility; the commit is what is fetched, so a moved branch can never change the data."""
    dest = Path(cache_dir) / "contoso-retail-superapp4"
    if dest.exists():
        if (git_head(dest) or "").startswith(commit[:7]):
            return dest
        raise RuntimeError(f"{dest} exists but is not at {commit[:7]}; delete it or choose another cache dir")
    cache_dir.mkdir(parents=True, exist_ok=True)
    tmp = Path(cache_dir) / ".fetch-tmp"
    errors: list[str] = []
    for src_url, ssh_cmd in (sources if sources is not None else [(url, None)]):
        shutil.rmtree(tmp, ignore_errors=True)
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        if ssh_cmd:
            env["GIT_SSH_COMMAND"] = ssh_cmd
        failed = None
        for cmd in (["git", "init", "-q", str(tmp)],
                    ["git", "-C", str(tmp), "fetch", "-q", "--depth", "1", src_url, commit],
                    ["git", "-C", str(tmp), "checkout", "-q", "--detach", "FETCH_HEAD"]):
            try:
                r = _run(cmd, env=env)
            except (OSError, subprocess.SubprocessError) as ex:
                failed = f"{type(ex).__name__}: {ex}"
                break
            if r.returncode != 0:
                failed = (r.stderr or r.stdout).strip()
                break
        if failed is None and (git_head(tmp) or "").startswith(commit[:7]):
            tmp.rename(dest)
            return dest
        errors.append(f"{mask(src_url)}: {mask(failed or 'fetched a different commit')[:240]}")
    shutil.rmtree(tmp, ignore_errors=True)
    raise RuntimeError(f"could not fetch the pinned commit {commit[:7]} of the source repo ({'; '.join(errors)}). Point the kit at a checkout that has it with "
                       f"--repo PATH (CONTOSO_REPO), or fix GitHub access for the operator user")


def resolve_repo(repo: Optional[str], clone: bool, cache_dir: Path, allow_drift: bool = False) -> Path:
    if repo:
        p = Path(repo).resolve()
        if not (p / "data" / "releases").is_dir():
            raise FileNotFoundError(f"{p} does not look like contoso-retail-superapp4 (data/releases missing)")
        head = git_head(p)
        if head and not head.startswith(REPO_COMMIT[:7]) and not allow_drift:
            raise RuntimeError(f"repo HEAD {head[:7]} != pinned {REPO_COMMIT[:7]}; pass --allow-drift to assess a different commit")
        return p
    if clone:
        return clone_pinned(cache_dir, sources=pinned_sources(None))
    raise SystemExit("give --repo <path> or --clone")


def load_json(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def release_dir(repo: Path, release: str) -> Path:
    return repo / "data" / "releases" / release


def read_release(repo: Path, release: str) -> dict[str, Any]:
    """Return {relative-json-path: parsed} for the release, excluding test-only qa/ answer keys."""
    root = release_dir(repo, release)
    out: dict[str, Any] = {}
    for p in sorted(root.rglob("*.json")):
        rel = p.relative_to(root).as_posix()
        if rel.startswith("qa/"):
            continue
        out[rel] = load_json(p)
    return out
