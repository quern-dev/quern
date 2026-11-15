"""Tests for the QuernMedia build helper.

No Swift toolchain is invoked: subprocess is mocked throughout, matching the
convention the rest of the suite uses (see tests/test_wda.py).
"""

from __future__ import annotations

import contextlib
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from server.device.media import media_engine

#: Imported before conftest's autouse fixture stubs it.
REAL_REFRESH_AT_START = media_engine.refresh_at_server_start


@pytest.fixture
def package(tmp_path: Path) -> Path:
    """A package tree shaped like the real one."""
    pkg = tmp_path / "QuernMedia"
    (pkg / "Sources" / "QuernMedia").mkdir(parents=True)
    (pkg / "Package.swift").write_text("// swift-tools-version: 6.0\n")
    (pkg / "Sources" / "QuernMedia" / "Frame.swift").write_text("struct Frame {}\n")
    return pkg


class TestPackageDiscovery:
    def test_returns_none_when_sources_absent(self, tmp_path: Path) -> None:
        with patch.object(media_engine, "_PACKAGE_CANDIDATES", [tmp_path / "nope"]):
            assert media_engine.package_dir() is None

    def test_requires_a_manifest_not_just_a_directory(self, tmp_path: Path) -> None:
        # A directory without Package.swift is not a package, and treating it
        # as one produces a confusing swift build failure instead of a clear
        # "sources not found".
        (tmp_path / "QuernMedia").mkdir()
        with patch.object(media_engine, "_PACKAGE_CANDIDATES", [tmp_path / "QuernMedia"]):
            assert media_engine.package_dir() is None

    def test_finds_a_real_package(self, package: Path) -> None:
        with patch.object(media_engine, "_PACKAGE_CANDIDATES", [package]):
            assert media_engine.package_dir() == package


def _fake_build(tmp_path: Path, package: Path, *, output: str = "fresh", calls=None):
    """The patches for one build: subprocess faked, ~/.quern/bin under tmp_path.
    Returns (installed path, the patch contexts)."""
    built = tmp_path / "out" / "quern-media"
    built.parent.mkdir(parents=True, exist_ok=True)
    installed = tmp_path / "bin" / "quern-media"
    installed.parent.mkdir(parents=True, exist_ok=True)

    def fake_run(cmd, **kwargs):  # noqa: ANN001, ANN003
        if calls is not None:
            calls.append(cmd)
        if "--show-bin-path" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout=str(built.parent), stderr="")
        built.write_text(output)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    return installed, [
        patch.object(media_engine, "_PACKAGE_CANDIDATES", [package]),
        patch.object(media_engine, "binary_path", return_value=installed),
        patch.object(media_engine, "QUERN_BIN_DIR", installed.parent),
        # build_media_engine_sync still runs SCRATCH_DIR.mkdir, and unpatched
        # that is CONFIG_DIR/build/QuernMedia -- a real path outside tmp_path.
        patch.object(media_engine, "SCRATCH_DIR", tmp_path / "scratch"),
        patch.object(media_engine, "xcode_available", return_value=True),
        patch("shutil.which", return_value="/usr/bin/swift"),
        patch("subprocess.run", side_effect=fake_run),
    ]


def _build(tmp_path: Path, package: Path, **kwargs) -> tuple[Path, list]:
    calls: list = []
    installed, patches = _fake_build(tmp_path, package, calls=calls, **kwargs)
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        media_engine.build_media_engine_sync()
    return installed, calls


