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

#: On disk a branch's '/' is written %2F: '%' is not in a branch name, so no
#: two branches share a name there (a plain substitution let `x-/y` and
#: `x/-y` share one checkout, review).
def _enc(branch: str) -> str:
    return branch.replace("/", "%2F")


def _dec(name: str) -> str:
    return name.replace("%2F", "/")

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


def checkout_of(path: Path) -> Path | None:
    """The sandbox checkout `path` is in, or None for a path outside them all.
    A path in the sandboxes but in no checkout (a bare repository) answers
    the sandboxes' own directory, which nothing may search above either."""
    base = Path(os.path.realpath(root()))
    try:
        parts = Path(os.path.realpath(path)).relative_to(base).parts
    except ValueError:
        return None
    if len(parts) >= 4 and parts[1] == "work":
        return base.joinpath(*parts[:4])
    return base


def check_repo(name: str) -> str:
    """`name`, or SandboxError: a repository name a URL or path can carry."""
    if not _REPO.match(name) or name.endswith(".git") or ".." in name:
        raise SandboxError(f"not a sandbox repository name: {name!r} "
                           "(letters, digits, '.', '_', '-'; 64 at most)")
    return name


def check_branch(name: str) -> str:
    if (not _BRANCH.match(name) or ".." in name or "//" in name or name.endswith("/")
            or name.endswith(".lock")):
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
        # The subcommand: the first argument that is not an option or its value.
        it = iter(args)
        command = next((a for a in it if not a.startswith("-") or next(it, None) is None),
                       args[0])
        raise SandboxError(f"git {command} failed: {err.decode(errors='replace').strip()}", 500)
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
    return principal_dir(principal) / "work" / repo / _enc(branch)


def _marker(gitdir: Path, branch: str) -> Path:
    return gitdir / "quern-index" / f"{_enc(branch)}.sha"


def _index(gitdir: Path, branch: str) -> Path:
    # Its own suffix: a branch named `main.sha` used `main`'s marker as its
    # index (review).
    return gitdir / "quern-index" / f"{_enc(branch)}.index"


async def _tree_bytes(gitdir: Path, sha: str) -> int:
    """What checking out `sha` writes: its blobs' sizes, as `ls-tree -l` gives."""
    out = await _git("--git-dir", str(gitdir), "ls-tree", "-r", "-l", "-z", sha)
    total = 0
    for entry in out.split("\0"):
        meta = entry.split("\t", 1)[0].split()
        if len(meta) == 4 and meta[3].isdigit():
            total += int(meta[3])
    return total


async def check_out(principal: str, repo: str, max_bytes: int | None = None,
                    min_free_bytes: int = 0) -> list[dict]:
    """Bring every branch's checkout up to what was pushed. Each has its own
    index, so `read-tree --reset -u` touches only files that changed -- an
    untouched file keeps its timestamp, and an incremental build stays
    incremental -- and removes files the new tree no longer has.

    A checkout is written only if what it expands to fits: the key's cap
    (`max_bytes`) counts what is on disk, and a pack of a few kilobytes can
    hold gigabytes of zeros, so the cap on what was received is not enough
    (review). Nor below the free-space floor. A branch that does not fit, or
    whose checkout fails, is reported, and the others still go."""
    gitdir = principal_dir(principal) / f"{check_repo(repo)}.git"
    refs = await _git("--git-dir", str(gitdir), "for-each-ref",
                      "--format=%(objectname) %(refname:strip=2)", "refs/heads")
    (gitdir / "quern-index").mkdir(exist_ok=True)
    branches = []
    for line in refs.splitlines():
        sha, _, branch = line.partition(" ")
        try:
            branches.append((sha, check_branch(branch)))
        except SandboxError:
            branches.append((sha, None))
    done = []
    folded: dict[str, str] = {}
    for sha, branch in branches:
        if branch is None:
            done.append({"commit": sha, "error": "not a name a checkout can have"})
            continue
        # APFS is case-insensitive: Main and main would share one checkout.
        other = folded.setdefault(branch.casefold(), branch)
        if other != branch:
            done.append({"branch": branch, "error": f"differs from {other!r} only in case, "
                                                   "and would share its checkout"})
            continue
        marker = _marker(gitdir, branch)
        prev = marker.read_text().strip() if marker.exists() else None
        if prev == sha:
            continue
        work = _checkout_dir(principal, repo, branch)
        try:
            need = await _tree_bytes(gitdir, sha)
            if max_bytes is not None:
                held = size_of(principal_dir(principal)) - size_of(work)
                if held + need > max_bytes:
                    raise SandboxError(
                        f"{repo}/{branch} expands to {need // 2**20} MB, and with the "
                        f"{held // 2**20} MB this key's sandboxes hold it would pass their "
                        f"cap of {max_bytes // 2**20} MB (sandboxes.max_mb)", 413)
            if min_free_bytes and free_bytes() - need < min_free_bytes:
                raise SandboxError(f"{repo}/{branch} expands to {need // 2**20} MB, which "
                                   "would take the server's disk under the free space it "
                                   "keeps (uploads.min_free_mb)", 507)
        except SandboxError as e:
            marker.unlink(missing_ok=True)
            done.append({"branch": branch, "commit": sha, "error": str(e)})
            continue
        env = {"GIT_INDEX_FILE": str(_index(gitdir, branch))}
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


def record_checkout(principal: str, repo: str, done: list[dict]) -> None:
    """Keep what the last push's checkout did, for the listing: a push's own
    answer has gone by the time it is known."""
    import json
    gitdir = principal_dir(principal) / f"{repo}.git"
    try:
        (gitdir / "quern-index").mkdir(exist_ok=True)
        (gitdir / "quern-index" / "last-checkout.json").write_text(
            json.dumps({"at": time.time(), "branches": done}))
    except OSError as e:
        logger.warning("sandbox %s/%s: could not record its checkout: %s", principal, repo, e)


