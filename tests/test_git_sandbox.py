"""Git sandboxes (#608): what a caller pushes, checked out to build.

These run the real `git` -- local only, on files under tmp_path: the checkout's
whole point is git's own behaviour (what `read-tree -m -u` writes, removes and
leaves alone), which a fake would only restate. Nothing reaches the network;
the end-to-end push goes to a server on loopback.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import config, sandboxes, visibility
from server.api import git_sandbox

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

OWNER, ALICE = "owner", "client:alice"


@pytest.fixture
def root(tmp_path, monkeypatch):
    base = tmp_path / "sandboxes"
    monkeypatch.setattr(sandboxes, "root", lambda: base)
    return base


def _git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
                          # A push that hangs fails the test, not the suite (CodeRabbit).
                          timeout=60).stdout


def _commit(work: Path, files: dict, message="c") -> str:
    for name, text in files.items():
        p = work / name
        if text is None:
            p.unlink()
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    _git("add", "-A", cwd=work)
    _git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", message, cwd=work)
    return _git("rev-parse", "HEAD", cwd=work).strip()


def _push(work: Path, gitdir: Path, branch="main"):
    _git("push", "-q", "--force", str(gitdir), f"HEAD:refs/heads/{branch}", cwd=work)


@pytest.fixture
def project(tmp_path):
    work = tmp_path / "project"
    work.mkdir()
    _git("init", "-q", cwd=work)
    return work


class TestNames:
    @pytest.mark.parametrize("name", ["App", "my-app_2.0", "a"])
    def test_repo_names_taken(self, name):
        assert sandboxes.check_repo(name) == name

    @pytest.mark.parametrize("name", ["", ".hidden", "-x", "a/b", "..", "a..b", "x.git",
                                      "a b", "a" * 65])
    def test_repo_names_refused(self, name):
        with pytest.raises(sandboxes.SandboxError):
            sandboxes.check_repo(name)

    @pytest.mark.parametrize("name", ["main", "feature/x", "quern-sync"])
    def test_branch_names_taken(self, name):
        assert sandboxes.check_branch(name) == name

    @pytest.mark.parametrize("name", ["", "../x", "a//b", "a/", "x.lock", ".x"])
    def test_branch_names_refused(self, name):
        with pytest.raises(sandboxes.SandboxError):
            sandboxes.check_branch(name)

    def test_each_principal_its_own_directory(self, root):
        assert sandboxes.principal_dir(OWNER) == root / "owner"
        assert sandboxes.principal_dir(ALICE) == root / "client-alice"


class TestCheckout:
    async def test_the_cap_is_measured_off_the_event_loop(self, root, project, monkeypatch):
        """A walk of up to sandboxes.max_mb, which on the loop stalled every
        other request for its length (CodeRabbit)."""
        threads = []
        real = sandboxes.size_of

        def recording(path):
            threads.append(threading.current_thread())
            return real(path)
        monkeypatch.setattr(sandboxes, "size_of", recording)
        gitdir = await sandboxes.ensure_repo(ALICE, "app")
        _commit(project, {"a.txt": "a\n"})
        _push(project, gitdir)
        await sandboxes.check_out(ALICE, "app", max_bytes=2**30)
        assert threads, "the cap was never measured: the test would be blind"
        assert threading.main_thread() not in threads

    async def test_a_push_is_checked_out_and_a_second_writes_only_what_changed(
            self, root, project):
        gitdir = await sandboxes.ensure_repo(ALICE, "app")
        _commit(project, {"keep.txt": "same\n", "gone/x.txt": "x\n", "edit.txt": "1\n"})
        _push(project, gitdir)
        assert [d["branch"] for d in await sandboxes.check_out(ALICE, "app")] == ["main"]
        work = root / "client-alice" / "work" / "app" / "main"
        assert (work / "gone" / "x.txt").read_text() == "x\n"
        kept = (work / "keep.txt").stat().st_mtime_ns
        time.sleep(0.05)

        _commit(project, {"gone/x.txt": None, "edit.txt": "2\n", "new.txt": "n\n"})
        _push(project, gitdir)
        await sandboxes.check_out(ALICE, "app")
        assert (work / "edit.txt").read_text() == "2\n"
        assert (work / "new.txt").exists()
        assert not (work / "gone").exists(), "a directory its files left goes too"
        assert (work / "keep.txt").stat().st_mtime_ns == kept, "an unchanged file is untouched"

    async def test_nothing_new_is_nothing_done(self, root, project):
        gitdir = await sandboxes.ensure_repo(ALICE, "app")
        _commit(project, {"a": "1"})
        _push(project, gitdir)
        await sandboxes.check_out(ALICE, "app")
        assert await sandboxes.check_out(ALICE, "app") == []

    async def test_a_branch_with_a_slash_has_its_own_checkout(self, root, project):
        gitdir = await sandboxes.ensure_repo(ALICE, "app")
        _commit(project, {"a": "1"})
        _push(project, gitdir, "feature/x")
        await sandboxes.check_out(ALICE, "app")
        assert (root / "client-alice" / "work" / "app" / "feature%2Fx" / "a").exists()
        assert sandboxes.resolve(ALICE, "sandbox:app/feature/x/a").endswith("feature%2Fx/a")

    async def test_different_branches_never_share_a_checkout(self, root, project):
        """`x-/y` and `x/-y` shared one under a plain substitution, and a
        branch called `main.sha` took `main`'s marker for its index (review)."""
        gitdir = await sandboxes.ensure_repo(ALICE, "app")
        for branch, text in (("x-/y", "A"), ("x/-y", "B"), ("main", "M"), ("main.sha", "S")):
            _commit(project, {"f": text})
            _push(project, gitdir, branch)
        done = await sandboxes.check_out(ALICE, "app")
        assert all("error" not in d for d in done), done
        for branch, text in (("x-/y", "A"), ("x/-y", "B"), ("main", "M"), ("main.sha", "S")):
            assert Path(sandboxes.resolve(ALICE, f"sandbox:app/{branch}/f")).read_text() == text

    async def test_branches_differing_only_in_case_are_refused_one(self, root, project):
        gitdir = await sandboxes.ensure_repo(ALICE, "app")
        sha = _commit(project, {"f": "1"})
        _push(project, gitdir, "Main")
        # git cannot write `main` beside `Main` on APFS itself (loose refs
        # collide); a case-sensitive Linux disk can, so plant it packed.
        with open(gitdir / "packed-refs", "a") as f:
            f.write(f"{sha} refs/heads/main\n")
        done = {d["branch"]: d for d in await sandboxes.check_out(ALICE, "app")}
        assert sum("error" in d for d in done.values()) == 1
        assert "only in case" in next(d["error"] for d in done.values() if "error" in d)

    async def test_a_checkout_that_would_pass_the_cap_is_not_written(self, root, project):
        """A pack of kilobytes can hold megabytes of zeros: the cap counts
        what the checkout writes, not only what arrived (review)."""
        gitdir = await sandboxes.ensure_repo(ALICE, "app")
        _commit(project, {"zeros.bin": "\0" * (3 * 2**20)})
        _push(project, gitdir)
        done = await sandboxes.check_out(ALICE, "app", max_bytes=2**20)
        assert done[0]["error"].startswith("app/main expands to 3 MB")
        assert not (root / "client-alice" / "work" / "app" / "main" / "zeros.bin").exists()
        with pytest.raises(sandboxes.SandboxError) as e:
            sandboxes.resolve(ALICE, "sandbox:app/main/zeros.bin")
        assert e.value.status == 409
        assert (await sandboxes.check_out(ALICE, "app", max_bytes=2**30))[0].get("error") is None

    async def test_nor_one_that_would_take_the_disk_under_its_floor(self, root, project,
                                                                    monkeypatch):
        gitdir = await sandboxes.ensure_repo(ALICE, "app")
        _commit(project, {"f": "x" * 4096})
        _push(project, gitdir)
        monkeypatch.setattr(sandboxes, "free_bytes", lambda: 10_000)
        done = await sandboxes.check_out(ALICE, "app", min_free_bytes=8_000)
        assert "free space" in done[0]["error"]

    async def test_a_failed_checkout_leaves_no_marker_and_says_so_at_build(
            self, root, project, monkeypatch):
        gitdir = await sandboxes.ensure_repo(ALICE, "app")
        _commit(project, {"a": "1"})
        _push(project, gitdir)
        await sandboxes.check_out(ALICE, "app")
        _commit(project, {"a": "2"})
        _push(project, gitdir)

        real = sandboxes._git

        async def failing(*args, env=None):
            if "read-tree" in args:
                raise sandboxes.SandboxError("git read-tree failed: boom", 500)
            return await real(*args, env=env)
        monkeypatch.setattr(sandboxes, "_git", failing)
        done = await sandboxes.check_out(ALICE, "app")
        assert done[0]["error"].endswith("boom")
        with pytest.raises(sandboxes.SandboxError) as e:
            sandboxes.resolve(ALICE, "sandbox:app/main/a")
        assert e.value.status == 409 and "not checked out" in str(e.value)


