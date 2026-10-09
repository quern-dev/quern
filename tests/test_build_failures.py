"""A failed build names what failed, even when nothing failed to compile.

`build_and_install` reported a production app's device build as "Build failed.
iphoneos: 0 error(s)." (2026-09-28). The cause was a SwiftLint build plug-in
xcodebuild would not run unapproved, and xcodebuild says so only as a failed
step, not an `error:` line. Signing and provisioning failures are `error:`
lines with no file:line:col, which the parser also skipped. The output below
is the real one, from Xcode 26.5, with the app's names replaced.
"""

from __future__ import annotations

import pytest

from server.sources.build import BuildAdapter

PLUGIN_REFUSED = """\
Prepare packages

Validate plug-in “SwiftLintBuildToolPlugin” in package “swiftlintplugins”

** BUILD FAILED **


The following build commands failed:
\tValidate plug-in “SwiftLintBuildToolPlugin” in package “swiftlintplugins”
\teyJ0eXBlIjp7IndvcmtzcGFjZSI6e319fQ==
\tBuilding workspace MyApp with scheme MyApp and configuration Debug
(3 failures)
"""

SIGNING = """\
error: Signing for "MyApp" requires a development team. Select a development team in the \
Signing & Capabilities editor. (in target 'MyApp' from project 'MyApp')
** BUILD FAILED **

error: Signing for "MyApp" requires a development team. Select a development team in the \
Signing & Capabilities editor. (in target 'MyApp' from project 'MyApp')

The following build commands failed:
\tBuilding project MyApp with scheme MyApp and configuration Debug
(1 failure)
"""

# Real, from the same build once the plug-in was let through: this Mac had no
# signing identity. Written against the project file, with no line.
NO_CERTIFICATE = """\
/Volumes/src/MyApp.xcodeproj: error: No signing certificate "iOS Development" found: No \
"iOS Development" signing certificate matching team ID "ABCDE12345" with a private key was \
found. (in target 'MyApp' from project 'MyApp')
note: Run script build phase 'Run Script (SwiftLint)' will be run during every build
** BUILD FAILED **


The following build commands failed:
\tBuilding workspace MyApp with scheme MyApp and configuration Debug
(1 failure)
"""

# Real shape, from a production app's simulator build on a stale DerivedData: the
# only errors it printed had no column.
STALE_MODULE = """\
<unknown>:0: error: file '/dd/Products/VendorSDK.framework/Headers/VendorSDK.h' has been \
modified since the module file '/dd/SwiftExplicitPrecompiledModules/VendorSDK.pcm' was built
<unknown>:0: error: file '/dd/Products/VendorSDK.framework/Headers/VendorSDK.h' has been \
modified since the module file '/dd/SwiftExplicitPrecompiledModules/VendorSDK.pcm' was built
** BUILD FAILED **

The following build commands failed:
\tSwiftCompile normal x86_64 Compiling\\ A.swift,\\ B.swift """ + "/src/X.swift " * 40 + """
\tBuilding workspace MyApp with scheme MyApp and configuration Debug
(2 failures)
"""

NOTHING_READABLE = """\
** BUILD FAILED **


The following build commands failed:
\tBuilding workspace MyApp with scheme MyApp and configuration Debug
(1 failure)
"""

COMPILE = """\
/src/App/Feed.swift:12:5: error: cannot find 'x' in scope
** BUILD FAILED **

The following build commands failed:
\tSwiftCompile normal arm64 /src/App/Feed.swift (in target 'App' from project 'App')
\tBuilding project App with scheme App and configuration Debug
(2 failures)
"""


async def _parse(text):
    return await BuildAdapter().parse_build_output(text)


