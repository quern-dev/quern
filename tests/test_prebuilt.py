"""The screen mirror and quern-media a release ships pre-built (#662, #663).

A release tarball carries signed copies under `prebuilt/`, stamped with the
sources they were built from. Where those match, they are installed instead of
compiled, so a Mac without the developer tools still gets USB iPhone mirroring,
simulator preview and recorded video.
"""

from __future__ import annotations

import contextlib
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from server.device.media import media_engine, preview
from tests.test_preview import _stage

#: Bound at import, before the conftest stub replaces it for every test.
REAL_MEDIA_REFRESH = media_engine.refresh_at_server_start


def _never(*a, **k):  # noqa: ANN001, ANN003
    raise AssertionError("compiled, with a matching shipped copy there")


# -- the screen mirror ------------------------------------------------------------------


def _ship_preview(tmp_path: Path, monkeypatch, *, digest: str | None = None) -> Path:
    """A shipped, sealed bundle stamped for `digest` (default: these sources)."""
    shipped = tmp_path / "prebuilt" / "Quern Preview.app"
    preview.write_app_bundle(shipped, digest or preview.expected_digest())
    (shipped / "Contents" / "MacOS" / "ios-preview").write_text("signed release build")
    (shipped / "Contents" / "_CodeSignature").mkdir()
    (shipped / "Contents" / "_CodeSignature" / "CodeResources").write_text("seal")
    monkeypatch.setattr(preview, "PREBUILT_DIR", tmp_path / "prebuilt")
    return shipped


def test_a_shipped_copy_of_these_sources_is_installed_with_no_tools(tmp_path, monkeypatch):
    bundle, binary, compiled = _stage(tmp_path, monkeypatch)
    _ship_preview(tmp_path, monkeypatch)
    monkeypatch.setattr(preview, "xcode_available", lambda: False)
    monkeypatch.setattr(preview.subprocess, "run", _never)

    assert preview.shipped_current()
    assert preview.build_preview_bundle() == binary
    assert binary.read_text() == "signed release build"
    assert preview.is_sealed(bundle)                    # the signature came with it
    assert preview.read_stamp(bundle).digest == preview.expected_digest()
    # And it is current from then on: nothing is rewritten over the seal.
    assert preview.build_preview_bundle() == binary


def test_a_shipped_copy_is_preferred_to_compiling_where_the_tools_are_there(
        tmp_path, monkeypatch):
    """Its signature is what the camera grant is keyed to, the same on every
    Mac and every update that leaves the preview alone (#662)."""
    bundle, binary, compiled = _stage(tmp_path, monkeypatch)
    _ship_preview(tmp_path, monkeypatch)
    preview.build_preview_bundle()
    assert compiled == [] and preview.is_sealed(bundle)


def test_a_shipped_copy_of_other_sources_is_compiled_over(tmp_path, monkeypatch):
    """A checkout whose sources moved past the release's builds its own."""
    bundle, binary, compiled = _stage(tmp_path, monkeypatch)
    _ship_preview(tmp_path, monkeypatch, digest="0" * 64)
    assert not preview.shipped_current()
    preview.build_preview_bundle()
    assert len(compiled) == 1 and binary.read_text() == "compiled"
    assert not preview.is_sealed(bundle)


def test_an_installed_copy_is_replaced_whole(tmp_path, monkeypatch):
    bundle, binary, compiled = _stage(tmp_path, monkeypatch)
    preview.write_app_bundle(bundle, "1" * 64)          # an older build
    binary.write_text("old")
    (bundle / "Contents" / "Resources" / "leftover").write_text("x")
    _ship_preview(tmp_path, monkeypatch)
    preview.build_preview_bundle()
    assert binary.read_text() == "signed release build"
    assert not (bundle / "Contents" / "Resources" / "leftover").exists()
    assert not list(bundle.parent.glob(".ios-preview-*"))   # no staging left


def test_no_shipped_copy_is_not_current(tmp_path, monkeypatch):
    _stage(tmp_path, monkeypatch)
    monkeypatch.setattr(preview, "PREBUILT_DIR", tmp_path / "nothing-here")
    assert not preview.shipped_current()


def test_the_release_bundle_is_stamped_as_a_local_build_would_be(tmp_path, monkeypatch):
    _stage(tmp_path, monkeypatch)
    built = tmp_path / "release-binary"
    built.write_text("universal")
    bundle = preview.stage_release_bundle(tmp_path / "out", built)
    stamp = preview.read_stamp(bundle)
    assert stamp.digest == preview.expected_digest()
    executable = bundle / "Contents" / "MacOS" / "ios-preview"
    assert executable.read_text() == "universal" and executable.stat().st_mode & 0o111