class TestResolve:
    @pytest.fixture
    async def checked_out(self, root, project):
        gitdir = await sandboxes.ensure_repo(ALICE, "app")
        _commit(project, {"App.xcodeproj/project.pbxproj": "p"})
        _push(project, gitdir)
        await sandboxes.check_out(ALICE, "app")
        return root / "client-alice" / "work" / "app" / "main"

    async def test_a_path_in_the_checkout(self, checked_out):
        assert sandboxes.resolve(ALICE, "sandbox:app/main/App.xcodeproj") == \
            str(checked_out / "App.xcodeproj")

    async def test_only_the_callers_own(self, checked_out):
        with pytest.raises(sandboxes.SandboxError) as e:
            sandboxes.resolve("client:bob", "sandbox:app/main/App.xcodeproj")
        assert e.value.status == 404

    @pytest.mark.parametrize("ref", ["sandbox:app/main/../../../etc",
                                     "sandbox:app/main/App.xcodeproj/../.."])
    async def test_walking_out_is_refused(self, checked_out, ref):
        with pytest.raises(sandboxes.SandboxError, match="walks out"):
            sandboxes.resolve(ALICE, ref)

    async def test_a_symlink_out_is_refused(self, checked_out, tmp_path):
        (checked_out / "out").symlink_to(tmp_path)
        with pytest.raises(sandboxes.SandboxError, match="leaves its checkout"):
            sandboxes.resolve(ALICE, "sandbox:app/main/out")

    async def test_missing_things_are_404(self, checked_out):
        for ref in ("sandbox:nope/main/x", "sandbox:app/main/missing", "sandbox:app/other/x"):
            with pytest.raises(sandboxes.SandboxError) as e:
                sandboxes.resolve(ALICE, ref)
            assert e.value.status == 404, ref

    @pytest.mark.parametrize("above", ["client-alice", ".", ".."])
    async def test_a_gradle_root_is_never_looked_for_above_the_checkout(
            self, checked_out, root, above):
        """A sandbox path with no settings.gradle of its own is not a Gradle
        project, whatever the owner keeps above it (review)."""
        from server.builds import gradle
        owner = (root / above).resolve()
        (owner / "settings.gradle").write_text("")
        (owner / "gradlew").write_text("")
        (checked_out / "lib").mkdir()
        assert not gradle.is_gradle_project(str(checked_out / "lib"))
        with pytest.raises(gradle.GradleProjectError, match="not in a Gradle project"):
            gradle.find_project(str(checked_out / "lib"))

    async def test_a_module_in_the_checkout_finds_its_root(self, checked_out):
        from server.builds import gradle
        (checked_out / "settings.gradle").write_text("")
        (checked_out / "gradlew").write_text("")
        (checked_out / "app").mkdir()
        (checked_out / "app" / "build.gradle").write_text("")
        assert gradle.find_project(str(checked_out / "app")).root == checked_out.resolve()

    async def test_a_path_in_no_checkout_stops_at_the_sandboxes(self, checked_out, root, tmp_path):
        assert sandboxes.checkout_of(root / "client-alice" / "app.git") == root.resolve()
        assert sandboxes.checkout_of(checked_out / "App.xcodeproj") == checked_out.resolve()
        assert sandboxes.checkout_of(tmp_path) is None

    async def test_a_build_counts_as_use(self, checked_out, root):
        used = root / "client-alice" / "app.git" / "quern-last-used"
        os.utime(used, (0, 0))
        sandboxes.resolve(ALICE, "sandbox:app/main/App.xcodeproj")
        assert used.stat().st_mtime > 1


