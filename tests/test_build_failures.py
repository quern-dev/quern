"""A failed build names what failed, even when nothing failed to compile.

`build_and_install` reported Geocaching's device build as "Build failed.
iphoneos: 0 error(s)." (2026-09-28). The cause was a SwiftLint build plug-in
xcodebuild would not run unapproved, and xcodebuild says so only as a failed
step, not an `error:` line. Signing and provisioning failures are `error:`
lines with no file:line:col, which the parser also skipped. The output below
is the real one, from Xcode 26.5.
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
\tBuilding workspace Geocaching with scheme Internal and configuration Debug
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
/Volumes/src/Geocaching.xcodeproj: error: No signing certificate "iOS Development" found: No \
"iOS Development" signing certificate matching team ID "8SSDY9WB6W" with a private key was \
found. (in target 'Geocaching' from project 'Geocaching')
note: Run script build phase 'Run Script (SwiftLint)' will be run during every build
** BUILD FAILED **


The following build commands failed:
\tBuilding workspace Geocaching with scheme Internal and configuration Debug
(1 failure)
"""

# Real shape, from a Geocaching simulator build on a stale DerivedData: the
# only errors it printed had no column.
STALE_MODULE = """\
<unknown>:0: error: file '/dd/Products/AppsFlyerLib.framework/Headers/AppsFlyerLib.h' has been \
modified since the module file '/dd/SwiftExplicitPrecompiledModules/AppsFlyerLib.pcm' was built
<unknown>:0: error: file '/dd/Products/AppsFlyerLib.framework/Headers/AppsFlyerLib.h' has been \
modified since the module file '/dd/SwiftExplicitPrecompiledModules/AppsFlyerLib.pcm' was built
** BUILD FAILED **

The following build commands failed:
\tSwiftCompile normal x86_64 Compiling\\ A.swift,\\ B.swift """ + "/src/X.swift " * 40 + """
\tBuilding workspace Geocaching with scheme Internal and configuration Debug
(2 failures)
"""

NOTHING_READABLE = """\
** BUILD FAILED **


The following build commands failed:
\tBuilding workspace Geocaching with scheme Internal and configuration Debug
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
        assert error.file == "/Volumes/src/Geocaching.xcodeproj"
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

    @pytest.mark.parametrize("text", [PLUGIN_REFUSED, SIGNING])
    async def test_the_summary_has_no_empty_location(self, text):
        from server.api.build_app import BuildAndInstallResponse, _build_install_summary

        result = await _parse(text)
        summary = _build_install_summary(
            BuildAndInstallResponse(build_iphoneos=result, devices=[], all_installed=False))
        assert "1 error(s)" in summary and "  : " not in summary
        assert result.generate_summary().count("  : ") == 0
