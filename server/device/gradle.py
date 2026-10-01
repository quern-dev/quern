"""Building an Android app with Gradle, for `build_and_install` (#347).

quern runs the project's own wrapper (`./gradlew`), never a Gradle of its own,
so the build is the one the developer runs. What quern adds is what a daemon
without a shell lacks -- a JDK and an Android SDK it can name -- and a reading
of the outcome: a failed build says why, and a failure that is about the
machine rather than the code (no suitable JDK, no SDK, packages not installed)
is reported as an environment problem, with what was found and the ways to fix
it, for the agent to act on or to put to the user. Nothing here installs
anything or edits a file.

Gradle's own daemon is left running, as Android Studio leaves it: a second
build then takes seconds instead of minutes.
"""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from pathlib import Path

from server.device import jdk as jdk_mod
from server.models import BuildDiagnostic, BuildResult, EnvironmentProblem

#: A clean build of a large app runs many minutes; this is for one that hangs.
BUILD_TIMEOUT = 1800  # s

_SETTINGS = ("settings.gradle.kts", "settings.gradle")


class GradleProjectError(ValueError):
    """The path is not a Gradle project quern can build."""


@dataclass(frozen=True)
class GradleProject:
    root: Path          # holds settings.gradle(.kts) and gradlew
    module: str         # "app", or "feature:checkout"
    module_dir: Path

    @property
    def wrapper(self) -> Path:
        return self.root / "gradlew"


def is_gradle_project(path: str) -> bool:
    """Whether `path`, or a directory above it, is a Gradle build's root."""
    return _root_of(Path(path).expanduser()) is not None


def _root_of(p: Path) -> Path | None:
    p = p.resolve()
    for d in (p, *p.parents):
        if any((d / s).is_file() for s in _SETTINGS):
            return d
    return None


def find_project(path: str, module: str | None = None) -> GradleProject:
    """The project to build from `path`: the build root, or a module inside it.

    A module directory names its module (`app/` → `app`); the root needs
    `module`, defaulting to `app`, the name Android Studio gives it.
    """
    p = Path(path).expanduser().resolve()
    root = _root_of(p)
    if root is None:
        raise GradleProjectError(f"{path} is not in a Gradle project: no settings.gradle(.kts) "
                                 f"here or above it")
    if not (root / "gradlew").is_file():
        raise GradleProjectError(f"{root} has no gradlew; quern runs the project's own Gradle "
                                 f"wrapper (`gradle wrapper` creates one)")
    if module is None:
        module = ":".join(p.relative_to(root).parts) if p != root else "app"
    module = module.strip(":")
    module_dir = root.joinpath(*module.split(":"))
    if not module_dir.is_dir():
        raise GradleProjectError(f"module {module!r} is not a directory under {root}")
    return GradleProject(root=root, module=module, module_dir=module_dir)


def daemon_jvm_version(project: GradleProject) -> int | None:
    """The Java version the project's Daemon JVM criteria ask for, if it has
    them (`gradle/gradle-daemon-jvm.properties`, `toolchainVersion=21`)."""
    value = jdk_mod.gradle_property(project.root / "gradle" / "gradle-daemon-jvm.properties",
                                    "toolchainVersion")
    return int(value) if value and value.isdigit() else None


def gradle_version(project: GradleProject) -> tuple[int, ...] | None:
    """(9, 5, 1) from the wrapper's distributionUrl, or None."""
    url = jdk_mod.gradle_property(project.root / "gradle" / "wrapper" / "gradle-wrapper.properties",
                                  "distributionUrl") or ""
    m = re.search(r"gradle-(\d+(?:\.\d+)*)-", url)
    return tuple(int(p) for p in m.group(1).split(".")) if m else None


