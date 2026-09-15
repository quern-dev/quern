"""Guard tests that keep README.md in sync with the code it documents.

The README's MCP tool table and API endpoint tables are hand-maintained, so
they drift silently every time a feature lands without a matching doc edit.
By v0.13.4 the tool table had fallen 37 tools behind the source and the prose
count ("78 tools") matched neither the table nor the code. These tests make
that drift a test failure instead of something a reader discovers first.

The CLI command block drifts the same way: `doctor` and `set-channel` both
shipped in 0.14.0 and neither reached the README, so a reader had no way to
find the beta channel's own entry point.

Each test reports the exact symbols to add or remove, so a failure is a
to-do list rather than a puzzle.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
README = REPO_ROOT / "README.md"
API_REFERENCE = REPO_ROOT / "docs" / "api-reference.md"
MCP_TOOLS_DIR = REPO_ROOT / "mcp" / "src" / "tools"
SERVER_DIR = REPO_ROOT / "server"
# The CLI surface is split across two files: argparse subparsers are built in
# main.py, while several commands are intercepted off sys.argv in __main__.py
# before argparse ever runs.
CLI_SOURCES = (REPO_ROOT / "server" / "__main__.py", REPO_ROOT / "server" / "main.py")

# Routes deliberately left out of the endpoint tables. Both are named in the
# README's public-paths sentence, so they aren't hidden — they just don't earn
# a table row. Anything else missing is drift.
# Routes deliberately left out of docs/api-reference.md. Currently empty — the
# reference covers every route, including the public probes and SSE streams that
# have no MCP tool. Kept so a future intentional omission has a documented home
# rather than being silently dropped from the check.
UNDOCUMENTED_ROUTES_ALLOWLIST: set[tuple[str, str]] = set()

# CLI subcommands intentionally absent from the README command block.
UNDOCUMENTED_CLI_ALLOWLIST = {
    "--version",  # flag spelling of `version`, which is listed
    "-V",         # ditto
}


def _normalize_path(path: str) -> str:
    """Collapse path params and trailing slashes so `/x/{id}/` == `/x/{}`."""
    path = re.sub(r"\{[^}]*\}", "{}", path)
    return path.rstrip("/") or "/"


# --------------------------------------------------------------------------
# Source of truth: the code
# --------------------------------------------------------------------------


def registered_tools() -> set[str]:
    """Every tool name passed to server.registerTool() in mcp/src/tools/."""
    names: set[str] = set()
    for ts_file in sorted(MCP_TOOLS_DIR.glob("*.ts")):
        names.update(
            re.findall(
                r'server\.registerTool\(\s*"([a-z_0-9]+)"',
                ts_file.read_text(),
            )
        )
    return names


def registered_routes() -> set[tuple[str, str]]:
    """Every (METHOD, full_path) served by a FastAPI router or the app."""
    routes: set[tuple[str, str]] = set()
    for py_file in sorted(SERVER_DIR.rglob("*.py")):
        text = py_file.read_text()
        prefix_match = re.search(r'APIRouter\(\s*prefix="([^"]*)"', text)
        prefix = prefix_match.group(1) if prefix_match else ""
        for method, path in re.findall(
            r'@(?:app|router)\.(get|post|put|delete|patch)\("([^"]*)"', text
        ):
            routes.add((method.upper(), _normalize_path(prefix + path)))
    return routes


# --------------------------------------------------------------------------
# What the README claims
# --------------------------------------------------------------------------


def _readme_section(start_heading: str, end_heading: str) -> str:
    text = README.read_text()
    start = text.index(start_heading)
    end = text.index(end_heading, start)
    return text[start:end]


def documented_tools() -> set[str]:
    """Tool names in the second column of the MCP Tools table."""
    section = _readme_section("## MCP Tools", "## API Endpoints")
    names: set[str] = set()
    for line in section.splitlines():
        if not line.startswith("|") or line.startswith("|--"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) != 2 or cells[0] == "Category":
            continue
        names.update(re.findall(r"`([a-z_0-9]+)`", cells[1]))
    return names


def documented_tool_count() -> int:
    """The count asserted in the prose above the table ('107 tools ...')."""
    section = _readme_section("## MCP Tools", "## API Endpoints")
    match = re.search(r"\b(\d+) tools available via MCP\b", section)
    assert match, "Could not find the 'N tools available via MCP' claim in README.md"
    return int(match.group(1))


def documented_routes() -> set[tuple[str, str]]:
    """(METHOD, path) pairs from docs/api-reference.md.

    Two table shapes live there: tool rows (`tool | method | path | desc`) and
    tool-less rows (`method | path | desc`), so the method column is not always
    in the same position.
    """
    text = API_REFERENCE.read_text()
    routes: set[tuple[str, str]] = set()
    for line in text.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        for i, cell in enumerate(cells[:-1]):
            if cell in {"GET", "POST", "PUT", "DELETE", "PATCH"}:
                path = re.match(r"`([^`]+)`", cells[i + 1])
                if path:
                    routes.add((cell, _normalize_path(path.group(1))))
                break
    return routes


def api_reference_tools() -> set[str]:
    """Tool names appearing in the first column of api-reference.md tables."""
    names: set[str] = set()
    for line in API_REFERENCE.read_text().splitlines():
        if line.startswith("|"):
            first = line.strip("|").split("|")[0].strip()
            match = re.fullmatch(r"`([a-z_0-9]+)`", first)
            if match:
                names.add(match.group(1))
    return names


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------


def _format(items: set) -> str:
    return "\n".join(f"  - {i}" for i in sorted(map(str, items)))


def test_readme_tool_table_matches_registered_tools():
    actual = registered_tools()
    documented = documented_tools()

    missing = actual - documented
    stale = documented - actual

    problems = []
    if missing:
        problems.append(
            f"{len(missing)} tool(s) registered in mcp/src/tools/ but absent "
            f"from the README MCP Tools table:\n{_format(missing)}"
        )
    if stale:
        problems.append(
            f"{len(stale)} tool(s) listed in the README MCP Tools table but no "
            f"longer registered in mcp/src/tools/:\n{_format(stale)}"
        )
    assert not problems, "\n\n".join(problems)


def test_readme_tool_count_matches_table():
    claimed = documented_tool_count()
    actual = len(registered_tools())
    assert claimed == actual, (
        f"README says '{claimed} tools available via MCP' but "
        f"{actual} tools are registered in mcp/src/tools/. "
        f"Update the sentence above the MCP Tools table."
    )


def test_readme_endpoint_tables_match_registered_routes():
    actual = registered_routes()
    documented = documented_routes()

    missing = actual - documented - UNDOCUMENTED_ROUTES_ALLOWLIST
    stale = documented - actual

    problems = []
    if missing:
        problems.append(
            f"{len(missing)} route(s) served by the API but absent from "
            f"docs/api-reference.md:\n{_format(missing)}\n"
            f"Add a table row, or add the route to "
            f"UNDOCUMENTED_ROUTES_ALLOWLIST with a reason."
        )
    if stale:
        problems.append(
            f"{len(stale)} route(s) documented in docs/api-reference.md "
            f"but not served by the API:\n{_format(stale)}"
        )
    assert not problems, "\n\n".join(problems)


def test_undocumented_route_allowlist_is_not_stale():
    """The allowlist should never outlive the routes it excuses."""
    actual = registered_routes()
    orphans = UNDOCUMENTED_ROUTES_ALLOWLIST - actual
    assert not orphans, (
        f"UNDOCUMENTED_ROUTES_ALLOWLIST excuses route(s) that no longer "
        f"exist:\n{_format(orphans)}\nRemove them from the allowlist."
    )


def test_api_reference_covers_every_registered_tool():
    """Every MCP tool needs a row in the reference agents read over MCP."""
    actual = registered_tools()
    documented = api_reference_tools()

    missing = actual - documented
    stale = documented - actual

    problems = []
    if missing:
        problems.append(
            f"{len(missing)} tool(s) registered but absent from "
            f"docs/api-reference.md:\n{_format(missing)}"
        )
    if stale:
        problems.append(
            f"{len(stale)} tool(s) in docs/api-reference.md that are no longer "
            f"registered:\n{_format(stale)}"
        )
    assert not problems, "\n\n".join(problems)


@pytest.mark.parametrize("path", [README, API_REFERENCE, MCP_TOOLS_DIR, SERVER_DIR])
def test_documentation_sources_exist(path: Path):
    """Fail loudly rather than silently passing on an empty scan."""
    assert path.exists(), f"{path} is missing — these guard tests cannot run."


def cli_commands() -> set[str]:
    """Every subcommand `quern` dispatches.

    Two mechanisms, and missing the second is how `set-channel` and
    `install-precommit-hook` stayed invisible: argparse subparsers (in
    main.py) show up in --help, but several commands are intercepted by hand
    off sys.argv in __main__.py before argparse ever runs, so --help alone
    under-reports the real surface.
    """
    names: set[str] = set()
    for source in CLI_SOURCES:
        text = source.read_text()
        names.update(re.findall(r'add_parser\(\s*"([a-z0-9-]+)"', text))
        for match in re.finditer(
            r'sys\.argv\[1\]\s*(?:==|in)\s*(\([^)]*\)|"[a-z0-9_-]+")', text
        ):
            names.update(re.findall(r'"([a-z0-9_-]+)"', match.group(1)))
    return names - {"-h", "--help"}


def documented_cli_commands() -> set[str]:
    """Commands listed in the README's `quern ...` shell block."""
    section = _readme_section("### Process Lifecycle", "## MCP Tools")
    return set(re.findall(r"^quern ([a-z0-9-]+)", section, re.MULTILINE))


