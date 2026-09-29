import Foundation

/// Everything the tool needs, parsed from argv.
public struct Options: Equatable {
    public enum Source: Equatable {
        case simulator(udid: String)
        case device(match: String)
    }

    public var source: Source
    public var codec: StreamPipeline.Codec
    public var servePort: UInt16?
    public var bindAll: Bool
    public var recordPath: String?
    public var fps: Double
    public var maxDimension: Int
    public var quality: Double
    public var bitrate: Int

    public static let defaultPort: UInt16 = 8422
}

public enum OptionsError: Error, Equatable, CustomStringConvertible {
    case help
    case list
    case noSource
    case conflictingSources
    case missingValue(String)
    case badValue(flag: String, value: String)
    case unknownFlag(String)
    case noOutput

    public var description: String {
        switch self {
        case .help: return "help requested"
        case .list: return "list requested"
        case .noSource:
            return "no source given: pass --sim-udid <UDID> or --device <name>"
        case .conflictingSources:
            return "--sim-udid and --device are mutually exclusive"
        case .missingValue(let flag): return "\(flag) needs a value"
        case .badValue(let flag, let value): return "\(flag): cannot parse \"\(value)\""
        case .unknownFlag(let flag): return "unknown flag \(flag)"
        case .noOutput:
            return "no output: pass --serve and/or --record"
        }
    }
}

public enum OptionsParser {
    private static let valueFlags: Set<String> = [
        "--sim-udid", "--device", "--serve", "--fps", "--max-dim",
        "--quality", "--bitrate", "--record",
    ]
    private static let boolFlags: Set<String> = [
        "--bind-all", "--h264", "--list", "--help", "-h",
    ]

    public static func parse(_ args: [String]) throws -> Options {
        var values: [String: String] = [:]
        var flags: Set<String> = []

        var i = 0
        while i < args.count {
            let arg = args[i]
            if valueFlags.contains(arg) {
                guard i + 1 < args.count else { throw OptionsError.missingValue(arg) }
                let next = args[i + 1]
                // A flag where a value belongs is a typo, not an empty value.
                // Checked by membership and not by a "--" prefix: `-h` is a
                // recognised flag that the prefix test waves through, so
                // `--record -h` stored "-h" as the output path and help was
                // never reached.
                guard !valueFlags.contains(next), !boolFlags.contains(next),
                      !next.hasPrefix("--") else {
                    throw OptionsError.missingValue(arg)
                }
                values[arg] = next
                i += 2
            } else if boolFlags.contains(arg) {
                flags.insert(arg)
                i += 1
            } else {
                // The spike ignored anything it did not recognise, so `--fp 30`
                // silently ran at the default rate. Refusing is kinder.
                throw OptionsError.unknownFlag(arg)
            }
        }

        if flags.contains("--help") || flags.contains("-h") { throw OptionsError.help }
        if flags.contains("--list") { throw OptionsError.list }

        let source: Options.Source
        switch (values["--sim-udid"], values["--device"]) {
        case (.some, .some): throw OptionsError.conflictingSources
        case (.some(let udid), nil): source = .simulator(udid: udid)
        case (nil, .some(let match)): source = .device(match: match)
        case (nil, nil): throw OptionsError.noSource
        }

        func number<T: LosslessStringConvertible>(_ flag: String, default def: T) throws -> T {
            guard let raw = values[flag] else { return def }
            guard let parsed = T(raw) else {
                throw OptionsError.badValue(flag: flag, value: raw)
            }
            return parsed
        }

        var servePort: UInt16?
        if let raw = values["--serve"] {
            guard let parsed = UInt16(raw), parsed > 0 else {
                throw OptionsError.badValue(flag: "--serve", value: raw)
            }
            servePort = parsed
        }

        // Bounded here as well as clamped in the encoder: argv is where a
        // value like 1e308 comes from, and the parser can say which flag was
        // wrong while the encoder can only defend itself.
        let fps: Double = try number("--fps", default: 15.0)
        guard fps.isFinite, fps > 0, fps <= 240 else {
            throw OptionsError.badValue(flag: "--fps", value: values["--fps"] ?? "\(fps)")
        }

        // Documented as 0...1, and passed straight to VTSessionSetProperty,
        // whose result nothing checks -- so an out-of-range value is accepted
        // in silence and simply does not do what was asked.
        let quality: Double = try number("--quality", default: 0.6)
        guard quality.isFinite, (0...1).contains(quality) else {
            throw OptionsError.badValue(
                flag: "--quality", value: values["--quality"] ?? "\(quality)"
            )
        }

        // Same shape as --quality above, and the same reasoning: these go to
        // VTSessionSetProperty or to a bind, and nothing reads the result, so
        // a nonsense value is accepted in silence and quietly does something
        // else. --bitrate 0 ran at VideoToolbox's own default; --max-dim -1
        // meant "native", which is documented as 0; --serve 0 bound an
        // ephemeral port and then advertised http://127.0.0.1:0/.
        let bitrate: Int = try number("--bitrate", default: 2_000_000)
        guard bitrate > 0 else {
            throw OptionsError.badValue(
                flag: "--bitrate", value: values["--bitrate"] ?? "\(bitrate)"
            )
        }
        let maxDimension: Int = try number("--max-dim", default: 900)
        guard maxDimension >= 0 else {
            throw OptionsError.badValue(
                flag: "--max-dim", value: values["--max-dim"] ?? "\(maxDimension)"
            )
        }

        let record = values["--record"]
        let options = Options(
            source: source,
            // Recording implies H.264: an .mp4 wants a video codec, and a
            // caller should not have to know that.
            codec: (flags.contains("--h264") || record != nil) ? .h264 : .mjpeg,
            servePort: servePort,
            bindAll: flags.contains("--bind-all"),
            recordPath: record,
            fps: fps,
            maxDimension: maxDimension,
            quality: quality,
            bitrate: bitrate
        )

        // A headless producer with no sink would capture frames and discard
        // them. Windows are the preview app's job, not this tool's.
        guard options.servePort != nil || options.recordPath != nil else {
            throw OptionsError.noOutput
        }
        return options
    }
}
