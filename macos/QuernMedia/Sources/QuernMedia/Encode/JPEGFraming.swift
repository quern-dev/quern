import Foundation

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

    public init(maxBuffer: Int = JPEGFraming.defaultMaxBuffer) {
        self.maxBuffer = maxBuffer
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
                // Nothing that could begin a frame; none of it is worth keeping.
                buffer.removeAll(keepingCapacity: true)
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
            buffer.removeAll(keepingCapacity: false)
        }
        return frames
    }
}
