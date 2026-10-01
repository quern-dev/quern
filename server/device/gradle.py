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
_BUILD_FILES = ("build.gradle.kts", "build.gradle")


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
    if not p.exists():
        raise GradleProjectError(f"{path} does not exist")
    root = _root_of(p)
    if root is None:
        raise GradleProjectError(f"{path} is not in a Gradle project: no settings.gradle(.kts) "
                                 f"here or above it")
    if not (root / "gradlew").is_file():
        raise GradleProjectError(f"{root} has no gradlew; quern runs the project's own Gradle "
                                 f"wrapper (`gradle wrapper` creates one)")
    if module is None and p != root:
        # The nearest directory with a build file is the module: a path to
        # app/src/main means app, not a module called app:src:main.
        d = p if p.is_dir() else p.parent
        while d != root and not any((d / f).is_file() for f in _BUILD_FILES):
            d = d.parent
        module = ":".join(d.relative_to(root).parts) if d != root else "app"
    elif module is None:
        module = "app"
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


#: The newest Java each Gradle runs on, from Gradle's compatibility matrix:
#: (first Gradle supporting it, Java). Newer than the last row is not known.
_GRADLE_MAX_JAVA = ((7, 3), 17), ((7, 5), 18), ((7, 6), 19), ((8, 3), 20), ((8, 5), 21), \
    ((8, 8), 22), ((8, 10), 23), ((8, 14), 24), ((9, 1), 25)


def java_range(project: GradleProject) -> tuple[int, int | None, str]:
    """(oldest, newest or None, why): the JDKs this build can start with.

    With Daemon JVM criteria, the JDK quern passes only starts Gradle's
    launcher, and Gradle runs the build on a JDK matching the criteria, which
    it finds or downloads itself -- measured: Java 8 and Java 11 as JAVA_HOME
    both built a project asking for 21. Without them, that JDK is the build's
    own, and it needs a floor (17 for Gradle 8 and later and the Android Gradle
    plugin 8, 11 before) and a ceiling: Java 21 failed a Gradle 7.6 build with
    "Unsupported class file major version 65" (measured). An approximation where
    the build file could say otherwise; a build that disagrees says so.
    """
    if daemon_jvm_version(project) is not None:
        return 8, None, "the project's Daemon JVM criteria choose the build's own JDK"
    version = gradle_version(project)
    if version is None:
        return 17, None, "a current Android build needs Java 17"
    minimum = 17 if version >= (8,) else 11
    maximum = 16
    for first, java in _GRADLE_MAX_JAVA:
        if version >= first:
            maximum = java
    if version[:2] > _GRADLE_MAX_JAVA[-1][0]:
        newest_known = None       # a Gradle newer than the table: no ceiling to state
    else:
        newest_known = maximum
    named = ".".join(str(p) for p in version)
    return minimum, newest_known, (f"Gradle {named} runs on Java {minimum}"
                                   + (f" to {newest_known}" if newest_known else " or later"))


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


#: The JDKs Homebrew has a versioned formula for: the LTS releases. Naming
#: `openjdk@19` would hand the reader a command that fails.
_BREW_JDKS = (21, 17, 11)


def _use_jdk(jdk: jdk_mod.Jdk, forced_by: str = "") -> str:
    """How to run on `jdk`: `java_home`, unless org.gradle.java.home is set,
    which wins over JAVA_HOME and so over java_home; -D on the command line
    gets past it without editing a file."""
    if forced_by:
        return (f'run with gradle_args=["-Dorg.gradle.java.home={jdk.home}"] '
                f'(Java {jdk.version}; org.gradle.java.home in {forced_by} would win over '
                f'java_home)')
    return f'run with java_home="{jdk.home}" (Java {jdk.version})'


def _install_jdk(minimum: int, maximum: int | None) -> str:
    """Advice for installing a JDK in [minimum, maximum], naming a command
    that exists."""
    brew = next((v for v in _BREW_JDKS if v >= minimum and (maximum is None or v <= maximum)),
                None)
    how = [f"`brew install openjdk@{brew}`"] if brew else []
    how += ["Android Studio, which bundles one", "https://adoptium.net"]
    return (f"install a JDK {jdk_mod._range(minimum, maximum).replace('Java ', '')} "
            f"(for example {', or '.join(how)}): a change to the machine")


