import AVFoundation
import CoreGraphics
import Foundation
import ImageIO
import IOSurface
import VideoToolbox
import UniformTypeIdentifiers

func loadImage(_ path: String) -> CGImage? {
    guard let src = CGImageSourceCreateWithURL(URL(fileURLWithPath: path) as CFURL, nil) else { return nil }
    return CGImageSourceCreateImageAtIndex(src, 0, nil)
}

// IOSurface-backed BGRA buffer, same shape both paths consume.
func makeBuffer(_ image: CGImage) -> CVPixelBuffer? {
    let w = image.width, h = image.height
    var pb: CVPixelBuffer?
    let attrs: [CFString: Any] = [
        kCVPixelBufferIOSurfacePropertiesKey: [:] as CFDictionary,
        kCVPixelBufferPixelFormatTypeKey: kCVPixelFormatType_32BGRA,
        kCVPixelBufferWidthKey: w, kCVPixelBufferHeightKey: h,
    ]
    guard CVPixelBufferCreate(nil, w, h, kCVPixelFormatType_32BGRA,
                              attrs as CFDictionary, &pb) == kCVReturnSuccess,
          let pb else { return nil }
    CVPixelBufferLockBaseAddress(pb, [])
    defer { CVPixelBufferUnlockBaseAddress(pb, []) }
    guard let cs = CGColorSpace(name: CGColorSpace.sRGB),
          let ctx = CGContext(data: CVPixelBufferGetBaseAddress(pb), width: w, height: h,
                              bitsPerComponent: 8, bytesPerRow: CVPixelBufferGetBytesPerRow(pb),
                              space: cs,
                              bitmapInfo: CGBitmapInfo.byteOrder32Little.rawValue
                                  | CGImageAlphaInfo.premultipliedFirst.rawValue)
    else { return nil }
    ctx.draw(image, in: CGRect(x: 0, y: 0, width: w, height: h))
    return pb
}

// --- Path A: exactly what the spike does today -----------------------------
func jpegPath(_ surface: IOSurface, maxDim: Int, quality: Double) -> Int {
    IOSurfaceLock(surface, .readOnly, nil)
    let w = IOSurfaceGetWidth(surface), h = IOSurfaceGetHeight(surface)
    guard let cs = CGColorSpace(name: CGColorSpace.sRGB),
          let ctx = CGContext(data: IOSurfaceGetBaseAddress(surface), width: w, height: h,
                              bitsPerComponent: 8, bytesPerRow: IOSurfaceGetBytesPerRow(surface),
                              space: cs,
                              bitmapInfo: CGBitmapInfo.byteOrder32Little.rawValue
                                  | CGImageAlphaInfo.premultipliedFirst.rawValue),
          let full = ctx.makeImage() else {
        IOSurfaceUnlock(surface, .readOnly, nil); return 0
    }
    IOSurfaceUnlock(surface, .readOnly, nil)
    var image = full
    let longest = max(w, h)
    if longest > maxDim {
        let f = Double(maxDim) / Double(longest)
        let tw = Int(Double(w) * f), th = Int(Double(h) * f)
        if let sctx = CGContext(data: nil, width: tw, height: th, bitsPerComponent: 8,
                                bytesPerRow: 0, space: cs,
                                bitmapInfo: CGImageAlphaInfo.premultipliedFirst.rawValue
                                    | CGBitmapInfo.byteOrder32Little.rawValue) {
            sctx.interpolationQuality = .medium
            sctx.draw(full, in: CGRect(x: 0, y: 0, width: tw, height: th))
            if let s = sctx.makeImage() { image = s }
        }
    }
    let out = NSMutableData()
    guard let dest = CGImageDestinationCreateWithData(out, UTType.jpeg.identifier as CFString, 1, nil)
    else { return 0 }
    CGImageDestinationAddImage(dest, image,
        [kCGImageDestinationLossyCompressionQuality: quality] as CFDictionary)
    guard CGImageDestinationFinalize(dest) else { return 0 }
    return out.length
}

// --- Path B: VideoToolbox H.264 -------------------------------------------
final class VTEncoder {
    var session: VTCompressionSession?
    var bytes = 0
    var frames = 0
    var hardware = false

