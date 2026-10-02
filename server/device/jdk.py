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
#: A project with Daemon JVM criteria needs far less -- see `gradle.java_range`.
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
    #: Set when the JDK is newer than the project's Gradle supports: it may
    #: still build, so it is used, and said.
    warning: str = ""


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


#: A java.util.Properties line: the key runs to the first unescaped `=`, `:`
#: or whitespace, and one separator (with blanks around it) follows.
_PROPERTY = re.compile(r"^((?:\\.|[^\\=:\s])+)\s*[=:\s]?\s*(.*)$")


def _unescape(text: str) -> str:
    r"""`\ ` to a space, `\t` to a tab and so on, as Properties reads them:
    `/Applications/Android\ Studio.app` is how a hand-written path looks."""
    out, i = [], 0
    while i < len(text):
        if text[i] == "\\" and i + 1 < len(text):
            out.append({"t": "\t", "n": "\n", "r": "\r", "f": "\f"}.get(text[i + 1],
                                                                       text[i + 1]))
            i += 2
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def _logical_lines(text: str) -> list[str]:
    """Physical lines joined where one ends in an odd run of backslashes, the
    continuation's leading blanks dropped -- as java.util.Properties does."""
    lines, held = [], ""
    for raw in text.splitlines():
        line = raw.lstrip()
        if not held and (not line or line.startswith(("#", "!"))):
            continue
        trailing = len(line) - len(line.rstrip("\\"))
        if trailing % 2:
            held += line[:-1]
            continue
        lines.append(held + line)
        held = ""
    if held:
        lines.append(held)
    return lines


def gradle_property(path: Path, key: str) -> str | None:
    """A property from a gradle.properties, read as Gradle reads it (Java
    Properties: `=`, `:` or a space between, backslash escapes and line
    continuations, the last of a repeated key winning), or None."""
    try:
        text = path.read_text()
    except (OSError, ValueError):
        return None
    found = None
    for line in _logical_lines(text):
        m = _PROPERTY.match(line)
        if m and _unescape(m.group(1)) == key:
            # Properties keeps trailing blanks in a value; so does Gradle.
            found = _unescape(m.group(2))
    return found


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


def _range(minimum: int, maximum: int | None) -> str:
    return f"Java {minimum} to {maximum}" if maximum else f"Java {minimum} or later"


def _fits(jdk: Jdk, minimum: int, maximum: int | None) -> bool:
    return jdk.major >= minimum and (maximum is None or jdk.major <= maximum)


def _outside(jdk: Jdk, minimum: int, maximum: int | None) -> str:
    """Why a JDK outside the range is used anyway, or "" if it is inside."""
    if jdk.major < minimum:
        return (f"Java {jdk.major} is older than the Java {minimum} this project's Gradle "
                f"is expected to need; it is used because it was chosen, and Gradle says "
                f"if it cannot run on it")
    if maximum is not None and jdk.major > maximum:
        return (f"Java {jdk.major} is newer than this project's Gradle supports (up to "
                f"{maximum}); it may still build -- Kotlin DSL scripts and a warm script "
                f"cache did, measured -- and a Groovy build script compiled on it fails")
    return ""


def _last_arg(gradle_args: list[str] | None, prefixes: tuple[str, ...],
              flags: tuple[str, ...] = ()) -> str | None:
    """The value of the last `-Dx=v` (or `-g v`) among the arguments: the one
    Gradle uses when a property is given twice."""
    found = None
    args = list(gradle_args or [])
    for i, arg in enumerate(args):
        for p in prefixes:
            if arg.startswith(p):
                found = arg[len(p):]
        if arg in flags and i + 1 < len(args):
            found = args[i + 1]
    return found


def java_home_override(gradle_args: list[str] | None) -> str | None:
    """`-Dorg.gradle.java.home=<path>` among the arguments, which Gradle
    honours over both gradle.properties files."""
    return _last_arg(gradle_args, ("-Dorg.gradle.java.home=",))


def gradle_user_home(gradle_args: list[str] | None, env: dict[str, str], home: str) -> Path:
    """Where Gradle keeps its own gradle.properties: `-g`, then
    `-Dgradle.user.home`, then GRADLE_USER_HOME, then ~/.gradle."""
    given = (_last_arg(gradle_args, ("--gradle-user-home=",), ("-g", "--gradle-user-home"))
             or _last_arg(gradle_args, ("-Dgradle.user.home=",)))
    if given:
        return Path(os.path.expanduser(given))
    return Path(env.get("GRADLE_USER_HOME") or Path(home) / ".gradle")


