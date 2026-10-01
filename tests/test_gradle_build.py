"""build_and_install for Gradle projects (#347).

No Gradle, JDK or adb runs here: JDKs are directories with a `release` file,
projects are laid out by hand, Gradle's output is real output captured from
Gradle 9.5.1 (tests/fixtures/gradle, home paths anonymised), and the controller
and adb are fakes.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from server.api import build_android, build_app
from server.device import gradle
from server.device import jdk as jdk_mod
from server.device.adb import AdbTimeout
from server.models import BuildDiagnostic, BuildResult, DeviceError, DeviceState, DeviceType

FIXTURES = Path(__file__).parent / "fixtures" / "gradle"


@pytest.fixture(autouse=True)
def _no_real_gradle_home(tmp_path, monkeypatch):
    """The route reads os.environ: without this, a developer's own
    org.gradle.java.home in ~/.gradle/gradle.properties decides the JDK in
    these tests (measured: four failed with one naming Java 11)."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("GRADLE_USER_HOME", str(tmp_path / "gradle-user-home"))
    for var in ("JAVA_HOME", "ANDROID_HOME", "ANDROID_SDK_ROOT"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(jdk_mod, "ANDROID_STUDIO_JBR", (str(tmp_path / "no-android-studio"),))
    monkeypatch.setattr(gradle, "_VARIANT_CACHE", {})


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
        assert c.jdk is None and "(Java 17 or later)" in c.problem and "Java 11.0.1" in c.problem

    def test_org_gradle_java_home_is_authoritative(self, tmp_path):
        """Gradle runs on it whatever JAVA_HOME says, so it is what is
        reported, not a better one quern happens to know."""
        old = _jdk_dir(tmp_path, "j11", "11.0.1")
        root = _project(tmp_path, props=f"org.gradle.java.home={old}\n")
        c = self._choose(root, [jdk_mod.Jdk("/jbr", "21.0.10", 21, "Android Studio")])
        assert c.jdk.home == old and "older than" in c.warning
        assert c.forced_by.startswith("the project's gradle.properties (")

    def test_an_unreadable_org_gradle_java_home_is_a_problem(self, tmp_path):
        root = _project(tmp_path, props=f"org.gradle.java.home={tmp_path / 'gone'}\n")
        c = self._choose(root, [jdk_mod.Jdk("/jbr", "21.0.10", 21, "Android Studio")])
        assert c.jdk is None and "not a JDK quern can read" in c.problem

    def test_a_java_home_it_overrides_is_said_to_be_ignored(self, tmp_path):
        forced = _jdk_dir(tmp_path, "j21", "21.0.2")
        root = _project(tmp_path, props=f"org.gradle.java.home={forced}\n")
        mine = jdk_mod.Jdk("/mine", "17.0.2", 17, "the java_home you passed")
        c = self._choose(root, [mine], java_home="/mine")
        assert c.jdk.home == forced and "the java_home you passed (/mine) is not used" in c.warning

    def test_a_good_org_gradle_java_home_is_used(self, tmp_path):
        good = _jdk_dir(tmp_path, "j21", "21.0.1")
        root = _project(tmp_path, props=f"org.gradle.java.home={good}\n")
        assert self._choose(root, []).jdk.home == good

    def test_an_explicit_java_home_wins_or_is_refused(self, tmp_path):
        root = _project(tmp_path)
        mine = jdk_mod.Jdk("/mine", "17.0.2", 17, "the java_home you passed")
        jbr = jdk_mod.Jdk("/jbr", "21.0.10", 21, "Android Studio")
        assert self._choose(root, [mine, jbr], java_home="/mine").jdk == mine
        # Below the expected floor: the floor is an approximation (Gradle 8 with
        # AGP 7 runs on 11), so a chosen JDK is used and the doubt is said.
        old = jdk_mod.Jdk("/old", "11.0.1", 11, "the java_home you passed")
        c = self._choose(root, [old, jbr], java_home="/old")
        assert c.jdk == old and "older than the Java 17" in c.warning

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
        (root / "feature" / "checkout").mkdir(parents=True)
        (root / "feature" / "checkout" / "build.gradle.kts").write_text("")
        p = gradle.find_project(str(root / "feature" / "checkout"))
        assert p.module == "feature:checkout"
        assert gradle.assemble_task(p, "debug") == ":feature:checkout:assembleDebug"

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

    def test_a_path_inside_a_module_is_that_module(self, tmp_path):
        """app/src/main is in module app, not a module called app:src:main."""
        root = _project(tmp_path)
        (root / "app" / "build.gradle.kts").write_text("")
        (root / "app" / "src" / "main").mkdir(parents=True)
        assert gradle.find_project(str(root / "app" / "src" / "main")).module == "app"

    def test_a_path_that_does_not_exist_says_so(self, tmp_path):
        with pytest.raises(gradle.GradleProjectError, match="does not exist"):
            gradle.find_project(str(tmp_path / "nope"))

    @pytest.mark.parametrize("daemon_jvm, gradle_v, minimum, maximum", [
        (21, "9.5.1", 8, None),   # Gradle picks the build's JDK itself: measured, Java 11 built it
        (None, "9.5.1", 17, None),  # newer than the table: no ceiling to state
        (None, "9.1.0", 17, 25),    # a patch number does not lift it past the table
        (None, "8.7", 17, 21), (None, "7.6.4", 11, 19), (None, "7.2", 11, 16),
        # Gradle 6 runs on Java 8 with AGP 4 and older, and 6.7 up to 15.
        (None, "6.7.1", 8, 15), (None, "5.6.4", 8, 12), (None, "5.0", 8, 11),
        (None, "4.10", 8, 10), (None, "4.6", 8, 9), (None, "4.2", 8, 8),
    ])
    def test_the_java_range_follows_the_project(self, tmp_path, daemon_jvm, gradle_v,
                                                minimum, maximum):
        p = gradle.find_project(str(_project(tmp_path, daemon_jvm=daemon_jvm, gradle_v=gradle_v)))
        assert gradle.java_range(p)[:2] == (minimum, maximum)

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
        assert e.message == "cannot find symbol (symbol: variable missingSymbol)"

    def test_a_missing_toolchain_is_an_environment_problem(self, tmp_path):
        jdk23 = jdk_mod.Jdk("/jdk23", "23.0.1", 23, "sdkman")
        result, env = self._parse(tmp_path, "toolchain.out", candidates=[jdk23])
        [p] = env
        assert p.kind == "toolchain_jdk" and "Java 23 toolchain" in p.summary
        assert "-Dorg.gradle.java.installations.paths=/jdk23" in p.options[0]
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
    def __init__(self, results, abis=("arm64-v8a",), uninstall_error=None):
        self.results = list(results)      # (code, out, err) per install, in order
        self.calls = []
        self.abis = abis                  # an exception: the device cannot be asked
        self.uninstall_error = uninstall_error

    async def supported_abis(self, serial):
        if isinstance(self.abis, Exception):
            raise self.abis
        return list(self.abis)

    async def install_apk_result(self, serial, apk, allow_downgrade=False):
        self.calls.append(("install", serial) if not allow_downgrade
                          else ("install -d", serial))
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    async def uninstall_result(self, serial, package, timeout=120):
        self.calls.append(("uninstall", serial, package))
        if isinstance(self.uninstall_error, BaseException):
            raise self.uninstall_error
        return self.uninstall_error or (0, "Success\n", "")


class FakeController:
    def __init__(self, adb, android=("emulator-5554",), active="emulator-5554", booted=None):
        self.adb = adb
        self.android = set(android)
        self._active_udid = active
        self.booted = list(android if booted is None else booted)
        self.resolved = []

    async def resolve_udid(self, udid, set_active=True):
        # The real one, given None, asks the device pool -- which picks, boots
        # and activates an iPhone simulator. Nothing here may call it so.
        assert udid, "resolve_udid(None) would go to the device pool"
        self.resolved.append(udid)
        return udid

    async def list_devices(self):
        return [SimpleNamespace(udid=u, state=DeviceState.BOOTED,
                                device_type=DeviceType.ANDROID_EMULATOR if u in self.android
                                else DeviceType.SIMULATOR)
                for u in self.booted]

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

    async def run(project, task, env, args, timeout=0, progress=None):
        ran.append((task, env["JAVA_HOME"], args))
        return 0, "BUILD SUCCESSFUL in 9s\n"
    monkeypatch.setattr(gradle, "run", run)
    listed = []

    async def list_variants(project, env, args):
        # Real `tasks --all` output of an unflavoured AGP 9.3.2 app: debug, release.
        listed.append(project.module)
        return gradle.parse_variants((FIXTURES / "tasks_plain.out").read_text()), ""
    monkeypatch.setattr(gradle, "list_variants", list_variants)
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
    return SimpleNamespace(root=root, ran=ran, recorded=recorded, listed=listed)


NO_SDK_DOWNLOAD = "-Pandroid.builder.sdkDownload=false"


def _go(controller, body):
    return asyncio.run(build_android.build_and_install(controller, body))


class TestTheRoute:
    def test_builds_installs_and_records(self, built):
        adb = FakeAdb([(0, "Success\n", "")])
        r = _go(FakeController(adb), _body(project_path=str(built.root)))
        assert built.ran == [(":app:assembleDebug", "/jbr", [NO_SDK_DOWNLOAD])]
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

    def test_a_java_home_in_gradle_args_decides_the_jdk(self, built, tmp_path):
        """Gradle runs on -Dorg.gradle.java.home whatever JAVA_HOME says, so the
        JDK quern reports and checks has to be that one."""
        j17 = _jdk_dir(tmp_path, "j17", "17.0.2")
        r = _go(FakeController(FakeAdb([(0, "Success\n", "")])),
                _body(project_path=str(built.root),
                      gradle_args=[f"-Dorg.gradle.java.home={j17}"]))
        assert built.ran[0][1] == j17 and "gradle_args" in r["java"]

    def test_java_home_and_gradle_args_reach_gradle(self, built, monkeypatch):
        mine = jdk_mod.Jdk("/mine", "17.0.2", 17, "the java_home you passed")
        monkeypatch.setattr(jdk_mod, "candidates", lambda **kw: [mine])
        _go(FakeController(FakeAdb([(0, "Success\n", "")])),
            _body(project_path=str(built.root), java_home="/mine", gradle_args=["--offline"]))
        assert built.ran == [(":app:assembleDebug", "/mine", ["--offline", NO_SDK_DOWNLOAD])]

    def test_a_failed_build_installs_nothing(self, built, monkeypatch):
        async def run(*a, **k):
            return 1, (FIXTURES / "javac.out").read_text()
        monkeypatch.setattr(gradle, "run", run)
        adb = FakeAdb([])
        r = _go(FakeController(adb), _body(project_path=str(built.root)))
        assert not r["build_android"].succeeded and adb.calls == [] and built.recorded == []
        assert r["devices"][0].error == "not installed: the build failed"

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


# ── the review's findings (#347) ─────────────────────────────────────────────


class TestTheJdkRange:
    """Below the minimum Gradle does not start; above the maximum it depends on
    the build. Measured on Gradle 7.6.4 with Java 21: a Groovy build script
    failed and the same project in Kotlin DSL built."""

    def _choose(self, root, found, **kw):
        return jdk_mod.choose(root, env={"HOME": str(root.parent)}, home=str(root.parent),
                              found=found, **kw)

    def test_a_too_new_jdk_is_passed_over(self, tmp_path):
        new = jdk_mod.Jdk("/jbr", "21.0.10", 21, "Android Studio")
        ok = jdk_mod.Jdk("/j17", "17.0.2", 17, "sdkman")
        assert self._choose(_project(tmp_path), [new, ok], minimum=11, maximum=19).jdk == ok

    def test_only_newer_ones_uses_the_closest_and_says_so(self, tmp_path):
        j25 = jdk_mod.Jdk("/j25", "25.0.1", 25, "sdkman")
        j21 = jdk_mod.Jdk("/jbr", "21.0.10", 21, "Android Studio")
        c = self._choose(_project(tmp_path), [j25, j21], minimum=11, maximum=19)
        assert c.jdk == j21 and "newer than this project's Gradle supports (up to 19)" in c.warning

    def test_a_newer_java_home_is_used_and_said(self, tmp_path):
        new = jdk_mod.Jdk("/jbr", "21.0.10", 21, "the java_home you passed")
        c = self._choose(_project(tmp_path), [new], java_home="/jbr", minimum=11, maximum=19)
        assert c.jdk == new and "up to 19" in c.warning

    def test_one_in_range_carries_no_warning(self, tmp_path):
        ok = jdk_mod.Jdk("/j17", "17.0.2", 17, "sdkman")
        assert self._choose(_project(tmp_path), [ok], minimum=11, maximum=19).warning == ""

    def test_the_route_builds_gradle_7_on_java_21_and_says_so(self, built, tmp_path):
        root = _project(tmp_path / "old", gradle_v="7.6.4")
        _go(FakeController(FakeAdb([])), _body(project_path=str(root)))
        assert built.ran and built.ran[0][1] == "/jbr"

    def test_the_warning_reaches_the_response(self, built, tmp_path):
        root = _project(tmp_path / "old", gradle_v="7.6.4")
        out = root / "app" / "build" / "outputs" / "apk" / "debug"
        out.mkdir(parents=True)
        (out / "app-debug.apk").write_bytes(b"PK")
        (out / "output-metadata.json").write_text(json.dumps({
            "applicationId": "com.example.app", "variantName": "debug",
            "elements": [{"outputFile": "app-debug.apk"}]}))
        r = _go(FakeController(FakeAdb([(0, "Success\n", "")])), _body(project_path=str(root)))
        assert "Warning: Java 21 is newer than this project's Gradle supports" in r["java"]

    def test_gradle_saying_the_jdk_is_too_new_names_one_that_fits(self, tmp_path):
        p = gradle.find_project(str(_project(tmp_path, gradle_v="7.6.4")))
        j17 = jdk_mod.Jdk("/j17", "17.0.2", 17, "sdkman")
        j21 = jdk_mod.Jdk("/jbr", "21.0.10", 21, "Android Studio")
        # Real: Gradle 7.6.4 on Java 21 with a Groovy settings.gradle.
        out = (FIXTURES / "jdk_too_new.out").read_text()
        _, [env] = gradle.parse(1, out, p, [j21, j17])
        assert env.kind == "jdk" and "Java 21" in env.summary
        assert any('java_home="/j17"' in o for o in env.options)
        assert not any('java_home="/jbr"' in o for o in env.options)


class TestWhoseJavaHomeWins:
    """Gradle reads org.gradle.java.home from -D, then the user's
    gradle.properties, then the project's -- and runs on it whatever JAVA_HOME
    says, so the choice has to be read in the same order."""

    def _choose(self, root, found, home, **kw):
        return jdk_mod.choose(root, env={"HOME": str(home)}, home=str(home), found=found, **kw)

    def test_the_users_properties_beat_the_projects(self, tmp_path):
        j11 = _jdk_dir(tmp_path, "j11", "11.0.1")
        j21 = _jdk_dir(tmp_path, "j21", "21.0.2")
        home = tmp_path / "home"
        (home / ".gradle").mkdir(parents=True)
        (home / ".gradle" / "gradle.properties").write_text(f"org.gradle.java.home={j21}\n")
        root = _project(tmp_path, props=f"org.gradle.java.home={j11}\n")
        c = self._choose(root, [], home)
        assert c.jdk.home == j21
        assert c.forced_by == (f"your Gradle user home's gradle.properties "
                               f"({home / '.gradle' / 'gradle.properties'})")

    def test_a_minus_d_argument_beats_both(self, tmp_path):
        j11 = _jdk_dir(tmp_path, "j11", "11.0.1")
        j21 = _jdk_dir(tmp_path, "j21", "21.0.2")
        root = _project(tmp_path, props=f"org.gradle.java.home={j11}\n")
        c = self._choose(root, [], tmp_path / "home",
                         gradle_args=[f"-Dorg.gradle.java.home={j21}"])
        assert c.jdk.home == j21 and "gradle_args" in c.forced_by

    def test_a_refused_forced_home_still_lists_what_was_found(self, tmp_path):
        """The options have to be able to name a JDK that works -- and through
        -D, since java_home cannot get past org.gradle.java.home."""
        root = _project(tmp_path, props=f"org.gradle.java.home={tmp_path / 'gone'}\n")
        jbr = jdk_mod.Jdk("/jbr", "21.0.10", 21, "Android Studio")
        c = self._choose(root, [jbr], tmp_path / "home")
        problem = gradle.jdk_problem(c, 17, None)
        assert c.jdk is None
        assert any('"-Dorg.gradle.java.home=/jbr"' in o for o in problem.options)

    def test_the_last_minus_d_wins_as_in_gradle(self):
        assert jdk_mod.java_home_override(["-Dorg.gradle.java.home=/a",
                                           "-Dorg.gradle.java.home=/b"]) == "/b"

    @pytest.mark.parametrize("args", [["-g", "{h}"], ["--gradle-user-home", "{h}"],
                                      ["--gradle-user-home={h}"], ["-Dgradle.user.home={h}"]])
    def test_a_gradle_user_home_argument_moves_the_users_properties(self, tmp_path, args):
        j21 = _jdk_dir(tmp_path, "j21", "21.0.2")
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "gradle.properties").write_text(f"org.gradle.java.home={j21}\n")
        c = self._choose(_project(tmp_path), [], tmp_path / "home",
                         gradle_args=[a.format(h=elsewhere) for a in args])
        assert c.jdk.home == j21

    def test_properties_are_read_as_java_reads_them(self, tmp_path):
        """A hand-written path escapes its space; `:` and a blank separate too."""
        p = tmp_path / "gradle.properties"
        p.write_text("# comment\n! also\norg.gradle.java.home=/Applications/Android\\ "
                     "Studio.app/jbr\na.b : spaced\nc d\n")
        assert jdk_mod.gradle_property(p, "org.gradle.java.home") == (
            "/Applications/Android Studio.app/jbr")
        assert jdk_mod.gradle_property(p, "a.b") == "spaced"
        assert jdk_mod.gradle_property(p, "c") == "d"

    def test_a_tilde_java_home_is_expanded(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        seen = {}

        def candidates(**kw):
            seen.update(kw)
            return []
        monkeypatch.setattr(jdk_mod, "candidates", candidates)
        jdk_mod.choose(_project(tmp_path), java_home="~/jdk", env={"HOME": str(tmp_path)},
                       home=str(tmp_path))
        assert seen["java_home"] == str(tmp_path / "jdk")


class TestVariants:
    def test_a_task_name_is_refused_with_the_variant_it_meant(self, built):
        with pytest.raises(HTTPException, match='variant="stagingDebug"'):
            _go(FakeController(FakeAdb([])), _body(project_path=str(built.root),
                                                   variant="assembleStagingDebug"))

    def test_the_outputs_are_found_whatever_the_case(self, built):
        r = _go(FakeController(FakeAdb([(0, "Success\n", "")])),
                _body(project_path=str(built.root), variant="Debug"))
        assert r["all_installed"]

    def test_a_flavour_wide_build_type_is_refused_before_building(self, built, monkeypatch):
        """Measured: `debug` on a flavoured app took two minutes to build every
        flavour's debug before anything could say which to pass."""
        async def list_variants(project, env, args):
            return gradle.parse_variants((FIXTURES / "tasks_flavoured.out").read_text()), ""
        monkeypatch.setattr(gradle, "list_variants", list_variants)
        with pytest.raises(HTTPException, match="not one variant of :app but several: pass one "
                                                "of productionDebug, stagingDebug"):
            _go(FakeController(FakeAdb([])), _body(project_path=str(built.root)))
        assert built.ran == []

    def test_a_flavour_wide_build_type_names_the_variants(self, built, monkeypatch):
        """And when the listing could not run, the outputs still say it."""
        async def list_variants(project, env, args):
            return None, "it timed out"
        monkeypatch.setattr(gradle, "list_variants", list_variants)
        out = built.root / "app" / "build" / "outputs" / "apk"
        for flavour in ("staging", "prod"):
            d = out / flavour / "debug"
            d.mkdir(parents=True)
            (d / "output-metadata.json").write_text(json.dumps({
                "variantName": f"{flavour}Debug",
                "elements": [{"outputFile": "a.apk"}]}))
        (out / "debug" / "output-metadata.json").unlink()
        r = _go(FakeController(FakeAdb([])), _body(project_path=str(built.root)))
        assert "every flavour's: pass one of prodDebug, stagingDebug" in r["devices"][0].error


class TestInstalling:
    def test_a_failed_reinstall_says_the_data_is_gone(self, built):
        adb = FakeAdb([(1, "", "Failure [INSTALL_FAILED_UPDATE_INCOMPATIBLE: sigs]"),
                       (1, "", "Failure [INSTALL_FAILED_INSUFFICIENT_STORAGE]")])
        r = _go(FakeController(adb), _body(project_path=str(built.root),
                                           uninstall_on_signature_mismatch=True))
        [d] = r["devices"]
        assert not d.installed and "erased its data" in d.error
        assert "INSUFFICIENT_STORAGE" in d.error

    @pytest.mark.parametrize("said", [
        (0, "Failure [DELETE_FAILED_INTERNAL_ERROR]\n", ""),   # older adb exits 0 on failure
        (1, "", "adb: device offline"),
    ])
    def test_a_failed_uninstall_is_said_and_nothing_more_is_tried(self, built, said):
        adb = FakeAdb([(1, "", "Failure [INSTALL_FAILED_UPDATE_INCOMPATIBLE: sigs]")],
                      uninstall_error=said)
        r = _go(FakeController(adb), _body(project_path=str(built.root),
                                           uninstall_on_signature_mismatch=True))
        [d] = r["devices"]
        assert not d.installed and "uninstall you allowed failed" in d.error
        assert "erased" not in d.error and adb.calls[-1][0] == "uninstall"

    def test_an_uninstall_that_hangs_may_have_erased_the_data(self, built):
        adb = FakeAdb([(1, "", "Failure [INSTALL_FAILED_UPDATE_INCOMPATIBLE: sigs]")],
                      uninstall_error=AdbTimeout("adb uninstall did not finish within 120s",
                                                 tool="adb"))
        r = _go(FakeController(adb), _body(project_path=str(built.root),
                                           uninstall_on_signature_mismatch=True))
        assert "may already be gone" in r["devices"][0].error

    @pytest.mark.parametrize("raised", [AdbTimeout("adb install did not finish within 300s",
                                                   tool="adb"), TimeoutError(), OSError("gone")])
    def test_a_reinstall_that_raises_still_says_the_data_is_gone(self, built, raised):
        adb = FakeAdb([(1, "", "Failure [INSTALL_FAILED_UPDATE_INCOMPATIBLE: sigs]"), raised])
        r = _go(FakeController(adb), _body(project_path=str(built.root),
                                           uninstall_on_signature_mismatch=True))
        [d] = r["devices"]
        assert d.error.startswith("uninstalled com.example.app first, which erased its data")
        # str(TimeoutError()) is "": the reason must still name something.
        assert "()" not in d.error and not d.error.endswith(": ")
        if isinstance(raised, TimeoutError) and not str(raised):
            assert "TimeoutError" in d.error

    def test_still_refused_after_the_uninstall_does_not_repeat_the_advice(self, built):
        adb = FakeAdb([(1, "", "Failure [INSTALL_FAILED_UPDATE_INCOMPATIBLE: sigs]"),
                       (1, "", "Failure [INSTALL_FAILED_UPDATE_INCOMPATIBLE: sigs]")])
        r = _go(FakeController(adb), _body(project_path=str(built.root),
                                           uninstall_on_signature_mismatch=True))
        error = r["devices"][0].error
        assert "still refused" in error and "pass uninstall_on_signature_mismatch" not in error

    def test_one_device_failing_is_not_all_installed(self, built):
        adb = FakeAdb([(0, "Success\n", ""), (1, "", "Failure [INSTALL_FAILED_OLDER_SDK]")])
        r = _go(FakeController(adb, android=("emulator-5554", "PIXEL")),
                _body(project_path=str(built.root), udids=["emulator-5554", "PIXEL"]))
        assert sum(d.installed for d in r["devices"]) == 1 and r["all_installed"] is False

    def test_unreadable_abis_are_said_rather_than_read_as_a_mismatch(self, built):
        d = built.root / "app" / "build" / "outputs" / "apk" / "debug"
        (d / "output-metadata.json").write_text(json.dumps({
            "applicationId": "com.example.app", "variantName": "debug",
            "elements": [{"outputFile": "app-debug.apk",
                          "filters": [{"filterType": "ABI", "value": "arm64-v8a"}]}]}))
        adb = FakeAdb([], abis=DeviceError("device offline", tool="adb"))
        r = _go(FakeController(adb), _body(project_path=str(built.root)))
        assert "ABIs could not be read (device offline)" in r["devices"][0].error

    def test_unreadable_abis_still_install_a_universal_apk(self, built):
        adb = FakeAdb([(0, "Success\n", "")], abis=DeviceError("offline", tool="adb"))
        r = _go(FakeController(adb), _body(project_path=str(built.root)))
        assert r["all_installed"]

    @pytest.mark.parametrize("code, out, err", [
        (0, "", ""),                                          # no verdict at all
        (0, "Performing Streamed Install\n", ""),
        (0, "", "Failure [INSTALL_FAILED_OLDER_SDK]"),        # older adb exits 0 on failure
    ])
    def test_installed_needs_android_to_say_success(self, code, out, err):
        assert gradle.install_outcome(code, out, err)[0] is False


class TestTheBuildRun:
    def test_sdk_downloads_stay_off_unless_asked_for(self, built):
        _go(FakeController(FakeAdb([(0, "Success\n", "")])),
            _body(project_path=str(built.root),
                  gradle_args=["-Pandroid.builder.sdkDownload=true"]))
        assert built.ran[0][2] == ["-Pandroid.builder.sdkDownload=true"]

    def test_java_says_it_only_starts_gradle_under_daemon_criteria(self, built):
        r = _go(FakeController(FakeAdb([(0, "Success\n", "")])),
                _body(project_path=str(built.root)))
        assert "starting Gradle" in r["java"] and "Java 21 the project's" in r["java"]

    def test_a_timeout_is_reported_and_nothing_installs(self, built, monkeypatch):
        async def run(*a, **k):
            raise TimeoutError
        monkeypatch.setattr(gradle, "run", run)
        adb = FakeAdb([])
        r = _go(FakeController(adb), _body(project_path=str(built.root)))
        assert "did not finish within 30 minutes" in r["build_android"].errors[0].message
        assert "timed out" in r["devices"][0].error and adb.calls == []

    def test_an_unrunnable_wrapper_is_an_environment_problem(self, built, monkeypatch):
        async def run(*a, **k):
            raise PermissionError(13, "Permission denied")
        monkeypatch.setattr(gradle, "run", run)
        r = _go(FakeController(FakeAdb([])), _body(project_path=str(built.root)))
        [p] = r["environment"]
        assert p.kind == "gradle_wrapper" and "chmod +x" in p.options[0]

    def test_a_timed_out_build_is_killed(self, tmp_path, monkeypatch):
        killed = []

        class Stdout:
            async def read(self, n):
                await asyncio.sleep(60)

        class Proc:
            returncode = None
            stdout = Stdout()

            def kill(self):
                killed.append(True)
                self.returncode = -9

            async def wait(self):
                return -9

        async def spawn(*a, **k):
            return Proc()
        monkeypatch.setattr(gradle.asyncio, "create_subprocess_exec", spawn)
        p = gradle.find_project(str(_project(tmp_path)))
        with pytest.raises(TimeoutError):
            asyncio.run(gradle.run(p, ":app:assembleDebug", {}, [], timeout=0.05))
        assert killed == [True]


class TestMoreOutput:
    def test_a_kotlin_warning_is_not_an_error(self, tmp_path):
        p = gradle.find_project(str(_project(tmp_path)))
        out = ("w: file:///p/app/src/A.kt:3:5 'x' is deprecated.\n"
               "e: file:///p/app/src/B.kt:7:1 Unresolved reference 'y'.\n")
        result, _ = gradle.parse(1, out, p)
        assert [e.file for e in result.errors] == ["/p/app/src/B.kt"]
        assert [w.file for w in result.warnings] == ["/p/app/src/A.kt"]

    @pytest.mark.parametrize("line, file", [
        ("ERROR: /p/app/src/main/res/layout/main.xml:4: AAPT: error: resource "
         "string/nope not found.", "/p/app/src/main/res/layout/main.xml"),
        ("com.example.app-main-5:/layout/main.xml:4: error: resource string/nope "
         "not found.", "layout/main.xml"),
    ])
    def test_a_resource_error_has_its_file_and_line(self, tmp_path, line, file):
        p = gradle.find_project(str(_project(tmp_path)))
        result, _ = gradle.parse(1, line + "\n", p)
        [e] = result.errors
        assert (e.file, e.line) == (file, 4) and "string/nope" in e.message

    def test_a_wrapper_that_cannot_download_gradle(self, tmp_path):
        p = gradle.find_project(str(_project(tmp_path)))
        out = ("Downloading https://services.gradle.org/distributions/gradle-9.5.1-bin.zip\n"
               "Exception in thread \"main\" java.io.IOException: Downloading failed\n"
               "\tat org.gradle.wrapper.Download.download(Download.java:83)\n"
               "Caused by: java.net.UnknownHostException: services.gradle.org\n"
               "\tat java.base/sun.nio.ch.NioSocketImpl.connect(NioSocketImpl.java:567)\n"
               "\t... 12 more\n")
        result, [env] = gradle.parse(1, out, p)
        assert env.kind == "gradle_distribution" and "gradle-9.5.1-bin.zip" in env.summary
        # The exception and its cause, not the stack under them.
        message = result.errors[0].message
        assert "Downloading failed" in message and "UnknownHostException" in message
        assert "NioSocketImpl" not in message and "12 more" not in message

    def test_missing_daemon_criteria_jdk_is_labelled_as_such(self, tmp_path):
        """Gradle 9.5 does not say "Daemon JVM" when the criteria fail."""
        p = gradle.find_project(str(_project(tmp_path, daemon_jvm=23)))
        _, [env] = gradle.parse(1, (FIXTURES / "toolchain.out").read_text(), p)
        assert "Daemon JVM criteria ask for Java 23" in env.summary

    def test_the_summary_ends_each_failure_once(self):
        resp = build_app.BuildAndInstallResponse(
            build_android=BuildResult(succeeded=True, summary="ok"), all_installed=False,
            devices=[build_app.DeviceInstallResult(udid="a", installed=False,
                                                   error="no room."),
                     build_app.DeviceInstallResult(udid="b", installed=False,
                                                   error="offline")],
            java="Java 21")
        text = build_app._android_summary(resp)
        assert "Install failed (a): no room. Install failed (b): offline." in text

    @pytest.mark.parametrize("ran, says", [(True, "Build failed: the machine"),
                                           (False, "Not built: the environment")])
    def test_the_summary_says_whether_gradle_ran(self, ran, says):
        resp = build_app.BuildAndInstallResponse(
            build_android=BuildResult(succeeded=False) if ran else None, all_installed=False,
            devices=[], environment=[gradle.sdk_problem(SimpleNamespace(root=Path("/p")))])
        assert build_app._android_summary(resp).startswith(says)


class TestTheDefaultDevice:
    def test_an_ios_active_device_is_passed_over_for_the_booted_android(self, built):
        c = FakeController(FakeAdb([(0, "Success\n", "")]), active="SIM-1",
                           booted=["emulator-5554"])
        r = _go(c, _body(project_path=str(built.root), udids=None))
        assert [d.udid for d in r["devices"]] == ["emulator-5554"]

    def test_two_booted_androids_ask_which(self, built):
        c = FakeController(FakeAdb([]), android=("emulator-5554", "PIXEL"), active="SIM-1")
        with pytest.raises(HTTPException, match="2 Android devices are booted"):
            _go(c, _body(project_path=str(built.root), udids=None))

    def test_none_booted_says_so_and_names_the_active_one(self, built):
        c = FakeController(FakeAdb([]), active="SIM-1", booted=[])
        with pytest.raises(HTTPException, match=r"no Android device .*SIM-1, is not one"):
            _go(c, _body(project_path=str(built.root), udids=None))

    def test_a_named_ios_device_is_still_refused_by_name(self, built):
        with pytest.raises(HTTPException, match="SIM-1 is not an Android device"):
            _go(FakeController(FakeAdb([])), _body(project_path=str(built.root),
                                                   udids=["SIM-1"]))


class TestWhatThisRunPackaged:
    def test_real_agp_output_names_its_variant(self, tmp_path):
        p = gradle.find_project(str(_project(tmp_path)))
        out = (FIXTURES / "agp_flavoured.out").read_text()
        # packageStagingDebugResources is not a variant: only stagingDebug is.
        assert gradle.packaged(out, p, "stagingDebug") == (True, ["stagingDebug"])
        assert gradle.packaged(out, p, "StagingDebug")[0] is True
        assert gradle.packaged(out, p, "debug") == (False, ["stagingDebug"])

    def test_quiet_output_cannot_say(self, tmp_path):
        p = gradle.find_project(str(_project(tmp_path)))
        assert gradle.packaged("", p, "debug") == (None, [])

    def test_an_orphaned_output_is_not_installed(self, built, monkeypatch):
        """outputs/apk/debug survives from before the project had flavours;
        assembleDebug now builds the flavours' and leaves it alone."""
        async def run(*a, **k):
            return 0, ("> Task :app:packageStagingDebug\n> Task :app:assembleStagingDebug\n"
                       "> Task :app:assembleDebug\nBUILD SUCCESSFUL in 3s\n")
        monkeypatch.setattr(gradle, "run", run)
        adb = FakeAdb([])
        r = _go(FakeController(adb), _body(project_path=str(built.root)))
        error = r["devices"][0].error
        assert "did not package debug in this run" in error and "stagingDebug" in error
        assert adb.calls == [] and built.recorded == []

    def test_a_quiet_build_installs(self, built, monkeypatch):
        async def run(*a, **k):
            return 0, ""
        monkeypatch.setattr(gradle, "run", run)
        r = _go(FakeController(FakeAdb([(0, "Success\n", "")])),
                _body(project_path=str(built.root), gradle_args=["-q"]))
        assert r["all_installed"]

    def test_quiet_does_not_make_a_failure_succeed(self, tmp_path):
        p = gradle.find_project(str(_project(tmp_path)))
        assert not gradle.parse(1, "", p, quiet=True)[0].succeeded

    def test_the_record_takes_the_variants_own_spelling(self, built):
        _go(FakeController(FakeAdb([(0, "Success\n", "")])),
            _body(project_path=str(built.root), variant="DEBUG"))
        assert built.recorded == ["debug"]

    def test_a_missing_apk_file_is_said_as_missing(self, built):
        (built.root / "app" / "build" / "outputs" / "apk" / "debug" / "app-debug.apk").unlink()
        r = _go(FakeController(FakeAdb([])), _body(project_path=str(built.root)))
        assert "is not there" in r["devices"][0].error

    def test_no_outputs_after_a_build_points_at_the_module(self, built, tmp_path):
        import shutil
        shutil.rmtree(built.root / "app" / "build")
        r = _go(FakeController(FakeAdb([])), _body(project_path=str(built.root)))
        assert "is app the application module" in r["devices"][0].error
        assert "build the variant first" not in r["devices"][0].error


class TestReadingTheJavaVersionRight:
    """"Unsupported class file major version N" names the class being read,
    not the JVM: Jetifier on a Java 15 dependency says 59 on any JDK."""

    def _parse(self, tmp_path, out, ran_on, gradle_v="8.7"):
        p = gradle.find_project(str(_project(tmp_path, gradle_v=gradle_v)))
        j17 = jdk_mod.Jdk("/j17", "17.0.2", 17, "sdkman")
        return gradle.parse(1, out, p, [j17], ran_on=ran_on)

    def test_a_dependencys_class_version_is_not_the_jdk(self, tmp_path):
        out = ("* What went wrong:\nExecution failed for task ':app:jetifyDebug'.\n"
               "> Failed to transform bcprov-jdk15on-1.68.jar\n"
               "   > Unsupported class file major version 59\n\n* Try:\n")
        result, env = self._parse(tmp_path, out, ran_on=17)
        assert env == [] and "major version 59" in result.errors[0].message

    def test_a_dependency_beyond_the_ceiling_is_not_the_jdk_either(self, tmp_path):
        """Gradle 7.6 (up to 19) on Java 17, Jetifier reading a Java 21 class:
        the number is past the ceiling and still not the JVM's."""
        out = ("* What went wrong:\nExecution failed for task ':app:jetifyDebug'.\n"
               "   > Unsupported class file major version 65\n\n* Try:\n")
        _, env = self._parse(tmp_path, out, ran_on=17, gradle_v="7.6.4")
        assert env == []

    def test_the_jvms_own_version_beyond_the_ceiling_is(self, tmp_path):
        out = (FIXTURES / "jdk_too_new.out").read_text()
        _, [env] = self._parse(tmp_path, out, ran_on=21, gradle_v="7.6.4")
        assert env.kind == "jdk" and "Java 21" in env.summary

    def test_with_no_fitting_jdk_the_options_say_how_to_get_one(self, tmp_path):
        p = gradle.find_project(str(_project(tmp_path, gradle_v="7.6.4")))
        out = (FIXTURES / "jdk_too_new.out").read_text()
        j21 = jdk_mod.Jdk("/jbr", "21.0.10", 21, "Android Studio")
        _, [env] = gradle.parse(1, out, p, [j21], ran_on=21)
        assert env.options[0].startswith("install a JDK 11 to 19")
        assert "openjdk@17" in env.options[0]

    def test_a_forced_java_home_is_got_past_with_minus_d(self, tmp_path):
        p = gradle.find_project(str(_project(tmp_path, gradle_v="7.6.4")))
        out = (FIXTURES / "jdk_too_new.out").read_text()
        j17 = jdk_mod.Jdk("/j17", "17.0.2", 17, "sdkman")
        _, [env] = gradle.parse(1, out, p, [j17], ran_on=21, forced_by="the project's file")
        assert '"-Dorg.gradle.java.home=/j17"' in env.options[0]


class TestMoreFormats:
    @pytest.mark.parametrize("line, file, where, message", [
        ("e: /p/app/build/tmp/kapt3/stubs/debug/X.java:130: error: [Dagger/MissingBinding] "
         "Foo cannot be provided", "/p/app/build/tmp/kapt3/stubs/debug/X.java", 130,
         "[Dagger/MissingBinding] Foo cannot be provided"),
        ("e: [ksp] /p/app/src/Foo.kt:12: Room cannot verify the data", "/p/app/src/Foo.kt", 12,
         "Room cannot verify the data"),
        ("e: file:///Users/a b/My App/app/src/Feed.kt:2:16 Unresolved reference 'x'.",
         "/Users/a b/My App/app/src/Feed.kt", 2, "Unresolved reference 'x'."),
        ("/Users/a b/My App/app/src/Foo.java:3: error: cannot find symbol",
         "/Users/a b/My App/app/src/Foo.java", 3, "cannot find symbol"),
    ])
    def test_processor_output_and_spaced_paths(self, tmp_path, line, file, where, message):
        p = gradle.find_project(str(_project(tmp_path)))
        [e] = gradle.parse(1, line + "\n", p)[0].errors
        assert (e.file, e.line, e.message) == (file, where, message)

    def test_real_agp_9_missing_platform_output(self, tmp_path):
        """AGP 9.3.2 with compileSdk = 99 and SDK downloads off. The package
        path is spelled as the SDK spells it: platforms;android-37.0 is how an
        installed 37 records itself."""
        p = gradle.find_project(str(_project(tmp_path)))
        out = (FIXTURES / "agp_missing_platform.out").read_text()
        _, [env] = gradle.parse(1, out, p)
        assert env.kind == "sdk_packages" and env.found == ["platforms;android-99.0"]

    def test_a_missing_platform_names_the_package(self, tmp_path):
        p = gradle.find_project(str(_project(tmp_path)))
        out = "> Failed to find Platform SDK with path: platforms;android-99\n"
        _, [env] = gradle.parse(1, out, p)
        assert env.kind == "sdk_packages" and env.found == ["platforms;android-99"]
        assert 'sdkmanager "platforms;android-99"' in env.options[0]

    def test_an_exception_after_gradle_started_is_not_a_download_failure(self, tmp_path):
        p = gradle.find_project(str(_project(tmp_path)))
        out = ("Downloading https://services.gradle.org/distributions/gradle-8.7-bin.zip\n"
               "Welcome to Gradle 8.7!\n> Task :app:kaptDebugKotlin\n"
               "e: /p/app/src/A.kt:3:5 Unresolved reference 'y'.\n"
               "Exception in thread \"kapt\" java.lang.OutOfMemoryError\nBUILD FAILED\n")
        result, env = gradle.parse(1, out, p)
        assert env == [] and result.errors[0].file == "/p/app/src/A.kt"

    def test_an_inherited_sdk_root_is_kept_in_step(self):
        jdk = jdk_mod.Jdk("/j", "17", 17, "t")
        env = gradle.build_env({"ANDROID_SDK_ROOT": "/old", "PATH": "/bin"}, jdk, "/sdk")
        assert env["ANDROID_HOME"] == env["ANDROID_SDK_ROOT"] == "/sdk"
        assert "ANDROID_SDK_ROOT" not in gradle.build_env({"PATH": "/bin"}, jdk, "/sdk")

    def test_a_missing_interpreter_is_not_called_a_permission_problem(self, built, monkeypatch):
        async def run(*a, **k):
            raise FileNotFoundError(2, "No such file or directory")
        monkeypatch.setattr(gradle, "run", run)
        r = _go(FakeController(FakeAdb([])), _body(project_path=str(built.root)))
        [p] = r["environment"]
        assert "chmod" not in p.options[0] and "CRLF" in p.options[0]

    def test_the_summary_keeps_the_compilers_errors(self):
        resp = build_app.BuildAndInstallResponse(
            build_android=BuildResult(succeeded=False, errors=[
                BuildDiagnostic(file="/p/A.kt", line=3, message="Unresolved")]),
            all_installed=False, devices=[],
            environment=[gradle.sdk_problem(SimpleNamespace(root=Path("/p")))])
        assert "/p/A.kt:3: Unresolved" in build_app._android_summary(resp)


class TestTheMcpToolWaits:
    def test_the_build_call_opts_out_of_fetchs_timeout(self):
        """fetch gives up on headers after 300s whatever signal it is passed
        (measured on Node 22: UND_ERR_HEADERS_TIMEOUT at 301s, where node:http
        got a 305s answer), so a build longer than five minutes read as a
        failed tool call."""
        import re

        src = (Path(__file__).parents[1] / "mcp" / "src" / "tools" / "build.ts").read_text()
        call = re.search(r'apiRequest\(\s*"POST",\s*"/api/v1/device/build-and-install",'
                         r'(?P<rest>[^;]*)\);', src)
        assert call and '"none"' in call.group("rest")

    def test_an_active_android_wins_among_several_booted(self, built):
        c = FakeController(FakeAdb([(0, "Success\n", "")]), android=("emulator-5554", "PIXEL"),
                           active="PIXEL")
        r = _go(c, _body(project_path=str(built.root), udids=None))
        assert [d.udid for d in r["devices"]] == ["PIXEL"]

    def test_an_active_android_that_is_not_running_is_passed_over(self, built):
        c = FakeController(FakeAdb([(0, "Success\n", "")]), android=("emulator-5554", "PIXEL"),
                           active="PIXEL", booted=["emulator-5554"])
        r = _go(c, _body(project_path=str(built.root), udids=None))
        assert [d.udid for d in r["devices"]] == ["emulator-5554"]


class TestTheThirdReview:
    @pytest.mark.parametrize("flag", ["-m", "--dry-run"])
    def test_a_dry_run_is_refused(self, built, flag):
        with pytest.raises(HTTPException, match="dry run"):
            _go(FakeController(FakeAdb([])), _body(project_path=str(built.root),
                                                   gradle_args=[flag]))
        assert built.ran == []

    @pytest.mark.parametrize("args, props, quiet", [
        (["-w"], "", True), (["--warn"], "", True), (["-q"], "", True),
        (["-Dorg.gradle.logging.level=warn"], "", True),
        ([], "org.gradle.logging.level=quiet\n", True),
        (["-Dorg.gradle.logging.level=lifecycle"], "org.gradle.logging.level=quiet\n", False),
        ([], "org.gradle.logging.level=info\n", False), ([], "", False),
    ])
    def test_the_log_level_decides_whether_success_is_printed(self, tmp_path, args, props,
                                                             quiet):
        p = gradle.find_project(str(_project(tmp_path, props=props)))
        assert gradle.quiet_logging(p, args, tmp_path / "guh") is quiet

    def test_a_warn_level_build_installs(self, built, monkeypatch):
        async def run(*a, **k):
            return 0, ""
        monkeypatch.setattr(gradle, "run", run)
        r = _go(FakeController(FakeAdb([(0, "Success\n", "")])),
                _body(project_path=str(built.root), gradle_args=["-w"]))
        assert r["all_installed"]

    def test_a_directory_named_like_a_source_file_is_not_the_file(self, tmp_path):
        p = gradle.find_project(str(_project(tmp_path)))
        line = "e: /Users/me/Projects/Shared.java/app/src/main/Foo.kt:3:1 Unresolved reference 'x'."
        [e] = gradle.parse(1, line + "\n", p)[0].errors
        assert (e.file, e.line, e.column) == ("/Users/me/Projects/Shared.java/app/src/main/Foo.kt",
                                              3, 1)

    def test_kotlin_2_percent_encodes_a_spaced_path(self, tmp_path):
        """Real Kotlin 2.4.10 output from a project under `sp ace/`."""
        p = gradle.find_project(str(_project(tmp_path)))
        out = (FIXTURES / "kotlin_spaced_path.out").read_text()
        [e] = gradle.parse(1, out, p)[0].errors
        assert e.file == "/Users/someone/src/sp ace/probe/app/src/main/kotlin/Feed.kt"
        assert (e.line, e.column) == (1, 11)

    def test_real_agp_licence_refusal_names_its_packages(self, tmp_path):
        """AGP 9.3.2 with SDK downloads on, against an empty SDK."""
        p = gradle.find_project(str(_project(tmp_path)))
        out = (FIXTURES / "agp_licences.out").read_text()
        _, [env] = gradle.parse(1, out, p)
        assert env.found == ["build-tools;36.0.0", "platforms;android-36"]
        assert 'sdkmanager "build-tools;36.0.0" "platforms;android-36"' in env.options[0]

    def test_properties_last_wins_and_continuations_join(self, tmp_path):
        p = tmp_path / "gradle.properties"
        p.write_text("org.gradle.java.home=/old\norg.gradle.java.home=/new\n"
                     "k=/Library/Java/\\\n    JavaVirtualMachines/x\n"
                     "# a comment ending in a backslash \\\nz=a\\ \n")
        assert jdk_mod.gradle_property(p, "org.gradle.java.home") == "/new"
        assert jdk_mod.gradle_property(p, "k") == "/Library/Java/JavaVirtualMachines/x"
        assert jdk_mod.gradle_property(p, "z") == "a "

    def test_the_forced_label_names_the_file_minus_g_moved(self, tmp_path):
        j21 = _jdk_dir(tmp_path, "j21", "21.0.2")
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "gradle.properties").write_text(f"org.gradle.java.home={j21}\n")
        c = jdk_mod.choose(_project(tmp_path), env={}, home=str(tmp_path / "home"), found=[],
                           gradle_args=["-g", str(elsewhere)])
        assert str(elsewhere / "gradle.properties") in c.forced_by

    def test_a_first_install_that_times_out_may_have_installed(self, built):
        adb = FakeAdb([AdbTimeout("adb install did not finish within 300s", tool="adb")])
        r = _go(FakeController(adb), _body(project_path=str(built.root)))
        assert "may still have installed" in r["devices"][0].error