    init?(width: Int, height: Int, bitrate: Int) {
        let spec: [CFString: Any] = [
            kVTVideoEncoderSpecification_EnableHardwareAcceleratedVideoEncoder: true
        ]
        var s: VTCompressionSession?
        let st = VTCompressionSessionCreate(
            allocator: nil, width: Int32(width), height: Int32(height),
            codecType: kCMVideoCodecType_H264,
            encoderSpecification: spec as CFDictionary,
            imageBufferAttributes: nil, compressedDataAllocator: nil,
            outputCallback: nil, refcon: nil, compressionSessionOut: &s)
        guard st == noErr, let s else { return nil }
        session = s
        VTSessionSetProperty(s, key: kVTCompressionPropertyKey_RealTime, value: kCFBooleanTrue)
        VTSessionSetProperty(s, key: kVTCompressionPropertyKey_ProfileLevel,
                             value: kVTProfileLevel_H264_Baseline_AutoLevel)
        VTSessionSetProperty(s, key: kVTCompressionPropertyKey_AverageBitRate,
                             value: NSNumber(value: bitrate))
        VTSessionSetProperty(s, key: kVTCompressionPropertyKey_AllowFrameReordering,
                             value: kCFBooleanFalse)
        var hw: CFTypeRef?
        if VTSessionCopyProperty(s,
            key: kVTCompressionPropertyKey_UsingHardwareAcceleratedVideoEncoder,
            allocator: nil, valueOut: &hw) == noErr, let n = hw as? NSNumber {
            hardware = n.boolValue
        }
        VTCompressionSessionPrepareToEncodeFrames(s)
    }

    func encode(_ pb: CVPixelBuffer, pts: CMTime) {
        guard let s = session else { return }
        VTCompressionSessionEncodeFrame(
            s, imageBuffer: pb, presentationTimeStamp: pts,
            duration: .invalid, frameProperties: nil, infoFlagsOut: nil
        ) { [weak self] status, _, sample in
            guard status == noErr, let sample,
                  let bb = CMSampleBufferGetDataBuffer(sample) else { return }
            self?.bytes += CMBlockBufferGetDataLength(bb)
            self?.frames += 1
        }
    }

    func finish() {
        guard let s = session else { return }
        VTCompressionSessionCompleteFrames(s, untilPresentationTimeStamp: .invalid)
        VTCompressionSessionInvalidate(s)
    }
}

// --- Run -------------------------------------------------------------------
let path = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "/tmp/headless-check.png"
guard let image = loadImage(path), let pb = makeBuffer(image) else {
    print("could not load \(path)"); exit(1)
}
let w = CVPixelBufferGetWidth(pb), h = CVPixelBufferGetHeight(pb)
guard let surfRef = CVPixelBufferGetIOSurface(pb)?.takeUnretainedValue() else {
    print("no IOSurface"); exit(1)
}
let surface = unsafeBitCast(surfRef, to: IOSurface.self)
print("source frame: \(w)x\(h) BGRA, IOSurface-backed")

func cpuSeconds() -> Double {
    var u = rusage()
    getrusage(RUSAGE_SELF, &u)
    return Double(u.ru_utime.tv_sec) + Double(u.ru_utime.tv_usec)/1e6
         + Double(u.ru_stime.tv_sec) + Double(u.ru_stime.tv_usec)/1e6
}

let N = 60
var t = Date()
var c0 = cpuSeconds()
var jbytes = 0
for _ in 0..<N { jbytes += jpegPath(surface, maxDim: 900, quality: 0.6) }
let jms = Date().timeIntervalSince(t) * 1000 / Double(N)
let jcpu = (cpuSeconds() - c0) * 1000 / Double(N)
print(String(format: "JPEG   wall %6.2f ms/f | CPU %6.2f ms/f | %3.0f%% of wall is CPU | %4d KB/f",
             jms, jcpu, jcpu/jms*100, jbytes/N/1024))

guard let enc = VTEncoder(width: w, height: h, bitrate: 2_000_000) else {
    print("VTCompressionSession unavailable"); exit(1)
}
print("VideoToolbox hardware encoder: \(enc.hardware ? "YES" : "no (software fallback)")")
t = Date()
c0 = cpuSeconds()
for i in 0..<N {
    enc.encode(pb, pts: CMTime(value: CMTimeValue(i), timescale: 30))
}
enc.finish()
let vms = Date().timeIntervalSince(t) * 1000 / Double(N)
let vcpu = (cpuSeconds() - c0) * 1000 / Double(N)
print(String(format: "H.264  wall %6.2f ms/f | CPU %6.2f ms/f | %3.0f%% of wall is CPU | %4d KB/f",
             vms, vcpu, vcpu/vms*100, enc.bytes/max(enc.frames,1)/1024))
print(String(format: "\nCPU cost ratio: H.264 uses %.2fx the CPU of JPEG; size ratio %.2fx",
             vcpu/jcpu, Double(enc.bytes/max(enc.frames,1))/Double(jbytes/N)))
