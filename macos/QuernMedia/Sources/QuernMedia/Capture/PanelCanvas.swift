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

/// How a panel's framebuffer is turned to show its interface upright.
///
/// A panel's framebuffer has one fixed shape -- the Duo's inner panel is
/// portrait, 2007x2853 -- while the interface on it turns: unfolded, the
/// inner panel's interface is landscape, so shown as captured it lay 90
/// degrees anticlockwise. `uiOrientation` says how the interface is turned,
/// as `take_screenshot` reads it (sim-bridge's `rotated`).
public enum PanelRotation: Equatable {
    case none, clockwise, anticlockwise, upsideDown

    /// From a display's `uiOrientation` (UIDeviceOrientation: 1 portrait,
    /// 2 upside down, 3 landscape left, 4 landscape right). Anything else
    /// is shown as captured.
    public init(uiOrientation: UInt32) {
        switch uiOrientation {
        case 2: self = .upsideDown
        case 3: self = .clockwise
        case 4: self = .anticlockwise
        default: self = .none
        }
    }

    public var turnsSideways: Bool { self == .clockwise || self == .anticlockwise }

    /// A width and height as they are once turned.
    public func apply(_ size: (width: Int, height: Int)) -> (width: Int, height: Int) {
        turnsSideways ? (size.height, size.width) : size
    }

    var videoToolbox: CFString {
        switch self {
        case .none: return kVTRotation_0
        case .clockwise: return kVTRotation_CW90
        case .anticlockwise: return kVTRotation_CCW90
        case .upsideDown: return kVTRotation_180
        }
    }
}

/// A multi-panel device's built-in panels and the one being shown: what
/// `SimulatorFramebuffer` decides per frame, pure apart from reading the
/// surfaces it is given, so a test drives it with synthetic ones.
public struct PanelTracker {
    /// In screenID order.
    public let panels: [Panel]
    /// The panel shown last. Survives a detach, since the device does.
    public private(set) var shown: UInt32?

    /// Nil unless there are two or more built-in panels. Whether a device
    /// has them is its device type's, read from the descriptors alone --
    /// never from whether their surfaces exist yet: they lag the booted state
    /// during boot, and a device judged single-panel then stayed on the
    /// largest framebuffer, the black inner panel, for the whole attach.
    public init?(panels found: [Panel?], shown: UInt32? = nil) {
        let real = found.compactMap { $0 }.sorted { $0.screenID < $1.screenID }
        guard real.count > 1 else { return nil }
        panels = real
        self.shown = shown
    }

    /// The panel to show now, given each panel's surface in `panels` order
    /// (nil where it has none yet), and whether that is a change. Nil when
    /// the chosen panel has no surface.
    public mutating func select(_ surfaces: [IOSurface?])
        -> (panel: Panel, index: Int, surface: IOSurface, changed: Bool)? {
        guard surfaces.count == panels.count else { return nil }
        let lit = surfaces.map { $0.map(surfaceIsLit) ?? false }
        guard let i = PanelChoice.choose(panels, lit: lit, last: shown),
              let surface = surfaces[i] else { return nil }
        let changed = panels[i].screenID != shown
        shown = panels[i].screenID
        return (panels[i], i, surface, changed)
    }

    /// The canvas for these surfaces, or nil while any panel has none yet:
    /// sized from some of them, it could be smaller than the panel that
    /// arrives later.
    ///
    /// Landscape, as long as the longest panel edge and as tall as the
    /// longest short edge: an unfolded Duo's inner panel turned upright,
    /// 2854x2008, which it fills. The folded cover, upright, sits inside with
    /// bars either side. Fixed, rather than following each panel's shape,
    /// because a movie's players show every frame at its first frame's size
    /// (measured: AVFoundation drew a 952x668 half of a movie squashed into
    /// its first 466x678); and from the panels' shapes rather than their
    /// orientations at start, since the unlit cover reports the inner
    /// panel's, so a canvas sized from orientations differed with whether
    /// capture started folded.
    public static func canvasSize(of surfaces: [IOSurface?]) -> (width: Int, height: Int)? {
        let present = surfaces.compactMap { $0 }
        guard present.count == surfaces.count else { return nil }
        let sizes = present.map { (IOSurfaceGetWidth($0), IOSurfaceGetHeight($0)) }
        let long = sizes.map { max($0.0, $0.1) }.max() ?? 0
        let short = sizes.map { min($0.0, $0.1) }.max() ?? 0
        return PanelChoice.canvasSize([(long, short)])
    }
}

/// Whether a framebuffer shows anything: 256 samples on a grid, any channel
/// above 8. An unlit panel is exactly black; a lit one showing a dark
/// interface still has a status bar. The same test as sim-bridge's
/// `surfaceIsLit`, which #536 measured on a Duo, with one addition: a
/// surface that is not 4 bytes a pixel cannot be read this way, and reads as
/// lit -- "cannot tell", which for two panels means neither is preferred and
/// the one shown last stays. A Duo's panels measure BGRA, 4 bytes.
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

