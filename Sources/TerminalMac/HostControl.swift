import AppKit
import CoreGraphics
import ImageIO
import UniformTypeIdentifiers
import Virtualization
import TerminalMacCore

// Virtualization's built-in VNC server is private API (it is what `tart --vnc-experimental` uses).
// These @objc protocols describe the selectors we call; the classes are looked up at runtime,
// so a macOS release without them fails cleanly instead of crashing.
@objc private protocol VNCSecurityConfiguration: NSObjectProtocol {
    init(password: String)
}
@objc private protocol VNCServer: NSObjectProtocol {
    init(port: UInt16, queue: DispatchQueue, securityConfiguration: AnyObject)
    var virtualMachine: VZVirtualMachine? { get set }
    var port: UInt16 { get }
    func start()
    func stop()
}

enum HostDisplayServer {
    static var isAvailable: Bool {
        NSClassFromString("_VZVNCServer") != nil && NSClassFromString("_VZVNCAuthenticationSecurityConfiguration") != nil
    }
}

/// Host control: the VM process answers the guest helper's mailbox protocol itself, reading the
/// VM's display and sending input through Virtualization's VNC server. Nothing runs in the guest
/// and no guest permissions are needed; it also works at the login window and in Setup Assistant.
/// The VNC server takes a random per-boot password and only this process knows it.
@MainActor
final class HostControl {
    private let server: VNCServer
    private let client: RFBClient
    private let mailbox: Mailbox
    private let scale: Int
    private var loop: Task<Void, Never>?
    private var lastRequest = Date.distantPast

    init(vm: VZVirtualMachine, exchange: URL, scale: Int) async throws {
        guard HostDisplayServer.isAvailable,
              let securityClass = NSClassFromString("_VZVNCAuthenticationSecurityConfiguration"),
              let serverClass = NSClassFromString("_VZVNCServer") else {
            throw Failure("This macOS has no Virtualization display server, so host control is unavailable. Switch the VM to guest control: terminal-mac control NAME --guest")
        }
        let password = String((0..<8).map { _ in "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789".randomElement()! })
        let security = unsafeBitCast(securityClass, to: VNCSecurityConfiguration.Type.self).init(password: password)
        let server = unsafeBitCast(serverClass, to: VNCServer.Type.self).init(port: 0, queue: DispatchQueue.global(), securityConfiguration: security)
        server.virtualMachine = vm
        server.start()
        do {
            var port: UInt16 = 0
            for _ in 0..<100 where port == 0 {  // the port is assigned shortly after start()
                port = server.port
                if port == 0 { try await Task.sleep(nanoseconds: 50_000_000) }
            }
            guard port != 0 else { throw Failure("The VM display server did not start") }
            client = try RFBClient(port: port, password: password)
            mailbox = try Mailbox(exchange, create: true)
        } catch { server.stop(); throw error }
        self.server = server
        self.scale = max(1, scale)
    }

    func start() {
        loop = Task { @MainActor [weak self] in
            while let self, !Task.isCancelled {
                do {
                    for request in try self.mailbox.requests() {
                        self.lastRequest = Date()
                        // Live frames long-poll for changes; input must never wait behind them.
                        if request.arguments["format"] == "bgra-lz4" { Task { await self.handle(request) } }
                        else { await self.handle(request) }
                    }
                } catch { stderr(error.localizedDescription) }
                try? await Task.sleep(nanoseconds: Date().timeIntervalSince(self.lastRequest) < 3 ? 3_000_000 : 40_000_000)
            }
        }
    }

    func stop() { loop?.cancel(); client.close(); server.stop() }

