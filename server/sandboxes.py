"""Git sandboxes: source a caller pushes here to build (#605, prototype).

A client on another machine -- a Linux or Windows agent paired with this Mac
-- has its project on its own disk, and a build needs it on this one. So each
principal gets a sandbox of bare git repositories it pushes to over git's
smart-HTTP protocol (`server/api/git_sandbox.py`, which hands the protocol to
`git http-backend`), and each pushed branch is checked out to a stable path:

    <state>/sandboxes/<principal>/<repo>.git          what was pushed
    <state>/sandboxes/<principal>/work/<repo>/<branch> what builds read

Stable, because DerivedData is keyed by project path: the same branch pushed
again rebuilds incrementally. A build names a checkout as
`sandbox:<repo>/<branch>/<path in the repo>`, never as a path on this Mac, so
a client still names no Mac path.

The checkout happens in quern, after `receive-pack` returns, not in a hook: a
push cannot install hooks, and nothing pushed runs until a build runs it.
Prototype limits: no quota, no pruning, submodules and LFS are not fetched.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
from pathlib import Path

from server.principal import label_of

logger = logging.getLogger(__name__)

#: A repository or branch name. No leading dot or dash, nothing that walks.
_REPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")

#: What a pushed branch's checkout is called on disk: '/' cannot be a level.
_SEP = "--"


class SandboxError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def root() -> Path:
    from server.config import CONFIG_DIR

    return CONFIG_DIR / "sandboxes"


def principal_dir(principal: str) -> Path:
    """This principal's sandbox: `owner`, or `client-<label>`."""
    label = label_of(principal)
    return root() / ("owner" if label is None else f"client-{label}")


def check_repo(name: str) -> str:
    """`name`, or SandboxError: a repository name a URL or path can carry."""
    if not _REPO.match(name) or name.endswith(".git") or ".." in name:
        raise SandboxError(f"not a sandbox repository name: {name!r} "
                           "(letters, digits, '.', '_', '-'; 64 at most)")
    return name


def check_branch(name: str) -> str:
    if (not _BRANCH.match(name) or ".." in name or "//" in name or name.endswith("/")
            or _SEP in name or name.endswith(".lock")):
        raise SandboxError(f"not a sandbox branch name: {name!r}")
    return name


async def _git(*args: str, cwd: Path | None = None, env: dict | None = None) -> str:
    proc = await asyncio.create_subprocess_exec(
        "git", *args, cwd=str(cwd) if cwd else None,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0", **(env or {})},
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise SandboxError(f"git {args[0]} failed: {err.decode(errors='replace').strip()}", 500)
    return out.decode(errors="replace")


async def ensure_repo(principal: str, repo: str) -> Path:
    """The bare repository `repo` in this principal's sandbox, made on first use."""
    gitdir = principal_dir(principal) / f"{check_repo(repo)}.git"
    if not (gitdir / "HEAD").exists():
        gitdir.mkdir(parents=True, exist_ok=True)
        await _git("init", "--bare", "--quiet", str(gitdir))
        for key, value in (("http.receivepack", "true"),
                           # A malformed object is refused at the door.
                           ("receive.fsckObjects", "true"),
                           ("core.logAllRefUpdates", "false")):
            await _git("--git-dir", str(gitdir), "config", key, value)
    return gitdir


def _checkout_dir(principal: str, repo: str, branch: str) -> Path:
    return principal_dir(principal) / "work" / repo / branch.replace("/", _SEP)


async def check_out(principal: str, repo: str) -> list[dict]:
    """Bring every branch's checkout up to what was pushed. Each has its own
    index, so `read-tree -u` touches only files that changed -- an untouched
    file keeps its timestamp, and an incremental build stays incremental --
    and removes files the new tree no longer has."""
    gitdir = principal_dir(principal) / f"{check_repo(repo)}.git"
    refs = await _git("--git-dir", str(gitdir), "for-each-ref",
                      "--format=%(objectname) %(refname:strip=2)", "refs/heads")
    done = []
    for line in refs.splitlines():
        sha, _, branch = line.partition(" ")
        try:
            check_branch(branch)
        except SandboxError:
            logger.warning("sandbox %s/%s: not checking out branch %r", principal, repo, branch)
            continue
        index_dir = gitdir / "quern-index"
        index_dir.mkdir(exist_ok=True)
        marker = index_dir / f"{branch.replace('/', _SEP)}.sha"
        prev = marker.read_text().strip() if marker.exists() else None
        if prev == sha:
            continue
        work = _checkout_dir(principal, repo, branch)
        work.mkdir(parents=True, exist_ok=True)
        index = index_dir / branch.replace("/", _SEP)
        env = {"GIT_INDEX_FILE": str(index)}
        args = ["--git-dir", str(gitdir), "--work-tree", str(work)]
        # The marker goes first: a checkout that fails part-way must not be
        # read as the previous one on the next push (a success marker never
        # survives a failure).
        marker.unlink(missing_ok=True)
        if prev and index.exists():
            # Two-tree merge: only what changed between them is written.
            await _git(*args, "read-tree", "-m", "-u", prev, sha, env=env)
        else:
            await _git(*args, "read-tree", "--reset", "-u", sha, env=env)
        marker.write_text(sha + "\n")
        submodules = (work / ".gitmodules").exists()
        done.append({"branch": branch, "commit": sha, "path": str(work),
                     **({"submodules_not_fetched": True} if submodules else {})})
    return done


def resolve(principal: str, ref: str) -> str:
    """`sandbox:<repo>/<branch>/<path>` as a path on this Mac, inside the
    caller's own checkout of that branch, or SandboxError."""
    rest = ref.removeprefix("sandbox:")
    repo, _, tail = rest.partition("/")
    check_repo(repo)
    work_root = principal_dir(principal) / "work" / repo
    if not work_root.is_dir():
        raise SandboxError(f"no sandbox repository {repo!r}: push to it first", 404)
    # The branch may hold '/', so find the longest checked-out branch prefix.
    parts = [p for p in tail.split("/") if p not in ("", ".")]
    if ".." in parts:
        raise SandboxError(f"{ref!r} walks out of its checkout")
    for n in range(len(parts), 0, -1):
        branch = "/".join(parts[:n])
        try:
            check_branch(branch)
        except SandboxError:
            continue
        work = _checkout_dir(principal, repo, branch)
        if work.is_dir():
            target = work.joinpath(*parts[n:])
            real, base = os.path.realpath(target), os.path.realpath(work)
            if real != base and not real.startswith(base + os.sep):
                raise SandboxError(f"{ref!r} leaves its checkout (a symlink?)")
            if not target.exists():
                raise SandboxError(f"{'/'.join(parts[n:]) or '.'} is not in "
                                   f"{repo}/{branch}", 404)
            return str(target)
    raise SandboxError(f"no checked-out branch in {ref!r}: push one first", 404)


def remove(principal: str) -> None:
    """Delete a principal's whole sandbox (a revoked key)."""
    shutil.rmtree(principal_dir(principal), ignore_errors=True)