class TestTheStamp:
    """Rebuilt when what it is built from changes, judged by content: file
    times are defeated by `git archive`, which stamps every file with its
    commit's time (stale preview app)."""

    def test_a_build_records_the_digest_of_its_sources(self, package, tmp_path):
        installed, _ = _build(tmp_path, package)

        stamp = installed.with_name("quern-media.sha256")
        assert stamp.read_text() == media_engine.stamp_text(
            media_engine.source_digest(package), media_engine.binary_digest(installed))

    def test_a_binary_replaced_under_a_correct_stamp_is_not_current(self, package, tmp_path):
        """A checkout from before stamps, or a second process building other
        sources, replaces the binary and leaves the stamp -- which still
        matches these sources (review)."""
        installed, _ = _build(tmp_path, package)
        installed.write_text("#!/bin/sh\necho someone else's build\n")

        with patch.object(media_engine, "binary_path", return_value=installed):
            assert media_engine.installed_digest() is None
        _, calls = _build(tmp_path, package)
        assert calls and installed.read_text() == "fresh"

    def test_a_one_field_stamp_reads_as_unstamped(self, package, tmp_path):
        installed, _ = _build(tmp_path, package)
        installed.with_name("quern-media.sha256").write_text(media_engine.source_digest(package))

        with patch.object(media_engine, "binary_path", return_value=installed):
            assert media_engine.installed_digest() is None

    def test_the_old_stamp_is_gone_before_the_binary_is_replaced(self, package, tmp_path):
        """An install that fails part way must not leave the old stamp beside
        whatever is there."""
        installed, _ = _build(tmp_path, package)
        (package / "Package.swift").write_text("// changed\n")
        _, patches = _fake_build(tmp_path, package)
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            stack.enter_context(patch.object(media_engine.shutil, "copy2",
                                             side_effect=OSError("disk full")))
            with pytest.raises(OSError):
                media_engine.build_media_engine_sync()

        assert not installed.with_name("quern-media.sha256").exists()

    def test_without_a_toolchain_an_installed_binary_is_used(self, package, tmp_path, caplog):
        """A macOS update that breaks the Command Line Tools must not end
        simulator preview while a working binary is there (review)."""
        installed, patches = _fake_build(tmp_path, package)
        installed.write_text("unstamped but working")
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            stack.enter_context(patch.object(media_engine, "xcode_available", return_value=False))
            run = stack.enter_context(patch("subprocess.run"))
            assert media_engine.build_media_engine_sync() == installed
            run.assert_not_called()
            with pytest.raises(RuntimeError, match="xcode-select --install"):
                media_engine.build_media_engine_sync(force=True)
            installed.unlink()
            with pytest.raises(RuntimeError, match="xcode-select --install"):
                media_engine.build_media_engine_sync()
        assert "no Swift toolchain" in caplog.text

    def test_a_second_build_with_nothing_changed_does_nothing(self, package, tmp_path):
        _build(tmp_path, package)
        _, calls = _build(tmp_path, package)

        assert calls == []

    def test_an_edited_source_rebuilds(self, package, tmp_path):
        _build(tmp_path, package)
        source = package / "Sources" / "QuernMedia" / "Frame.swift"
        source.write_text("struct Frame { var x = 1 }\n")
        installed, calls = _build(tmp_path, package, output="newer")

        assert calls, "an edited source must rebuild"
        assert installed.read_text() == "newer"

    def test_an_edited_source_that_looks_older_still_rebuilds(self, package, tmp_path):
        """The tarball case: the new source carries its commit's time, older
        than the binary built last week. The old mtime check kept the binary."""
        installed, _ = _build(tmp_path, package)
        source = package / "Sources" / "QuernMedia" / "Frame.swift"
        source.write_text("struct Frame { var y = 2 }\n")
        os.utime(source, (1_000_000, 1_000_000))
        os.utime(installed, (2_000_000_000, 2_000_000_000))

        _, calls = _build(tmp_path, package)
        assert calls

    def test_a_binary_newer_than_every_source_but_unstamped_rebuilds(self, package, tmp_path):
        """Every quern-media built before this change: no stamp beside it."""
        installed, _ = _build(tmp_path, package)
        installed.with_name("quern-media.sha256").unlink()
        os.utime(installed, (2_000_000_000, 2_000_000_000))

        _, calls = _build(tmp_path, package)
        assert calls

    def test_a_deleted_binary_with_its_stamp_left_rebuilds(self, package, tmp_path):
        installed, _ = _build(tmp_path, package)
        installed.unlink()

        _, calls = _build(tmp_path, package)
        assert calls and installed.exists()

    @pytest.mark.parametrize("rel", [
        "Sources/QuernMedia/Capture/New.swift",     # a new file, nested
        "Sources/QuernMedia/Resources/page.html",   # not Swift
    ])
    def test_any_file_added_under_sources_counts(self, package, rel):
        before = media_engine.source_digest(package)
        (package / rel).parent.mkdir(parents=True, exist_ok=True)
        (package / rel).write_text("x")

        assert media_engine.source_digest(package) != before

    @pytest.mark.parametrize("rel", [
        "Sources/QuernMedia/.DS_Store",
        "Sources/.hidden/Thing.swift",
    ])
    def test_hidden_files_do_not_count(self, package, rel):
        """Finder writes .DS_Store into any folder it opens: counted, it would
        rebuild on a look, and no shipped copy (#663) would match."""
        before = media_engine.source_digest(package)
        (package / rel).parent.mkdir(parents=True, exist_ok=True)
        (package / rel).write_text("x")

        assert media_engine.source_digest(package) == before

    def test_bytes_cannot_shift_a_file_boundary(self, package):
        """Run together, a\\0<path>\\0b in one file and a, b in two read the
        same: each file is hashed apart (CR)."""
        res = package / "Sources" / "QuernMedia" / "Resources"
        res.mkdir()
        (res / "a.bin").write_bytes(b"x\0Sources/QuernMedia/Resources/b.bin\0y")
        one = media_engine.source_digest(package)
        (res / "a.bin").write_bytes(b"x")
        (res / "b.bin").write_bytes(b"y")

        assert media_engine.source_digest(package) != one

    def test_the_manifest_counts(self, package):
        before = media_engine.source_digest(package)
        (package / "Package.swift").write_text("// swift-tools-version: 6.1\n")

        assert media_engine.source_digest(package) != before

    def test_a_renamed_source_counts(self, package):
        """Same bytes, another path: a different target or module layout."""
        before = media_engine.source_digest(package)
        src = package / "Sources" / "QuernMedia" / "Frame.swift"
        src.rename(src.with_name("Frames.swift"))

        assert media_engine.source_digest(package) != before

    def test_tests_do_not_count(self, package):
        """The test target is not in the binary: editing a test must not
        rebuild everyone's quern-media."""
        before = media_engine.source_digest(package)
        (package / "Tests" / "QuernMediaTests").mkdir(parents=True)
        (package / "Tests" / "QuernMediaTests" / "FrameTests.swift").write_text("x")

        assert media_engine.source_digest(package) == before

    def test_the_build_recipe_counts(self, package, monkeypatch):
        before = media_engine.source_digest(package)
        monkeypatch.setattr(media_engine, "_DIGEST_RECIPE", "quern-media build 2")

        assert media_engine.source_digest(package) != before

    def test_the_digest_does_not_depend_on_where_the_package_is(self, package, tmp_path):
        """A signed copy built elsewhere from the same sources is current here
        (#663): the release step writes this digest beside it."""
        import shutil

        elsewhere = tmp_path / "elsewhere" / "QuernMedia"
        shutil.copytree(package, elsewhere)

        assert media_engine.source_digest(elsewhere) == media_engine.source_digest(package)

    def test_a_shipped_binary_with_a_matching_stamp_is_not_rebuilt(self, package, tmp_path):
        """#663's release puts the binary and its stamp in ~/.quern/bin."""
        installed, patches = _fake_build(tmp_path, package)
        installed.write_text("signed")
        installed.with_name("quern-media.sha256").write_text(media_engine.stamp_text(
            media_engine.source_digest(package), media_engine.binary_digest(installed)))

        _, calls = _build(tmp_path, package)
        assert calls == [] and installed.read_text() == "signed"

    def test_a_failed_build_leaves_no_stamp(self, package, tmp_path):
        """A stamp written up front would mark the failure current."""
        installed, patches = _fake_build(tmp_path, package)
        with contextlib.ExitStack() as stack:
            for p in patches[:-1]:
                stack.enter_context(p)
            stack.enter_context(patch("subprocess.run", return_value=subprocess.CompletedProcess(
                [], 1, stdout="", stderr="error: boom")))
            with pytest.raises(RuntimeError, match="boom"):
                media_engine.build_media_engine_sync()

        assert not installed.with_name("quern-media.sha256").exists()

    def test_a_stamp_that_cannot_be_written_does_not_fail_the_build(
        self, package, tmp_path, caplog,
    ):
        """It costs a rebuild next time, the safe direction."""
        installed, patches = _fake_build(tmp_path, package)
        real_replace = os.replace

        def replace(src, dst):  # noqa: ANN001
            if str(dst).endswith(".sha256"):
                raise PermissionError("read-only")
            return real_replace(src, dst)

        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            stack.enter_context(patch.object(media_engine.os, "replace", side_effect=replace))
            assert media_engine.build_media_engine_sync() == installed

        assert installed.read_text() == "fresh"
        assert not installed.with_name("quern-media.sha256").exists()
        assert not list(installed.parent.glob(".quern-media.sha256*")), "temp stamp left behind"
        assert "rebuilt next time" in caplog.text

    @pytest.mark.parametrize("content", [b"", b"\xff\xfe not utf-8"])
    def test_an_unreadable_stamp_reads_as_unstamped(self, package, tmp_path, content):
        installed, _ = _build(tmp_path, package)
        installed.with_name("quern-media.sha256").write_bytes(content)

        with patch.object(media_engine, "binary_path", return_value=installed):
            assert media_engine.installed_digest() is None

    def test_sources_that_cannot_be_read_are_a_runtime_error(self, package, tmp_path):
        """The documented contract: setup catches RuntimeError and OSError,
        but preview's callers expect RuntimeError."""
        _, patches = _fake_build(tmp_path, package)
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            stack.enter_context(patch.object(media_engine, "source_digest",
                                             side_effect=PermissionError("denied")))
            with pytest.raises(RuntimeError, match="Could not read the QuernMedia sources"):
                media_engine.build_media_engine_sync()

    def test_two_builds_at_once_run_one_after_the_other(self, package, tmp_path):
        """The server's start-up check and a first stream can both find it out
        of date; the second, let in after the first, finds it current."""
        import threading

        installed, patches = _fake_build(tmp_path, package)
        running = []
        overlap = []
        real = media_engine._build_locked

        def watched(force):
            running.append(1)
            overlap.append(len(running))
            threading.Event().wait(0.05)
            try:
                return real(force)
            finally:
                running.pop()

        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            stack.enter_context(patch.object(media_engine, "_build_locked", side_effect=watched))
            threads = [threading.Thread(target=media_engine.build_media_engine_sync)
                       for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(5)

        assert overlap == [1, 1]


class TestTheServerStartCheck:
    def _start(self, monkeypatch, package, tmp_path, *, owns=True, darwin=True, xcode=True):
        from server.device.media import preview

        installed, patches = _fake_build(tmp_path, package)
        monkeypatch.setattr("platform.system", lambda: "Darwin" if darwin else "Linux")
        monkeypatch.setattr(preview, "_owns_the_bin_dir", lambda: owns)
        stack = contextlib.ExitStack()
        for p in patches:
            stack.enter_context(p)
        stack.enter_context(patch.object(media_engine, "xcode_available", return_value=xcode))
        return installed, stack

    def test_it_rebuilds_a_stale_binary_and_says_so(self, monkeypatch, package, tmp_path):
        installed, stack = self._start(monkeypatch, package, tmp_path)
        installed.write_text("old")
        with stack:
            assert REAL_REFRESH_AT_START() is True
            assert REAL_REFRESH_AT_START() is False, "a second start has nothing to do"
        assert installed.read_text() == "fresh"

    def test_a_binary_deleted_under_its_stamp_is_a_rebuild(self, monkeypatch, package, tmp_path):
        """Same digest before and after: judged by the file, not the stamp."""
        installed, stack = self._start(monkeypatch, package, tmp_path)
        with stack:
            REAL_REFRESH_AT_START()
            installed.unlink()
            assert REAL_REFRESH_AT_START() is True

    @pytest.mark.parametrize("kwargs", [{"owns": False}, {"darwin": False}, {"xcode": False}])
    def test_it_does_nothing_where_it_should_not(self, monkeypatch, package, tmp_path, kwargs):
        """Another checkout sharing ~/.quern/bin would rebuild the installed
        quern's engine from its own sources, as with the mirror app."""
        installed, stack = self._start(monkeypatch, package, tmp_path, **kwargs)
        with stack:
            assert REAL_REFRESH_AT_START() is None
        assert not installed.exists()


class TestBuildInvocation:
    def _run_build(self, package: Path, tmp_path: Path, capture: list):  # noqa: ANN001
        built = tmp_path / "out" / "quern-media"
        built.parent.mkdir(parents=True, exist_ok=True)
        built.write_text("binary")
        installed = tmp_path / "bin" / "quern-media"
        installed.parent.mkdir(parents=True, exist_ok=True)

        def fake_run(cmd, **kwargs):  # noqa: ANN001, ANN003
            capture.append(cmd)
            if "--show-bin-path" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout=str(built.parent), stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with (
            patch.object(media_engine, "_PACKAGE_CANDIDATES", [package]),
            patch.object(media_engine, "binary_path", return_value=installed),
            patch.object(media_engine, "QUERN_BIN_DIR", installed.parent),
            # build_media_engine_sync still runs SCRATCH_DIR.mkdir, and
            # unpatched that is CONFIG_DIR/build/QuernMedia -- a real path
            # outside tmp_path, which the test would create on the developer's
            # machine and fail on wherever it is not writable.
            patch.object(media_engine, "SCRATCH_DIR", tmp_path / "scratch"),
            patch("shutil.which", return_value="/usr/bin/swift"),
            patch("subprocess.run", side_effect=fake_run),
        ):
            return media_engine.build_media_engine_sync(force=True)

    def test_build_products_go_outside_the_repo(self, package: Path, tmp_path: Path) -> None:
        # SwiftPM writes ~116 MB next to Package.swift by default, and quern's
        # sources can be under site-packages. This must not depend on an
        # ignore rule to stay out of the tree.
        calls: list = []
        self._run_build(package, tmp_path, calls)
        build_cmd = calls[0]
        assert "--scratch-path" in build_cmd
        scratch = Path(build_cmd[build_cmd.index("--scratch-path") + 1])
        assert package not in scratch.parents and scratch != package

    def test_builds_release_configuration(self, package: Path, tmp_path: Path) -> None:
        calls: list = []
        self._run_build(package, tmp_path, calls)
        assert "-c" in calls[0] and "release" in calls[0]

    def test_asks_swiftpm_where_the_binary_landed(self, package: Path, tmp_path: Path) -> None:
        # The path under the scratch directory contains the target triple, so
        # guessing it would break on a different architecture.
        calls: list = []
        self._run_build(package, tmp_path, calls)
        assert any("--show-bin-path" in c for c in calls)


    def test_the_binary_is_renamed_into_place_not_overwritten(
        self, package: Path, tmp_path: Path
    ) -> None:
        """copy2 keeps the inode and rewrites in place. macOS validates
        code-signature pages per vnode, so rewriting the file underneath a
        running quern-media can kill it with a signature fault, and a
        concurrent launch can exec a half-written one. A rename gives the new
        file its own inode and leaves running processes on the old one."""
        import os

        capture: list = []
        installed = self._run_build(package, tmp_path, capture)

        before = os.stat(installed).st_ino
        # A second build over the top of the first.
        self._run_build(package, tmp_path, [])
        after = os.stat(installed).st_ino

        assert before != after, (
            "the binary was rewritten in place; a running process would have "
            "had its file changed underneath it"
        )


class TestFailures:
    def test_missing_sources_are_reported_clearly(self, tmp_path: Path) -> None:
        with patch.object(media_engine, "_PACKAGE_CANDIDATES", [tmp_path / "nope"]):
            with pytest.raises(RuntimeError, match="QuernMedia sources not found"):
                media_engine.build_media_engine_sync()

    def test_missing_toolchain_suggests_the_fix(self, package: Path, tmp_path: Path) -> None:
        with (
            patch.object(media_engine, "_PACKAGE_CANDIDATES", [package]),
            patch.object(media_engine, "binary_path", return_value=tmp_path / "absent"),
            patch("shutil.which", return_value=None),
        ):
            with pytest.raises(RuntimeError, match="xcode-select --install"):
                media_engine.build_media_engine_sync()

    def test_without_the_developer_tools_the_swift_stub_is_never_run(
        self, package: Path, tmp_path: Path
    ) -> None:
        """/usr/bin/swift exists on every Mac; running it without the tools
        shows macOS's install dialog (#659)."""
        with (
            patch.object(media_engine, "_PACKAGE_CANDIDATES", [package]),
            patch.object(media_engine, "binary_path", return_value=tmp_path / "absent"),
            patch.object(media_engine, "xcode_available", return_value=False),
            patch("shutil.which", return_value="/usr/bin/swift"),
            patch.object(media_engine.subprocess, "run") as run,
        ):
            with pytest.raises(RuntimeError, match="xcode-select --install"):
                media_engine.build_media_engine_sync()
        run.assert_not_called()

    def test_compile_errors_surface_the_compiler_output(
        self, package: Path, tmp_path: Path
    ) -> None:
        def fake_run(cmd, **kwargs):  # noqa: ANN001, ANN003
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="error: no such module")

        with (
            patch.object(media_engine, "_PACKAGE_CANDIDATES", [package]),
            patch.object(media_engine, "binary_path", return_value=tmp_path / "absent"),
            patch("shutil.which", return_value="/usr/bin/swift"),
            patch("subprocess.run", side_effect=fake_run),
        ):
            with pytest.raises(RuntimeError, match="no such module"):
                media_engine.build_media_engine_sync()

    def test_timeout_names_the_toolchain_check(self, package: Path, tmp_path: Path) -> None:
        with (
            patch.object(media_engine, "_PACKAGE_CANDIDATES", [package]),
            patch.object(media_engine, "binary_path", return_value=tmp_path / "absent"),
            patch("shutil.which", return_value="/usr/bin/swift"),
            patch("subprocess.run", side_effect=subprocess.TimeoutExpired("swift", 180)),
        ):
            with pytest.raises(RuntimeError, match="xcode-select -p"):
                media_engine.build_media_engine_sync()

    def test_a_successful_build_with_no_binary_is_an_error(
        self, package: Path, tmp_path: Path
    ) -> None:
        # swift build can report success while producing nothing at the path
        # we expect; silently returning a nonexistent path defers the failure
        # to whoever tries to execute it.
        def fake_run(cmd, **kwargs):  # noqa: ANN001, ANN003
            if "--show-bin-path" in cmd:
                return subprocess.CompletedProcess(
                    cmd, 0, stdout=str(tmp_path / "empty"), stderr=""
                )
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        (tmp_path / "empty").mkdir()
        with (
            patch.object(media_engine, "_PACKAGE_CANDIDATES", [package]),
            patch.object(media_engine, "binary_path", return_value=tmp_path / "absent"),
            patch("shutil.which", return_value="/usr/bin/swift"),
            patch("subprocess.run", side_effect=fake_run),
        ):
            with pytest.raises(RuntimeError, match="is missing"):
                media_engine.build_media_engine_sync()


class TestAsyncBuildWrapper:
    @pytest.mark.asyncio
    async def test_the_build_does_not_run_on_the_event_loop(self, monkeypatch) -> None:
        """A cold build takes up to BUILD_TIMEOUT. Called straight from a
        request path that is three minutes of stalled server, so the wrapper
        has to hand the blocking half to a thread."""
        import threading

        from server.device.media import media_engine

        seen: dict = {}

        def fake_sync(force: bool = False):
            seen["thread"] = threading.current_thread().name
            seen["force"] = force
            return Path("/tmp/quern-media")

        monkeypatch.setattr(media_engine, "build_media_engine_sync", fake_sync)

        seen["loop_thread"] = threading.current_thread().name
        assert await media_engine.build_media_engine(force=True) == Path("/tmp/quern-media")
        assert seen["force"] is True
        assert seen["thread"] != seen["loop_thread"], "the build blocked the loop"