class TestPruneAndList:
    async def test_revoked_keys_go_the_owner_stays(self, root):
        for who, repo in ((ALICE, "a"), ("client:bob", "b"), (OWNER, "o")):
            sandboxes.bind(who, "k1")
            await sandboxes.ensure_repo(who, repo)
        removed = sandboxes.prune(lambda who: None if who == ALICE else "k1", max_idle_days=14)
        assert removed == ["client-alice"]
        assert (root / "client-bob" / "b.git").exists() and (root / "owner" / "o.git").exists()

    async def test_could_not_tell_keeps_everything(self, root):
        """An unreadable clients.json is no evidence of revocation (review)."""
        sandboxes.bind(ALICE, "k1")
        await sandboxes.ensure_repo(ALICE, "a")
        assert sandboxes.prune(lambda who: ..., max_idle_days=14) == []
        assert (root / "client-alice" / "a.git").exists()

    async def test_a_label_issued_again_does_not_inherit_the_old_keys_sandbox(self, root):
        sandboxes.bind(ALICE, "issued-monday")
        await sandboxes.ensure_repo(ALICE, "secret")
        assert sandboxes.prune(lambda who: "issued-friday", max_idle_days=14) == ["client-alice"]
        sandboxes.bind(ALICE, "issued-monday")
        await sandboxes.ensure_repo(ALICE, "secret")
        sandboxes.bind(ALICE, "issued-friday")       # the new key's first request
        assert sandboxes.listing(ALICE) == []

    async def test_idle_repositories_go_unless_kept(self, root):
        gitdir = await sandboxes.ensure_repo(ALICE, "old")
        await sandboxes.ensure_repo(ALICE, "new")
        os.utime(gitdir / "quern-last-used", (0, 0))
        assert sandboxes.prune(lambda w: True, max_idle_days=0) == []
        assert sandboxes.prune(lambda w: True, max_idle_days=14) == ["client-alice/old.git"]
        assert (root / "client-alice" / "new.git").exists()

    async def test_listing_and_deleting(self, root, project):
        gitdir = await sandboxes.ensure_repo(ALICE, "app")
        _commit(project, {"a": "1"})
        _push(project, gitdir)
        await sandboxes.check_out(ALICE, "app")
        [repo] = sandboxes.listing(ALICE)
        assert repo["repo"] == "app" and repo["bytes"] > 0
        assert repo["branches"][0]["path"] == "sandbox:app/main"
        assert sandboxes.listing("client:bob") == []
        assert sandboxes.remove_repo(ALICE, "app") is True
        assert sandboxes.listing(ALICE) == []
        assert not (root / "client-alice" / "work" / "app").exists()
        assert sandboxes.remove_repo(ALICE, "app") is False


