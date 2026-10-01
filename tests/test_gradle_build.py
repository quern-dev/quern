"""build_and_install for Gradle projects (#347).

No Gradle, JDK or adb runs here: JDKs are directories with a `release` file,
projects are laid out by hand, Gradle's output is real output captured from
Gradle 9.5.1 (tests/fixtures/gradle, home paths anonymised), and the controller
and adb are fakes.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from server.api import build_android, build_app
from server.device import gradle
from server.device import jdk as jdk_mod

FIXTURES = Path(__file__).parent / "fixtures" / "gradle"


def _jdk_dir(root: Path, name: str, version: str) -> str:
    d = root / name
    d.mkdir(parents=True)
    (d / "release").write_text(f'JAVA_VERSION="{version}"\nOS_NAME="Darwin"\n')
    return str(d)


def _project(tmp_path: Path, *, daemon_jvm: int | None = None, gradle_v="9.5.1",
             props: str = "", local: str = "") -> Path:
    root = tmp_path / "proj"
    (root / "app").mkdir(parents=True)
    (root / "settings.gradle.kts").write_text('include(":app")\n')
    (root / "gradlew").write_text("#!/bin/sh\n")
    (root / "gradle" / "wrapper").mkdir(parents=True)
    (root / "gradle" / "wrapper" / "gradle-wrapper.properties").write_text(
        f"distributionUrl=https\\://services.gradle.org/distributions/gradle-{gradle_v}-bin.zip\n")
    if daemon_jvm:
        (root / "gradle" / "gradle-daemon-jvm.properties").write_text(
            f"toolchainVersion={daemon_jvm}\n")
    if props:
        (root / "gradle.properties").write_text(props)
    if local:
        (root / "local.properties").write_text(local)
    return root


# ── the JDK ─────────────────────────────────────────────────────────────────


class TestReadingAJdk:
    @pytest.mark.parametrize("version, major", [("17.0.20", 17), ("21", 21),
                                                ("1.8.0_212", 8), ("11.0.32.1", 11)])
    def test_the_major_version(self, tmp_path, version, major):
        jdk = jdk_mod.read_jdk(_jdk_dir(tmp_path, "j", version), "test")
        assert (jdk.version, jdk.major) == (version, major)

    def test_a_directory_without_a_release_file_is_not_a_jdk(self, tmp_path):
        (tmp_path / "j").mkdir()
        assert jdk_mod.read_jdk(str(tmp_path / "j"), "test") is None


class TestChoosingAJdk:
    def _choose(self, root, found, **kw):
        return jdk_mod.choose(root, env={"HOME": str(root.parent)}, home=str(root.parent),
                              found=found, **kw)

    def test_the_first_new_enough_one_wins(self, tmp_path):
        root = _project(tmp_path)
        old = jdk_mod.Jdk("/j11", "11.0.1", 11, "JAVA_HOME")
        new = jdk_mod.Jdk("/jbr", "21.0.10", 21, "Android Studio's bundled JDK")
        assert self._choose(root, [old, new]).jdk == new

    def test_none_new_enough_is_a_problem_naming_what_was_found(self, tmp_path):
        root = _project(tmp_path)
        c = self._choose(root, [jdk_mod.Jdk("/j11", "11.0.1", 11, "JAVA_HOME")])
        assert c.jdk is None and "no JDK 17 or later" in c.problem and "Java 11.0.1" in c.problem

    def test_org_gradle_java_home_is_authoritative(self, tmp_path):
        """Gradle runs on it whatever JAVA_HOME says, so it is validated, not
        overridden by a better one quern happens to know."""
        old = _jdk_dir(tmp_path, "j11", "11.0.1")
        root = _project(tmp_path, props=f"org.gradle.java.home={old}\n")
        c = self._choose(root, [jdk_mod.Jdk("/jbr", "21.0.10", 21, "Android Studio")])
        assert c.jdk is None and "org.gradle.java.home" in c.problem
        assert c.forced_by == "the project's gradle.properties"

    def test_a_good_org_gradle_java_home_is_used(self, tmp_path):
        good = _jdk_dir(tmp_path, "j21", "21.0.1")
        root = _project(tmp_path, props=f"org.gradle.java.home={good}\n")
        assert self._choose(root, []).jdk.home == good

    def test_an_explicit_java_home_wins_or_is_refused(self, tmp_path):
        root = _project(tmp_path)
        mine = jdk_mod.Jdk("/mine", "17.0.2", 17, "the java_home you passed")
        jbr = jdk_mod.Jdk("/jbr", "21.0.10", 21, "Android Studio")
        assert self._choose(root, [mine, jbr], java_home="/mine").jdk == mine
        old = jdk_mod.Jdk("/old", "11.0.1", 11, "the java_home you passed")
        c = self._choose(root, [old, jbr], java_home="/old")
        assert c.jdk is None and "the java_home you passed is Java 11.0.1" in c.problem

    def test_a_low_minimum_takes_what_there_is(self, tmp_path):
        root = _project(tmp_path)
        old = jdk_mod.Jdk("/j11", "11.0.1", 11, "JAVA_HOME")
        assert self._choose(root, [old], minimum=8).jdk == old

    def test_candidates_are_found_without_a_shell(self, tmp_path):
        """Android Studio's JDK, java_home's list, Gradle's own and sdkman's,
        each once: a daemon from the menu bar has no JAVA_HOME."""
        home = tmp_path / "home"
        sdk = _jdk_dir(home / ".sdkman" / "candidates" / "java", "17.0.20-tem", "17.0.20")
        (home / ".sdkman" / "candidates" / "java" / "current").symlink_to(sdk)
        provisioned = _jdk_dir(home / ".gradle" / "jdks" / "adoptium-21", "jdk-21", "21.0.7")

        def run(argv, **kw):
            return SimpleNamespace(stderr=f'    17.0.20 (arm64) "Homebrew" - "OpenJDK" {sdk}\n')
        found = jdk_mod.candidates(java_home=None, env={}, home=str(home), run=run)
        homes = [j.home for j in found]
        assert sdk in homes and provisioned in homes
        assert len(homes) == len(set(homes)), "the same JDK by two routes counted twice"


# ── the project ─────────────────────────────────────────────────────────────


class TestTheProject:
    def test_the_root_builds_app_by_default(self, tmp_path):
        root = _project(tmp_path)
        p = gradle.find_project(str(root))
        assert (p.root, p.module) == (root.resolve(), "app")
        assert gradle.assemble_task(p, "stagingDebug") == ":app:assembleStagingDebug"

    def test_a_module_directory_names_its_module(self, tmp_path):
        root = _project(tmp_path)
        assert gradle.find_project(str(root / "app")).module == "app"

    @pytest.mark.parametrize("break_it, says", [
        (lambda r: (r / "gradlew").unlink(), "no gradlew"),
        (lambda r: (r / "settings.gradle.kts").unlink(), "not in a Gradle project"),
    ])
    def test_what_it_cannot_build_says_why(self, tmp_path, break_it, says):
        root = _project(tmp_path)
        break_it(root)
        with pytest.raises(gradle.GradleProjectError, match=says):
            gradle.find_project(str(root))

    def test_an_unknown_module(self, tmp_path):
        with pytest.raises(gradle.GradleProjectError, match="module 'wear'"):
            gradle.find_project(str(_project(tmp_path)), "wear")

    @pytest.mark.parametrize("daemon_jvm, gradle_v, minimum", [
        (21, "9.5.1", 8),       # Gradle picks the build's JDK itself: measured, Java 11 built it
        (None, "9.5.1", 17), (None, "8.7", 17), (None, "7.6.4", 11),
    ])
    def test_the_launcher_minimum_follows_the_project(self, tmp_path, daemon_jvm, gradle_v,
                                                      minimum):
        p = gradle.find_project(str(_project(tmp_path, daemon_jvm=daemon_jvm, gradle_v=gradle_v)))
        assert gradle.launcher_minimum(p)[0] == minimum

    def test_the_sdk_from_local_properties_first(self, tmp_path):
        sdk = tmp_path / "sdk"
        sdk.mkdir()
        p = gradle.find_project(str(_project(tmp_path, local=f"sdk.dir={sdk}\n")))
        assert gradle.android_sdk(p, {"ANDROID_HOME": "/elsewhere"}, str(tmp_path)) == (
            str(sdk), "sdk.dir in local.properties")

    def test_no_sdk_anywhere(self, tmp_path):
        p = gradle.find_project(str(_project(tmp_path)))
        assert gradle.android_sdk(p, {}, str(tmp_path / "nohome")) == (None, "")


# ── reading Gradle's output ─────────────────────────────────────────────────


class TestParsingRealOutput:
    def _parse(self, tmp_path, name, code=1, candidates=None):
        p = gradle.find_project(str(_project(tmp_path)))
        return gradle.parse(code, (FIXTURES / name).read_text(), p, candidates)

    def test_a_javac_error_has_its_file_and_line(self, tmp_path):
        result, env = self._parse(tmp_path, "javac.out")
        assert not result.succeeded and env == []
        [e] = result.errors
        assert e.file.endswith("probe/Foo.java") and e.line == 3
        assert e.message == "cannot find symbol"

    def test_a_missing_toolchain_is_an_environment_problem(self, tmp_path):
        jdk23 = jdk_mod.Jdk("/jdk23", "23.0.1", 23, "sdkman")
        result, env = self._parse(tmp_path, "toolchain.out", candidates=[jdk23])
        [p] = env
        assert p.kind == "toolchain_jdk" and "Java 23 toolchain" in p.summary
        assert "-Porg.gradle.java.installations.paths=/jdk23" in p.options[0]
        assert "Cannot find a Java installation" in result.errors[0].message

    def test_a_launcher_too_old_is_an_environment_problem(self, tmp_path):
        result, env = self._parse(tmp_path, "jdk8.out")
        assert [p.kind for p in env] == ["jdk"]
        assert "Gradle requires JVM 17" in result.errors[0].message

    def test_a_kotlin_error_has_its_file_line_and_column(self, tmp_path):
        result, env = self._parse(tmp_path, "kotlin.out")
        [e] = result.errors
        assert e.file.endswith("probe/Feed.kt") and (e.line, e.column) == (2, 16)
        assert e.message == "Unresolved reference 'missingThing'." and env == []

    def test_a_dependency_that_could_not_be_fetched_says_so(self, tmp_path):
        """Not a compile error, and not the machine's JDK or SDK: Gradle's own
        account of what went wrong is the error."""
        result, env = self._parse(tmp_path, "offline_deps.out")
        [e] = result.errors
        assert "No cached version available for offline mode" in e.message and env == []

    def test_a_failure_never_reads_as_zero_errors(self, tmp_path):
        result, _ = self._parse(tmp_path, "jdk8.out")
        assert result.errors and "0 error" not in result.summary

    def test_success(self, tmp_path):
        p = gradle.find_project(str(_project(tmp_path)))
        result, env = gradle.parse(0, "> Task :app:assembleDebug\n\nBUILD SUCCESSFUL in 4s\n", p)
        assert result.succeeded and not result.errors and env == []

    def test_exit_zero_without_success_is_not_success(self, tmp_path):
        p = gradle.find_project(str(_project(tmp_path)))
        result, _ = gradle.parse(0, "something else\n", p)
        assert not result.succeeded

    @pytest.mark.parametrize("line, file, lineno, col", [
        ("e: file:///Users/someone/app/src/Feed.kt:12:5 Unresolved reference 'x'.",
         "/Users/someone/app/src/Feed.kt", 12, 5),
        ("e: /Users/someone/app/src/Feed.kt: (12, 5): Unresolved reference: x",
         "/Users/someone/app/src/Feed.kt", 12, 5),
    ])
    def test_kotlin_errors_both_formats(self, tmp_path, line, file, lineno, col):
        p = gradle.find_project(str(_project(tmp_path)))
        result, _ = gradle.parse(1, f"{line}\nBUILD FAILED in 3s\n", p)
        [e] = result.errors
        assert (e.file, e.line, e.column) == (file, lineno, col)

    @pytest.mark.parametrize("text, kind", [
        ("SDK location not found. Define a valid SDK location with an ANDROID_HOME", "android_sdk"),
        ("Failed to find target with hash string 'android-35' in: /sdk", "sdk_packages"),
        ("Failed to install the following Android SDK packages as some licences have not "
         "been accepted.", "sdk_packages"),
        ("NDK not configured. Download it with SDK manager.", "ndk"),
        ("Android Gradle plugin requires Java 17 to run. You are currently using Java 11.",
         "jdk"),
    ])
    def test_machine_problems_are_named(self, tmp_path, text, kind):
        p = gradle.find_project(str(_project(tmp_path)))
        _, env = gradle.parse(1, f"* What went wrong:\n{text}\n\n* Try:\nBUILD FAILED\n", p)
        assert [e.kind for e in env] == [kind]


# ── installing ──────────────────────────────────────────────────────────────


class TestInstallOutcome:
    def test_success(self):
        assert gradle.install_outcome(0, "Performing Streamed Install\nSuccess\n", "") == (
            True, None, "")

    def test_an_old_adb_failure_with_exit_zero_is_a_failure(self):
        """Reporting it installed would be the success that did not happen."""
        ok, reason, msg = gradle.install_outcome(
            0, "Failure [INSTALL_FAILED_UPDATE_INCOMPATIBLE: Existing package signatures do "
               "not match]\n", "")
        assert not ok and reason == "INSTALL_FAILED_UPDATE_INCOMPATIBLE"
        assert "uninstall_on_signature_mismatch" in msg

    def test_a_new_adb_failure(self):
        ok, reason, _ = gradle.install_outcome(
            1, "", "adb: failed to install /a.apk: Failure [INSTALL_FAILED_OLDER_SDK: x]")
        assert (ok, reason) == (False, "INSTALL_FAILED_OLDER_SDK")

    def test_an_unexplained_failure_says_what_adb_said(self):
        ok, reason, msg = gradle.install_outcome(1, "", "adb: device offline")
        assert not ok and reason is None and "device offline" in msg


class TestPickingTheApk:
    META = {"_dir": "/out", "elements": [
        {"outputFile": "app-arm64-v8a-debug.apk", "filters": [{"filterType": "ABI",
                                                              "value": "arm64-v8a"}]},
        {"outputFile": "app-x86_64-debug.apk", "filters": [{"filterType": "ABI",
                                                           "value": "x86_64"}]},
        {"outputFile": "app-universal-debug.apk", "filters": []},
    ]}

    def test_the_devices_own_abi(self):
        assert gradle.pick_apk(self.META, ["x86_64", "arm64-v8a"]).name == "app-x86_64-debug.apk"

    def test_universal_when_no_split_fits(self):
        assert gradle.pick_apk(self.META, ["armeabi-v7a"]).name == "app-universal-debug.apk"

    def test_unknown_abis_take_the_universal_one(self):
        assert gradle.pick_apk(self.META, []).name == "app-universal-debug.apk"

    def test_nothing_fits(self):
        meta = {"_dir": "/out", "elements": self.META["elements"][:1]}
        assert gradle.pick_apk(meta, ["x86_64"]) is None


# ── the route ───────────────────────────────────────────────────────────────


class FakeAdb:
    def __init__(self, results):
        self.results = list(results)      # (code, out, err) per install, in order
        self.calls = []

    async def supported_abis(self, serial):
        return ["arm64-v8a"]

    async def install_apk_result(self, serial, apk, allow_downgrade=False):
        self.calls.append(("install", serial) if not allow_downgrade
                          else ("install -d", serial))
        return self.results.pop(0)

    async def uninstall_app(self, serial, package):
        self.calls.append(("uninstall", serial, package))


class FakeController:
    def __init__(self, adb, android=("emulator-5554",)):
        self.adb = adb
        self.android = set(android)

    async def resolve_udid(self, udid):
        return udid or "emulator-5554"

    def _is_android(self, udid):
        return udid in self.android


def _body(**kw):
    return build_app.BuildAndInstallRequest(**{"project_path": "", "udids": ["emulator-5554"],
                                               "variant": "debug", **kw})


@pytest.fixture
def built(tmp_path, monkeypatch):
    """A project whose Gradle build succeeds and leaves one universal APK."""
    root = _project(tmp_path, daemon_jvm=21)
    out = root / "app" / "build" / "outputs" / "apk" / "debug"
    out.mkdir(parents=True)
    (out / "app-debug.apk").write_bytes(b"PK")
    (out / "output-metadata.json").write_text(json.dumps({
        "applicationId": "com.example.app", "variantName": "debug",
        "elements": [{"outputFile": "app-debug.apk", "versionCode": 1, "versionName": "1.0"}]}))
    jbr = jdk_mod.Jdk("/jbr", "21.0.10", 21, "Android Studio's bundled JDK")
    monkeypatch.setattr(jdk_mod, "candidates", lambda **kw: [jbr])
    monkeypatch.setattr(gradle, "android_sdk", lambda *a: ("/sdk", "test"))
    ran = []

    async def run(project, task, env, args, timeout=0):
        ran.append((task, env["JAVA_HOME"], args))
        return 0, "BUILD SUCCESSFUL in 9s\n"
    monkeypatch.setattr(gradle, "run", run)
    recorded = []

    async def record(module_dir, variant, **kw):
        from datetime import UTC, datetime

        from server.models import BuildRecord
        recorded.append(variant)
        return BuildRecord(build_id="b1", created_at=datetime.now(UTC), project_path="x",
                           scheme=variant, configuration=variant, platform="android",
                           app_path="x")
    monkeypatch.setattr(build_android.build_records, "record_android_build", record)
    monkeypatch.setattr(build_android.build_records, "save", lambda r: None)
    monkeypatch.setattr(build_android.build_records, "prune", lambda: [])
    return SimpleNamespace(root=root, ran=ran, recorded=recorded)


def _go(controller, body):
    return asyncio.run(build_android.build_and_install(controller, body))


class TestTheRoute:
    def test_builds_installs_and_records(self, built):
        adb = FakeAdb([(0, "Success\n", "")])
        r = _go(FakeController(adb), _body(project_path=str(built.root)))
        assert built.ran == [(":app:assembleDebug", "/jbr", [])]
        assert r["all_installed"] and r["devices"][0].installed
        assert built.recorded == ["debug"] and r["build_records"][0].installed_on == [
            "emulator-5554"]
        assert "Android Studio" in r["java"]

    def test_its_own_build_is_not_called_stale(self, built, monkeypatch):
        """Gradle left an up-to-date APK from last week: this source's APK, not a stale
        one -- the live run said "built 7 days before" about a build just checked."""
        seen = {}

        async def record(module_dir, variant, **kw):
            seen.update(kw)
            raise RuntimeError("stop here")
        monkeypatch.setattr(build_android.build_records, "record_android_build", record)
        _go(FakeController(FakeAdb([(0, "Success\n", "")])), _body(project_path=str(built.root)))
        assert seen == {"just_built": True}

    def test_a_variant_is_required(self, built):
        with pytest.raises(HTTPException, match="variant is required"):
            _go(FakeController(FakeAdb([])), _body(project_path=str(built.root), variant=""))

    def test_an_ios_target_is_refused_by_name(self, built):
        with pytest.raises(HTTPException, match="ABCD.*Android only"):
            _go(FakeController(FakeAdb([])), _body(project_path=str(built.root),
                                                   udids=["ABCD"]))

    def test_no_jdk_is_reported_and_nothing_runs(self, built, monkeypatch, tmp_path):
        root = _project(tmp_path / "other")          # no daemon criteria: needs 17
        monkeypatch.setattr(jdk_mod, "candidates",
                            lambda **kw: [jdk_mod.Jdk("/j11", "11.0.1", 11, "JAVA_HOME")])
        r = _go(FakeController(FakeAdb([])), _body(project_path=str(root)))
        [p] = r["environment"]
        assert p.kind == "jdk" and "Java 11.0.1" in " ".join(p.found)
        assert built.ran == [] and r["all_installed"] is False
        assert "environment" in r["devices"][0].error

    def test_no_sdk_is_reported(self, built, monkeypatch):
        monkeypatch.setattr(gradle, "android_sdk", lambda *a: (None, ""))
        r = _go(FakeController(FakeAdb([])), _body(project_path=str(built.root)))
        assert [p.kind for p in r["environment"]] == ["android_sdk"] and built.ran == []

    def test_java_home_and_gradle_args_reach_gradle(self, built, monkeypatch):
        mine = jdk_mod.Jdk("/mine", "17.0.2", 17, "the java_home you passed")
        monkeypatch.setattr(jdk_mod, "candidates", lambda **kw: [mine])
        _go(FakeController(FakeAdb([(0, "Success\n", "")])),
            _body(project_path=str(built.root), java_home="/mine", gradle_args=["--offline"]))
        assert built.ran == [(":app:assembleDebug", "/mine", ["--offline"])]

    def test_a_failed_build_installs_nothing(self, built, monkeypatch):
        async def run(*a, **k):
            return 1, (FIXTURES / "javac.out").read_text()
        monkeypatch.setattr(gradle, "run", run)
        adb = FakeAdb([])
        r = _go(FakeController(adb), _body(project_path=str(built.root)))
        assert not r["build_android"].succeeded and adb.calls == [] and built.recorded == []

    def test_a_signature_mismatch_is_said_and_not_uninstalled_by_default(self, built):
        adb = FakeAdb([(1, "", "Failure [INSTALL_FAILED_UPDATE_INCOMPATIBLE: sigs]")])
        r = _go(FakeController(adb), _body(project_path=str(built.root)))
        assert not r["devices"][0].installed and ("uninstall", "emulator-5554",
                                                  "com.example.app") not in adb.calls
        assert "uninstall_on_signature_mismatch" in r["devices"][0].error

    def test_opting_in_uninstalls_then_installs(self, built):
        adb = FakeAdb([(1, "", "Failure [INSTALL_FAILED_UPDATE_INCOMPATIBLE: sigs]"),
                       (0, "Success\n", "")])
        r = _go(FakeController(adb), _body(project_path=str(built.root),
                                           uninstall_on_signature_mismatch=True))
        assert adb.calls == [("install", "emulator-5554"),
                             ("uninstall", "emulator-5554", "com.example.app"),
                             ("install", "emulator-5554")]
        assert r["devices"][0].installed and "uninstalled com.example.app" in r["devices"][0].note


class TestDowngrade:
    def test_a_downgrade_is_said_with_the_way_past_it(self, built):
        adb = FakeAdb([(1, "", "Failure [INSTALL_FAILED_VERSION_DOWNGRADE: Downgrade detected]")])
        r = _go(FakeController(adb), _body(project_path=str(built.root)))
        assert "allow_downgrade=true" in r["devices"][0].error

    def test_allowing_it_passes_minus_d(self, built):
        adb = FakeAdb([(0, "Success\n", "")])
        r = _go(FakeController(adb), _body(project_path=str(built.root), allow_downgrade=True))
        assert adb.calls == [("install -d", "emulator-5554")] and r["all_installed"]


class TestRouteDispatch:
    def test_an_xcode_project_inside_a_gradle_repo_is_xcode(self, tmp_path):
        root = _project(tmp_path)
        (root / "ios").mkdir()
        (root / "ios" / "App.xcodeproj").mkdir()
        assert build_app._is_gradle(str(root / "app"))
        assert not build_app._is_gradle(str(root / "ios"))
        assert not build_app._is_gradle(str(root / "ios" / "App.xcodeproj"))
