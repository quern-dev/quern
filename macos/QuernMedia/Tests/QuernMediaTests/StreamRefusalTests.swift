import Foundation
import Testing
@testable import QuernMedia

@Test("a 503 says what quern-media said: the simulator's state")
func a503CarriesTheState() {
    #expect(StreamRefusal.describe(code: 503, body: Data("the simulator is shutdown\n".utf8))
        == "the simulator is shutdown")
}

@Test("an empty 503 claims only what is known")
func anEmpty503IsNotAGuess() {
    // "the simulator is not running" would be a guess the server never made.
    #expect(StreamRefusal.describe(code: 503, body: Data()) == "quern-media answered HTTP 503")
}

@Test("a 409 names another simulator, and the window shows it without the UDIDs")
func a409NamesAnotherSimulator() {
    let full = StreamRefusal.describe(
        code: 409, body: Data("this stream is SIM-B, not SIM-A".utf8)
    )
    #expect(full == "this port is streaming another simulator now (this stream is SIM-B, not SIM-A)")
    #expect(StreamRefusal.short(full) == "this port is streaming another simulator now")
}

@Test("anything else gives the code and what came with it")
func otherCodesAreReported() {
    #expect(StreamRefusal.describe(code: 404, body: Data()) == "quern-media answered HTTP 404")
    #expect(StreamRefusal.describe(code: 500, body: Data(" boom ".utf8))
        == "quern-media answered HTTP 500: boom")
}

@Test("a reason with no parenthesis is shown whole")
func shortLeavesPlainReasons() {
    #expect(StreamRefusal.short("the simulator is shutdown") == "the simulator is shutdown")
}