def jdk_problem(choice: jdk_mod.Choice, minimum: int,
                maximum: int | None) -> EnvironmentProblem:
    found = [f"Java {j.version} at {j.home} ({j.source})" for j in choice.candidates]
    usable = [j for j in choice.candidates
              if j.major >= minimum and (maximum is None or j.major <= maximum)]
    options = [_use_jdk(j, choice.forced_by) for j in usable[:3]]
    if choice.forced_by:
        options.append(f"change or remove org.gradle.java.home in {choice.forced_by}")
    options.append(_install_jdk(minimum, maximum))
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
    if "ANDROID_SDK_ROOT" in env:
        # An inherited ANDROID_SDK_ROOT naming another SDK makes the Android
        # Gradle plugin refuse both ("Several environment variables ...").
        env["ANDROID_SDK_ROOT"] = sdk
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

#: Kotlin 2 (`e: file:///p/Foo.kt:12:5 msg`), Kotlin 1 (`e: /p/Foo.kt: (12, 5):
#: msg`), and what annotation processors print through it: kapt as
#: `e: /p/kapt3/stubs/X.java:130: error: [Dagger/MissingBinding] …` and KSP as
#: `e: [ksp] /p/Foo.kt:12: msg`. Paths may hold spaces, so a path runs to the
#: first source extension rather than the first blank.
_KOTLIN = re.compile(r"^(?P<sev>[ew]): (?:\[ksp\] )?(?:file://)?(?P<file>/.+?\.(?:kts?|java))"
                     r"(?::(?P<l1>\d+):(?P<c1>\d+)|:(?P<l2>\d+)"
                     r"|: \((?P<l3>\d+), (?P<c3>\d+)\))?:?\s*(?:(?:error|warning): )?"
                     r"(?P<msg>.*)$")
_JAVAC = re.compile(r"^(/.+?\.java):(\d+): (error|warning): (.*)$")
_AAPT = re.compile(r"^ERROR:\s*(/.+?):(\d+):\s*AAPT: error: (.*)$")
#: AGP 9: `com.example.app-main-5:/layout/main.xml:4: error: resource … not found.`
_AAPT9 = re.compile(r"^[\w.\-]+:(/.+?):(\d+): error: (.*)$")
_WENT_WRONG = re.compile(r"\* What went wrong:\n(.*?)(?:\n\* Try:|\n\* Exception is:|\Z)", re.S)


