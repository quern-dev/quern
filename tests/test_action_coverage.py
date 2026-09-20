"""Every API route is either an action or explicitly not one.

The trace is only as good as its coverage, and coverage rots silently: a new
router appears, nothing logs it, and nobody notices until a bug report has no
record of the thing that caused it. That is how `proxy` ended up with eighteen
tools and no entries at all.

**Why this is a test and not middleware.** Wrapping every request uniformly
was the obvious alternative and it is wrong twice over. It would guess the
outcome from the status code, which cannot express `suspect`, `ambiguous` or
`not_found` -- the three we added precisely because they are not failures. And
it would have to wrap `send` for the three streaming endpoints (video, logs
SSE, proxy SSE), giving them an action entry whose "duration" is how long
someone watched a stream. The judgement of what counts as an action is the
point, so it is made per route, here, in the open.

See docs/proposals/logging-spec.md.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

_API = pathlib.Path(__file__).resolve().parents[1] / "server" / "api"

#: A route that is neither wrapped nor listed below fails this test, which is
#: the whole mechanism: the decision has to be made rather than defaulted.
_NOT_ACTIONS: frozenset[str] = frozenset({
    "app_state.py:get_plist_watch_config_endpoint",
    "builds.py:get_latest_build",
    "device.py:tool_sites",
    "device.py:video_stream",
    "device.py:preview_status",
    "device.py:preview_devices",
    "device.py:get_timeline",
    "landmarks.py:list_landmarks",
    "logs.py:stream_logs",
    "logs.py:query_logs",
    "logs.py:get_summary",
    "logs.py:get_errors",
    "logs.py:list_sources",
    "logs.py:set_filter",
    "logs.py:get_filter",
    "proxy.py:proxy_status",
    "proxy.py:query_flows",
    "proxy.py:flow_summary",
    "proxy.py:stream_flows",
    "proxy.py:get_flow",
    "proxy_certs.py:download_cert",
    "proxy_certs.py:cert_status",
    "proxy_certs.py:setup_guide",
    "proxy_intercept.py:list_held_flows",
    "proxy_intercept.py:list_mocks",
    "proxy_intercept.py:get_bypass",
    "system.py:update_status",
    "system.py:get_channel",
})
"""Routes that deliberately emit nothing.

The line is what the route touches, not whether it is a read. A read *of the
device* is an action -- `get_ui_tree` talks to the device and takes time, and
`device.read` exists so those can be filtered out of a trace in one clause. A
read of quern's own state is not: querying the trace is not part of the trace,
and a liveness probe is not a thing that happened to anyone.