def choose(project_root: Path, *, java_home: str | None = None,
           env: dict[str, str] | None = None, home: str | None = None,
           minimum: int = MIN_LAUNCHER_MAJOR, maximum: int | None = None,
           gradle_args: list[str] | None = None,
           found: list[Jdk] | None = None) -> Choice:
    """The JDK to run Gradle in `project_root` with: one in [minimum, maximum].

    `org.gradle.java.home` is authoritative -- Gradle runs on it whatever
    JAVA_HOME says -- so it is validated rather than overridden. Gradle reads
    it from `-Dorg.gradle.java.home` first, then your ~/.gradle/gradle.properties,
    then the project's; a refusal still lists every JDK found, so the options
    can name one that works. Otherwise the first candidate in range wins, an
    explicit `java_home` first.

    The range is what quern chooses within, not what it refuses. Both ends
    are approximations: the floor follows the Gradle version though the
    Android Gradle plugin sets its own (a Gradle 8 + AGP 7 build runs on Java
    11), and above the ceiling it depends on the build -- measured on Gradle
    7.6.4 with Java 21, a Groovy build script failed ("Unsupported class file
    major version 65") and the same project in Kotlin DSL built. So a JDK in
    range is preferred; one newer is used when it is all there is; and a JDK
    the caller or the project chose is used wherever it falls, with `warning`
    saying so. Gradle's own refusal, if it comes, is what `parse` reports.
    """
    env = dict(os.environ if env is None else env)
    home = home or env.get("HOME") or str(Path.home())
    java_home = os.path.expanduser(java_home) if java_home else None
    jdks = found if found is not None else candidates(java_home=java_home, env=env, home=home)
    want = _range(minimum, maximum)
    user_props = gradle_user_home(gradle_args, env, home) / "gradle.properties"
    # Labels name the file: "change it in ..." must point where it is, and
    # -g or GRADLE_USER_HOME can put the user's file anywhere.
    project_props = project_root / "gradle.properties"
    forced_sources = [(java_home_override(gradle_args), "-Dorg.gradle.java.home in gradle_args"),
                      (gradle_property(user_props, "org.gradle.java.home"),
                       f"your Gradle user home's gradle.properties ({user_props})"),
                      (gradle_property(project_props, "org.gradle.java.home"),
                       f"the project's gradle.properties ({project_props})")]
    for forced, label in forced_sources:
        if not forced:
            continue
        jdk = read_jdk(os.path.expanduser(forced), f"org.gradle.java.home, from {label}")
        if jdk is None:
            return Choice(jdk=None, candidates=jdks, forced_by=label,
                          problem=f"org.gradle.java.home ({label}) is {forced}, which is not "
                                  f"a JDK quern can read; Gradle uses it whatever JAVA_HOME "
                                  f"says, and this build needs {want}")
        notes = [_outside(jdk, minimum, maximum)]
        if java_home and os.path.realpath(java_home) != os.path.realpath(jdk.home):
            notes.append(f"the java_home you passed ({java_home}) is not used: Gradle runs "
                         f"on org.gradle.java.home ({label}) whatever JAVA_HOME says; pass "
                         f"-Dorg.gradle.java.home in gradle_args to choose instead")
        return Choice(jdk=jdk, candidates=jdks, forced_by=label,
                      warning="; ".join(n for n in notes if n))
    if java_home:
        given = next((j for j in jdks if j.source.startswith("the java_home")), None)
        if given is None:
            return Choice(jdk=None, candidates=jdks,
                          problem=f"the java_home you passed, {java_home}, is not a JDK")
        return Choice(jdk=given, candidates=jdks, warning=_outside(given, minimum, maximum))
    usable = [j for j in jdks if _fits(j, minimum, maximum)]
    if usable:
        return Choice(jdk=usable[0], candidates=jdks)
    newer = sorted((j for j in jdks if j.major >= minimum), key=lambda j: j.major)
    if newer:
        # The closest above the ceiling: the likeliest to work.
        return Choice(jdk=newer[0], candidates=jdks,
                      warning=_outside(newer[0], minimum, maximum))
    have = ", ".join(f"Java {j.version}" for j in jdks) or "none"
    return Choice(jdk=None, candidates=jdks,
                  problem=f"no JDK in the range this build needs ({want}) was found "
                          f"(found: {have})")