/// Turns a surface upright, on the GPU, ahead of the letterbox.
public final class PanelTurner {
    private var rotator: VTPixelRotationSession?
    /// One pool per turned size: a panel turned sideways is its own shape.
    private var pools: [String: CVPixelBufferPool] = [:]

    public init() {}

    deinit {
        if let rotator { VTPixelRotationSessionInvalidate(rotator) }
    }

    /// `source` turned by `rotation`, or nil if it could not be. `.none` is
    /// not a turn: callers keep the surface they have. One queue at a time.
    public func turn(_ source: CVPixelBuffer, _ rotation: PanelRotation) -> CVPixelBuffer? {
        if rotator == nil {
            var made: VTPixelRotationSession?
            guard VTPixelRotationSessionCreate(nil, &made) == noErr, let made else { return nil }
            rotator = made
        }
        guard let rotator else { return nil }
        let size = rotation.apply((CVPixelBufferGetWidth(source), CVPixelBufferGetHeight(source)))
        let key = "\(size.width)x\(size.height)"
        if pools[key] == nil { pools[key] = makeBGRAPool(width: size.width, height: size.height) }
        guard let pool = pools[key], let dest = bufferFrom(pool),
              VTSessionSetProperty(rotator, key: kVTPixelRotationPropertyKey_Rotation,
                                   value: rotation.videoToolbox) == noErr,
              VTPixelRotationSessionRotateImage(rotator, source, dest) == noErr
        else { return nil }
        return dest
    }
}

func makeBGRAPool(width: Int, height: Int) -> CVPixelBufferPool? {
    let attrs: [CFString: Any] = [
        kCVPixelBufferPixelFormatTypeKey: kCVPixelFormatType_32BGRA,
        kCVPixelBufferWidthKey: width,
        kCVPixelBufferHeightKey: height,
        kCVPixelBufferIOSurfacePropertiesKey: [:] as [CFString: Any],
    ]
    var created: CVPixelBufferPool?
    guard CVPixelBufferPoolCreate(nil, nil, attrs as CFDictionary, &created) == kCVReturnSuccess
    else { return nil }
    return created
}

func bufferFrom(_ pool: CVPixelBufferPool) -> CVPixelBuffer? {
    var out: CVPixelBuffer?
    guard CVPixelBufferPoolCreatePixelBuffer(nil, pool, &out) == kCVReturnSuccess else { return nil }
    return out
}

func wrap(_ surface: IOSurface) -> CVPixelBuffer? {
    var src: Unmanaged<CVPixelBuffer>?
    guard CVPixelBufferCreateWithIOSurface(nil, surface, nil, &src) == kCVReturnSuccess else { return nil }
    return src?.takeRetainedValue()
}

/// Turns a surface upright and letterboxes it into a fixed-size one, on the
/// GPU. See `PanelTracker.canvasSize(of:)` for why the size is fixed.
///
/// Only a device with more than one panel pays for this: a phone keeps the
/// zero-copy path, its framebuffer handed straight to the encoder.
public final class PanelCanvas {
    public let width: Int
    public let height: Int
    private let transfer: VTPixelTransferSession
    private let pool: CVPixelBufferPool
    private let turner = PanelTurner()

    public init?(width: Int, height: Int) {
        guard width > 0, height > 0, let made = makeBGRAPool(width: width, height: height) else {
            return nil
        }
        pool = made
        var session: VTPixelTransferSession?
        guard VTPixelTransferSessionCreate(allocator: nil, pixelTransferSessionOut: &session) == noErr,
              let session else { return nil }
        // Checked: unset, the default scaling stretches every panel to the
        // canvas's shape, a distorted picture with nothing to say so.
        guard VTSessionSetProperty(session, key: kVTPixelTransferPropertyKey_ScalingMode,
                                   value: kVTScalingMode_Letterbox) == noErr else {
            VTPixelTransferSessionInvalidate(session)
            return nil
        }
        transfer = session
        self.width = width
        self.height = height
    }

    deinit {
        VTPixelTransferSessionInvalidate(transfer)
    }

    /// `surface` turned by `rotation` and letterboxed into the canvas, or nil
    /// if it could not be. Called from one queue at a time.
    public func compose(_ surface: IOSurface, rotation: PanelRotation = .none) -> IOSurface? {
        guard var source = wrap(surface) else { return nil }
        if rotation != .none {
            guard let turned = turner.turn(source, rotation) else { return nil }
            source = turned
        }
        guard let dest = bufferFrom(pool) else { return nil }
        // Letterboxing paints the bars black as well as drawing the panel
        // (measured: a destination filled white came back with black bars),
        // so a pooled buffer that last held the larger panel shows none of it
        // around the smaller one. The fold-back test holds VideoToolbox to it.
        guard VTPixelTransferSessionTransferImage(transfer, from: source, to: dest) == noErr,
              let out = CVPixelBufferGetIOSurface(dest)?.takeUnretainedValue() else { return nil }
        // Nothing holds `dest` past here, and nothing needs to: the encoders
        // wrap the surface in a CVPixelBuffer of their own, which raises its
        // IOSurface use count, and the pool does not hand back a surface in
        // use (measured: a held one was skipped and kept its pixels).
        return out
    }
}
