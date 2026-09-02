#!/usr/bin/env bash
# Start an emulator with settings that survive a long automation run.
#
# The default graphics path (gpu auto -> vulkan/lavapipe) has hung twice on this
# hardware, both times with the same signature: repeated VkInstance
# create/destroy, then
#
#   ERROR | detected a hanging thread 'QEMU2 main loop'. No response for 15017 ms
#
# and the process dies, taking the run with it. Software rendering has not
# reproduced it. That is a worthwhile trade for a fixture: the probe app draws
# almost nothing, so the GPU is not what makes the run slow.
#
# These are flags rather than edits to the AVD's config.ini on purpose — the
# same AVD stays fast for interactive use.
set -euo pipefail
AVD="${1:-Pixel_7}"

exec emulator -avd "$AVD" \
    -no-window \
    -no-audio \
    -no-boot-anim \
    -no-metrics \
    -gpu swiftshader_indirect \
    "${@:2}"
