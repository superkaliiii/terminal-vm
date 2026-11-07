import CommonCrypto
import Darwin
import Foundation

/// The parts of the RFB (VNC) protocol that host control needs: VNC password authentication,
/// X11 keysyms for text and chords, and client message encoding. RFC 6143.
public enum RFB {
    /// DES-encrypts the 16-byte server challenge with the password, bit-reversed per byte as VNC requires.
    public static func vncAuthResponse(challenge: [UInt8], password: String) -> [UInt8] {
        precondition(challenge.count == 16, "VNC challenges are 16 bytes")
        var key = Array(password.utf8.prefix(8))
        key += [UInt8](repeating: 0, count: 8 - key.count)
        key = key.map { byte in (0..<8).reduce(UInt8(0)) { $0 | ((byte >> $1) & 1) << (7 - $1) } }
        var output = [UInt8](repeating: 0, count: 16)
        var moved = 0
        let status = CCCrypt(CCOperation(kCCEncrypt), CCAlgorithm(kCCAlgorithmDES), CCOptions(kCCOptionECBMode),
                             key, key.count, nil, challenge, challenge.count, &output, output.count, &moved)
        precondition(status == CCCryptorStatus(kCCSuccess) && moved == 16, "DES is always available")
        return output
    }

    /// Keysym for one Unicode scalar: Latin-1 maps directly, everything else uses the Unicode keysym range.
    public static func keysym(for scalar: Unicode.Scalar) -> UInt32 {
        switch scalar.value {
        case 0x0A, 0x0D: return 0xff0d
        case 0x09: return 0xff09
        case 0x08: return 0xff08
        case 0x20...0x7e, 0xa0...0xff: return scalar.value
        default: return 0x0100_0000 + scalar.value
        }
    }

    public static let namedKeys: [String: UInt32] = {
        var keys: [String: UInt32] = [
            "return": 0xff0d, "enter": 0xff0d, "tab": 0xff09, "space": 0x20, "backspace": 0xff08, "delete": 0xffff,
            "escape": 0xff1b, "esc": 0xff1b, "home": 0xff50, "end": 0xff57, "pageup": 0xff55, "pagedown": 0xff56,
            "left": 0xff51, "up": 0xff52, "right": 0xff53, "down": 0xff54
        ]
        for n in 1...12 { keys["f\(n)"] = 0xffbe + UInt32(n - 1) }
        return keys
    }()
    /// Command is Super; this is how VNC clients send the Mac Command key.
    public static let modifierKeys: [String: UInt32] = [
        "cmd": 0xffeb, "command": 0xffeb, "ctrl": 0xffe3, "control": 0xffe3, "alt": 0xffe9, "option": 0xffe9, "shift": 0xffe1
    ]

    /// Keysyms of a chord such as `cmd+shift+a`, modifiers first. Press in order, release in reverse.
    public static func chord(_ text: String) throws -> [UInt32] {
        let parts = text.lowercased().split(separator: "+", omittingEmptySubsequences: false).map(String.init)
        guard let key = parts.last, !key.isEmpty else { throw Failure("Unknown key: \(text). Example: cmd+space, return, ctrl+a") }
        var keysyms = try parts.dropLast().map { modifier -> UInt32 in
            guard let keysym = modifierKeys[modifier] else { throw Failure("Unknown modifier: \(modifier)") }
            return keysym
        }
        if let named = namedKeys[key] { keysyms.append(named) }
        else if key.unicodeScalars.count == 1, let scalar = key.unicodeScalars.first { keysyms.append(keysym(for: scalar)) }
        else { throw Failure("Unknown key: \(text). Example: cmd+space, return, ctrl+a") }
        return keysyms
    }

    public static func pointerEvent(buttons: UInt8, x: Int, y: Int) -> [UInt8] { [5, buttons] + be16(x) + be16(y) }
    public static func keyEvent(_ keysym: UInt32, down: Bool) -> [UInt8] { [4, down ? 1 : 0, 0, 0] + be32(keysym) }
    /// 32-bit little-endian true colour with red at bit 16: bytes B, G, R, X in memory, which is 32BGRA.
    static let pixelFormat: [UInt8] = [32, 24, 0, 1, 0, 255, 0, 255, 0, 255, 16, 8, 0, 0, 0, 0]

