import Foundation

/// Decides which frames to encode, to hold an output rate below the source's.
///
/// Deadline-based, not delay-based, and that distinction is the whole reason
/// this is a type with tests rather than two lines inline.
///
/// The obvious implementation — "encode if at least 1/fps has passed since the
/// last encode" — resets its reference point to each frame's *arrival*, which
/// throws away the remainder. A source faster than the target then quantises
/// to an integer division of it. Against a 60 fps source, a 30 fps target
/// skips every second frame and delivers ~24, not 30. That shipped in the
/// spike and cost real time to find, because nothing about it looks wrong.
///
/// Advancing a deadline by whole intervals preserves the phase and averages
/// out to the requested rate.
public struct FrameThrottle {
    private let interval: Double
    private var nextDeadline: Double?

    /// - Parameter fps: target output rate. Values <= 0 disable throttling.
    public init(fps: Double) {
        self.interval = fps > 0 ? 1.0 / fps : 0
    }

    /// - Parameter now: monotonic seconds. Same clock across calls.
    /// - Returns: whether this frame should be encoded.
    public mutating func shouldEncode(at now: Double) -> Bool {
        guard interval > 0 else { return true }

        guard let deadline = nextDeadline else {
            nextDeadline = now + interval
            return true
        }
        // A thousandth of an interval of slack. The deadline accumulates by
        // repeated addition while frame times are computed independently, so
        // over a long session the two drift by float rounding. The tolerance
        // is far above that noise and far below anything perceptible, and it
        // keeps a source running at exactly the target rate from shedding
        // frames to comparisons that land a hair short.
        guard now >= deadline - interval * 0.001 else { return false }

        nextDeadline = deadline + interval
        // Fallen more than a whole interval behind: the source went quiet, or
        // stalled. Resync instead of emitting a catch-up burst of stale
        // frames, which is what a naive `+= interval` loop would do after an
        // idle screen produced nothing for a minute.
        if nextDeadline! <= now {
            nextDeadline = now + interval
        }
        return true
    }
}
