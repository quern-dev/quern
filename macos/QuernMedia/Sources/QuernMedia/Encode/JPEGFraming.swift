import Foundation

// NOTHING MAY BE IMPORTED HERE BUT FOUNDATION, and nothing from the rest of
// this package may be referenced.
//
// `tools/ios-preview/main.swift` compiles this file directly, outside SwiftPM
// and without the rest of the module, so a reference to anything else here
// breaks `quern setup` for every user. It does not break `swift build`,
// `swift test`, or any test in this package — all of which keep this file
// company inside the module and stay green.
//
// Measured, not theorised: adding a single `MediaLog.log` call to the cap
// below left all 106 package tests passing and broke the app build. The
// "Compile ios-preview" step in CI exists to catch exactly that, and did.

/// Pulls complete JPEGs out of a byte stream by their markers.
///
/// The consumer half of MJPEG, and deliberately the *only* part of the preview
/// client that is not tied to URLSession or AppKit — it is pure bytes in,
/// frames out, so it can be tested. `tools/ios-preview/main.swift` compiles this
/// same file alongside itself rather than carrying a second copy; two parsers
/// that agree today are two parsers that disagree later.
///
/// Frames are found by scanning for start- and end-of-image markers rather
/// than by splitting on the multipart boundary. URLSession parses
/// `multipart/x-mixed-replace` itself and yields part bodies with the framing
/// already stripped, so a boundary parser finds nothing to split on; marker
/// scanning works whether the framing survives or not. FF bytes inside
/// entropy-coded data are byte-stuffed as FF00, so FFD9 appears only as a real
/// EOI — an embedded EXIF thumbnail would break that, and VideoToolbox does
/// not write one.
///
/// Getting this wrong is quiet: a parser that never completes a frame and an
/// idle screen that never sends one look exactly the same from outside.
public struct JPEGFraming {
    public static let startOfImage = Data([0xFF, 0xD8])
    public static let endOfImage = Data([0xFF, 0xD9])

    /// Past this, whatever is arriving is not something this can parse, and
    /// holding it only grows.
    public static let defaultMaxBuffer = 8 << 20

    private var buffer = Data()
    private let maxBuffer: Int

    /// How many times the cap has discarded a buffer. A parser that never
    /// completes a frame looks exactly like a screen that never sends one, so
    /// the discard is counted rather than merely happening.
    public private(set) var timesCapped = 0

    /// - Parameter maxBuffer: bytes to hold before giving up on finding a
    ///   frame. Clamped to at least one marker pair, because a cap smaller
    ///   than a frame starves every frame that spans a chunk — and the public
    ///   initialiser is reachable with 0 or a negative.
    public init(maxBuffer: Int = JPEGFraming.defaultMaxBuffer) {
        self.maxBuffer = max(maxBuffer, JPEGFraming.startOfImage.count
            + JPEGFraming.endOfImage.count)
    }

    /// Bytes held pending a complete frame. Exposed so a test can assert the
    /// parser is not accumulating forever.
    public var pendingBytes: Int { buffer.count }

    /// Appends received bytes and returns every complete JPEG now available.
    ///
    /// Returns them in arrival order. A chunk may complete none, one, or
    /// several — TCP does not respect frame boundaries in either direction.
    public mutating func append(_ data: Data) -> [Data] {
        buffer.append(data)
        var frames: [Data] = []

        while true {
            guard let start = buffer.range(of: Self.startOfImage) else {
                // Nothing that could begin a frame. Keep a trailing 0xFF: a
                // chunk can end between the two bytes of an SOI, and throwing
                // that byte away resynced at the *following* frame instead —
                // one whole frame lost, silently, per unlucky split.
                let keepTrailingMarkerByte = buffer.last == Self.startOfImage.first
                buffer.removeAll(keepingCapacity: true)
                if keepTrailingMarkerByte { buffer.append(Self.startOfImage.first!) }
                break
            }
            guard let end = buffer.range(
                of: Self.endOfImage, options: [], in: start.upperBound..<buffer.endIndex
            ) else {
                // A partial frame. Drop only what precedes it, so the leading
                // garbage before a stream's first SOI cannot accumulate.
                if start.lowerBound > buffer.startIndex {
                    buffer.removeSubrange(buffer.startIndex..<start.lowerBound)
                }
                break
            }
            frames.append(Data(buffer[start.lowerBound..<end.upperBound]))
            buffer.removeSubrange(buffer.startIndex..<end.upperBound)
        }

        if buffer.count > maxBuffer {
            timesCapped += 1
            buffer.removeAll(keepingCapacity: false)
        }
        return frames
    }
}