class TestWhatFailed:
    async def test_a_refused_plug_in_is_named_with_the_way_past_it(self):
        result = await _parse(PLUGIN_REFUSED)
        assert not result.succeeded
        [error] = result.errors
        assert error.message.startswith(
            "Validate plug-in “SwiftLintBuildToolPlugin” in package “swiftlintplugins” failed")
        assert "skip_plugin_validation=true" in error.message
        assert "approve it" in error.message

    async def test_the_noise_in_the_step_list_is_left_out(self):
        """A base64 token under the plug-in step; the whole-build step."""
        result = await _parse(PLUGIN_REFUSED)
        assert not any("eyJ0" in e.message or "Building workspace" in e.message
                       for e in result.errors)

    async def test_a_signing_error_without_a_location_is_read_once(self):
        result = await _parse(SIGNING)
        assert [e.message for e in result.errors] == [
            "Signing for \"MyApp\" requires a development team. Select a development team in "
            "the Signing & Capabilities editor. (in target 'MyApp' from project 'MyApp')"]
        assert result.errors[0].file == ""

    async def test_an_error_against_the_project_file_keeps_it_as_the_file(self):
        result = await _parse(NO_CERTIFICATE)
        [error] = result.errors
        assert error.file == "/Volumes/src/MyApp.xcodeproj"
        assert error.message.startswith('No signing certificate "iOS Development" found')
        assert error.line is None

    async def test_the_compilers_placeless_form_is_read_once(self):
        result = await _parse(STALE_MODULE)
        [error] = result.errors
        assert error.file == "" and "has been modified since the module file" in error.message

    async def test_a_failed_step_is_cut_short(self):
        text = STALE_MODULE.replace("<unknown>:0: error:", "<unknown>:0: note:")
        [error] = (await _parse(text)).errors
        assert error.message.startswith("SwiftCompile normal x86_64")
        assert len(error.message) < 240 and "…" in error.message

    async def test_a_failure_with_nothing_readable_still_says_it_failed(self):
        """Never "0 errors" for a failed build."""
        result = await _parse(NOTHING_READABLE)
        [error] = result.errors
        assert "printed no error quern could read" in error.message

    async def test_compile_errors_are_not_repeated_as_steps(self):
        result = await _parse(COMPILE)
        assert [e.message for e in result.errors] == ["cannot find 'x' in scope"]

    async def test_a_build_that_succeeded_gets_nothing(self):
        result = await _parse("** BUILD SUCCEEDED **\n")
        assert result.succeeded and result.errors == []


class TestHowItReads:
    async def test_the_log_line_has_no_empty_location(self):
        entries = []

        async def collect(entry):
            entries.append(entry)

        await BuildAdapter(on_entry=collect).parse_build_output(SIGNING)
        assert entries and not entries[0].message.startswith(":")
        assert ":None" not in entries[0].message

    async def test_an_error_against_a_file_logs_the_file_and_no_line(self):
        entries = []

        async def collect(entry):
            entries.append(entry)

        await BuildAdapter(on_entry=collect).parse_build_output(NO_CERTIFICATE)
        [entry] = entries
        assert entry.message.startswith(
            '/Volumes/src/MyApp.xcodeproj: No signing certificate "iOS Development"')
        assert ":None" not in entry.message

    @pytest.mark.parametrize("text", [PLUGIN_REFUSED, SIGNING])
    async def test_the_summary_has_no_empty_location(self, text):
        from server.api.build_app import BuildAndInstallResponse, _build_install_summary

        result = await _parse(text)
        summary = _build_install_summary(
            BuildAndInstallResponse(build_iphoneos=result, devices=[], all_installed=False))
        assert "1 error(s)" in summary and "  : " not in summary
        assert result.generate_summary().count("  : ") == 0


# -- package resolution (#442) -------------------------------------------------------
# The reason is on the indented lines under the error, and the result ended
# at the colon. Real output from Xcode 26.5, three causes, paths shortened.

PACKAGE_REPO_MISSING = """\
Command line invocation:
    /Applications/Xcode.app/Contents/Developer/usr/bin/xcodebuild \
-workspace W.xcworkspace -scheme Pkg build

Resolve Package Graph

skipping cache due to an error: Failed to clone repository https://github.com/example/no-such-package.git:
    Cloning into bare repository \
'/Users/me/Library/Caches/org.swift.swiftpm/repositories/no-such-package-3995d913'...
    remote: Repository not found.
    fatal: repository 'https://github.com/example/no-such-package.git/' not found

Resolved source packages:
  Pkg: (null)

2026-10-08 22:46:56.850 xcodebuild[58359:227958050] Writing error result bundle to \
/var/folders/T/ResultBundle.xcresult
xcodebuild: error: Could not resolve package dependencies:
  Failed to clone repository https://github.com/example/no-such-package.git:
    Cloning into bare repository '/src/dd/SourcePackages/repositories/no-such-package-3995d913'...
    remote: Repository not found.
    fatal: repository 'https://github.com/example/no-such-package.git/' not found

"""