def launcher_minimum(project: GradleProject) -> tuple[int, str]:
    """(the oldest JDK to start this build with, why).

    With Daemon JVM criteria, the JDK quern passes only starts Gradle's
    launcher, and Gradle runs the build on a JDK matching the criteria, which
    it finds or downloads itself -- measured: Java 11 as JAVA_HOME built a
    project asking for 21. Without them, that JDK is the build's own: 17 for
    Gradle 8 and later (and the Android Gradle plugin 8 that goes with it), 11
    before. An approximation where the build file could say otherwise; a build
    that disagrees says so, and that is reported too.
    """
    if daemon_jvm_version(project) is not None:
        return 8, "the project's Daemon JVM criteria choose the build's own JDK"
    version = gradle_version(project)
    if version is None or version >= (8,):
        return 17, "Gradle 8 and later, and the Android Gradle plugin 8, need Java 17"
    return 11, "the Android Gradle plugin 7 needs Java 11"


def assemble_task(project: GradleProject, variant: str) -> str:
    return f":{project.module}:assemble{variant[:1].upper()}{variant[1:]}"


# ── the environment ──────────────────────────────────────────────────────────

def android_sdk(project: GradleProject, env: dict[str, str], home: str) -> tuple[str | None, str]:
    """(the SDK directory, where it came from), or (None, "") if none."""
    sdk_dir = jdk_mod.gradle_property(project.root / "local.properties", "sdk.dir")
    if sdk_dir and Path(sdk_dir).is_dir():
        return sdk_dir, "sdk.dir in local.properties"
    for var in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        if env.get(var) and Path(env[var]).is_dir():
            return env[var], var
    default = Path(home) / "Library" / "Android" / "sdk"
    if default.is_dir():
        return str(default), "Android Studio's default location"
    return None, ""


def jdk_problem(choice: jdk_mod.Choice, minimum: int) -> EnvironmentProblem:
    found = [f"Java {j.version} at {j.home} ({j.source})" for j in choice.candidates]
    usable = [j for j in choice.candidates if j.major >= minimum]
    options = [f'run with java_home="{j.home}" (Java {j.version})' for j in usable[:3]]
    if choice.forced_by:
        options.insert(0, f"change or remove org.gradle.java.home in {choice.forced_by}")
    options.append(f"install a JDK {minimum} or later (for example "
                   f"`brew install openjdk@21`, or Android Studio, which bundles one)")
    return EnvironmentProblem(kind="jdk", summary=choice.problem, found=found, options=options)


def sdk_problem(project: GradleProject) -> EnvironmentProblem:
    return EnvironmentProblem(
        kind="android_sdk",
        summary="no Android SDK was found: not in local.properties (sdk.dir), ANDROID_HOME, "
                "ANDROID_SDK_ROOT, or ~/Library/Android/sdk",
        options=[f"set sdk.dir in {project.root / 'local.properties'} to the SDK directory",
                 "install the SDK with Android Studio (Settings > Languages & Frameworks > "
                 "Android SDK)"])


def build_env(base: dict[str, str], jdk: jdk_mod.Jdk, sdk: str) -> dict[str, str]:
    """The environment Gradle runs in: the daemon's, plus the JDK and SDK."""
    env = dict(base)
    env["JAVA_HOME"] = jdk.home
    env["ANDROID_HOME"] = sdk
    env["PATH"] = os.pathsep.join([str(Path(jdk.home) / "bin"), env.get("PATH", "")])
    return env