def test_readme_cli_block_matches_dispatched_commands():
    actual = cli_commands()
    documented = documented_cli_commands()

    missing = actual - documented - UNDOCUMENTED_CLI_ALLOWLIST
    stale = documented - actual

    problems = []
    if missing:
        problems.append(
            f"{len(missing)} CLI command(s) dispatched by server/main.py or "
            f"server/__main__.py but "
            f"absent from the README command block:\n{_format(missing)}"
        )
    if stale:
        problems.append(
            f"{len(stale)} command(s) listed in the README command block that "
            f"`quern` does not dispatch:\n{_format(stale)}"
        )
    assert not problems, "\n\n".join(problems)


# --------------------------------------------------------------------------
# CLI flags
# --------------------------------------------------------------------------
#
# `cli_commands()` extracts subcommands only, so a flag added to an existing
# command was invisible to every guard in this file. `quern update --tools` and
# `quern doctor --fix` both shipped in 0.15.0-beta.1 documented by hand, and
# nothing would have noticed if they had not been.
#
# The bar is the same as for routes: a new flag is documented, or it is
# allowlisted on purpose. It is not silently absent.

UNDOCUMENTED_CLI_FLAG_ALLOWLIST = {
    # `quern start` tuning knobs. README documents the commands and the flags
    # that change what a command *does*; these adjust how the server runs and
    # are discoverable through `--help`, which is where they belong.
    "--foreground", "--host", "--port", "--proxy-port", "--buffer-size",
    "--verbose", "--process", "--subsystem",
    "--oslog", "--no-oslog", "--syslog", "--no-syslog",
    "--no-proxy",
    "--crash-dir", "--crash-process-filter", "--simulator-crashes",
    "--no-crash", "--on-crash",
    # Generated by BooleanOptionalAction, not written anywhere.
    "--no-simulator-crashes",
}