def _environment(output: str, project: GradleProject, candidates: list[jdk_mod.Jdk],
                 ran_on: int | None, forced_by: str) -> list[EnvironmentProblem]:
    """Failures that are about the machine, not the code.

    `ran_on` is the Java the build itself ran on, when known: the JDK given,
    or the Daemon JVM criteria's version.
    """
    problems = []
    m = re.search(r"Downloading (https?://\S+gradle-[\w.\-]+\.zip)", output)
    # Only before Gradle started: a first run downloads Gradle and then can
    # fail on anything at all, and that is not the network's fault.
    started = ("Welcome to Gradle" in output or "> Task " in output
               or "BUILD FAILED" in output)
    if m and not started and ("Exception in thread" in output
                              or "Could not install Gradle" in output):
        problems.append(EnvironmentProblem(
            kind="gradle_distribution",
            summary=f"the Gradle wrapper could not download Gradle from {m.group(1)}",
            options=["check the network, then build again: the wrapper downloads Gradle "
                     "once and keeps it in ~/.gradle/wrapper/dists"]))
    minimum, maximum, why = java_range(project)
    java = None
    if m := re.search(r"incompatible Java (\d+)[\d.]* and Gradle", output):
        java = int(m.group(1))
    elif m := re.search(r"Unsupported class file major version (\d+)", output):
        # The number is the version of whatever class was being read -- a
        # dependency's, under Jetifier or an old ASM -- not the JVM's. It is
        # this JDK being too new only when it is the JVM the build ran on,
        # and newer than the project's Gradle supports.
        seen = int(m.group(1)) - 44
        if maximum is not None and seen > maximum and ran_on in (None, seen):
            java = seen
    if java is not None:
        fits = [j for j in candidates
                if j.major >= minimum and (maximum is None or j.major <= maximum)]
        problems.append(EnvironmentProblem(
            kind="jdk",
            summary=f"Gradle ran on Java {java}, which is too new for this project's Gradle "
                    f"({why})",
            found=[f"Java {j.version} at {j.home}" for j in candidates],
            options=[_use_jdk(j, forced_by) for j in fits[:3]]
            + ([] if fits else [_install_jdk(minimum, maximum)])
            + ["update the project's Gradle wrapper (`./gradlew wrapper "
               "--gradle-version <newer>`): a change to the project"]))
    m = re.search(r"languageVersion=(\d+)", output)
    if m and ("Cannot find a Java installation" in output
              or "No matching toolchains found" in output
              or "No locally installed toolchains match" in output):
        want = int(m.group(1))
        # Gradle 9.5 does not say "Daemon JVM" when criteria fail; the
        # project's own criteria asking for this version is what tells.
        for_daemon = "Daemon JVM" in output or daemon_jvm_version(project) == want
        have = [j for j in candidates if j.major == want]
        paths = ",".join(j.home for j in have)
        options = []
        if have:
            options.append(f'let Gradle use the Java {want} it did not look for: '
                           f'gradle_args=["-Dorg.gradle.java.installations.paths={paths}"]')
        options.append(_install_jdk(want, want))
        if for_daemon:
            options.append("or let Gradle download it: the criteria's toolchainUrl entries "
                           "do that unless org.gradle.java.installations.auto-download=false")
        else:
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
        need = int(m.group(1))
        fits = [j for j in candidates if j.major >= need]
        problems.append(EnvironmentProblem(
            kind="jdk",
            summary=f"Gradle ran on a JDK older than the Java {need} this build needs",
            found=[f"Java {j.version} at {j.home}" for j in candidates],
            options=[_use_jdk(j, forced_by) for j in fits[:3]]
            or [_install_jdk(need, None)]))
    if "SDK location not found" in output:
        problems.append(sdk_problem(project))
    packages = list(dict.fromkeys(
        [f"platforms;{p}" for p in re.findall(r"Failed to find target with hash string "
                                              r"'([^']+)'", output)]
        + [f"build-tools;{v}" for v in re.findall(r"Failed to find Build Tools revision "
                                                  r"([\d.]+)", output)]
        + re.findall(r"Failed to find Platform SDK with path: (\S+)", output)
        + re.findall(r"^\s*-\s.*\(([a-z\-]+;[^)]+)\)\s*$", output, re.M)))
    unlicensed = ("licences have not been accepted" in output
                  or "License for package" in output
                  or re.search(r"install the following (?:Android )?SDK (?:packages|components)",
                               output))
    if packages or unlicensed:
        named = " ".join(f'"{p}"' for p in packages) or '"<package>"'
        problems.append(EnvironmentProblem(
            kind="sdk_packages",
            summary="Android SDK packages the build needs are missing or their licences "
                    "are not accepted" + (f" ({', '.join(packages)})" if packages else ""),
            found=packages,
            options=[f"install them with Android Studio's SDK Manager, or `sdkmanager "
                     f"--licenses` then `sdkmanager {named}`: a change to the machine",
                     'let the Android Gradle plugin install them as it builds: gradle_args='
                     '["-Pandroid.builder.sdkDownload=true"], once their licences are '
                     'accepted']))
    if re.search(r"NDK (?:not configured|at \S+ did not have a source\.properties)"
                 r"|No version of NDK matched", output):
        problems.append(EnvironmentProblem(
            kind="ndk",
            summary="the NDK this build needs is not installed",
            options=["install the NDK version the build names, with Android Studio's SDK "
                     "Manager (SDK Tools > NDK, Show Package Details)"]))
    return problems


