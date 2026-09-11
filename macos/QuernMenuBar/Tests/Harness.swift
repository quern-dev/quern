// A test harness small enough to read in one sitting.
//
// Not XCTest: that means SwiftPM, which wants Sources/<Target>/ and would force
// splitting these sources into a library and an executable -- and `build.sh`
// and `release-menubar.sh` both depend on the current layout. Restructuring the
// two scripts that produce a signed artifact is a poor trade for test
// discovery. `run-tests.sh` compiles this against the same sources the app is
// built from, minus its `main.swift`.

import Foundation

enum Harness {
    nonisolated(unsafe) static var failures: [String] = []
    nonisolated(unsafe) static var passed = 0
    nonisolated(unsafe) static var current = ""

    static func test(_ name: String, _ body: () -> Void) {
        current = name
        let before = failures.count
        body()
        if failures.count == before {
            passed += 1
            print("  ok   \(name)")
        }
    }

    static func expect(_ condition: Bool, _ message: String,
                       file: StaticString = #file, line: UInt = #line) {
        guard !condition else { return }
        let where_ = "\(URL(fileURLWithPath: "\(file)").lastPathComponent):\(line)"
        failures.append("\(current): \(message)  [\(where_)]")
        print("  FAIL \(current): \(message)  [\(where_)]")
    }

    static func expect<T: Equatable>(_ actual: T, _ expected: T, _ what: String,
                                     file: StaticString = #file, line: UInt = #line) {
        expect(actual == expected, "\(what): expected \(expected), got \(actual)",
               file: file, line: line)
    }

    static func report() -> Int32 {
        print("")
        if failures.isEmpty {
            print("\(passed) passed")
            return 0
        }
        print("\(passed) passed, \(failures.count) failed:")
        for f in failures { print("  - \(f)") }
        return 1
    }
}