def test_every_environment_kind_the_code_reports_is_described():
    """The model's description is what an agent reads to know the kinds; one
    added in the code and not there is a kind nobody is told about (review)."""
    import re as _re

    from server.models import EnvironmentProblem
    produced = set()
    for source in (Path(__file__).parents[1] / "server" / "device" / "gradle.py",
                   Path(__file__).parents[1] / "server" / "api" / "build_android.py"):
        produced |= set(_re.findall(r'kind="([a-z_]+)"', source.read_text()))
    described = set(_re.findall(r"'([a-z_]+)'",
                                EnvironmentProblem.model_fields["kind"].description))
    assert produced and produced <= described, sorted(produced - described)


# ── variants, signing and progress (#347, part 2) ────────────────────────────


class TestListingVariants:
    def test_a_flavoured_app_and_its_groups(self):
        """Real `tasks --all`, AGP 9.3.2, flavours staging and production."""
        v = gradle.parse_variants((FIXTURES / "tasks_flavoured.out").read_text())
        assert v.names == ("productionDebug", "productionRelease", "stagingDebug",
                           "stagingRelease")
        assert v.group("debug") == ("productionDebug", "stagingDebug")
        assert v.group("Staging") == ("stagingDebug", "stagingRelease")
        assert v.find("STAGINGDEBUG") == "stagingDebug" and v.find("stagingDeb") is None

    def test_an_unflavoured_app_has_no_groups(self):
        v = gradle.parse_variants((FIXTURES / "tasks_plain.out").read_text())
        assert v.names == ("debug", "release") and v.groups == {}
        assert v.group("debug") == ()

    def test_test_apks_are_not_variants_to_install(self):
        v = gradle.parse_variants((FIXTURES / "tasks_flavoured.out").read_text())
        assert not any(n.endswith(("AndroidTest", "UnitTest")) for n in v.names)

    def test_no_variant_lines_is_none_not_empty(self):
        """"Listed nothing" is not "has no variants": a library module, or
        output this does not read."""
        assert gradle.parse_variants("BUILD SUCCESSFUL in 1s\n") is None

    def _listing(self, tmp_path, monkeypatch, result):
        seen = []

        async def run(project, task, env, args, timeout=0, progress=None):
            seen.append((task, args, timeout))
            if isinstance(result, BaseException):
                raise result
            return result
        monkeypatch.setattr(gradle, "run", run)
        p = gradle.find_project(str(_project(tmp_path)))
        return p, seen, asyncio.run(gradle.list_variants(
            p, {}, ["-Pflavour=x", "-Dk=v", "--offline", ":app:somethingElse", "--info"]))

    def test_it_runs_tasks_all_with_only_the_arguments_that_shape_the_build(self, tmp_path,
                                                                            monkeypatch):
        out = (FIXTURES / "tasks_plain.out").read_text()
        p, seen, (v, why) = self._listing(tmp_path, monkeypatch, (0, out))
        assert why == "" and v.names == ("debug", "release")
        [(task, args, timeout)] = seen
        assert task == ":app:tasks" and timeout == gradle.LIST_TIMEOUT
        assert args == ["--all", "-q", "-Pflavour=x", "-Dk=v", "--offline"]

    def test_a_failed_listing_says_why(self, tmp_path, monkeypatch):
        out = (FIXTURES / "signing_missing_keystore.out").read_text()
        _, _, (v, why) = self._listing(tmp_path, monkeypatch, (1, out))
        assert v is None and "Keystore file" in why

    def test_a_listing_that_fails_part_way_is_not_trusted(self, tmp_path, monkeypatch):
        """Variants printed and then a non-zero exit: the list may be partial,
        and a partial list would refuse a variant that exists."""
        out = (FIXTURES / "tasks_plain.out").read_text() + "\nBUILD FAILED in 2s\n"
        _, _, (v, why) = self._listing(tmp_path, monkeypatch, (1, out))
        assert v is None and why

    def test_a_module_with_no_variants_says_so(self, tmp_path, monkeypatch):
        _, _, (v, why) = self._listing(tmp_path, monkeypatch, (0, "BUILD SUCCESSFUL\n"))
        assert v is None and "listed no variants for :app" in why

    @pytest.mark.parametrize("raised, says", [(TimeoutError(), "did not finish"),
                                              (PermissionError(13, "denied"), "could not be run")])
    def test_a_listing_that_cannot_run(self, tmp_path, monkeypatch, raised, says):
        _, _, (v, why) = self._listing(tmp_path, monkeypatch, raised)
        assert v is None and says in why

    def test_a_changed_build_file_drops_the_cache(self, tmp_path):
        p = gradle.find_project(str(_project(tmp_path)))
        v = gradle.parse_variants((FIXTURES / "tasks_plain.out").read_text())
        gradle.remember_variants(p, gradle._build_files_key(p), v)
        assert gradle.cached_variants(p) == v
        build = p.module_dir / "build.gradle.kts"
        build.write_text("// flavours added\n")
        os.utime(build, ns=(1, 1))
        assert gradle.cached_variants(p) is None