# -- quern-media ------------------------------------------------------------------------


@pytest.fixture
def media(tmp_path: Path):
    """A checkout shaped like the real one: <root>/macos/QuernMedia, a shipped
    binary at <root>/prebuilt, ~/.quern/bin under tmp_path, no developer tools."""
    root = tmp_path / "root"
    package = root / "macos" / "QuernMedia"
    (package / "Sources" / "QuernMedia").mkdir(parents=True)
    (package / "Package.swift").write_text("// swift-tools-version: 6.0\n")
    (package / "Sources" / "QuernMedia" / "Frame.swift").write_text("struct Frame {}\n")
    installed = tmp_path / "bin" / "quern-media"
    with contextlib.ExitStack() as stack:
        for p in (patch.object(media_engine, "_PACKAGE_CANDIDATES", [package]),
                  patch.object(media_engine, "binary_path", return_value=installed),
                  patch.object(media_engine, "QUERN_BIN_DIR", installed.parent),
                  patch.object(media_engine, "SCRATCH_DIR", tmp_path / "scratch"),
                  patch.object(media_engine, "xcode_available", return_value=False),
                  patch("subprocess.run", side_effect=_never)):
            stack.enter_context(p)
        yield root, package, installed


def _ship_media(root: Path, package: Path, *, source: str | None = None,
                binary: str | None = None) -> Path:
    shipped = root / "prebuilt" / "quern-media"
    shipped.parent.mkdir(parents=True, exist_ok=True)
    shipped.write_text("signed quern-media")
    (root / "prebuilt" / "quern-media.sha256").write_text(media_engine.stamp_text(
        source or media_engine.source_digest(package),
        binary or media_engine.binary_digest(shipped)))
    return shipped


def test_a_shipped_quern_media_of_these_sources_is_installed_and_stamped(media):
    root, package, installed = media
    _ship_media(root, package)
    assert media_engine.shipped_current()
    assert media_engine.build_media_engine_sync() == installed
    assert installed.read_text() == "signed quern-media"
    # Stamped as a build would be, so it stays current without the tools.
    assert media_engine.installed_digest() == media_engine.source_digest(package)
    assert media_engine.build_media_engine_sync() == installed


@pytest.mark.parametrize("stamp", ["sources", "binary"])
def test_a_shipped_quern_media_whose_stamp_does_not_hold_is_not_used(media, stamp):
    """Another release's sources, or a binary that is not the one stamped."""
    root, package, installed = media
    if stamp == "sources":
        _ship_media(root, package, source="0" * 64)
    else:
        _ship_media(root, package, binary="0" * 64)
    assert not media_engine.shipped_current()
    with pytest.raises(RuntimeError, match="swift not found"):
        media_engine.build_media_engine_sync()
    assert not installed.exists()


def test_the_server_start_installs_a_shipped_quern_media_with_no_tools(media, monkeypatch):
    root, package, installed = media
    _ship_media(root, package)
    monkeypatch.setattr(preview, "_owns_the_bin_dir", lambda: True)
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    assert REAL_MEDIA_REFRESH() is True
    assert installed.read_text() == "signed quern-media"


# -- setup --------------------------------------------------------------------------------


def test_setup_installs_both_with_no_tools_and_says_so(tmp_path, monkeypatch, media):
    from server.lifecycle import setup

    root, package, installed = media
    _ship_media(root, package)
    bundle, binary, compiled = _stage(tmp_path, monkeypatch)
    _ship_preview(tmp_path, monkeypatch)
    for module in (setup, preview):
        monkeypatch.setattr(module, "xcode_available", lambda: False)
    monkeypatch.setattr(preview, "stale_mirror_pids", lambda _b: [])

    mirror = setup.build_preview_app()
    engine = setup.build_simulator_preview_engine()

    assert mirror.status == setup.CheckStatus.OK, mirror
    assert mirror.message.startswith("Installed the release's signed copy")
    assert engine.status == setup.CheckStatus.OK, engine
    assert engine.message.startswith("Installed the release's signed copy")
    assert compiled == []