    static func be16(_ value: Int) -> [UInt8] { [UInt8((value >> 8) & 0xff), UInt8(value & 0xff)] }
    static func be32(_ value: UInt32) -> [UInt8] { [UInt8(value >> 24), UInt8((value >> 16) & 0xff), UInt8((value >> 8) & 0xff), UInt8(value & 0xff)] }
    static func number<C: Collection>(_ bytes: C) -> Int where C.Element == UInt8 { bytes.reduce(0) { $0 << 8 | Int($1) } }
}

/// A minimal RFB 3.8 client: VNC or no authentication, Raw encoding, desktop resizes.
/// A background thread keeps a local copy of the framebuffer up to date.
public final class RFBClient: @unchecked Sendable {
    public struct Frame { public let pixels: Data; public let width: Int; public let height: Int; public let sequence: Int }
    private let fd: Int32
    private let lock = NSLock()
    private let writeLock = NSLock()
    private var pixels: Data
    private var width: Int
    private var height: Int
    private var sequence = 0
    private var failure: Error?

    public init(port: UInt16, password: String) throws {
        let fd = socket(AF_INET, SOCK_STREAM, 0)
        guard fd >= 0 else { throw Failure("Cannot create a socket") }
        var one: Int32 = 1
        setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &one, socklen_t(MemoryLayout<Int32>.size))
        setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, socklen_t(MemoryLayout<Int32>.size))
        var address = sockaddr_in()
        address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        address.sin_family = sa_family_t(AF_INET)
        address.sin_port = port.bigEndian
        address.sin_addr.s_addr = inet_addr("127.0.0.1")
        let connected = withUnsafePointer(to: &address) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { connect(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size)) }
        }
        do {
            guard connected == 0 else { throw Failure("Cannot connect to the VM display server: \(String(cString: strerror(errno)))") }
            var timeout = timeval(tv_sec: 10, tv_usec: 0)
            setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, socklen_t(MemoryLayout<timeval>.size))
            (width, height) = try Self.handshake(fd, password: password)
            timeout = timeval(tv_sec: 0, tv_usec: 0)  // the reader thread waits for updates indefinitely
            setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, socklen_t(MemoryLayout<timeval>.size))
            try Self.writeAll(fd, [0, 0, 0, 0] + RFB.pixelFormat)                                       // SetPixelFormat
            try Self.writeAll(fd, [2, 0] + RFB.be16(2) + RFB.be32(0) + RFB.be32(UInt32(bitPattern: -223)))  // Raw, DesktopSize
        } catch { Darwin.close(fd); throw error }
        self.fd = fd
        pixels = Data(count: width * height * 4)
        try requestUpdate(incremental: false)
        Thread { [self] in readLoop() }.start()
    }

    /// The latest complete framebuffer, or nil before the first update.
    public var current: Frame? {
        lock.withLock { sequence > 0 ? Frame(pixels: pixels, width: width, height: height, sequence: sequence) : nil }
    }
    public func check() throws { if let failure = lock.withLock({ failure }) { throw failure } }
    public func pointer(buttons: UInt8, x: Int, y: Int) throws { try send(RFB.pointerEvent(buttons: buttons, x: x, y: y)) }
    public func key(_ keysym: UInt32, down: Bool) throws { try send(RFB.keyEvent(keysym, down: down)) }
    public func close() { shutdown(fd, SHUT_RDWR); Darwin.close(fd) }

    private func send(_ bytes: [UInt8]) throws {
        try check()
        try writeLock.withLock { try Self.writeAll(fd, bytes) }
    }
    private func requestUpdate(incremental: Bool) throws {
        let (w, h) = lock.withLock { (width, height) }
        try send([3, incremental ? 1 : 0] + RFB.be16(0) + RFB.be16(0) + RFB.be16(w) + RFB.be16(h))
    }

    private func readLoop() {
        do {
            while true {
                switch try Self.readExact(fd, 1)[0] {
                case 0:  // FramebufferUpdate
                    let rectangles = RFB.number(try Self.readExact(fd, 3).suffix(2))
                    for _ in 0..<rectangles {
                        let header = try Self.readExact(fd, 12)
                        let x = RFB.number(header[0..<2]), y = RFB.number(header[2..<4])
                        let w = RFB.number(header[4..<6]), h = RFB.number(header[6..<8])
                        let encoding = Int32(bitPattern: UInt32(RFB.number(header[8..<12])))
                        if encoding == -223 {
                            lock.withLock { width = w; height = h; pixels = Data(count: w * h * 4) }
                            continue
                        }
                        guard encoding == 0 else { throw Failure("Unsupported VNC encoding \(encoding)") }
                        let data = try Self.readExact(fd, w * h * 4)
                        try lock.withLock {
                            guard x + w <= width, y + h <= height else { throw Failure("VNC rectangle outside the display") }
                            let stride = width
                            pixels.withUnsafeMutableBytes { destination in
                                data.withUnsafeBytes { source in
                                    for row in 0..<h {
                                        memcpy(destination.baseAddress! + ((y + row) * stride + x) * 4, source.baseAddress! + row * w * 4, w * 4)
                                    }
                                }
                            }
                        }
                    }
                    lock.withLock { sequence += 1 }
                    try requestUpdate(incremental: true)
                case 1:  // SetColourMapEntries (unused with true colour)
                    let header = try Self.readExact(fd, 5)
                    _ = try Self.readExact(fd, RFB.number(header[3..<5]) * 6)
                case 2: break  // Bell
                case 3:  // ServerCutText: ignored, the host clipboard is never touched
                    let header = try Self.readExact(fd, 7)
                    _ = try Self.readExact(fd, RFB.number(header[3..<7]))
                case let type: throw Failure("Unknown VNC message \(type)")
                }
            }
        } catch { lock.withLock { failure = error } }
    }

    static func handshake(_ fd: Int32, password: String) throws -> (Int, Int) {
        guard try readExact(fd, 12).starts(with: Array("RFB ".utf8)) else { throw Failure("The VM display server is not a VNC server") }
        try writeAll(fd, Array("RFB 003.008\n".utf8))
        let count = Int(try readExact(fd, 1)[0])
        guard count > 0 else { throw Failure("VNC server refused the connection: \(try reason(fd))") }
        let types = try readExact(fd, count)
        if types.contains(2) {
            try writeAll(fd, [2])
            try writeAll(fd, RFB.vncAuthResponse(challenge: try readExact(fd, 16), password: password))
        } else if types.contains(1) {
            try writeAll(fd, [1])
        } else { throw Failure("VNC server offers no supported security type (\(types))") }
        guard RFB.number(try readExact(fd, 4)) == 0 else { throw Failure("VNC authentication failed: \(try reason(fd))") }
        try writeAll(fd, [1])  // ClientInit: shared
        let server = try readExact(fd, 24)
        _ = try readExact(fd, RFB.number(server[20..<24]))  // desktop name
        return (RFB.number(server[0..<2]), RFB.number(server[2..<4]))
    }
    static func reason(_ fd: Int32) throws -> String { String(decoding: try readExact(fd, RFB.number(try readExact(fd, 4))), as: UTF8.self) }
    static func readExact(_ fd: Int32, _ count: Int) throws -> [UInt8] {
        var buffer = [UInt8](repeating: 0, count: count)
        var offset = 0
        while offset < count {
            let n = buffer.withUnsafeMutableBytes { Darwin.read(fd, $0.baseAddress! + offset, count - offset) }
            if n < 0 && errno == EINTR { continue }
            guard n > 0 else { throw Failure(n == 0 ? "VNC connection closed" : "VNC read failed: \(String(cString: strerror(errno)))") }
            offset += n
        }
        return buffer
    }
    static func writeAll(_ fd: Int32, _ bytes: [UInt8]) throws {
        var offset = 0
        while offset < bytes.count {
            let n = bytes.withUnsafeBytes { Darwin.write(fd, $0.baseAddress! + offset, bytes.count - offset) }
            if n < 0 && errno == EINTR { continue }
            guard n > 0 else { throw Failure("VNC write failed: \(String(cString: strerror(errno)))") }
            offset += n
        }
    }
}
