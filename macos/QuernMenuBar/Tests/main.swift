// Test entry point. Compiled by run-tests.sh against the app's own sources,
// minus its main.swift -- two top-level files cannot coexist in one binary.

import Foundation

print("QuernMenuBar tests")
print("")
UpdaterTests.all()
LifecycleControllerTests.all()
SettingsModelTests.all()
MinimumDisplayTests.all()
// The count is deliberate. See Harness.report(expected:) -- without it, a suite
// that runs nothing exits 0.
exit(Harness.report(expected: 42))