async def run(project: GradleProject, task: str, env: dict[str, str],
              extra_args: list[str], timeout: float = BUILD_TIMEOUT) -> tuple[int, str]:
    """Run the wrapper; (exit code, combined output). Raises TimeoutError."""
    proc = await asyncio.create_subprocess_exec(
        str(project.wrapper), task, "--console=plain", *extra_args,
        cwd=str(project.root), env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except BaseException:
        if proc.returncode is None:
            proc.kill()
            await asyncio.shield(proc.wait())
        raise
    return proc.returncode or 0, out.decode(errors="replace")


# ── reading the outcome ──────────────────────────────────────────────────────

#: Kotlin 2 (`e: file:///p/Foo.kt:12:5 msg`) and 1 (`e: /p/Foo.kt: (12, 5): msg`).
_KOTLIN = re.compile(r"^([ew]): (?:file://)?(/[^\s:]+\.kts?)"
                     r"(?::(\d+):(\d+)|: \((\d+), (\d+)\):)\s*(.*)$")
_JAVAC = re.compile(r"^(/\S+\.java):(\d+): (error|warning): (.*)$")
_AAPT = re.compile(r"^ERROR:\s*(/\S+?):(\d+):\s*AAPT: error: (.*)$")
_WENT_WRONG = re.compile(r"\* What went wrong:\n(.*?)(?:\n\* Try:|\n\* Exception is:|\Z)", re.S)


def _environment(output: str, project: GradleProject,
                 candidates: list[jdk_mod.Jdk]) -> list[EnvironmentProblem]:
    """Failures that are about the machine, not the code."""
    problems = []
    m = re.search(r"languageVersion=(\d+)", output)
    if m and ("Cannot find a Java installation" in output
              or "No matching toolchains found" in output
              or "No locally installed toolchains match" in output):
        want = int(m.group(1))
        for_daemon = "Daemon JVM" in output
        have = [j for j in candidates if j.major == want]
        paths = ",".join(j.home for j in have)
        options = []
        if have:
            options.append(f'let Gradle use the Java {want} it did not look for: '
                           f'gradle_args=["-Porg.gradle.java.installations.paths={paths}"]')
        options.append(f"install a JDK {want} where Gradle looks for one (for example "
                       f"`brew install openjdk@{want}` or `sdk install java {want}-tem`)")
        options.append("or let Gradle download it: apply the "
                       "`org.gradle.toolchains.foojay-resolver-convention` plugin in "
                       "settings.gradle")
        problems.append(EnvironmentProblem(
            kind="toolchain_jdk",
            summary=(f"the project's Daemon JVM criteria ask for Java {want}, and Gradle "
                     f"found none and could not download one" if for_daemon else
                     f"the build asks for a Java {want} toolchain and Gradle found none"),
            found=[f"Java {j.version} at {j.home}" for j in have],
            options=options))
    m = re.search(r"(?:Gradle requires JVM|Android Gradle plugin requires Java) (\d+)", output)
    if m:
        problems.append(EnvironmentProblem(
            kind="jdk",
            summary=f"Gradle ran on a JDK older than the Java {m.group(1)} this build needs",
            options=["pass java_home= a newer JDK, or change org.gradle.java.home"]))
    if "SDK location not found" in output:
        problems.append(sdk_problem(project))
    m = re.search(r"Failed to (?:find target with hash string '([^']+)'|install the following "
                  r"Android SDK packages[^\n]*)", output)
    if m or "licences have not been accepted" in output or "License for package" in output:
        problems.append(EnvironmentProblem(
            kind="sdk_packages",
            summary="Android SDK packages the build needs are missing or their licences "
                    "are not accepted" + (f" ({m.group(1)})" if m and m.group(1) else ""),
            options=["install them with Android Studio's SDK Manager, or "
                     "`sdkmanager --licenses` then `sdkmanager \"<package>\"`"]))
    if re.search(r"NDK (?:not configured|at \S+ did not have a source\.properties)"
                 r"|No version of NDK matched", output):
        problems.append(EnvironmentProblem(
            kind="ndk",
            summary="the NDK this build needs is not installed",
            options=["install the NDK version the build names, with Android Studio's SDK "
                     "Manager (SDK Tools > NDK, Show Package Details)"]))
    return problems


def parse(code: int, output: str, project: GradleProject,
          candidates: list[jdk_mod.Jdk] | None = None,
          ) -> tuple[BuildResult, list[EnvironmentProblem]]:
    """The build's result, and any environment problems it ran into."""
    lines = output.splitlines()
    errors: list[BuildDiagnostic] = []
    warnings: list[BuildDiagnostic] = []
    for i, line in enumerate(lines):
        if m := _KOTLIN.match(line):
            d = BuildDiagnostic(file=m.group(2), line=int(m.group(3) or m.group(5)),
                                column=int(m.group(4) or m.group(6)), message=m.group(7),
                                severity="error" if m.group(1) == "e" else "warning")
            (errors if d.severity == "error" else warnings).append(d)
        elif m := _JAVAC.match(line):
            d = BuildDiagnostic(file=m.group(1), line=int(m.group(2)), severity=m.group(3),
                                message=m.group(4))
            (errors if d.severity == "error" else warnings).append(d)
        elif m := _AAPT.match(line):
            errors.append(BuildDiagnostic(file=m.group(1), line=int(m.group(2)),
                                          message=f"AAPT: {m.group(3)}"))
    succeeded = code == 0 and "BUILD SUCCESSFUL" in output
    environment = [] if succeeded else _environment(output, project, candidates or [])
    if not succeeded and not errors:
        # Not a compile error: Gradle says what went wrong in one block, which
        # is what the reader needs -- never "0 error(s)" for a failed build.
        m = _WENT_WRONG.search(output)
        why = " ".join(m.group(1).split()) if m else ""
        if not why:
            tail = [ln for ln in lines if ln.strip()][-5:]
            why = " / ".join(tail) or f"gradlew exited {code} with no output"
        errors.append(BuildDiagnostic(message=why))
    result = BuildResult(succeeded=succeeded, errors=errors, warnings=warnings,
                         warning_count=len(warnings), raw_line_count=len(lines))
    result.summary = result.generate_summary()
    return result, environment


# ── the APK and installing it ────────────────────────────────────────────────

def pick_apk(metadata: dict, abis: list[str]) -> Path | None:
    """The APK of a build for a device supporting `abis`, most preferred first.

    ABI splits give one APK per ABI, each with a filter; a universal APK has
    none. The device's own ABI wins, then a universal one.
    """
    elements = metadata.get("elements") or []
    apk_dir = Path(metadata["_dir"])

    def abi_of(e: dict) -> str | None:
        for f in e.get("filters") or []:
            if isinstance(f, dict) and f.get("filterType") == "ABI":
                return f.get("value")
        return None

    for abi in abis:
        for e in elements:
            if abi_of(e) == abi and e.get("outputFile"):
                return apk_dir / e["outputFile"]
    for e in elements:
        if abi_of(e) is None and e.get("outputFile"):
            return apk_dir / e["outputFile"]
    return None


#: `adb install` failures worth a sentence, keyed by Android's code.
INSTALL_FAILURES = {
    "INSTALL_FAILED_UPDATE_INCOMPATIBLE":
        "the installed app is signed with a different key; uninstalling it first erases its "
        "data -- pass uninstall_on_signature_mismatch=true to do that",
    "INSTALL_FAILED_VERSION_DOWNGRADE":
        "the installed app has a higher versionCode",
    "INSTALL_FAILED_OLDER_SDK":
        "the device's Android version is below the app's minSdk",
    "INSTALL_FAILED_NO_MATCHING_ABIS":
        "the APK has no native libraries for this device's CPU",
    "INSTALL_FAILED_INSUFFICIENT_STORAGE":
        "the device does not have room for it",
    "INSTALL_PARSE_FAILED_NO_CERTIFICATES":
        "the APK is not signed: this variant has no signing config, so it cannot be installed",
}

_FAILURE = re.compile(r"Failure \[([A-Z_]+)(?::\s*([^\]]*))?\]")


def install_outcome(code: int, stdout: str, stderr: str) -> tuple[bool, str | None, str]:
    """(installed, Android's failure code, a message), from `adb install`.

    Read from the output, not the exit code alone: older adb exits 0 on
    `Failure [...]`, and reporting that as installed is the success that
    did not happen.
    """
    text = f"{stdout}\n{stderr}"
    m = _FAILURE.search(text)
    if m:
        reason = m.group(1)
        explain = INSTALL_FAILURES.get(reason, m.group(2) or reason)
        return False, reason, f"{reason}: {explain}"
    if code == 0 and "Success" in stdout:
        return True, None, ""
    said = (stderr.strip() or stdout.strip())[-300:]
    return False, None, f"adb install exited {code}" + (f": {said}" if said else "")