def cli_flags() -> set[str]:
    """Every flag the CLI accepts, from all three mechanisms.

    Parsed with `ast` rather than a regex: `add_argument` calls span multiple
    lines, and the keyword that matters -- `action=BooleanOptionalAction` -- sits
    on a different line from the flag name in the one place it is used.

    `argparse.BooleanOptionalAction` silently defines a second spelling:
    `--simulator-crashes` also accepts `--no-simulator-crashes`. Extracting only
    the written spelling left the generated one outside every guard here.

    `--tools` is read straight off `sys.argv` before argparse runs -- the same
    split that let `set-channel` hide from `cli_commands()`.
    """
    flags: set[str] = set()
    for source in CLI_SOURCES:
        text = source.read_text()
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr == "add_argument"):
                continue
            names = [a.value for a in node.args
                     if isinstance(a, ast.Constant) and isinstance(a.value, str)
                     and a.value.startswith("--")]
            if not names:
                continue
            flags.update(names)
            boolean_optional = any(
                kw.arg == "action" and ast.unparse(kw.value).endswith("BooleanOptionalAction")
                for kw in node.keywords
            )
            if boolean_optional:
                # argparse generates the negative spelling; nothing writes it down.
                flags.update(f"--no-{n[2:]}" for n in names)
        flags.update(re.findall(r'"(--[a-z0-9-]+)"\s*in\s*sys\.argv', text))
        flags.update(re.findall(r'sys\.argv\[\d+\]\s*==\s*"(--[a-z0-9-]+)"', text))
    return flags - {"--help"}