class TestCheckingTheVariant:
    def test_no_variant_lists_them(self, built):
        with pytest.raises(HTTPException, match="variant is required for a Gradle project. "
                                                "Variants of :app: debug, release"):
            _go(FakeController(FakeAdb([])), _body(project_path=str(built.root), variant=None))
        assert built.ran == []

    def test_no_variant_and_no_listing_says_why(self, built, monkeypatch):
        async def list_variants(project, env, args):
            return None, "it timed out"
        monkeypatch.setattr(gradle, "list_variants", list_variants)
        with pytest.raises(HTTPException, match="could not be listed: it timed out"):
            _go(FakeController(FakeAdb([])), _body(project_path=str(built.root), variant=None))

    def test_an_unknown_variant_lists_them_without_building(self, built):
        with pytest.raises(HTTPException, match=r":app has no variant 'debg'. Variants: debug"):
            _go(FakeController(FakeAdb([])), _body(project_path=str(built.root), variant="debg"))
        assert built.ran == []

    def test_an_abbreviation_is_refused(self, built):
        """Gradle would build `debug` for `deb`, then the outputs would be
        looked for under `deb`."""
        with pytest.raises(HTTPException, match="has no variant 'deb'"):
            _go(FakeController(FakeAdb([])), _body(project_path=str(built.root), variant="deb"))

    def test_the_variants_own_spelling_is_built(self, built):
        _go(FakeController(FakeAdb([(0, "Success\n", "")])),
            _body(project_path=str(built.root), variant="DEBUG"))
        assert built.ran[0][0] == ":app:assembleDebug"

    def test_a_cached_answer_skips_the_listing(self, built):
        for _ in range(2):
            _go(FakeController(FakeAdb([(0, "Success\n", "")])),
                _body(project_path=str(built.root)))
        assert built.listed == ["app"], "the second build should not list again"

    def test_a_variant_missing_from_the_cache_is_asked_about_again(self, built):
        """A flavour added in a convention plugin changes no file the cache
        keys on: a cached "no" must not refuse it."""
        _go(FakeController(FakeAdb([(0, "Success\n", "")])), _body(project_path=str(built.root)))
        with pytest.raises(HTTPException):
            _go(FakeController(FakeAdb([])), _body(project_path=str(built.root), variant="beta"))
        assert built.listed == ["app", "app"]

    def test_a_failed_listing_lets_the_build_decide(self, built, monkeypatch):
        async def list_variants(project, env, args):
            return None, "it timed out"
        monkeypatch.setattr(gradle, "list_variants", list_variants)
        r = _go(FakeController(FakeAdb([(0, "Success\n", "")])),
                _body(project_path=str(built.root)))
        assert r["all_installed"]

    def test_gradles_own_typo_answer_reaches_the_errors(self, tmp_path):
        """Real Gradle 9.5.1 output for assembleStagingDebg."""
        p = gradle.find_project(str(_project(tmp_path)))
        result, _ = gradle.parse(1, (FIXTURES / "task_not_found.out").read_text(), p)
        assert "Some candidates are: 'assembleStagingDebug'" in result.errors[0].message


