import CoreVideo
import Foundation
import IOSurface
import VideoToolbox

/// One built-in panel of a simulator with more than one -- iPhone Duo (#538).
public struct Panel: Equatable {
    public let screenID: UInt32
    /// "primary" for the cover, "primary-1" for the inner panel.
    public let deviceName: String
}

/// Which panel to show, and the fixed frame every panel is shown in.
///
/// A Duo has two built-in panels and three more framebuffers besides, and
/// only one panel is lit at a time. The capture took the largest framebuffer,
/// which on a Duo is always the inner panel: a recording of a folded Duo was
/// black from start to finish (measured, 41s folded-open-closed, the cover
/// never once in it).
///
/// The lit panel changes under a running stream when the device folds, and
/// the panels differ in size (cover 1398x2034, inner 2007x2853). A movie's
/// format is fixed by its first frame and a live viewer does not survive a
/// resolution change, so every panel is letterboxed into one frame the size
/// of the largest, in its native orientation -- the inner panel sideways, as
/// a landscape app on any iPhone records.
public enum PanelChoice {
    /// The panel to show: the one lit, if exactly one is. Neither or both
    /// (mid-fold, or a lit panel showing a black screen): the one shown last,
    /// else the cover, which a Duo boots on. The same rule as sim-bridge's
    /// `choosePanel`, so a recording shows what a screenshot does.
    public static func choose(_ panels: [Panel], lit: [Bool], last: UInt32?) -> Int? {
        guard !panels.isEmpty, panels.count == lit.count else { return nil }
        let litIndices = lit.indices.filter { lit[$0] }
        if litIndices.count == 1 { return litIndices[0] }
        if let last, let i = panels.firstIndex(where: { $0.screenID == last }) { return i }
        return panels.firstIndex(where: { $0.deviceName == "primary" }) ?? 0
    }

    /// The frame every panel is letterboxed into: wide enough for the widest
    /// and tall enough for the tallest. Even, as H.264 needs.
    public static func canvasSize(_ sizes: [(width: Int, height: Int)]) -> (width: Int, height: Int)? {
        guard let w = sizes.map(\.width).max(), let h = sizes.map(\.height).max(),
              w > 0, h > 0 else { return nil }
        return (w + w % 2, h + h % 2)
    }
}

/// Whether a framebuffer shows anything: 256 samples on a grid, any channel
/// above 8. An unlit panel is exactly black; a lit one showing a dark
/// interface still has a status bar. The same test as sim-bridge's
/// `surfaceIsLit`, which #536 measured on a Duo.
public func surfaceIsLit(_ surface: IOSurface) -> Bool {
    guard IOSurfaceGetBytesPerElement(surface) == 4 else { return true }
    IOSurfaceLock(surface, .readOnly, nil)
    defer { IOSurfaceUnlock(surface, .readOnly, nil) }
    let w = IOSurfaceGetWidth(surface), h = IOSurfaceGetHeight(surface)
    let bpr = IOSurfaceGetBytesPerRow(surface)
    guard w > 0, h > 0 else { return false }
    let base = IOSurfaceGetBaseAddress(surface).assumingMemoryBound(to: UInt8.self)
    for gy in 0..<16 {
        for gx in 0..<16 {
            let px = base + ((gy * 2 + 1) * h / 32) * bpr + ((gx * 2 + 1) * w / 32) * 4
            if px[0] > 8 || px[1] > 8 || px[2] > 8 { return true }
        }
    }
    return false
}

/// Letterboxes a surface into a fixed-size one, on the GPU.
///
/// Only a device with more than one panel pays for this: a phone keeps the
/// zero-copy path, its framebuffer handed straight to the encoder.
public final class PanelCanvas {
    public let width: Int
    public let height: Int
    private var session: VTPixelTransferSession?
    private var pool: CVPixelBufferPool?
    /// The buffer handed out last, held until the next compose: the encoders
    /// wrap the surface rather than copying it, so the pool must not recycle
    /// it while one is still reading.
    private var lastOut: CVPixelBuffer?

    public init?(width: Int, height: Int) {
        guard width > 0, height > 0 else { return nil }
        self.width = width
        self.height = height
        let attrs: [CFString: Any] = [
            kCVPixelBufferPixelFormatTypeKey: kCVPixelFormatType_32BGRA,
            kCVPixelBufferWidthKey: width,
            kCVPixelBufferHeightKey: height,
            kCVPixelBufferIOSurfacePropertiesKey: [:] as [CFString: Any],
        ]
        var created: CVPixelBufferPool?
        guard CVPixelBufferPoolCreate(nil, nil, attrs as CFDictionary, &created) == kCVReturnSuccess,
              let created else { return nil }
        pool = created
        var transfer: VTPixelTransferSession?
        guard VTPixelTransferSessionCreate(allocator: nil, pixelTransferSessionOut: &transfer) == noErr,
              let transfer else { return nil }
        VTSessionSetProperty(transfer, key: kVTPixelTransferPropertyKey_ScalingMode,
                             value: kVTScalingMode_Letterbox)
        session = transfer
    }

    deinit {
        if let session { VTPixelTransferSessionInvalidate(session) }
    }

    /// `surface` letterboxed into the canvas, or nil if it could not be.
    /// Called from one queue at a time.
    public func compose(_ surface: IOSurface) -> IOSurface? {
        guard let session, let pool else { return nil }
        var src: Unmanaged<CVPixelBuffer>?
        guard CVPixelBufferCreateWithIOSurface(nil, surface, nil, &src) == kCVReturnSuccess,
              let source = src?.takeRetainedValue() else { return nil }
        var dst: CVPixelBuffer?
        guard CVPixelBufferPoolCreatePixelBuffer(nil, pool, &dst) == kCVReturnSuccess,
              let dest = dst else { return nil }
        // Letterboxing paints the bars black as well as drawing the panel
        // (measured: a destination filled white came back with black bars),
        // so a pooled buffer that last held the larger panel shows none of it
        // around the smaller one. The fold-back test holds VideoToolbox to it.
        guard VTPixelTransferSessionTransferImage(session, from: source, to: dest) == noErr,
              let out = CVPixelBufferGetIOSurface(dest)?.takeUnretainedValue() else { return nil }
        lastOut = dest
        return out
    }
}
