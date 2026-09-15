import CoreGraphics
import CoreVideo
import Foundation
import ImageIO
import IOSurface

/// A synthetic BGRA IOSurface with drawn content.
///
/// The point of splitting capture from encoding: the encoders can be tested
/// against a surface conjured out of nothing, with no simulator booted and no
/// device plugged in. `swift test` needs neither.
enum TestSurface {
    static func make(width: Int, height: Int) -> IOSurface? {
        let props: [IOSurfacePropertyKey: Any] = [
            .width: width,
            .height: height,
            .bytesPerElement: 4,
            .pixelFormat: kCVPixelFormatType_32BGRA,
        ]
        guard let surface = IOSurface(properties: props) else { return nil }

        surface.lock(options: [], seed: nil)
        defer { surface.unlock(options: [], seed: nil) }
        guard let space = CGColorSpace(name: CGColorSpace.sRGB),
              let ctx = CGContext(
                  data: surface.baseAddress,
                  width: width, height: height,
                  bitsPerComponent: 8,
                  bytesPerRow: surface.bytesPerRow,
                  space: space,
                  bitmapInfo: CGBitmapInfo.byteOrder32Little.rawValue
                      | CGImageAlphaInfo.premultipliedFirst.rawValue
              ) else { return nil }

        // Detail rather than flat colour: a uniform surface compresses to
        // almost nothing and would hide a broken encode behind a plausible
        // byte count.
        ctx.setFillColor(CGColor(red: 0.1, green: 0.1, blue: 0.15, alpha: 1))
        ctx.fill(CGRect(x: 0, y: 0, width: width, height: height))
        for i in stride(from: 0, to: width, by: 17) {
            let shade = Double(i % 255) / 255.0
            ctx.setFillColor(CGColor(red: shade, green: 1 - shade, blue: 0.5, alpha: 1))
            ctx.fill(CGRect(x: i, y: (i * 7) % max(height - 20, 1), width: 11, height: 19))
        }
        return surface
    }

    /// Decoded pixel dimensions of an encoded image, for asserting on output.
    static func imageSize(_ data: Data) -> (width: Int, height: Int)? {
        guard let src = CGImageSourceCreateWithData(data as CFData, nil),
              let props = CGImageSourceCopyPropertiesAtIndex(src, 0, nil) as? [CFString: Any],
              let w = props[kCGImagePropertyPixelWidth] as? Int,
              let h = props[kCGImagePropertyPixelHeight] as? Int else { return nil }
        return (w, h)
    }

    static func isJPEG(_ data: Data) -> Bool {
        data.count > 3 && data[data.startIndex] == 0xFF
            && data[data.startIndex + 1] == 0xD8 && data[data.startIndex + 2] == 0xFF
    }
}