class TestSigning:
    def test_an_unsigned_release_is_not_installed(self, built):
        d = built.root / "app" / "build" / "outputs" / "apk" / "debug"
        meta = json.loads((FIXTURES / "unsigned_release_metadata.json").read_text())
        meta["variantName"] = "debug"
        (d / "output-metadata.json").write_text(json.dumps(meta))
        (d / meta["elements"][0]["outputFile"]).write_bytes(b"PK")
        adb = FakeAdb([])
        r = _go(FakeController(adb), _body(project_path=str(built.root)))
        error = r["devices"][0].error
        assert "app-staging-release-unsigned.apk) is unsigned" in error
        assert adb.calls == [] and built.recorded == []

    def test_unsigned_reads_the_output_file_name(self):
        meta = json.loads((FIXTURES / "unsigned_release_metadata.json").read_text())
        assert gradle.unsigned(meta)
        assert not gradle.unsigned({"elements": [{"outputFile": "app-debug.apk"}]})
        assert not gradle.unsigned({"elements": []})

    @pytest.mark.parametrize("fixture, says", [
        ("signing_missing_keystore.out", "/Users/someone/keys/missing.jks, is not on this machine"),
        ("signing_wrong_password.out", "keystore password was incorrect"),
        ("signing_no_alias.out", "No key with alias 'nokey'"),
    ])
    def test_real_signing_failures_are_environment_problems(self, tmp_path, fixture, says):
        p = gradle.find_project(str(_project(tmp_path)))
        _, [env] = gradle.parse(1, (FIXTURES / fixture).read_text(), p)
        assert env.kind == "signing" and says in env.summary
        assert env.options[0].startswith("build a debug variant")

    def test_a_password_problem_mentions_the_daemons_environment(self, tmp_path):
        p = gradle.find_project(str(_project(tmp_path)))
        _, [env] = gradle.parse(1, (FIXTURES / "signing_wrong_password.out").read_text(), p)
        assert any("shell's exports" in o for o in env.options)