def test_setup_without_tools_or_a_shipped_copy_still_skips(tmp_path, monkeypatch):
    from server.lifecycle import setup

    _stage(tmp_path, monkeypatch)
    monkeypatch.setattr(preview, "PREBUILT_DIR", tmp_path / "nothing-here")
    monkeypatch.setattr(setup, "xcode_available", lambda: False)
    monkeypatch.setattr(setup, "_prompt_yn", lambda *a, **k: False)
    assert setup.build_preview_app().status == setup.CheckStatus.SKIPPED


def test_a_failed_compile_is_not_a_subprocess_the_tests_could_reach():
    """The fixtures above refuse every subprocess; make sure that refusal is
    what a compile hits, so a passing test means nothing was compiled."""
    with pytest.raises(AssertionError, match="compiled"):
        _never(["swiftc"])
    assert subprocess.run is not _never


def test_the_server_start_installs_a_shipped_screen_mirror_with_no_tools(
        tmp_path, monkeypatch):
    from tests.test_preview import REAL_REFRESH_AT_START

    bundle, binary, compiled = _stage(tmp_path, monkeypatch)
    _ship_preview(tmp_path, monkeypatch)
    monkeypatch.setattr(preview, "xcode_available", lambda: False)
    monkeypatch.setattr(preview, "_owns_the_bin_dir", lambda: True)
    monkeypatch.setattr(preview, "stale_mirror_pids", lambda _b: [])
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    refreshed = REAL_REFRESH_AT_START()
    assert refreshed is not None and refreshed.rebuilt
    assert preview.is_sealed(bundle) and compiled == []


def test_the_release_s_version_is_stamped_and_the_copy_is_still_current(tmp_path, monkeypatch):
    """The cut builds this before its version bump: the sources still say the
    previous version, and the About panel should say the release's."""
    _stage(tmp_path, monkeypatch)
    built = tmp_path / "release-binary"
    built.write_text("universal")
    bundle = preview.stage_release_bundle(tmp_path / "out", built, "9.9.9-beta.1")
    stamp = preview.read_stamp(bundle)
    assert stamp.version == "9.9.9-beta.1"
    assert stamp.digest == preview.expected_digest()


# -- CodeRabbit on #723 -----------------------------------------------------------------


def test_an_installed_copy_is_dated_when_it_was_installed(tmp_path, monkeypatch):
    """`stale_mirror_pids` takes a window started before the binary was
    written as old. Dated as the release build, a window opened between the
    build and the update was passed over and kept running the old build."""
    import os
    import time

    bundle, binary, compiled = _stage(tmp_path, monkeypatch)
    shipped = _ship_preview(tmp_path, monkeypatch)
    built_long_ago = time.time() - 86400
    os.utime(shipped / "Contents" / "MacOS" / "ios-preview", (built_long_ago, built_long_ago))
    before = time.time()
    preview.build_preview_bundle()
    assert binary.stat().st_mtime >= before - 1


def _failing_swap(monkeypatch, *, restore_fails: bool):
    """os.replace that fails moving the new copy into place, and -- when
    asked -- putting the old one back too."""
    import os

    real = os.replace

    def replace(src, dst):
        name = Path(src).name
        if name == "Quern Preview.app" and ".installing" in str(src):
            raise OSError("disk full")
        if name == "previous" and restore_fails:
            raise OSError("still full")
        return real(src, dst)
    monkeypatch.setattr(preview.os, "replace", replace)


def test_a_failed_swap_puts_the_old_bundle_back(tmp_path, monkeypatch):
    bundle, binary, compiled = _stage(tmp_path, monkeypatch)
    preview.write_app_bundle(bundle, "1" * 64)
    binary.write_text("old")
    _ship_preview(tmp_path, monkeypatch)
    _failing_swap(monkeypatch, restore_fails=False)
    with pytest.raises(RuntimeError, match="disk full"):
        preview.build_preview_bundle()
    assert binary.read_text() == "old"                  # still a Screen Mirror
    assert not list(bundle.parent.glob(".ios-preview-*"))


def test_an_old_bundle_that_cannot_be_put_back_is_kept(tmp_path, monkeypatch):
    bundle, binary, compiled = _stage(tmp_path, monkeypatch)
    preview.write_app_bundle(bundle, "1" * 64)
    binary.write_text("old")
    _ship_preview(tmp_path, monkeypatch)
    _failing_swap(monkeypatch, restore_fails=True)
    with pytest.raises(RuntimeError, match="disk full"):
        preview.build_preview_bundle()
    [kept] = list(bundle.parent.glob(".ios-preview-*.installing/previous"))
    assert (kept / "Contents" / "MacOS" / "ios-preview").read_text() == "old"