Streams are here too. The duration of `stream_logs` is how long somebody
watched it.
"""

#: Routes that have not been classified yet. This list may only shrink.
#:
#: Landed as a backlog rather than 84 failing tests, so that the guard is
#: active *now*: a route added tomorrow is neither wrapped nor listed here, so
#: it fails. Working through these is ordinary follow-up work; letting a new
#: one join them silently is the thing worth preventing.
_UNCLASSIFIED: frozenset[str] = frozenset({
    "app_state.py:save_app_state",
    "app_state.py:restore_app_state",
    "app_state.py:list_app_states",
    "app_state.py:delete_app_state",
    "app_state.py:read_app_plist",
    "app_state.py:set_app_plist_value",
    "app_state.py:set_app_plist_values",
    "app_state.py:diff_app_plist",
    "app_state.py:delete_app_plist_key",
    "app_state.py:start_plist_watch",
    "app_state.py:stop_plist_watch",
    "app_state.py:configure_plist_watch",
    "app_state.py:clear_plist_watch_config_endpoint",
    "build_app.py:build_and_install",
    "builds.py:parse_build",
    "builds.py:parse_build_file",
    "crashes.py:get_latest_crashes",
    "device.py:list_devices",
    "device.py:boot_device",
    "device.py:shutdown_device",
    "device.py:erase_device",
    "device.py:set_active_device",
    "device.py:install_app",
    "device.py:launch_app",
    "device.py:terminate_app",
    "device.py:uninstall_app",
    "device.py:list_apps",
    "device.py:take_screenshot",
    "device.py:set_location",
    "device.py:open_url",
    "device.py:grant_permission",
    "device.py:set_locale",
    "device.py:set_hardware_keyboard",
    "device.py:set_font_scale",
    "device.py:set_display_density",
    "device.py:start_simulator_logging",
    "device.py:stop_simulator_logging",
    "device.py:start_device_logging",
    "device.py:stop_device_logging",
    "device.py:preview_start",
    "device.py:preview_stop",
    "device.py:screenshot_annotated",
    "device.py:start_timeline",
    "device.py:stop_timeline",
    "device_pool.py:refresh_pool",
    "device_pool.py:resolve_device",
    "device_pool.py:ensure_devices",
    "device_ui.py:get_element",
    "device_ui.py:get_screen_summary",
    "device_ui.py:restore_input",
    "device_ui.py:scroll_to_element",
    "landmarks.py:load_landmarks",
    "landmarks.py:identify_screen",
    "landmarks.py:unload_landmarks",
    "landmarks.py:validate_landmarks",
    "logs.py:start_oslog_streaming",
    "logs.py:stop_oslog_streaming",
    "proxy.py:start_proxy",
    "proxy.py:stop_proxy",
    "proxy.py:configure_system",
    "proxy.py:unconfigure_system",
    "proxy.py:wait_for_flow",
    "proxy.py:start_capture",
    "proxy.py:stop_capture",
    "proxy.py:set_proxy_filter",
    "proxy.py:set_local_capture",
    "proxy_certs.py:verify_cert",
    "proxy_certs.py:record_device_proxy_config_endpoint",
    "proxy_intercept.py:set_intercept",
    "proxy_intercept.py:clear_intercept",
    "proxy_intercept.py:release_flow",
    "proxy_intercept.py:release_all",
    "proxy_intercept.py:replay_flow",
    "proxy_intercept.py:set_mock",
    "proxy_intercept.py:update_mock",
    "proxy_intercept.py:delete_mock",
    "proxy_intercept.py:delete_all_mocks",
    "proxy_intercept.py:set_bypass",
    "proxy_intercept.py:clear_bypass",
    "system.py:put_channel",
    "system.py:trigger_update",
    "wda.py:setup_wda",
    "wda.py:start_wda_driver",
    "wda.py:stop_wda_driver",
})


def _routes_in(path: pathlib.Path) -> list[tuple[str, ast.AST]]:
    """(function name, node) for every decorated route handler in a module."""
    tree = ast.parse(path.read_text())
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for dec in node.decorator_list:
            target = dec.func if isinstance(dec, ast.Call) else dec
            if (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "router"
                and target.attr in {"get", "post", "put", "delete", "patch"}
            ):
                found.append((node.name, node))
                break
    return found


def _wraps_an_action(node: ast.AST) -> bool:
    """Does this handler open an `action(...)` / `_action(...)` block?"""
    for sub in ast.walk(node):
        if not isinstance(sub, ast.With | ast.AsyncWith):
            continue
        for item in sub.items:
            call = item.context_expr
            if isinstance(call, ast.Call) and getattr(call.func, "id", "") in {
                "action", "_action",
            }:
                return True
    return False


def _all_routes():
    for module in sorted(_API.glob("*.py")):
        for name, node in _routes_in(module):
            yield f"{module.name}:{name}", node


@pytest.mark.parametrize(
    ("route_id", "node"), list(_all_routes()), ids=lambda v: v if isinstance(v, str) else "",
)
def test_every_route_is_classified(route_id, node):
    """Either it emits an action entry, or it is listed as not an action."""
    if route_id in _NOT_ACTIONS or route_id in _UNCLASSIFIED:
        return
    assert _wraps_an_action(node), (
        f"{route_id} emits no action entry and is not listed as a non-action.\n"
        "Either wrap it in `with action(...)` so it appears in the trace, or "
        "add it to _NOT_ACTIONS with the reason it should not. A route that "
        "changes device or proxy state belongs in the trace."
    )


def test_the_non_action_list_does_not_rot():
    """An entry naming a route that no longer exists exempts nothing, and
    hides the next route that takes its name."""
    live = {route_id for route_id, _ in _all_routes()}
    stale = sorted((_NOT_ACTIONS | _UNCLASSIFIED) - live)

    assert not stale, f"these name routes that no longer exist: {stale}"


def test_a_listed_route_does_not_also_emit():
    """Listed as not-an-action *and* wrapped is a contradiction -- one of the
    two is out of date, and which one is not obvious later."""
    both = [
        route_id for route_id, node in _all_routes()
        if route_id in (_NOT_ACTIONS | _UNCLASSIFIED) and _wraps_an_action(node)
    ]

    assert not both, (
        f"these are listed as unclassified or non-actions but now emit an "
        f"action entry: {both}. Remove them from the list -- a backlog entry "
        f"for work that is done hides the work that is not."
    )
