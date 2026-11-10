# Building and installing

*`quern://guide/devices/builds` -- part of `quern://guide/devices` in the Quern agent guide. Start from `quern://guide`.*

`build_and_install` builds an app and installs it on one or more devices: an Xcode scheme on iOS (simulators and physical iPhones) or a Gradle project on Android (`variant` names the build variant; omit it and the 400 lists them). A failed build always names a cause. `get_build_result` reads the most recent build's errors, warnings and test results, and `parse_build_output` parses a log from an `xcodebuild` you ran yourself. Builds are recorded so their crashes can be symbolicated; for an Android build made outside quern, call `record_android_build` right after building. On a client key, builds are the server owner's setting (`quern://guide/sharing`).