class TestProgress:
    def test_the_current_task_is_tracked_across_chunks(self, tmp_path, monkeypatch):
        """A task line split between two reads is still read whole."""
        parts = [b"Starting a Gradle Daemon\n> Task :app:preBuild UP-TO-DATE\n> Task :app:comp",
                 b"ileDebugKotlin\n", b"BUILD SUCCESSFUL in 3s\n", b""]

        class Stdout:
            async def read(self, n):
                return parts.pop(0)

        class Proc:
            returncode = 0
            stdout = Stdout()

            async def wait(self):
                return 0

        async def spawn(*a, **k):
            return Proc()
        monkeypatch.setattr(gradle.asyncio, "create_subprocess_exec", spawn)
        p = gradle.find_project(str(_project(tmp_path)))
        progress = gradle.BuildProgress(project="p", task=":app:assembleDebug")
        code, out = asyncio.run(gradle.run(p, ":app:assembleDebug", {}, [], progress=progress))
        assert code == 0 and out.endswith("BUILD SUCCESSFUL in 3s\n")
        assert progress.current == "> Task :app:compileDebugKotlin" and progress.tasks_run == 2

    def test_a_build_is_listed_while_it_runs_and_gone_after(self, built, monkeypatch):
        seen = []

        async def run(project, task, env, args, timeout=0, progress=None):
            progress.current = "> Task :app:compileDebugKotlin"
            seen.append(await build_app.build_progress())
            return 0, "BUILD SUCCESSFUL in 9s\n"
        monkeypatch.setattr(gradle, "run", run)
        _go(FakeController(FakeAdb([(0, "Success\n", "")])), _body(project_path=str(built.root)))
        [during] = seen
        [b] = during["builds"]
        assert b["task"] == ":app:assembleDebug" and b["current"].endswith("compileDebugKotlin")
        assert asyncio.run(build_app.build_progress()) == {"builds": []}

    def test_a_build_that_raises_is_not_left_listed(self, built, monkeypatch):
        async def run(*a, **k):
            raise TimeoutError
        monkeypatch.setattr(gradle, "run", run)
        _go(FakeController(FakeAdb([])), _body(project_path=str(built.root)))
        assert gradle.ACTIVE == {}

    def test_the_build_stays_listed_through_each_stage(self, built, monkeypatch):
        """Through the variant check and the installs, not only Gradle: a gap
        reads as a hung build to whoever is waiting."""
        stages = []

        async def list_variants(project, env, args):
            stages.append([b["stage"] for b in (await build_app.build_progress())["builds"]])
            return gradle.parse_variants((FIXTURES / "tasks_plain.out").read_text()), ""
        monkeypatch.setattr(gradle, "list_variants", list_variants)

        async def run(project, task, env, args, timeout=0, progress=None):
            stages.append([b["stage"] for b in (await build_app.build_progress())["builds"]])
            return 0, "BUILD SUCCESSFUL in 9s\n"
        monkeypatch.setattr(gradle, "run", run)

        class Adb(FakeAdb):
            async def install_apk_result(self, serial, apk, allow_downgrade=False):
                stages.append([b["stage"] for b in (await build_app.build_progress())["builds"]])
                return await super().install_apk_result(serial, apk, allow_downgrade)

        _go(FakeController(Adb([(0, "Success\n", "")])), _body(project_path=str(built.root)))
        assert stages == [["checking the variant"], ["building"], ["installing on 1 device(s)"]]
        assert gradle.ACTIVE == {}
