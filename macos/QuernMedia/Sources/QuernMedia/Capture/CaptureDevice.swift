import AVFoundation
import CoreMedia
import CoreMediaIO
import Foundation
import IOSurface

public enum CaptureDeviceError: Error, CustomStringConvertible {
    case notFound(String)
    case cannotAddInput
    case cannotAddOutput
    case noIOSurface

    public var description: String {
        switch self {
        case .notFound(let m): return "no connected capture device matching \"\(m)\""
        case .cannotAddInput: return "session rejected the device input"
        case .cannotAddOutput: return "session rejected the video data output"
        case .noIOSurface: return "sample buffers are not IOSurface-backed"
        }
    }
}

/// Discovery for iOS devices exposed as CoreMediaIO capture devices.
public enum CaptureDeviceDiscovery {
    /// macOS hides iOS screen-capture devices until this opt-in is set.
    public static func enableScreenCaptureDevices() {
        var prop = CMIOObjectPropertyAddress(
            mSelector: CMIOObjectPropertySelector(kCMIOHardwarePropertyAllowScreenCaptureDevices),
            mScope: CMIOObjectPropertyScope(kCMIOObjectPropertyScopeGlobal),
            mElement: CMIOObjectPropertyElement(kCMIOObjectPropertyElementMain)
        )
        var allow: UInt32 = 1
        CMIOObjectSetPropertyData(
            CMIOObjectID(kCMIOObjectSystemObject), &prop, 0, nil,
            UInt32(MemoryLayout<UInt32>.size), &allow
        )
    }

    public static let screenCaptureModelID = "iOS Device"

    /// Only devices whose model ID marks them as iOS screens.
    ///
    /// The model ID is the whole assertion. Accepting any muxed external
    /// device would be looser: muxed means "audio and video together", which
    /// an unrelated capture device can also be.
    public static func devices() -> [AVCaptureDevice] {
        let muxed = AVCaptureDevice.DiscoverySession(
            deviceTypes: [.external], mediaType: .muxed, position: .unspecified
        ).devices
        let video = AVCaptureDevice.DiscoverySession(
            deviceTypes: [.external], mediaType: .video, position: .unspecified
        ).devices

        var seen = Set<String>()
        return (muxed + video).filter { device in
            guard seen.insert(device.uniqueID).inserted else { return false }
            return device.modelID == screenCaptureModelID
        }
    }

    /// Devices appear asynchronously after the opt-in, and only while a run
    /// loop is turning. Blocking the main thread with `Thread.sleep` starves
    /// the notifications and finds nothing — which reads exactly like "no
    /// device connected".
    public static func waitForDevices(timeout: TimeInterval = 3.0) -> [AVCaptureDevice] {
        enableScreenCaptureDevices()
        let deadline = Date().addingTimeInterval(timeout)
        var found = devices()
        while found.isEmpty, Date() < deadline {
            RunLoop.current.run(until: Date().addingTimeInterval(0.1))
            found = devices()
        }
        return found
    }
}

/// Frames from a USB-connected iOS device.
///
/// The shipping preview attaches only an `AVCaptureVideoPreviewLayer`, which
/// draws to screen and hands back no pixels — fine for a window, useless for
/// streaming or recording. Adding an `AVCaptureVideoDataOutput` to the *same*
/// session yields sample buffers without disturbing the layer: one session,
/// one device, two consumers.
///
/// `videoSettings` is pinned to 32BGRA so these buffers match the simulator's
/// framebuffer layout byte for byte and both sources can share one encoder.
/// Left alone a DAL device negotiates YUV, which the encoders would read as
/// BGRA and render as garbage.
public final class CaptureDeviceSource: NSObject, FrameSource,
                                        AVCaptureVideoDataOutputSampleBufferDelegate {
    /// Exposed so a preview window can share this session rather than opening
    /// a second one on the same device.
    public let session = AVCaptureSession()

    private let device: AVCaptureDevice
    private let output = AVCaptureVideoDataOutput()
    private let queue = DispatchQueue(label: "quern.media.capture", qos: .userInteractive)
    private let onFrame: (CapturedFrame) -> Void
    private var describedFormat = false
    private var warnedNoSurface = false

    public init(device: AVCaptureDevice, onFrame: @escaping (CapturedFrame) -> Void) {
        self.device = device
        self.onFrame = onFrame
        super.init()
    }

    public func start() throws {
        session.beginConfiguration()
        do {
            let input = try AVCaptureDeviceInput(device: device)
            guard session.canAddInput(input) else {
                session.commitConfiguration()
                throw CaptureDeviceError.cannotAddInput
            }
            session.addInput(input)
        } catch let error as CaptureDeviceError {
            throw error
        } catch {
            session.commitConfiguration()
            throw error
        }

        output.videoSettings = [
            kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA
        ]
        // Drop rather than queue. A slow consumer must cost frames, not
        // latency — a preview that is correct but ten seconds late is worse
        // than one that skips.
        output.alwaysDiscardsLateVideoFrames = true
        output.setSampleBufferDelegate(self, queue: queue)

        guard session.canAddOutput(output) else {
            session.commitConfiguration()
            throw CaptureDeviceError.cannotAddOutput
        }
        session.addOutput(output)
        session.commitConfiguration()
        session.startRunning()
    }

    public func stop() {
        session.stopRunning()
        output.setSampleBufferDelegate(nil, queue: nil)
    }

    public func captureOutput(
        _ output: AVCaptureOutput,
        didOutput sampleBuffer: CMSampleBuffer,
        from connection: AVCaptureConnection
    ) {
        guard let pixels = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }

        if !describedFormat {
            describedFormat = true
            MediaLog.log("[capture] \(CVPixelBufferGetWidth(pixels))"
                + "x\(CVPixelBufferGetHeight(pixels)) px, "
                + "format \(FourCC.string(CVPixelBufferGetPixelFormatType(pixels)))")
        }

        // Why one encoder can serve both sources: a CVPixelBuffer from this
        // output is IOSurface-backed, so it arrives as the same type the
        // simulator framebuffer hands over.
        guard let ref = CVPixelBufferGetIOSurface(pixels)?.takeUnretainedValue() else {
            if !warnedNoSurface {
                warnedNoSurface = true
                MediaLog.log("[capture] \(CaptureDeviceError.noIOSurface)")
            }
            return
        }
        // A real presentation timestamp, already on the host clock — strictly
        // better than stamping on arrival, so it is marked as reported.
        onFrame(CapturedFrame(
            surface: unsafeBitCast(ref, to: IOSurface.self),
            time: CMSampleBufferGetPresentationTimeStamp(sampleBuffer),
            timeAccuracy: .reported
        ))
    }
}

public enum FourCC {
    public static func string(_ value: OSType) -> String {
        let bytes = [
            UInt8((value >> 24) & 0xFF), UInt8((value >> 16) & 0xFF),
            UInt8((value >> 8) & 0xFF), UInt8(value & 0xFF),
        ]
        return String(bytes: bytes, encoding: .ascii) ?? "\(value)"
    }
}
