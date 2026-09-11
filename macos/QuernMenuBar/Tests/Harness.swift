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
    nonisolated(unsafe) private static var checks = 0
    /// Cases entered. Counted separately from `failures`, which holds one entry
    /// per failed *expectation* -- a test with three failing assertions adds
    /// three. Comparing that sum against the expected number of tests reported
    /// "a case is not being reached" whenever a single test failed twice, which
    /// is a confident answer to the wrong question.
    nonisolated(unsafe) private static var ran = 0

    static func test(_ name: String, _ body: () -> Void) {
        current = name
        ran += 1
        let before = failures.count
        let checksBefore = checks
        body()
        // A body that asserted nothing is not a passing test. It printed "ok"
        // and counted, so a case abandoned mid-edit -- or one that returns
        // early past every expectation -- read as coverage that does not exist.
        if checks == checksBefore {
            failures.append("\(name): asserted nothing")
            print("  FAIL \(name): asserted nothing")
            return
        }
        if failures.count == before {
            passed += 1
            print("  ok   \(name)")
        }
    }

    static func expect(_ condition: Bool, _ message: String,
                       file: StaticString = #file, line: UInt = #line) {
        checks += 1
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

    /// `expected` is the number of cases that should have run.
    ///
    /// Without it, a suite that runs nothing prints "0 passed" and exits 0.
    /// Verified: commenting out both `all()` calls gave a green CI. There is no
    /// test discovery here -- `main.swift` calls each suite by hand and each
    /// suite lists its cases by hand -- so a case dropped during an edit is
    /// silently not run, and the count that remains still looks healthy.
    static func report(expected: Int) -> Int32 {
        print("")
        if !failures.isEmpty {
            print("\(passed) passed, \(failures.count) failed:")
            for f in failures { print("  - \(f)") }
            return 1
        }
        if ran != expected {
            print("expected \(expected) tests, ran \(ran) — a suite or a case "
                  + "is not being reached")
            return 1
        }
        print("\(passed) passed")
        return 0
    }
}
