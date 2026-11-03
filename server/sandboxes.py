"""Git sandboxes: source a caller pushes here to build (#608).

A client on another machine -- a Linux or Windows agent paired with this Mac
-- has its project on its own disk, and a build needs it on this one. So each
principal gets a sandbox of bare git repositories it pushes to over git's
smart-HTTP protocol (`server/api/git_sandbox.py`, which hands the protocol to
`git http-backend`), and each pushed branch is checked out to a stable path:

    <state>/sandboxes/<principal>/<repo>.git           what was pushed
    <state>/sandboxes/<principal>/work/<repo>/<branch> what builds read

Stable, because DerivedData is keyed by project path: the same branch pushed
again rebuilds incrementally. A build names a checkout as
`sandbox:<repo>/<branch>/<path in the repo>`, never as a path on this Mac, so
a client still names no Mac path.

The checkout happens in quern, after `receive-pack` returns, not in a hook: a
push cannot install hooks, and nothing pushed runs until a build runs it.

What one key's sandboxes hold is capped (`sandboxes.max_mb`), the volume keeps
the uploads' free-space floor, and a repository unused for
`sandboxes.max_idle_days` -- no push, no build -- is removed by the sweep, as
is every sandbox of a key that has been revoked. Submodules and LFS objects are
not fetched; the MCP refuses to send a project that needs them.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import time
from pathlib import Path

from server.principal import label_of

logger = logging.getLogger(__name__)

#: A repository or branch name. No leading dot or dash, nothing that walks.
_REPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")

#: What a pushed branch's checkout is called on disk: '/' cannot be a level.
_SEP = "--"

#: Touched on every push and every build that names the repository.
_LAST_USED = "quern-last-used"

OWNER_DIR = "owner"
CLIENT_PREFIX = "client-"


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
    return root() / (OWNER_DIR if label is None else f"{CLIENT_PREFIX}{label}")


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


async def _git(*args: str, env: dict | None = None) -> str:
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", *args, env={**os.environ, "GIT_TERMINAL_PROMPT": "0", **(env or {})},
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except OSError as e:
        raise SandboxError(f"git could not be run: {e}", 500) from None
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise SandboxError(f"git {args[2] if args[:1] == ('--git-dir',) else args[0]} failed: "
                           f"{err.decode(errors='replace').strip()}", 500)
    return out.decode(errors="replace")


def _touch(gitdir: Path) -> None:
    try:
        (gitdir / _LAST_USED).touch()
    except OSError as e:
        logger.warning("sandbox %s: could not record its use: %s", gitdir, e)


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
    _touch(gitdir)
    return gitdir


def _checkout_dir(principal: str, repo: str, branch: str) -> Path:
    return principal_dir(principal) / "work" / repo / branch.replace("/", _SEP)


async def check_out(principal: str, repo: str) -> list[dict]:
    """Bring every branch's checkout up to what was pushed. Each has its own
    index, so `read-tree --reset -u` touches only files that changed -- an untouched
    file keeps its timestamp, and an incremental build stays incremental --
    and removes files the new tree no longer has. A branch whose checkout
    fails is reported, and the others still go."""
    gitdir = principal_dir(principal) / f"{check_repo(repo)}.git"
    refs = await _git("--git-dir", str(gitdir), "for-each-ref",
                      "--format=%(objectname) %(refname:strip=2)", "refs/heads")
    index_dir = gitdir / "quern-index"
    index_dir.mkdir(exist_ok=True)
    done = []
    for line in refs.splitlines():
        sha, _, branch = line.partition(" ")
        try:
            check_branch(branch)
        except SandboxError:
            done.append({"branch": branch, "error": "not a name a checkout can have"})
            continue
        marker = index_dir / f"{branch.replace('/', _SEP)}.sha"
        prev = marker.read_text().strip() if marker.exists() else None
        if prev == sha:
            continue
        work = _checkout_dir(principal, repo, branch)
        env = {"GIT_INDEX_FILE": str(index_dir / branch.replace("/", _SEP))}
        args = ["--git-dir", str(gitdir), "--work-tree", str(work)]
        # The marker goes first: a checkout that fails part-way must not be
        # read as the previous one on the next push.
        marker.unlink(missing_ok=True)
        try:
            work.mkdir(parents=True, exist_ok=True)
            # With the branch's own index, -u rewrites only the files whose
            # entries changed and removes those the tree no longer has.
            await _git(*args, "read-tree", "--reset", "-u", sha, env=env)
        except (SandboxError, OSError) as e:
            logger.warning("sandbox %s/%s: checkout of %s failed: %s", principal, repo, branch, e)
            done.append({"branch": branch, "commit": sha, "error": str(e)})
            continue
        marker.write_text(sha + "\n")
        done.append({"branch": branch, "commit": sha})
    return done


def resolve(principal: str, ref: str) -> str:
    """`sandbox:<repo>/<branch>/<path>` as a path on this Mac, inside the
    caller's own checkout of that branch, or SandboxError."""
    rest = ref.removeprefix("sandbox:")
    repo, _, tail = rest.partition("/")
    check_repo(repo)
    gitdir = principal_dir(principal) / f"{repo}.git"
    work_root = principal_dir(principal) / "work" / repo
    if not work_root.is_dir():
        raise SandboxError(f"no sandbox repository {repo!r}: push to it first", 404)
    parts = [p for p in tail.split("/") if p not in ("", ".")]
    if ".." in parts:
        raise SandboxError(f"{ref!r} walks out of its checkout")
    # The branch may hold '/', so take the longest prefix that is one.
    for n in range(len(parts), 0, -1):
        branch = "/".join(parts[:n])
        try:
            check_branch(branch)
        except SandboxError:
            continue
        work = _checkout_dir(principal, repo, branch)
        if not work.is_dir():
            continue
        marker = gitdir / "quern-index" / f"{branch.replace('/', _SEP)}.sha"
        if not marker.exists():
            raise SandboxError(f"{repo}/{branch} was not checked out after its last push "
                               "(see the server log); push it again", 409)
        target = work.joinpath(*parts[n:])
        real, base = os.path.realpath(target), os.path.realpath(work)
        if real != base and not real.startswith(base + os.sep):
            raise SandboxError(f"{ref!r} leaves its checkout (a symlink?)")
        if not target.exists():
            raise SandboxError(f"{'/'.join(parts[n:]) or '.'} is not in {repo}/{branch}", 404)
        _touch(gitdir)
        return str(target)
    raise SandboxError(f"no checked-out branch in {ref!r}: push one first", 404)


