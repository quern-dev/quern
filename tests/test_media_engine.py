"""Tests for the QuernMedia build helper.

No Swift toolchain is invoked: subprocess is mocked throughout, matching the
convention the rest of the suite uses (see tests/test_wda.py).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from server.device import media_engine


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


class TestFreshness:
    def test_notices_a_touched_source_not_just_the_manifest(self, package: Path) -> None:
        # The normal case is editing a source without touching Package.swift.
        # A freshness test that only stats the manifest would serve a stale
        # binary forever.
        before = media_engine._newest_source_mtime(package)
        source = package / "Sources" / "QuernMedia" / "Frame.swift"
        source.write_text("struct Frame { var x = 1 }\n")
        import os

        os.utime(source, (before + 100, before + 100))
        assert media_engine._newest_source_mtime(package) > before

    def test_skips_the_build_when_the_binary_is_newer(
        self, package: Path, tmp_path: Path
    ) -> None:
        installed = tmp_path / "bin" / "quern-media"
        installed.parent.mkdir(parents=True)
        installed.write_text("binary")
        import os

        os.utime(installed, (2_000_000_000, 2_000_000_000))

        with (
            patch.object(media_engine, "_PACKAGE_CANDIDATES", [package]),
            patch.object(media_engine, "binary_path", return_value=installed),
            patch("subprocess.run") as run,
        ):
            assert media_engine.build_media_engine() == installed
            run.assert_not_called()

    def test_rebuilds_when_a_source_is_newer(self, package: Path, tmp_path: Path) -> None:
        installed = tmp_path / "bin" / "quern-media"
        installed.parent.mkdir(parents=True)
        installed.write_text("stale")
        import os

        os.utime(installed, (1_000_000, 1_000_000))
        os.utime(package / "Package.swift", (2_000_000_000, 2_000_000_000))

        built = tmp_path / "out" / "quern-media"
        built.parent.mkdir(parents=True)
        built.write_text("fresh")

        def fake_run(cmd, **kwargs):  # noqa: ANN001, ANN003
            if "--show-bin-path" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout=str(built.parent), stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with (
            patch.object(media_engine, "_PACKAGE_CANDIDATES", [package]),
            patch.object(media_engine, "binary_path", return_value=installed),
            patch.object(media_engine, "QUERN_BIN_DIR", installed.parent),
            patch("shutil.which", return_value="/usr/bin/swift"),
            patch("subprocess.run", side_effect=fake_run),
        ):
            media_engine.build_media_engine()
        assert installed.read_text() == "fresh"


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
            patch("shutil.which", return_value="/usr/bin/swift"),
            patch("subprocess.run", side_effect=fake_run),
        ):
            return media_engine.build_media_engine(force=True)

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


class TestFailures:
    def test_missing_sources_are_reported_clearly(self, tmp_path: Path) -> None:
        with patch.object(media_engine, "_PACKAGE_CANDIDATES", [tmp_path / "nope"]):
            with pytest.raises(RuntimeError, match="QuernMedia sources not found"):
                media_engine.build_media_engine()

    def test_missing_toolchain_suggests_the_fix(self, package: Path, tmp_path: Path) -> None:
        with (
            patch.object(media_engine, "_PACKAGE_CANDIDATES", [package]),
            patch.object(media_engine, "binary_path", return_value=tmp_path / "absent"),
            patch("shutil.which", return_value=None),
        ):
            with pytest.raises(RuntimeError, match="xcode-select --install"):
                media_engine.build_media_engine()

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
                media_engine.build_media_engine()

    def test_timeout_names_the_toolchain_check(self, package: Path, tmp_path: Path) -> None:
        with (
            patch.object(media_engine, "_PACKAGE_CANDIDATES", [package]),
            patch.object(media_engine, "binary_path", return_value=tmp_path / "absent"),
            patch("shutil.which", return_value="/usr/bin/swift"),
            patch("subprocess.run", side_effect=subprocess.TimeoutExpired("swift", 180)),
        ):
            with pytest.raises(RuntimeError, match="xcode-select -p"):
                media_engine.build_media_engine()

    def test_a_successful_build_with_no_binary_is_an_error(
        self, package: Path, tmp_path: Path
    ) -> None:
        # swift build can report success while producing nothing at the path
        # we expect; silently returning a nonexistent path defers the failure
        # to whoever tries to execute it.
        def fake_run(cmd, **kwargs):  # noqa: ANN001, ANN003
            if "--show-bin-path" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout=str(tmp_path / "empty"), stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        (tmp_path / "empty").mkdir()
        with (
            patch.object(media_engine, "_PACKAGE_CANDIDATES", [package]),
            patch.object(media_engine, "binary_path", return_value=tmp_path / "absent"),
            patch("shutil.which", return_value="/usr/bin/swift"),
            patch("subprocess.run", side_effect=fake_run),
        ):
            with pytest.raises(RuntimeError, match="is missing"):
                media_engine.build_media_engine()