    func handle(_ request: Request) async {
        do {
            try client.check()
            var values: [String: String] = [:]
            switch request.action {
            case "status":
                let size = try await pointSize()
                values = ["screenRecording": "true", "accessibility": "true", "control": "host",
                          "width": String(size.width), "height": String(size.height), "version": appVersion]
            case "screenshot" where request.arguments["format"] == "bgra-lz4":
                let since = Int(request.arguments["since"] ?? "") ?? -1
                let wait = Double(min(1000, max(0, Int(request.arguments["wait"] ?? "0") ?? 0))) / 1000
                let maxWidth = try number(request, "maxWidth", range: 16...7680), maxHeight = try number(request, "maxHeight", range: 16...4320)
                let size = try await pointSize()
                values = ["pointWidth": String(size.width), "pointHeight": String(size.height)]
                guard let frame = try await frame(after: since, timeout: wait) else { values["unchanged"] = "true"; break }
                // Never above the native pixels, so the viewer draws them 1:1 instead of resampling.
                let fit = min(1, Double(maxWidth) / Double(frame.width), Double(maxHeight) / Double(frame.height))
                let width = max(2, Int(Double(frame.width) * fit) & ~1), height = max(2, Int(Double(frame.height) * fit) & ~1)
                let scaled = try render(frame, width: width, height: height)
                let filename = "image-\(request.id.uuidString).bin"
                try mailbox.write(try (scaled.data as NSData).compressed(using: .lz4) as Data, named: filename)
                values.merge(["image": filename, "format": "bgra-lz4", "width": String(width), "height": String(height),
                              "bytesPerRow": String(scaled.bytesPerRow), "sequence": String(frame.sequence)]) { $1 }
            case "screenshot", "snapshot":
                guard let frame = try await frame(after: -1, timeout: 5) else { throw Failure("The VM display has not drawn yet") }
                // Point-sized, so image pixels are the coordinates the other actions take.
                let width = frame.width / scale, height = frame.height / scale
                guard let image = try render(frame, width: width, height: height).image else { throw Failure("Cannot encode the screenshot") }
                let png = NSMutableData()
                guard let destination = CGImageDestinationCreateWithData(png, UTType.png.identifier as CFString, 1, nil) else { throw Failure("Cannot encode the screenshot") }
                CGImageDestinationAddImage(destination, image, nil)
                guard CGImageDestinationFinalize(destination) else { throw Failure("Cannot encode the screenshot") }
                let filename = "image-\(request.id.uuidString).png"
                try mailbox.write(png as Data, named: filename)
                values = ["image": filename, "width": String(width), "height": String(height)]
            case "click":
                let point = try await coordinates(request)
                let mask: UInt8 = request.arguments["button"] == "right" ? 4 : 1
                let clicks = request.arguments["double"] == "true" ? 2 : 1
                try client.pointer(buttons: 0, x: point.x, y: point.y)
                for _ in 0..<clicks {
                    try client.pointer(buttons: mask, x: point.x, y: point.y)
                    try client.pointer(buttons: 0, x: point.x, y: point.y)
                }
            case "move":
                let point = try await coordinates(request)
                try client.pointer(buttons: 0, x: point.x, y: point.y)
            case "drag":
                let start = try await coordinates(request), end = try await coordinates(request, x: "x2", y: "y2")
                try client.pointer(buttons: 0, x: start.x, y: start.y)
                try client.pointer(buttons: 1, x: start.x, y: start.y)
                let steps = max(2, min(30, Int(hypot(Double(end.x - start.x), Double(end.y - start.y)) / Double(12 * scale))))
                for step in 1...steps {
                    let t = Double(step) / Double(steps)
                    try client.pointer(buttons: 1, x: start.x + Int(Double(end.x - start.x) * t), y: start.y + Int(Double(end.y - start.y) * t))
                    try await Task.sleep(nanoseconds: 8_000_000)
                }
                try client.pointer(buttons: 0, x: end.x, y: end.y)
            case "scroll":
                let dy = try number(request, "dy", range: -1000...1000)
                var dx = 0
                if request.arguments["dx"] != nil { dx = try number(request, "dx", range: -1000...1000) }
                var at = (x: 0, y: 0)
                if request.arguments["x"] != nil {
                    at = try await coordinates(request)
                    try client.pointer(buttons: 0, x: at.x, y: at.y)
                }
                // VNC scrolls in wheel notches (buttons 4-7); about 40 points per notch.
                for (delta, positive, negative) in [(dy, UInt8(8), UInt8(16)), (dx, UInt8(32), UInt8(64))] where delta != 0 {
                    for _ in 0..<max(1, abs(delta) / 40) {
                        try client.pointer(buttons: delta > 0 ? positive : negative, x: at.x, y: at.y)
                        try client.pointer(buttons: 0, x: at.x, y: at.y)
                    }
                }
            case "type":
                guard let text = request.arguments["text"], text.utf8.count <= 32768 else { throw Failure("Provide text (maximum 32 KiB)") }
                for scalar in text.unicodeScalars {
                    let keysym = RFB.keysym(for: scalar)
                    try client.key(keysym, down: true); try client.key(keysym, down: false)
                }
            case "key":
                let keysyms = try RFB.chord(request.arguments["key"] ?? "")
                for keysym in keysyms { try client.key(keysym, down: true) }
                for keysym in keysyms.reversed() { try client.key(keysym, down: false) }
            default: throw Failure("Unknown guest action: \(request.action)")
            }
            try mailbox.respond(Reply(id: request.id, values: values))
        } catch { try? mailbox.respond(Reply(id: request.id, error: error.localizedDescription)) }
    }