def flags_in_prose(text: str) -> set[str]:
    """Every flag attached to a `quern <command>` example in `text`.

    Split out from `documented_cli_flags` so it can be exercised on strings.
    Its one interesting behaviour -- taking *all* flags on a line rather than
    the first -- cannot be tested through the guards: truncating makes an
    undocumented flag invisible rather than failing, so the bug hides itself.
    """
    found: set[str] = set()
    for line in re.findall(r"^quern [a-z-]+ .*$", text, re.M):
        found.update(re.findall(r"(--[a-z0-9-]+)", line))
    for fragment in re.findall(r"`quern [a-z-]+ [^`]*`", text):
        found.update(re.findall(r"(--[a-z0-9-]+)", fragment))
    return found


def documented_cli_flags() -> set[str]:
    """Every flag README shows attached to a command."""
    return flags_in_prose(README.read_text())


def test_every_flag_on_a_line_is_read_not_just_the_first():
    """`quern start --host H --port P` documents two flags."""
    assert flags_in_prose("quern start --host 127.0.0.1 --port 9100") == {"--host", "--port"}


def test_flags_are_read_from_inline_code_too():
    assert flags_in_prose("run `quern doctor --fix --dry-run` first") == {"--fix", "--dry-run"}


def test_prose_without_a_command_contributes_nothing():
    """A bare `--fix` in a sentence is not a documented flag of any command."""
    assert flags_in_prose("pass --fix when you mean it") == set()


def test_new_cli_flags_are_documented_or_allowlisted():
    undocumented = cli_flags() - documented_cli_flags() - UNDOCUMENTED_CLI_FLAG_ALLOWLIST
    assert not undocumented, (
        f"{len(undocumented)} CLI flag(s) neither documented in README.md nor "
        f"allowlisted:\n{_format(sorted(undocumented))}\n"
        f"Document it, or add it to UNDOCUMENTED_CLI_FLAG_ALLOWLIST with a reason."
    )


def test_the_cli_flag_allowlist_is_not_stale():
    """The allowlist should never outlive the flags it excuses."""
    orphans = UNDOCUMENTED_CLI_FLAG_ALLOWLIST - cli_flags()
    assert not orphans, (
        f"UNDOCUMENTED_CLI_FLAG_ALLOWLIST excuses flag(s) the CLI no longer "
        f"accepts:\n{_format(sorted(orphans))}"
    )


def test_documented_flags_are_really_accepted():
    """Catches the other direction: a flag README promises but the CLI dropped."""
    phantom = documented_cli_flags() - cli_flags()
    assert not phantom, (
        f"README.md documents flag(s) the CLI does not accept:\n"
        f"{_format(sorted(phantom))}"
    )


def test_help_output_lists_every_dispatched_command():
    """`quern --help` must name every command the CLI dispatches.

    Commands handled in `server/__main__.py` never reach argparse, so the
    parser does not know they exist and cannot list them. Six were missing,
    including `update` -- one of the most used commands in the tool. Nothing
    caught it, because the README check compares the README against dispatch
    and never looks at the help output.
    """
    import io as _io
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "-m", "server", "--help"],
        capture_output=True, text=True, timeout=60,
        cwd=str(REPO_ROOT),
    )
    help_text = proc.stdout + proc.stderr
    # A command must be *listed*, which means starting its own line in the
    # help. Substring containment passed because `update` occurs inside
    # set-channel's description; word boundaries passed for the same reason.
    # Only the listing column actually answers "can a user find this".
    listed = {
        m.group(1)
        for m in re.finditer(r"^\s+([a-z][a-z0-9-]*)(?:[,\s]|$)", help_text, re.M)
    }
    missing = [c for c in cli_commands() if c.lstrip("-") not in listed]
    assert not missing, (
        f"{len(missing)} command(s) the CLI dispatches are absent from "
        f"`quern --help`:\n  - " + "\n  - ".join(sorted(missing))
    )
    assert _io


def test_help_is_a_command_not_only_a_flag():
    """`quern help` is what people type. It used to exit 2 with an
    "invalid choice" error, because argparse only understands -h/--help."""
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "-m", "server", "help"],
        capture_output=True, text=True, timeout=60,
        cwd=str(REPO_ROOT),
    )
    assert proc.returncode == 0, f"`quern help` exited {proc.returncode}"
    assert "usage: quern" in (proc.stdout + proc.stderr), "usage line names the wrong program"


# --------------------------------------------------------------------------
# The MCP surface has to keep up with the endpoints it posts to
# --------------------------------------------------------------------------
#
# Not a documentation check like the rest of this file, but the same failure:
# a surface that falls behind the code with nothing to notice. The MCP tools
# are the one surface with no compiler and no caller to complain, and
# `skip_cert_check` fell behind there for a whole release cycle.

CAPTURE_GATE = "_ensure_ca_is_trusted"