class TestSettings:
    @pytest.mark.parametrize("raw, mb, days, noted", [
        (None, 4096, 14, False),
        ({"max_mb": 10, "max_idle_days": 0}, 10, 0, False),
        ({"max_mb": 0}, 4096, 14, True),
        ({"max_mb": "10", "max_idle_days": -1}, 4096, 14, True),
        ({"max_mb": True}, 4096, 14, True),
    ])
    def test_values(self, monkeypatch, raw, mb, days, noted):
        cfg = {} if raw is None else {"sandboxes": raw}
        monkeypatch.setattr(config, "read_user_config", lambda strict=False: cfg)
        got = config.sandbox_settings()
        assert (got["max_bytes"], got["max_idle_days"]) == (mb * 2**20, days)
        assert bool(got["note"]) is noted


# -- the routes ----------------------------------------------------------------

def _app():
    app = FastAPI()
    app.include_router(git_sandbox.router)
    return app


@pytest.fixture
def caller(monkeypatch):
    def set_(principal=OWNER):
        monkeypatch.setattr(visibility, "current_caller", lambda: SimpleNamespace(
            principal=principal, session="s1", is_owner=principal == OWNER))
    set_()
    return set_


@pytest.fixture
def builds(monkeypatch):
    state = {"on": False}
    monkeypatch.setattr(config, "client_builds", lambda: state["on"])
    return state