def _branches(gitdir: Path) -> set[str]:
    """The repository's branches, as git lists them."""
    import subprocess
    try:
        out = subprocess.run(["git", "--git-dir", str(gitdir), "for-each-ref",
                              "--format=%(refname:strip=2)", "refs/heads"],
                             capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        raise SandboxError(f"the sandbox's branches could not be read: {e}", 500) from None
    if out.returncode != 0:
        raise SandboxError(f"the sandbox's branches could not be read: {out.stderr.strip()}", 500)
    return set(out.stdout.split())


def resolve(principal: str, ref: str) -> str:
    """`sandbox:<repo>/<branch>/<path>` as a path on this Mac, inside the
    caller's own checkout of that branch, or SandboxError: 404 for a
    repository, branch or path that is not there, 409 for a branch pushed but
    not checked out (it did not fit, or failed)."""
    rest = ref.removeprefix("sandbox:")
    repo, _, tail = rest.partition("/")
    check_repo(repo)
    gitdir = principal_dir(principal) / f"{repo}.git"
    if not (gitdir / "HEAD").exists():
        raise SandboxError(f"no sandbox repository {repo!r}: push to it first", 404)
    parts = [p for p in tail.split("/") if p not in ("", ".")]
    if ".." in parts:
        raise SandboxError(f"{ref!r} walks out of its checkout")
    # The branch may hold '/', so take the longest prefix that is one.
    branches = _branches(gitdir)
    n = next((n for n in range(len(parts), 0, -1) if "/".join(parts[:n]) in branches), 0)
    if not n:
        raise SandboxError(f"no branch of {repo} in {ref!r}: push one first", 404)
    branch = "/".join(parts[:n])
    work = _checkout_dir(principal, repo, branch)
    if not _marker(gitdir, branch).exists() or not work.is_dir():
        raise SandboxError(f"{repo}/{branch} was pushed but not checked out: it did not fit, "
                           "or failed (GET /api/v1/sandboxes says which)", 409)
    target = work.joinpath(*parts[n:])
    real, base = os.path.realpath(target), os.path.realpath(work)
    if real != base and not real.startswith(base + os.sep):
        raise SandboxError(f"{ref!r} leaves its checkout (a symlink?)")
    if not target.exists():
        raise SandboxError(f"{'/'.join(parts[n:]) or '.'} is not in {repo}/{branch}", 404)
    _touch(gitdir)
    return str(target)


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
        branches = [{"branch": _dec(m.name[:-4]), "commit": m.read_text().strip(),
                     "path": f"sandbox:{repo}/{_dec(m.name[:-4])}"}
                    for m in markers]
        used = gitdir / _LAST_USED
        last = gitdir / "quern-index" / "last-checkout.json"
        try:
            import json
            last_checkout = json.loads(last.read_text()) if last.exists() else None
        except (OSError, ValueError):
            last_checkout = None
        out.append({"repo": repo, "branches": branches, "last_checkout": last_checkout,
                    "bytes": size_of(gitdir) + size_of(base / "work" / repo),
                    "last_used": used.stat().st_mtime if used.exists() else None})
    return out


_STAMP = "quern-key"


def bind(principal: str, stamp: str) -> None:
    """Tie this principal's sandbox to the key it was made for: `stamp` names
    the key (a client key's issue time). A label revoked and issued again is a
    new key, and must not inherit the old one's sandbox, so a sandbox stamped
    for another key is removed first (review)."""
    base = principal_dir(principal)
    marker = base / _STAMP
    try:
        if base.is_dir() and marker.exists() and marker.read_text().strip() != stamp:
            shutil.rmtree(base, ignore_errors=True)
        base.mkdir(parents=True, exist_ok=True)
        if not marker.exists():
            marker.write_text(stamp + "\n")
    except OSError as e:
        raise SandboxError(f"the sandbox could not be prepared: {e}", 500) from None


def remove_repo(principal: str, repo: str) -> bool:
    """Delete one repository and its checkouts; whether it existed."""
    base = principal_dir(principal)
    gitdir = base / f"{check_repo(repo)}.git"
    existed = gitdir.exists()
    shutil.rmtree(base / "work" / repo, ignore_errors=True)
    shutil.rmtree(gitdir, ignore_errors=True)
    return existed


def prune(key_of, max_idle_days: int, now: float | None = None) -> list[str]:
    """Remove what no longer belongs to a key, and what has gone unused.

    `key_of(principal)` says which key a client principal is now: its stamp,
    `None` when that key has been revoked, or `...` when it cannot tell (an
    unreadable clients.json). Only a revocation or a different key removes a
    sandbox: "could not tell" keeps it, rather than reading every key as
    revoked (review). Repositories idle past `max_idle_days` go too (0 keeps
    them). What was removed, by name."""
    now = time.time() if now is None else now
    removed = []
    base = root()
    if not base.is_dir():
        return removed
    for pdir in base.iterdir():
        if not pdir.is_dir():
            continue
        if pdir.name.startswith(CLIENT_PREFIX):
            key = key_of(f"client:{pdir.name[len(CLIENT_PREFIX):]}")
            stamp = pdir / _STAMP
            mine = stamp.read_text().strip() if stamp.exists() else None
            if key is None or (key is not ... and mine is not None and key != mine):
                shutil.rmtree(pdir, ignore_errors=True)
                removed.append(pdir.name)
                continue
        elif pdir.name != OWNER_DIR:
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