# HTTP methods that can carry a body worth checking.
_ROUTE_METHODS = {"get", "post", "put", "delete", "patch"}


def _unwrap_annotation(ann: ast.AST | None) -> str | None:
    """The bare model name behind `X`, `X | None`, `Optional[X]`, `Annotated[X, ...]`.

    Every one of these spellings appears or could appear on a handler, and a
    check that understands only the first two reports "takes no typed body"
    about a handler that plainly has one -- which sends the reader looking for
    a bug in their own code.
    """
    seen = 0
    while ann is not None and seen < 10:
        seen += 1
        if isinstance(ann, ast.Name):
            return ann.id
        if isinstance(ann, ast.BinOp):  # X | None
            ann = ann.left
        elif isinstance(ann, ast.Subscript):  # Optional[X], Annotated[X, ...]
            base = getattr(ann.value, "id", None) or getattr(ann.value, "attr", None)
            if base not in {"Optional", "Annotated"}:
                return None
            ann = ann.slice.elts[0] if isinstance(ann.slice, ast.Tuple) else ann.slice
        else:
            return None
    return None


def _calls(node: ast.AST, name: str) -> bool:
    """Does this function call `name`, as a bare name or an attribute?

    A handler in another module reaches the gate as `proxy._ensure_ca_is_trusted`,
    which is an `ast.Attribute` -- invisible to a check that only reads
    `func.id`, and exactly the shape a future gated path would take.
    """
    for n in ast.walk(node):
        if not isinstance(n, ast.Call):
            continue
        func = n.func
        if isinstance(func, ast.Name) and func.id == name:
            return True
        if isinstance(func, ast.Attribute) and func.attr == name:
            return True
    return False


def _decorator_path(dec: ast.AST) -> str | None:
    """The route path off `@router.post("/x")` or `@router.post(path="/x")`."""
    if not isinstance(dec, ast.Call):
        return None
    if not (isinstance(dec.func, ast.Attribute) and dec.func.attr in _ROUTE_METHODS):
        return None
    if dec.args:
        value = getattr(dec.args[0], "value", None)
        if isinstance(value, str):
            return value
    for kw in dec.keywords:
        if kw.arg == "path":
            value = getattr(kw.value, "value", None)
            if isinstance(value, str):
                return value
    return None


def gate_guarded_routes() -> dict[str, str | None]:
    """{full path: body model name} for every handler that calls the gate.

    Walks all of `server/`, not one file: `proxy_certs.py` and
    `proxy_intercept.py` already mount under the same `/api/v1/proxy` prefix,
    so a cert-adjacent capture path landing in one of them is the realistic
    future case rather than a hypothetical one.

    Asserts its own floor rather than leaving that to a sibling test. A parse
    that silently finds nothing makes every caller trivially true, and a guard
    living in a separate test protects the suite without protecting the
    assertions that actually range over this.
    """
    routes: dict[str, str | None] = {}
    for py_file in sorted(SERVER_DIR.rglob("*.py")):
        text = py_file.read_text()
        if CAPTURE_GATE not in text:
            continue
        prefix_match = re.search(r'APIRouter\(\s*prefix="([^"]*)"', text)
        prefix = prefix_match.group(1) if prefix_match else ""
        for node in ast.walk(ast.parse(text)):
            if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
                continue
            if not _calls(node, CAPTURE_GATE):
                continue
            for dec in node.decorator_list:
                path = _decorator_path(dec)
                if path is None:
                    continue
                # Found by type, not by the parameter being spelled `body`.
                # Keying on the name reports "takes no typed body" about a
                # handler that plainly has one, which is a worse failure than
                # not checking it -- it describes the reader's code back to
                # them incorrectly.
                model = None
                for arg in [*node.args.args, *node.args.kwonlyargs]:
                    candidate = _unwrap_annotation(arg.annotation)
                    if candidate and candidate != "Request" and candidate[0].isupper():
                        model = candidate
                routes[_normalize_path(prefix + path)] = model

    if len(routes) < 2:
        raise AssertionError(
            f"expected the shared capture gate on at least the two known paths, "
            f"found {routes or 'nothing'}. If {CAPTURE_GATE} was renamed, rename "
            f"it here too; if the parse broke, every check below went vacuous."
        )
    return routes


def _skip_strings(src: str, i: int) -> int:
    """Index just past a string literal starting at `i`, else `i`."""
    quote = src[i]
    if quote not in "\"'`":
        return i
    j = i + 1
    while j < len(src):
        if src[j] == "\\":
            j += 2
            continue
        if src[j] == quote:
            return j + 1
        j += 1
    return j