def size_of(path: Path) -> int:
    """Bytes under `path`, symlinks counted as themselves; 0 if it is gone."""
    total = 0
    for dirpath, dirnames, filenames in os.walk(path):
        for name in filenames + dirnames:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                pass
    return total


def free_bytes() -> int:
    target = root()
    while not target.exists():
        target = target.parent
    return shutil.disk_usage(target).free


def listing(principal: str) -> list[dict]:
    """The caller's repositories: each one's branches, size and last use."""
    base = principal_dir(principal)
    out = []
    for gitdir in sorted(base.glob("*.git")) if base.is_dir() else []:
        repo = gitdir.name[:-4]
        markers = sorted((gitdir / "quern-index").glob("*.sha"))
        branches = [{"branch": m.name[:-4].replace(_SEP, "/"), "commit": m.read_text().strip(),
                     "path": f"sandbox:{repo}/{m.name[:-4].replace(_SEP, '/')}"}
                    for m in markers]
        used = gitdir / _LAST_USED
        out.append({"repo": repo, "branches": branches,
                    "bytes": size_of(gitdir) + size_of(base / "work" / repo),
                    "last_used": used.stat().st_mtime if used.exists() else None})
    return out


def remove_repo(principal: str, repo: str) -> bool:
    """Delete one repository and its checkouts; whether it existed."""
    base = principal_dir(principal)
    gitdir = base / f"{check_repo(repo)}.git"
    existed = gitdir.exists()
    shutil.rmtree(base / "work" / repo, ignore_errors=True)
    shutil.rmtree(gitdir, ignore_errors=True)
    return existed


def prune(still_valid, max_idle_days: int, now: float | None = None) -> list[str]:
    """Remove the sandboxes of principals no longer valid, and repositories
    idle past `max_idle_days` (0 keeps them). What was removed, by name."""
    now = time.time() if now is None else now
    removed = []
    base = root()
    if not base.is_dir():
        return removed
    for pdir in base.iterdir():
        if not pdir.is_dir():
            continue
        if pdir.name == OWNER_DIR:
            principal = "owner"
        elif pdir.name.startswith(CLIENT_PREFIX):
            principal = f"client:{pdir.name[len(CLIENT_PREFIX):]}"
        else:
            continue
        if principal != "owner" and not still_valid(principal):
            shutil.rmtree(pdir, ignore_errors=True)
            removed.append(pdir.name)
            continue
        if max_idle_days <= 0:
            continue
        for gitdir in pdir.glob("*.git"):
            used = gitdir / _LAST_USED
            try:
                last = used.stat().st_mtime if used.exists() else gitdir.stat().st_mtime
            except OSError:
                continue
            if now - last > max_idle_days * 86400:
                shutil.rmtree(pdir / "work" / gitdir.name[:-4], ignore_errors=True)
                shutil.rmtree(gitdir, ignore_errors=True)
                removed.append(f"{pdir.name}/{gitdir.name}")
    return removed