    private func frame(after sequence: Int, timeout: TimeInterval) async throws -> RFBClient.Frame? {
        let deadline = Date().addingTimeInterval(timeout)
        while true {
            try client.check()
            if let frame = client.current, frame.sequence != sequence { return frame }
            if Date() >= deadline { return nil }
            try await Task.sleep(nanoseconds: 4_000_000)
        }
    }
    private func pointSize() async throws -> (width: Int, height: Int) {
        guard let frame = try await frame(after: -1, timeout: 5) else { throw Failure("The VM display has not drawn yet") }
        return (frame.width / scale, frame.height / scale)
    }
    private func number(_ request: Request, _ key: String, range: ClosedRange<Int>) throws -> Int {
        guard let raw = request.arguments[key], let value = Int(raw), range.contains(value) else { throw Failure("Invalid \(key); expected \(range)") }
        return value
    }
    /// Guest points from the request, as framebuffer pixels.
    private func coordinates(_ request: Request, x: String = "x", y: String = "y") async throws -> (x: Int, y: Int) {
        let size = try await pointSize()
        return (try number(request, x, range: 0...(size.width - 1)) * scale, try number(request, y, range: 0...(size.height - 1)) * scale)
    }
    /// BGRA pixels of the frame scaled to width x height (the framebuffer is 32-bit little-endian XRGB).
    private func render(_ frame: RFBClient.Frame, width: Int, height: Int) throws -> (data: Data, bytesPerRow: Int, image: CGImage?) {
        let info = CGImageAlphaInfo.noneSkipFirst.rawValue | CGBitmapInfo.byteOrder32Little.rawValue
        let space = CGColorSpace(name: CGColorSpace.sRGB)!
        guard let provider = CGDataProvider(data: frame.pixels as CFData),
              let source = CGImage(width: frame.width, height: frame.height, bitsPerComponent: 8, bitsPerPixel: 32, bytesPerRow: frame.width * 4,
                                   space: space, bitmapInfo: CGBitmapInfo(rawValue: info), provider: provider, decode: nil,
                                   shouldInterpolate: true, intent: .defaultIntent) else { throw Failure("Cannot read the VM display") }
        let bytesPerRow = width * 4
        var data = Data(count: bytesPerRow * height)
        let image: CGImage? = try data.withUnsafeMutableBytes { buffer in
            guard let context = CGContext(data: buffer.baseAddress, width: width, height: height, bitsPerComponent: 8, bytesPerRow: bytesPerRow,
                                          space: space, bitmapInfo: info) else { throw Failure("Cannot scale the VM display") }
            context.interpolationQuality = .high
            context.draw(source, in: CGRect(x: 0, y: 0, width: width, height: height))
            return context.makeImage()
        }
        return (data, bytesPerRow, image)
    }
}
