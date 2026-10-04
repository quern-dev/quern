"""Modules that build paths from their own location find them in this checkout.

Each path is built from the module's own location, so moving the module moves
what it points at (#396). Nothing fails loudly when one goes wrong: the preview
bundle skips a missing icon, and a missing source reads as "not installed".
`wda.ICON_PATH` is checked in tests/test_wda.py.
"""

from server.device.media import media_engine, preview


def test_the_quernmedia_package_is_found():
    assert (media_engine._PACKAGE_CANDIDATES[0] / "Package.swift").is_file()


def test_the_preview_sources_are_found():
    for path in preview._SOURCE_CANDIDATES + preview._SHARED_SOURCE_CANDIDATES:
        assert path.is_file(), path


def test_the_preview_icon_is_found():
    assert (preview._RESOURCES_DIR / "wda-icon.png").is_file()


def test_the_sim_bridge_source_is_found():
    from server.device.ios import sim_bridge

    for path in sim_bridge._SOURCE_CANDIDATES:
        assert path.is_file(), path


def test_every_package_under_server_has_an_init():
    """Without one a directory still imports, as a namespace package, so the
    suite passes -- but pyproject's `packages.find` skips it, and a built
    install ships without it. ios/ and android/ were created without one."""
    import pathlib

    server = pathlib.Path(__file__).resolve().parents[1] / "server"
    missing = [
        str(d.relative_to(server.parent)) for d in [server, *server.rglob("*")]
        if d.is_dir() and d.name != "__pycache__" and any(d.glob("*.py"))
        and not (d / "__init__.py").is_file()
    ]
    assert not missing, missing
