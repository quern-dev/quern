"""`build_and_install` for a Gradle project (#347).

The iOS route builds an Xcode scheme; this one runs `:module:assemble<Variant>`
with the project's own Gradle wrapper, installs the APK on each Android device
in parallel, and records the build the way `record_android_build` does, so its
crashes can be symbolicated without a second call.

Problems with the machine rather than the code -- no suitable JDK, no Android
SDK, missing SDK packages -- come back as `environment`, each with what was
found and the ways to fix it, for the agent to act on or to put to the user.
Nothing here installs anything or edits a file.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

from fastapi import HTTPException

from server.device import build_records, gradle
from server.device import jdk as jdk_mod
from server.device.adb import AdbTimeout
from server.models import (
    BuildDiagnostic,
    BuildRecord,
    BuildResult,
    DeviceError,
    DeviceState,
    DeviceType,
)

logger = logging.getLogger(__name__)


async def build_and_install(controller, body, *, env: dict[str, str] | None = None) -> dict:
    """The Android half of `build_and_install`; returns the response fields.

    `env` is the environment Gradle and the JDK search see, the daemon's own
    by default; tests pass one so no lookup reaches the developer's machine.
    """
    variant = (body.variant or "").strip()
    if variant.lower().startswith("assemble") and len(variant) > len("assemble"):
        # The likeliest mistake: the task, not the variant. Gradle would be
        # asked for assembleAssembleStagingDebug and say only "not found".
        bare = variant[len("assemble"):]
        raise HTTPException(
            status_code=400,
            detail=f"variant is the variant's name, not its task: pass "
                   f"variant=\"{bare[:1].lower()}{bare[1:]}\"")
    if {"-m", "--dry-run"} & set(body.gradle_args or []):
        # Gradle prints every task SKIPPED and BUILD SUCCESSFUL: what is on
        # disk would be installed as though this run had built it.
        raise HTTPException(status_code=400,
                            detail="gradle_args asks for a dry run (-m / --dry-run), which builds "
                                   "nothing to install")
    try:
        project = gradle.find_project(body.project_path, body.module)
    except gradle.GradleProjectError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    serials = await _android_targets(controller, body)

    # The machine first: a build that cannot start should say why before
    # Gradle is asked, in words an agent can act on.
    env = dict(os.environ if env is None else env)
    home = env.get("HOME") or str(Path.home())
    minimum, maximum, why_range = gradle.java_range(project)
    choice = await asyncio.to_thread(jdk_mod.choose, project.root, java_home=body.java_home,
                                     env=env, home=home, minimum=minimum, maximum=maximum,
                                     gradle_args=body.gradle_args)
    sdk, sdk_source = gradle.android_sdk(project, env, home)
    environment = []
    if choice.jdk is None:
        problem = gradle.jdk_problem(choice, minimum, maximum)
        problem.summary += f" ({why_range})"
        environment.append(problem)
    if sdk is None:
        environment.append(gradle.sdk_problem(project))
    criteria = gradle.daemon_jvm_version(project)
    used = {
        # With Daemon JVM criteria this JDK only starts Gradle, which runs the
        # build on a Java `criteria` it finds itself: said, not implied.
        "java": (f"Java {choice.jdk.version} at {choice.jdk.home} ({choice.jdk.source})"
                 + (f", starting Gradle, which builds on the Java {criteria} the project's "
                    f"Daemon JVM criteria ask for" if criteria else "")
                 + (f". Warning: {choice.warning}" if choice.warning else "")
                 if choice.jdk else None),
        "android_sdk": f"{sdk} ({sdk_source})" if sdk else None,
    }
    if environment:
        return _not_built(serials, environment, used, "the environment is not ready to build")

    args = list(body.gradle_args or [])
    if not any(a.startswith("-Pandroid.builder.sdkDownload") for a in args):
        # The Android Gradle plugin downloads missing SDK packages itself once
        # their licences are accepted: an install on the user's machine nobody
        # chose. Off, a missing package is an environment problem to decide on.
        args.append("-Pandroid.builder.sdkDownload=false")
    gradle_env = gradle.build_env(env, choice.jdk, sdk)
    variant = await _check_variant(project, variant, gradle_env, args)

    task = gradle.assemble_task(project, variant)
    logger.info("Building %s with %s (%s)", project.root, task, used["java"])
    progress = gradle.BuildProgress(project=str(project.root), task=task)
    gradle.ACTIVE[id(progress)] = progress
    try:
        code, output = await gradle.run(project, task, gradle_env, args, progress=progress)
    except TimeoutError:
        result = BuildResult(errors=[BuildDiagnostic(
            message=f"the build did not finish within {gradle.BUILD_TIMEOUT // 60} minutes")])
        result.summary = result.generate_summary()
        return {**_not_built(serials, [], used, "the build timed out"), "build_android": result}
    except OSError as e:
        fix = (f"make it executable: chmod +x {project.wrapper}" if isinstance(e, PermissionError)
               else f"check the first line of {project.wrapper}: a CRLF line ending, or an "
                    f"interpreter that is not installed, reads as a missing file")
        return _not_built(serials, [gradle.EnvironmentProblem(
            kind="gradle_wrapper", summary=f"{project.wrapper} could not be run: {e}",
            options=[fix])], used, "the Gradle wrapper could not be run")
    finally:
        gradle.ACTIVE.pop(id(progress), None)
    ran_on = criteria or (choice.jdk.major if choice.jdk else None)
    quiet = gradle.quiet_logging(project, args, jdk_mod.gradle_user_home(args, env, home))
    result, environment = gradle.parse(code, output, project, choice.candidates, ran_on=ran_on,
                                       forced_by=choice.forced_by, quiet=quiet)
    if not result.succeeded:
        return {**_not_built(serials, environment, used, "the build failed"),
                "build_android": result}

    did, variants = gradle.packaged(output, project, variant)
    try:
        metadata = await asyncio.to_thread(build_records._android_metadata,
                                           project.module_dir, variant, after_build=True)
    except build_records.AndroidBuildNotFound as e:
        # Gradle said it built; the outputs say otherwise. Said, not guessed.
        return {**_not_built(serials, [], used, f"the build left no APK: {e}"),
                "build_android": result}
    if gradle.unsigned(metadata):
        # Android installs only signed APKs: said here, by the variant, not
        # as INSTALL_PARSE_FAILED_NO_CERTIFICATES once per device.
        apk = metadata["elements"][0].get("outputFile")
        return {**_not_built(serials, [], used,
                             f"the {variant} APK ({apk}) is unsigned: the variant has no signing "
                             f"config, and Android installs only signed APKs. Build a debug "
                             f"variant, which this machine's debug key signs, or give the "
                             f"{variant} build type a signingConfig (a change to the project)"),
                "build_android": result}
    if did is False:
        # Outputs for this variant exist, and this run did not make them: an
        # orphan from before the project gained flavours, or the wrong module.
        # Installing it would be the success that did not happen.
        packaged_here = (f"; it packaged {', '.join(variants)}" if variants
                         else "; it packaged no variant of this module")
        return {**_not_built(serials, [], used,
                             f"Gradle did not package {variant} in this run, so the APK under "
                             f"{metadata['_dir']} is from an earlier build{packaged_here}"),
                "build_android": result}

    record_task = asyncio.create_task(_record(project, str(metadata.get("variantName")
                                                           or variant)))
    try:
        devices = await asyncio.gather(*(
            _install_one(controller, s, metadata, body.uninstall_on_signature_mismatch,
                         body.allow_downgrade)
            for s in serials))
    except BaseException:
        record_task.cancel()
        raise
    record = await _finish_record(record_task, devices)
    return {
        "build_android": result,
        "devices": list(devices),
        "all_installed": bool(devices) and all(d.installed for d in devices),
        "build_records": [record],
        "environment": [],
        **used,
    }


async def _check_variant(project: gradle.GradleProject, variant: str, env: dict[str, str],
                         args: list[str]) -> str:
    """The variant to build, in its own spelling, or a 400 that lists them.

    Checked before the build, because Gradle's own answers come late or
    wrong: `debug` on a flavoured project assembles every flavour's debug
    (two minutes, measured, before anything could say so), and an
    abbreviation (`stagingDeb`) builds stagingDebug while the outputs are
    looked for under the name given. Listing costs about a second on a warm
    daemon and is then cached for as long as the build files are unchanged.
    """
    cached = gradle.cached_variants(project)
    if variant and cached and cached.find(variant):
        return cached.find(variant)
    key = gradle._build_files_key(project)
    known, why = await gradle.list_variants(project, env, args)
    if known is not None:
        gradle.remember_variants(project, key, known)
    where = f":{project.module}"
    if known is None:
        if not variant:
            raise HTTPException(status_code=400,
                                detail=f"variant is required for a Gradle project, and the "
                                       f"variants of {where} could not be listed: {why}")
        # The check could not run: said in the log, and the build decides.
        # Refusing on a listing that failed would block builds that work.
        logger.warning("Could not list the variants of %s %s: %s", project.root, where, why)
        return variant
    listed = ", ".join(known.names)
    if not variant:
        raise HTTPException(status_code=400,
                            detail=f"variant is required for a Gradle project. Variants of "
                                   f"{where}: {listed}")
    if own := known.find(variant):
        return own
    if group := known.group(variant):
        raise HTTPException(status_code=400,
                            detail=f"{variant!r} is not one variant of {where} but several: "
                                   f"pass one of {', '.join(group)}")
    raise HTTPException(status_code=400,
                        detail=f"{where} has no variant {variant!r}. Variants: {listed}")


async def _android_targets(controller, body) -> list[str]:
    raw = [u for u in [body.udid, *(body.udids or [])] if u]
    raw = list(dict.fromkeys(raw))
    if not raw:
        return [await _default_android(controller)]
    try:
        resolved = [await controller.resolve_udid(u) for u in raw]
    except DeviceError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    not_android = [u for u in resolved if not controller._is_android(u)]
    if not_android:
        raise HTTPException(
            status_code=400,
            detail=f"{', '.join(not_android)} is not an Android device or emulator quern knows; "
                   f"a Gradle project installs on Android only. List devices first, or check "
                   f"the id.")
    return resolved


async def _default_android(controller) -> str:
    """The device to install on when none is named: the active one if it is
    Android, else the one booted Android device or emulator.

    Not simply the active device: that is often an iOS simulator, and
    refusing it as "not an Android device ... check the id" answers a
    question about an id the caller never gave.

    The active device is read, not resolved: `resolve_udid(None)` with
    nothing active goes to the device pool, which picks an iPhone simulator,
    boots it if none is booted, and makes it active -- an Android build
    booting an iOS simulator. And it must be booted to be chosen: an
    emulator that is not running fails only at install, after the build.
    """
    active = controller._active_udid
    try:
        devices = await controller.list_devices()
    except (DeviceError, OSError) as e:
        raise HTTPException(status_code=400, detail=f"no device named, and the device list "
                                                    f"could not be read: {e}") from e
    booted = [d.udid for d in devices if d.state == DeviceState.BOOTED
              and d.device_type in (DeviceType.ANDROID_EMULATOR, DeviceType.ANDROID_DEVICE)]
    if active in booted:
        return await controller.resolve_udid(active)
    if len(booted) == 1:
        return await controller.resolve_udid(booted[0])
    if booted:
        raise HTTPException(status_code=400,
                            detail=f"no device named, and {len(booted)} Android devices are "
                                   f"booted: pass udid or udids from {', '.join(booted)}")
    raise HTTPException(status_code=400,
                        detail="no device named, and no Android device or emulator is booted"
                               + (f" (the active device, {active}, is not one)"
                                  if active else "")
                               + ": boot one, or pass its udid")


def _not_built(serials: list[str], environment: list, used: dict, why: str) -> dict:
    from server.api.build_app import DeviceInstallResult

    return {
        "build_android": None,
        "devices": [DeviceInstallResult(udid=s, installed=False, error=f"not installed: {why}")
                    for s in serials],
        "all_installed": False,
        "build_records": [],
        "environment": environment,
        **used,
    }


async def _install_one(controller, serial: str, metadata: dict, may_uninstall: bool,
                       allow_downgrade: bool = False):
    from server.api.build_app import DeviceInstallResult

    asked = ""
    try:
        abis = await controller.adb.supported_abis(serial)
    except DeviceError as e:
        abis, asked = [], f"; the device's ABIs could not be read ({e})"
    apk = gradle.pick_apk(metadata, abis)
    if apk is not None and not apk.is_file():
        return DeviceInstallResult(
            udid=serial, installed=False,
            error=f"the APK the build's output-metadata.json names, {apk}, is not there")
    if apk is None:
        # "Could not ask" is not "nothing fits": with ABI splits and no
        # universal APK, an unreadable device reads the same as a mismatch.
        return DeviceInstallResult(
            udid=serial, installed=False,
            error=f"no APK in the build fits this device (ABIs: "
                  f"{', '.join(abis) or 'unknown'}){asked}")
    # Once the uninstall ran, every outcome says the data is gone -- an
    # exception from the reinstall included, which is the likeliest to drop it.
    gone = ""
    try:
        code, out, err = await controller.adb.install_apk_result(
            serial, str(apk), allow_downgrade=allow_downgrade)
        ok, reason, message = gradle.install_outcome(code, out, err)
        if not ok and reason == "INSTALL_FAILED_UPDATE_INCOMPATIBLE" and may_uninstall:
            package = str(metadata.get("applicationId") or "")
            if not package:
                return DeviceInstallResult(udid=serial, installed=False, app_path=str(apk),
                                           error=f"{message}; and the package to uninstall "
                                                 f"is not in the build's metadata")
            logger.info("Uninstalling %s from %s: its signature does not match", package, serial)
            try:
                ucode, uout, uerr = await controller.adb.uninstall_result(serial, package)
            except AdbTimeout as e:
                return DeviceInstallResult(
                    udid=serial, installed=False, app_path=str(apk),
                    error=f"{message}; the uninstall you allowed did not finish ({e}), so "
                          f"{package} and its data may already be gone: check with list_apps "
                          f"before trying again")
            # Read like install: older adb exits 0 on `Failure [DELETE_FAILED_...]`.
            if not (ucode == 0 and "Success" in uout):
                said = (uerr.strip() or uout.strip())[-300:] or f"exit {ucode}"
                return DeviceInstallResult(
                    udid=serial, installed=False, app_path=str(apk),
                    error=f"{message}; the uninstall you allowed failed too: {said}")
            gone = (f"uninstalled {package} first, which erased its data, because its "
                    f"signature did not match")
            code, out, err = await controller.adb.install_apk_result(
                serial, str(apk), allow_downgrade=allow_downgrade)
            ok, reason, message = gradle.install_outcome(code, out, err)
            if reason == "INSTALL_FAILED_UPDATE_INCOMPATIBLE":
                # Not "pass uninstall_on_signature_mismatch=true": they did.
                message = (f"{reason}: still refused for its signature after the uninstall; "
                           f"another user on the device (a work profile) may still have it")
            message = gone if ok else f"{gone}; then the install failed: {message}"
    except (DeviceError, OSError, TimeoutError) as e:
        why = str(e) or type(e).__name__
        # A timeout is not "could not run": adb was killed mid-install, and
        # the device may have finished regardless.
        what = (f"adb install did not finish ({why}); it may still have installed: check with "
                f"list_apps" if isinstance(e, AdbTimeout | TimeoutError)
                else f"adb install could not run: {why}")
        return DeviceInstallResult(udid=serial, installed=False, app_path=str(apk),
                                   error=(f"{gone}; then " if gone else "") + what)
    return DeviceInstallResult(udid=serial, installed=ok, app_path=str(apk),
                               error=None if ok else message,
                               note=message if ok and message else None)


async def _record(project: gradle.GradleProject, variant: str) -> BuildRecord:
    try:
        return await build_records.record_android_build(project.module_dir, variant,
                                                        just_built=True)
    except Exception as e:  # noqa: BLE001 -- an installed build must not fail on its record
        logger.exception("Recording the Android build failed")
        return BuildRecord(build_id="", created_at=datetime.now(UTC),
                           project_path=str(project.module_dir), scheme=variant,
                           configuration=variant, platform="android", app_path="",
                           error=f"the build could not be recorded: {type(e).__name__}: {e}")


async def _finish_record(task: asyncio.Task, devices) -> BuildRecord:
    record = await task
    record.installed_on = [d.udid for d in devices if d.installed]
    if record.build_id and not record.error:
        try:
            await asyncio.to_thread(build_records.save, record)
        except Exception as e:  # noqa: BLE001 -- the install succeeded
            record.notes.append(f"where it was installed could not be saved: {e}")
        try:
            removed = await asyncio.to_thread(build_records.prune)
        except Exception as e:  # noqa: BLE001
            logger.exception("Build record retention failed")
            record.notes.append(f"retention of older build records failed: {e}")
        else:
            if removed:
                logger.info("Build record retention removed: %s", "; ".join(removed))
    return record
