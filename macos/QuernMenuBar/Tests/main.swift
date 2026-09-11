// Test entry point. Compiled by run-tests.sh against the app's own sources,
// minus its main.swift -- two top-level files cannot coexist in one binary.

import Foundation

print("QuernMenuBar tests")
print("")
UpdaterTests.all()
exit(Harness.report())