@pytest.fixture
def limits(monkeypatch):
    state = {"max_bytes": 2**30, "max_idle_days": 14, "min_free_bytes": 0, "note": None}
    monkeypatch.setattr(config, "sandbox_settings", lambda: dict(state))
    return state


class TestRoutes:
    def test_a_client_needs_client_builds(self, root, caller, builds, limits):
        caller(ALICE)
        c = TestClient(_app())
        r = c.get("/git/app.git/info/refs", params={"service": "git-receive-pack"})
        assert r.status_code == 403 and '"client_builds": true' in r.json()["detail"]
        assert c.get("/api/v1/sandboxes").status_code == 403
        builds["on"] = True
        assert c.get("/git/app.git/info/refs",
                     params={"service": "git-receive-pack"}).status_code == 200

    def test_only_smart_http(self, root, caller, limits):
        c = TestClient(_app())
        assert c.get("/git/app.git/info/refs").status_code == 403
        assert c.get("/git/app.git/info/refs", params={"service": "x"}).status_code == 403

    def test_a_fetch_from_nothing_is_404_and_makes_nothing(self, root, caller, limits):
        c = TestClient(_app())
        r = c.get("/git/app.git/info/refs", params={"service": "git-upload-pack"})
        assert r.status_code == 404 and not (root / "owner" / "app.git").exists()

    @pytest.mark.parametrize("repo, status", [("app", 404), (".x.git", 400), ("a..b.git", 400)])
    def test_bad_names(self, root, caller, limits, repo, status):
        r = TestClient(_app()).get(f"/git/{repo}/info/refs",
                                   params={"service": "git-receive-pack"})
        assert r.status_code == status

    def test_a_push_advert_makes_the_repository(self, root, caller, limits):
        r = TestClient(_app()).get("/git/app.git/info/refs",
                                   params={"service": "git-receive-pack"})
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/x-git-receive-pack-advertisement"
        assert (root / "owner" / "app.git" / "HEAD").exists()

    def test_at_the_cap_a_push_is_413(self, root, caller, limits):
        (root / "owner").mkdir(parents=True)
        (root / "owner" / "big").write_bytes(b"x" * 2048)
        limits["max_bytes"] = 1024
        r = TestClient(_app()).get("/git/app.git/info/refs",
                                   params={"service": "git-receive-pack"})
        assert r.status_code == 413 and "sandboxes.max_mb" in r.json()["detail"]

    def test_a_declared_length_past_the_room_is_413(self, root, caller, limits):
        limits["max_bytes"] = 1024
        r = TestClient(_app()).post("/git/app.git/git-receive-pack", content=b"x" * 4096,
                                    headers={"content-type":
                                             "application/x-git-receive-pack-request"})
        assert r.status_code == 413 and "left" in r.json()["detail"]

    def test_under_the_free_space_floor_is_507(self, root, caller, limits, monkeypatch):
        limits["min_free_bytes"] = 10 * 2**30
        monkeypatch.setattr(sandboxes, "free_bytes", lambda: 2**20)
        r = TestClient(_app()).get("/git/app.git/info/refs",
                                   params={"service": "git-receive-pack"})
        assert r.status_code == 507

    def test_a_compressed_push_is_refused(self, root, caller, limits):
        """Inflated after every size check, it passed the cap 15 times over
        in review. git never compresses a push."""
        r = TestClient(_app()).post("/git/app.git/git-receive-pack", content=b"\x1f\x8b",
                                    headers={"content-encoding": "gzip", "content-type":
                                             "application/x-git-receive-pack-request"})
        assert r.status_code == 415

    def test_list_and_delete(self, root, caller, limits):
        c = TestClient(_app())
        c.get("/git/app.git/info/refs", params={"service": "git-receive-pack"})
        assert [r["repo"] for r in c.get("/api/v1/sandboxes").json()["repos"]] == ["app"]
        assert c.delete("/api/v1/sandboxes/app").status_code == 200
        assert c.delete("/api/v1/sandboxes/app").status_code == 404
        assert c.delete("/api/v1/sandboxes/..").status_code in (400, 404)