def parse(code: int, output: str, project: GradleProject,
          candidates: list[jdk_mod.Jdk] | None = None, *, ran_on: int | None = None,
          forced_by: str = "", quiet: bool = False,
          ) -> tuple[BuildResult, list[EnvironmentProblem]]:
    """The build's result, and any environment problems it ran into.

    `quiet` is a build run with `-q`, which prints no BUILD SUCCESSFUL: there
    the exit code is all Gradle says.
    """
    lines = output.splitlines()
    errors: list[BuildDiagnostic] = []
    warnings: list[BuildDiagnostic] = []
    for i, line in enumerate(lines):
        if m := _KOTLIN.match(line):
            where = m.group("l1") or m.group("l2") or m.group("l3")
            col = m.group("c1") or m.group("c3")
            d = BuildDiagnostic(file=m.group("file"), line=int(where) if where else None,
                                column=int(col) if col else None, message=m.group("msg"),
                                severity="error" if m.group("sev") == "e" else "warning")
            (errors if d.severity == "error" else warnings).append(d)
        elif m := _JAVAC.match(line):
            message = m.group(4)
            # `cannot find symbol` names the symbol two lines down; without it
            # two different missing symbols read the same.
            for follow in lines[i + 1:i + 4]:
                if follow.strip().startswith("symbol:"):
                    message += f" ({' '.join(follow.split())})"
                    break
            d = BuildDiagnostic(file=m.group(1), line=int(m.group(2)), severity=m.group(3),
                                message=message)
            (errors if d.severity == "error" else warnings).append(d)
        elif m := _AAPT9.match(line):
            errors.append(BuildDiagnostic(file=m.group(1).lstrip("/"), line=int(m.group(2)),
                                          message=f"resource: {m.group(3)}"))
        elif m := _AAPT.match(line):
            errors.append(BuildDiagnostic(file=m.group(1), line=int(m.group(2)),
                                          message=f"AAPT: {m.group(3)}"))
    succeeded = code == 0 and ("BUILD SUCCESSFUL" in output or quiet)
    environment = [] if succeeded else _environment(output, project, candidates or [],
                                                    ran_on, forced_by)
    if not succeeded and not errors:
        # Not a compile error: Gradle says what went wrong in one block, which
        # is what the reader needs -- never "0 error(s)" for a failed build.
        m = _WENT_WRONG.search(output)
        why = " ".join(m.group(1).split()) if m else ""
        if not why:
            # No summary block: the wrapper itself failed. Its exception says
            # why; the stack frames under it do not.
            cause = [ln.strip() for ln in lines
                     if ln.startswith(("Exception in thread", "Caused by:", "Error:"))]
            tail = [ln for ln in lines if ln.strip() and not ln.lstrip().startswith("at ")][-3:]
            why = (" / ".join(cause[-2:]) or " / ".join(tail)
                   or f"gradlew exited {code} with no output")
        errors.append(BuildDiagnostic(message=why))
    result = BuildResult(succeeded=succeeded, errors=errors, warnings=warnings,
                         warning_count=len(warnings), raw_line_count=len(lines))
    result.summary = result.generate_summary()
    return result, environment


# ── the APK and installing it ────────────────────────────────────────────────

def packaged(output: str, project: GradleProject, variant: str) -> tuple[bool | None, list[str]]:
    """(whether this run packaged `variant`, the variants it did package).

    None when Gradle listed no tasks (`-q`), so cannot say. With
    `--console=plain` every task is listed, up to date or not, so an absent
    `package<Variant>` means the APK on disk is from some earlier build: an
    orphaned `debug` output in a project that has since gained flavours.
    """
    tasks = re.findall(r"^> Task (:\S+)", output, re.M)
    if not tasks:
        return None, []
    module = f":{project.module}:".lower()
    mine = {t.lower()[len(module):]: t[len(module):] for t in tasks
            if t.lower().startswith(module)}
    # package<X> alone is not a variant: AGP also runs packageDebugResources
    # and packageDebugAssets. A variant has both package<X> and assemble<X>.
    names = sorted({name[len("package"):][:1].lower() + name[len("package"):][1:]
                    for low, name in mine.items()
                    if low.startswith("package") and len(low) > len("package")
                    and "assemble" + low[len("package"):] in mine})
    return any(n.lower() == variant.lower() for n in names), names

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
        "the installed app has a higher versionCode; for a debuggable build, "
        "allow_downgrade=true installs over it and keeps its data",
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