def _balanced(src: str, open_idx: int) -> int:
    """Index of the `)` matching the `(` at `open_idx`, ignoring strings.

    Parens inside a `.describe()` string are common ("(default: false)"), so a
    naive counter miscounts on prose. Strings are skipped wholesale.
    """
    depth = 0
    i = open_idx
    while i < len(src):
        c = src[i]
        if c in "\"'`":
            i = _skip_strings(src, i)
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def tool_registrations() -> dict[str, tuple[str, str]]:
    """{tool name: (its inputSchema source, everything after it)}.

    The schema is delimited by matching parentheses rather than by a `}, async`
    literal. Three tool files use the fully expanded handler form, which
    contains no such literal -- against those, a literal split silently returns
    the whole remainder and a parameter mentioned anywhere in the handler body
    reads as a declared one.
    """
    out: dict[str, tuple[str, str]] = {}
    for ts_file in sorted(MCP_TOOLS_DIR.glob("*.ts")):
        text = ts_file.read_text()
        for chunk in text.split("server.registerTool(")[1:]:
            name_match = re.match(r'\s*"([a-z_0-9]+)"', chunk)
            if not name_match:
                continue
            key = chunk.find("inputSchema:")
            if key == -1:
                out[name_match.group(1)] = ("", chunk)
                continue
            open_paren = chunk.find("(", key)
            close = _balanced(chunk, open_paren) if open_paren != -1 else -1
            if close == -1:
                out[name_match.group(1)] = ("", chunk)
                continue
            out[name_match.group(1)] = (chunk[key:close], chunk[close:])
    return out


def tools_posting_to(path: str) -> dict[str, tuple[str, str]]:
    return {
        name: parts
        for name, parts in tool_registrations().items()
        if f'"{path}"' in parts[0] + parts[1]
    }


def test_the_capture_gate_is_found_on_both_known_paths():
    """The floor, asserted where a reader will look for it. `gate_guarded_routes`
    raises on its own, so this is a named restatement rather than the guard."""
    routes = gate_guarded_routes()
    assert "/api/v1/proxy/local-capture" in routes
    assert "/api/v1/proxy/configure-system" in routes


def test_the_schema_parse_finds_a_real_schema_for_every_tool():
    """Guards the TS half. A parse returning empty schemas would make the
    parameter check below pass for every tool without reading anything."""
    empty = [
        name for name, (schema, _) in tool_registrations().items()
        if "inputSchema" not in schema
    ]
    assert not empty, f"could not locate an inputSchema for: {_format(set(empty))}"


def test_every_gated_endpoint_accepts_the_skip():
    """The HTTP half: the body model behind each gated path has the field the
    refusal names."""
    import server.models as models

    for path, model_name in gate_guarded_routes().items():
        assert model_name, (
            f"{path} calls the capture gate but takes no typed body, so there "
            f"is nowhere for skip_cert_check to live"
        )
        model = getattr(models, model_name)
        assert "skip_cert_check" in model.model_fields, (
            f"{path} refuses with 428 naming skip_cert_check, but {model_name} "
            f"has no such field"
        )


def test_every_gated_endpoint_offers_the_skip_to_agents():
    """The half that shipped broken. A tool posting to a gated path must both
    declare `skip_cert_check` and forward it.

    Declaring without forwarding is the worse of the two failures: validation
    accepts the field, the request omits it, the same 428 comes back, and the
    agent has done exactly what the error told it to.
    """
    for path in gate_guarded_routes():
        posting = tools_posting_to(path)
        assert posting, f"no MCP tool posts to {path}"
        for name, (schema, handler) in posting.items():
            assert "skip_cert_check" in schema, (
                f"{name} posts to {path}, which refuses with 428 and tells the "
                f"caller to pass skip_cert_check -- but the tool's schema is "
                f"strict and does not accept it"
            )
            # Past the arrow, so the destructuring pattern does not count as
            # forwarding. `async ({ skip_cert_check: skipCertCheck }) => {`
            # mentions the field while doing nothing with it, and deleting the
            # line that puts it in the body leaves that mention behind.
            arrow = handler.split("=> {", 1)
            assert len(arrow) == 2, f"{name}: cannot find the handler body"
            assert "skip_cert_check" in arrow[1], (
                f"{name} declares skip_cert_check but never puts it in the "
                f"request body, so passing it changes nothing -- the same 428 "
                f"comes back after the agent did exactly what it was told"
            )