class TestEndToEnd:
    """A real `git push` through the HTTP stack: the CGI bridge, streaming,
    and the checkout before the response ends."""

    @pytest.fixture
    def server(self, root, caller, limits):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        srv = uvicorn.Server(uvicorn.Config(_app(), host="127.0.0.1", port=port,
                                            log_level="warning"))
        thread = threading.Thread(target=srv.run, daemon=True)
        thread.start()
        deadline = time.time() + 10
        while not srv.started and time.time() < deadline:
            time.sleep(0.05)
        yield f"http://127.0.0.1:{port}"
        srv.should_exit = True
        thread.join(5)

    def test_push_then_build_path_then_clone_back(self, server, root, project, tmp_path):
        _commit(project, {"App/main.swift": "print(1)\n", "README": "r\n"})
        _git("push", "-q", f"{server}/git/app.git", "HEAD:refs/heads/quern-sync", cwd=project)
        # Checked out before the push returned: no wait.
        path = sandboxes.resolve(OWNER, "sandbox:app/quern-sync/App/main.swift")
        assert Path(path).read_text() == "print(1)\n"
        _git("clone", "-q", "-b", "quern-sync", f"{server}/git/app.git", str(tmp_path / "back"))
        assert (tmp_path / "back" / "README").read_text() == "r\n"

    def test_a_large_chunked_push_completes(self, server, root, project):
        """The backend writes its CGI headers before it reads the body; a 200
        sent on them mid-upload made curl stop sending, and the push hung.
        A small push never showed it."""
        _commit(project, {"blob.bin": os.urandom(2**20).hex()})
        r = subprocess.run(["git", "-c", "http.postBuffer=1024", "push",
                            f"{server}/git/app.git", "HEAD:refs/heads/main"],
                           cwd=project, capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr
        assert (root / "owner" / "work" / "app" / "main" / "blob.bin").stat().st_size == 2 * 2**20

    def test_an_expanding_push_is_kept_out_of_the_checkout_and_says_why(
            self, server, root, project, limits):
        limits["max_bytes"] = 2**20
        _commit(project, {"zeros.bin": "\0" * (8 * 2**20)})     # a tiny pack
        _git("push", "-q", f"{server}/git/app.git", "HEAD:refs/heads/main", cwd=project)
        assert not (root / "owner" / "work" / "app" / "main" / "zeros.bin").exists()
        [repo] = sandboxes.listing(OWNER)
        assert "expands to 8 MB" in repo["last_checkout"]["branches"][0]["error"]

    def test_a_body_that_goes_quiet_does_not_hold_a_process(self, server, root, monkeypatch):
        """A client that sends half a body and waits held an http-backend for
        as long as it liked (review). A body that sends nothing for
        BODY_IDLE_SECONDS is dropped, with the socket still open."""
        monkeypatch.setattr(git_sandbox, "BODY_IDLE_SECONDS", 1)
        started = []
        real = git_sandbox.asyncio.create_subprocess_exec

        async def recording(*args, **kwargs):
            proc = await real(*args, **kwargs)
            started.append(proc.pid)
            return proc
        monkeypatch.setattr(git_sandbox.asyncio, "create_subprocess_exec", recording)

        def alive(pid: int) -> bool:
            # Its process group: the backend and the receive-pack beneath it.
            try:
                os.killpg(pid, 0)
                return True
            except (ProcessLookupError, PermissionError):
                return False

        def backends() -> int:
            return sum(alive(p) for p in started)
        port = int(server.rsplit(":", 1)[1])
        s = socket.create_connection(("127.0.0.1", port))
        try:
            s.sendall(b"POST /git/app.git/git-receive-pack HTTP/1.1\r\nHost: x\r\n"
                      b"Content-Type: application/x-git-receive-pack-request\r\n"
                      b"Content-Length: 1000\r\n\r\n0000")
            time.sleep(0.5)
            assert backends() >= 1, "the push's backend never started: the test would be blind"
            deadline = time.time() + 10
            while backends() and time.time() < deadline:
                time.sleep(0.2)
            assert backends() == 0, "a quiet body still holds its backend"
        finally:
            s.close()

    async def test_a_response_that_never_starts_still_releases_the_push(
            self, root, caller, limits, monkeypatch):
        """A client gone once its upload is in, before the answer starts: the
        body never runs, and the push lock and the backend were left held
        until a restart, so every later push of that key waited (CodeRabbit)."""
        monkeypatch.setattr(git_sandbox, "_push_locks", {})
        started = []
        real = git_sandbox.asyncio.create_subprocess_exec

        async def recording(*args, **kwargs):
            proc = await real(*args, **kwargs)
            # The backend alone: setting up the repository runs git through
            # the same call, and those would satisfy the guard below without
            # any backend having started (CodeRabbit).
            if args[:2] == ("git", "http-backend"):
                started.append(proc)
            return proc
        monkeypatch.setattr(git_sandbox.asyncio, "create_subprocess_exec", recording)
        sent = [{"type": "http.request", "body": b"0000", "more_body": False}]

        async def receive():
            return sent.pop(0) if sent else {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.start":
                raise OSError("the client is gone")
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                 "method": "POST", "scheme": "http", "path": "/git/app.git/git-receive-pack",
                 "raw_path": b"/git/app.git/git-receive-pack", "query_string": b"",
                 "root_path": "", "client": ("127.0.0.1", 1), "server": ("test", 80),
                 "headers": [(b"content-type", b"application/x-git-receive-pack-request"),
                             (b"content-length", b"4")]}
        with pytest.raises(OSError):
            await asyncio.wait_for(_app()(scope, receive, send), 30)
        assert started, "the push's backend never started: the test would be blind"
        assert git_sandbox._push_locks and not any(
            lock.locked() for lock in git_sandbox._push_locks.values()), "the push lock is held"
        for proc in started:
            # Gone, whether killed or finished: the lock above is what fails
            # without the fix. A backend given a whole body may exit unaided.
            await asyncio.wait_for(proc.wait(), 5)

    def test_a_chunked_push_past_the_cap_is_cut_off(self, server, root, project, limits):
        limits["max_bytes"] = 64 * 1024
        # Incompressible, and big enough that git sends it chunked.
        _commit(project, {"blob.bin": os.urandom(2 * 2**20).hex()})
        r = subprocess.run(["git", "-c", "http.postBuffer=1024", "push",
                            f"{server}/git/app.git", "HEAD:refs/heads/main"],
                           cwd=project, capture_output=True, text=True, timeout=60)
        assert r.returncode != 0
        assert not (root / "owner" / "work" / "app" / "main").exists()
        assert json.dumps(sandboxes.listing(OWNER))  # still readable

