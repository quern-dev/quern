import AVFoundation
import AppKit
import IOSurface

/// A window showing raw frames from a `FrameSource`.
///
/// Not a `FrameSink`: a window wants pixels, not an encoded payload, so it
/// consumes `CapturedFrame` directly off the source. Handing the IOSurface to
/// a CALayer is the cheapest path there is — WindowServer composites it on
/// the GPU with no copy and no codec, which is why a local preview costs
/// almost nothing next to a stream.
public final class SurfacePreviewWindow: NSObject, NSWindowDelegate {
    public let window: NSWindow
    public var onClose: (() -> Void)?

    private let contentLayer = CALayer()
    private let viaCGImage: Bool

    // Frames arrive faster than AppKit needs to draw. Keep only the newest:
    // an older frame is worthless the moment a newer one exists, and queueing
    // every callback onto main is how a preview ends up seconds behind.
    private let lock = NSLock()
    private var pending: IOSurface?
    private var scheduled = false
    private var sized = false

    public init(title: String, viaCGImage: Bool = false) {
        self.viaCGImage = viaCGImage
        let screen = NSScreen.main?.frame ?? NSRect(x: 0, y: 0, width: 1920, height: 1080)
        let w: CGFloat = 400, h: CGFloat = 710
        window = NSWindow(
            contentRect: NSRect(x: 50, y: screen.height - h - 80, width: w, height: h),
            styleMask: [.titled, .closable, .resizable], backing: .buffered, defer: false
        )
        window.title = title
        window.isReleasedWhenClosed = false

        contentLayer.frame = NSRect(x: 0, y: 0, width: w, height: h)
        contentLayer.contentsGravity = .resizeAspect
        contentLayer.backgroundColor = NSColor.black.cgColor
        contentLayer.autoresizingMask = [.layerWidthSizable, .layerHeightSizable]

        let view = NSView(frame: NSRect(x: 0, y: 0, width: w, height: h))
        view.layer = contentLayer
        view.wantsLayer = true
        window.contentView = view

        super.init()
        window.delegate = self
        window.makeKeyAndOrderFront(nil)
    }

    public func present(_ surface: IOSurface) {
        lock.lock()
        pending = surface
        let already = scheduled
        scheduled = true
        lock.unlock()
        guard !already else { return }
        DispatchQueue.main.async { [weak self] in self?.drain() }
    }

    public func close() {
        window.delegate = nil
        window.close()
    }

    public func windowWillClose(_ notification: Notification) { onClose?() }

    private func drain() {
        lock.lock()
        let surface = pending
        pending = nil
        scheduled = false
        lock.unlock()
        guard let surface else { return }

        if !sized {
            sizeTo(surface)
            sized = true
        }
        // Implicit animation would cross-fade every frame.
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        contentLayer.contents = viaCGImage ? (cgImage(from: surface) as Any?) : (surface as Any?)
        CATransaction.commit()
    }

    private func sizeTo(_ surface: IOSurface) {
        let pw = CGFloat(IOSurfaceGetWidth(surface))
        let ph = CGFloat(IOSurfaceGetHeight(surface))
        guard pw > 0, ph > 0 else { return }
        let width: CGFloat = 400
        let size = NSSize(width: width, height: (width * ph / pw).rounded())
        window.setContentSize(size)
        contentLayer.frame = NSRect(origin: .zero, size: size)
    }

    /// Fallback path. `CALayer.contents` takes an IOSurface directly, which
    /// keeps the frame on the GPU; this copies it through a CGContext. Kept
    /// behind a flag so a format mismatch on some future runtime is a
    /// one-word change rather than a rewrite.
    private func cgImage(from surface: IOSurface) -> CGImage? {
        IOSurfaceLock(surface, .readOnly, nil)
        defer { IOSurfaceUnlock(surface, .readOnly, nil) }
        guard let space = CGColorSpace(name: CGColorSpace.sRGB),
              let ctx = CGContext(
                  data: IOSurfaceGetBaseAddress(surface),
                  width: IOSurfaceGetWidth(surface),
                  height: IOSurfaceGetHeight(surface),
                  bitsPerComponent: 8,
                  bytesPerRow: IOSurfaceGetBytesPerRow(surface),
                  space: space,
                  bitmapInfo: CGBitmapInfo.byteOrder32Little.rawValue
                      | CGImageAlphaInfo.premultipliedFirst.rawValue
              ) else { return nil }
        return ctx.makeImage()
    }
}

/// A window for a physical device, driven by AVFoundation.
///
/// Shares the capture session with `CaptureDeviceSource` rather than opening a
/// second one on the same device. Unlike the simulator window it pushes no
/// frames itself: letting AVFoundation drive the layer is strictly better than
/// re-rendering surfaces already handed to the encoder.
public final class CaptureSessionWindow: NSObject, NSWindowDelegate {
    public let window: NSWindow
    public var onClose: (() -> Void)?

    public init(title: String, session: AVCaptureSession) {
        let screen = NSScreen.main?.frame ?? NSRect(x: 0, y: 0, width: 1920, height: 1080)
        let w: CGFloat = 400, h: CGFloat = 710
        window = NSWindow(
            contentRect: NSRect(x: 50, y: screen.height - h - 80, width: w, height: h),
            styleMask: [.titled, .closable, .resizable], backing: .buffered, defer: false
        )
        window.title = title
        window.isReleasedWhenClosed = false

        let layer = AVCaptureVideoPreviewLayer(session: session)
        layer.videoGravity = .resizeAspect
        layer.frame = NSRect(x: 0, y: 0, width: w, height: h)
        layer.autoresizingMask = [.layerWidthSizable, .layerHeightSizable]

        let view = NSView(frame: NSRect(x: 0, y: 0, width: w, height: h))
        view.wantsLayer = true
        view.layer?.addSublayer(layer)
        window.contentView = view

        super.init()
        window.delegate = self
        window.makeKeyAndOrderFront(nil)
    }

    public func close() {
        window.delegate = nil
        window.close()
    }

    public func windowWillClose(_ notification: Notification) { onClose?() }
}
