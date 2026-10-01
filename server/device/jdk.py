"""Which JDK runs a Gradle build, found the way a GUI-launched daemon can (#347).

The user's `java` usually comes from sdkman, asdf or a shell's `JAVA_HOME`, all
of which live in shell startup files. A quern daemon started by the Quern app
gets launchd's PATH and none of them, so `./gradlew` there failed with "JAVA_HOME
is not set" on a machine with five JDKs -- the same shape as the Node problem in
#214. So quern finds one itself, from places that do not need a shell, and says
which it used.

Nothing here installs a JDK or edits a file. When none is suitable the answer is
an `EnvironmentProblem` naming what was found and what would fix it, for the
agent to act on or to put to the user.

Versions are read from each JDK's `release` file (`JAVA_VERSION="17.0.20"`),
which every JDK since 9 ships and Java 8 builds mostly do: nothing is executed.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

#: The oldest JDK a current Gradle build runs on when that JDK *is* the
#: build's: Gradle 9, and the Android Gradle plugin since 8.0, both require 17.
#: A project with Daemon JVM criteria needs far less -- see
#: `gradle.launcher_minimum`.
MIN_LAUNCHER_MAJOR = 17

ANDROID_STUDIO_JBR = ("/Applications/Android Studio.app/Contents/jbr/Contents/Home",
                      "/Applications/Android Studio Preview.app/Contents/jbr/Contents/Home")


@dataclass(frozen=True)
class Jdk:
    home: str
    version: str          # "17.0.20", "1.8.0_212"
    major: int
    source: str           # where it was found, for the report: "Android Studio"


@dataclass
class Choice:
    """The JDK to run Gradle with, and everything looked at to decide."""

    jdk: Jdk | None
    #: Every JDK found, in the order considered, for the report and the options.
    candidates: list[Jdk] = field(default_factory=list)
    #: Set when the choice is not ours to make: `org.gradle.java.home`, which
    #: Gradle uses whatever JAVA_HOME says, names an unusable JDK.
    forced_by: str = ""
    problem: str = ""


def major_of(version: str) -> int | None:
    """17 from "17.0.20", 8 from "1.8.0_212"."""
    m = re.match(r"(\d+)(?:\.(\d+))?", version)
    if not m:
        return None
    first = int(m.group(1))
    return int(m.group(2) or 0) if first == 1 else first


def read_jdk(home: str, source: str, *,
             read_text: Callable[[Path], str] = lambda p: p.read_text()) -> Jdk | None:
    """The JDK at `home`, or None if it is not one (or cannot be read)."""
    try:
        release = read_text(Path(home) / "release")
    except (OSError, ValueError):
        return None
    m = re.search(r'^JAVA_VERSION="([^"]+)"', release, re.M)
    major = major_of(m.group(1)) if m else None
    if not m or major is None:
        return None
    return Jdk(home=str(home), version=m.group(1), major=major, source=source)


def gradle_property(path: Path, key: str) -> str | None:
    """A `key=value` from a gradle.properties, or None."""
    try:
        text = path.read_text()
    except (OSError, ValueError):
        return None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip() == key:
            return v.strip()
    return None


def _java_home_tool(run: Callable[..., subprocess.CompletedProcess]) -> list[str]:
    """Every JDK `/usr/libexec/java_home -V` lists, newest first."""
    try:
        result = run(["/usr/libexec/java_home", "-V"], capture_output=True, text=True,
                     timeout=10)
    except (OSError, subprocess.SubprocessError):
        return []
    # It lists on stderr: `    17.0.20.1 (arm64) "Homebrew" - "OpenJDK…" /path`
    homes = []
    for line in (result.stderr or "").splitlines():
        m = re.search(r'"\s+(/\S.*)$', line)
        if m:
            homes.append(m.group(1).strip())
    return homes


def candidates(*, java_home: str | None, env: dict[str, str], home: str,
               run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
               exists: Callable[[str], bool] = os.path.isdir,
               read: Callable[[str, str], Jdk | None] = read_jdk) -> list[Jdk]:
    """JDKs this machine has, best first, from places a daemon without a shell
    can see. Duplicates (the same home by two routes) appear once."""
    found: list[Jdk] = []
    seen: set[str] = set()

    def add(path: str | None, source: str) -> None:
        if not path:
            return
        real = os.path.realpath(path)
        if real in seen or not exists(path):
            return
        jdk = read(path, source)
        if jdk:
            seen.add(real)
            found.append(jdk)

    add(java_home, "the java_home you passed")
    add(env.get("JAVA_HOME"), "JAVA_HOME")
    for jbr in ANDROID_STUDIO_JBR:
        add(jbr, "Android Studio's bundled JDK")
    for path in _java_home_tool(run):
        add(path, "/usr/libexec/java_home")
    # JDKs Gradle provisioned itself, for a toolchain or the daemon:
    # ~/.gradle/jdks/<vendor-version-arch>/<jdk>/Contents/Home on macOS.
    gradle_jdks = Path(env.get("GRADLE_USER_HOME") or Path(home) / ".gradle") / "jdks"
    try:
        for d in sorted(gradle_jdks.glob("*/*"), reverse=True) if gradle_jdks.is_dir() else []:
            mac = d / "Contents" / "Home"
            add(str(mac if mac.is_dir() else d), "provisioned by Gradle")
    except OSError:
        pass
    sdkman = Path(home) / ".sdkman" / "candidates" / "java"
    add(str(sdkman / "current"), "sdkman (current)")
    try:
        for d in sorted(sdkman.iterdir(), reverse=True) if sdkman.is_dir() else []:
            add(str(d), "sdkman")
    except OSError:
        pass
    return found


def choose(project_root: Path, *, java_home: str | None = None,
           env: dict[str, str] | None = None, home: str | None = None,
           minimum: int = MIN_LAUNCHER_MAJOR,
           found: list[Jdk] | None = None) -> Choice:
    """The JDK to run Gradle in `project_root` with.

    `org.gradle.java.home` (the project's gradle.properties, then the user's)
    is authoritative: Gradle runs on it whatever JAVA_HOME says, so it is
    validated rather than overridden. Otherwise the first candidate at least
    `minimum` wins, an explicit `java_home` first.
    """
    env = dict(os.environ if env is None else env)
    home = home or env.get("HOME") or str(Path.home())
    for props, label in ((project_root / "gradle.properties", "the project's gradle.properties"),
                         (Path(env.get("GRADLE_USER_HOME") or Path(home) / ".gradle")
                          / "gradle.properties", "your ~/.gradle/gradle.properties")):
        forced = gradle_property(props, "org.gradle.java.home")
        if forced:
            jdk = read_jdk(forced, f"org.gradle.java.home in {label}")
            if jdk and jdk.major >= minimum:
                return Choice(jdk=jdk, candidates=[jdk], forced_by=label)
            what = f"Java {jdk.version}" if jdk else "not a JDK quern can read"
            return Choice(jdk=None, candidates=[jdk] if jdk else [], forced_by=label,
                          problem=f"org.gradle.java.home in {label} is {forced} ({what}); "
                                  f"Gradle uses it whatever JAVA_HOME says, and this build "
                                  f"needs Java {minimum} or later")
    jdks = found if found is not None else candidates(java_home=java_home, env=env, home=home)
    if java_home:
        given = next((j for j in jdks if j.source.startswith("the java_home")), None)
        if given is None:
            return Choice(jdk=None, candidates=jdks,
                          problem=f"the java_home you passed, {java_home}, is not a JDK")
        if given.major < minimum:
            return Choice(jdk=None, candidates=jdks,
                          problem=f"the java_home you passed is Java {given.version}; this "
                                  f"build needs Java {minimum} or later")
        return Choice(jdk=given, candidates=jdks)
    usable = [j for j in jdks if j.major >= minimum]
    if usable:
        return Choice(jdk=usable[0], candidates=jdks)
    have = ", ".join(f"Java {j.version}" for j in jdks) or "none"
    return Choice(jdk=None, candidates=jdks,
                  problem=f"no JDK {minimum} or later was found (found: {have}); Gradle "
                          f"needs one to run")