PACKAGE_PATH_MISSING = """\
xcodebuild: error: Could not resolve package dependencies:
  the package at '/src/Missing/NoSuchDir' cannot be accessed \
(/src/Missing/NoSuchDir doesn't exist in file system)

"""

PACKAGE_VERSION_UNSATISFIABLE = """\
xcodebuild: error: Could not resolve package dependencies:
  Failed to resolve dependencies Dependencies could not be resolved because no versions of \
'swift-argument-parser' match the requirement 999.0.0..<1000.0.0 and root depends on \
'swift-argument-parser' 999.0.0..<1000.0.0.

"""


class TestPackageResolution:
    @pytest.mark.parametrize("text,why", [
        (PACKAGE_REPO_MISSING, "remote: Repository not found."),
        (PACKAGE_PATH_MISSING, "doesn't exist in file system"),
        (PACKAGE_VERSION_UNSATISFIABLE, "no versions of 'swift-argument-parser' match"),
    ])
    async def test_the_reason_is_kept(self, text, why):
        result = await _parse(text)
        assert not result.succeeded
        [error] = result.errors
        assert error.message.startswith("Could not resolve package dependencies:\n")
        assert why in error.message

    async def test_the_reason_stops_at_the_blank_line(self):
        [error] = (await _parse(PACKAGE_REPO_MISSING)).errors
        assert error.message.splitlines()[-1].strip().startswith("fatal: repository")

    async def test_the_reason_reaches_the_summary(self):
        from server.api.build_app import BuildAndInstallResponse, _build_install_summary

        result = await _parse(PACKAGE_PATH_MISSING)
        summary = _build_install_summary(
            BuildAndInstallResponse(build_iphonesimulator=result, devices=[], all_installed=False))
        assert "doesn't exist in file system" in summary

    async def test_an_error_not_ending_in_a_colon_takes_no_lines_below_it(self):
        """Under any other error an indented line is the next command's, not
        part of the message."""
        text = ('error: Signing for "MyApp" requires a development team.\n'
                "    cd /src/MyApp\n"
                "** BUILD FAILED **\n")
        [error] = (await _parse(text)).errors
        assert error.message == 'Signing for "MyApp" requires a development team.'

    async def test_a_long_reason_is_cut_and_says_so(self):
        lines = "".join(f"    remote: line {n}\n" for n in range(40))
        text = f"xcodebuild: error: Could not resolve package dependencies:\n{lines}\n"
        [error] = (await _parse(text)).errors
        kept = error.message.splitlines()
        assert len(kept) == 1 + 12 + 1
        assert kept[-1].strip() == "… 28 more line(s)"

    async def test_the_same_block_twice_is_one_error(self):
        [error] = (await _parse(PACKAGE_PATH_MISSING + PACKAGE_PATH_MISSING)).errors
        assert "cannot be accessed" in error.message


# -- the exit code (#442) ----------------------------------------------------------


class TestExitCode:
    async def test_a_non_zero_exit_fails_a_build_whose_output_said_nothing(self):
        result = await BuildAdapter().parse_build_output("Resolve Package Graph\n", exit_code=74)
        assert not result.succeeded
        [error] = result.errors
        assert error.message.startswith("xcodebuild exited 74; ")

    async def test_a_non_zero_exit_overrides_a_success_line(self):
        result = await BuildAdapter().parse_build_output("** BUILD SUCCEEDED **\n", exit_code=65)
        assert not result.succeeded and result.errors

    @pytest.mark.parametrize("code", [0, None])
    async def test_a_clean_exit_changes_nothing(self, code):
        result = await BuildAdapter().parse_build_output("** BUILD SUCCEEDED **\n", exit_code=code)
        assert result.succeeded and result.errors == []

    async def test_the_build_step_passes_xcodebuilds_exit_code(self, monkeypatch, tmp_path):
        from server.api import build_app as route

        class Proc:
            returncode = 74

            async def communicate(self):
                return b"Resolve Package Graph\n", b""

        async def fake_exec(*_argv, **_kwargs):
            return Proc()

        monkeypatch.setattr(route.asyncio, "create_subprocess_exec", fake_exec)
        result = await route._build("-workspace", "/p/W.xcworkspace", "S", "Debug",
                                    "generic/platform=iOS Simulator", tmp_path, BuildAdapter())
        assert not result.succeeded
