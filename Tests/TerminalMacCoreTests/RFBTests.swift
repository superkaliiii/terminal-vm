import XCTest
import Darwin
import Foundation
@testable import TerminalMacCore

final class RFBTests: XCTestCase {
    static let challenge = Array(0x00...0xff).filter { $0 % 0x11 == 0 }.map(UInt8.init)  // 00 11 22 … ff

    func testVNCAuthMatchesOpenSSLReference() {
        // openssl enc -des-ecb -nopad -K cea6c64ea62e8c4c (bit-reversed "secret12") over 00112233…ff
        XCTAssertEqual(Self.challenge.count, 16)
        XCTAssertEqual(RFB.vncAuthResponse(challenge: Self.challenge, password: "secret12").map { String(format: "%02x", $0) }.joined(),
                       "817402416b165d7ade9c46b5faa42370")
    }
    func testKeysyms() {
        XCTAssertEqual(RFB.keysym(for: "a"), 0x61)
        XCTAssertEqual(RFB.keysym(for: "A"), 0x41)
        XCTAssertEqual(RFB.keysym(for: "é"), 0xe9)
        XCTAssertEqual(RFB.keysym(for: "€"), 0x0100_20ac)
        XCTAssertEqual(RFB.keysym(for: "\n"), 0xff0d)
    }
    func testChords() throws {
        XCTAssertEqual(try RFB.chord("cmd+shift+A"), [0xffeb, 0xffe1, 0x61])
        XCTAssertEqual(try RFB.chord("return"), [0xff0d])
        XCTAssertEqual(try RFB.chord("ctrl+f5"), [0xffe3, 0xffc2])
        XCTAssertThrowsError(try RFB.chord("hyper+a"))
        XCTAssertThrowsError(try RFB.chord("cmd+"))
        XCTAssertThrowsError(try RFB.chord("notakey"))
    }
    func testMessages() {
        XCTAssertEqual(RFB.pointerEvent(buttons: 1, x: 300, y: 2), [5, 1, 1, 44, 0, 2])
        XCTAssertEqual(RFB.keyEvent(0xff0d, down: true), [4, 1, 0, 0, 0, 0, 0xff, 0x0d])
    }

    /// A scripted VNC server: handshake with password auth, one 4x2 raw frame, then one pointer event.
    func testClientHandshakeFrameAndInput() throws {
        let listener = socket(AF_INET, SOCK_STREAM, 0)
        XCTAssertGreaterThanOrEqual(listener, 0)
        defer { close(listener) }
        var address = sockaddr_in()
        address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        address.sin_family = sa_family_t(AF_INET)
        address.sin_addr.s_addr = inet_addr("127.0.0.1")
        var length = socklen_t(MemoryLayout<sockaddr_in>.size)
        try withUnsafeMutablePointer(to: &address) { pointer in
            try pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                guard bind(listener, $0, length) == 0, listen(listener, 1) == 0, getsockname(listener, $0, &length) == 0 else { throw Failure("bind failed") }
            }
        }
        let port = UInt16(bigEndian: address.sin_port)
        let pixels = (0..<32).map { UInt8($0) }
        var authOK = false, pointer: [UInt8] = []
        let done = expectation(description: "server finished")
        Thread {
            defer { done.fulfill() }
            let c = accept(listener, nil, nil)
            defer { close(c) }
            do {
                try RFBClient.writeAll(c, Array("RFB 003.008\n".utf8))
                _ = try RFBClient.readExact(c, 12)
                try RFBClient.writeAll(c, [1, 2])
                guard try RFBClient.readExact(c, 1) == [2] else { return }
                try RFBClient.writeAll(c, Self.challenge)
                authOK = try RFBClient.readExact(c, 16) == RFB.vncAuthResponse(challenge: Self.challenge, password: "secret12")
                try RFBClient.writeAll(c, [0, 0, 0, 0])
                _ = try RFBClient.readExact(c, 1)                               // ClientInit
                try RFBClient.writeAll(c, [0, 4, 0, 2] + [UInt8](repeating: 0, count: 16) + [0, 0, 0, 1] + [UInt8(ascii: "x")])
                _ = try RFBClient.readExact(c, 20 + 12 + 10)                    // SetPixelFormat, SetEncodings, full update request
                try RFBClient.writeAll(c, [0, 0, 0, 1, 0, 0, 0, 0, 0, 4, 0, 2, 0, 0, 0, 0] + pixels)
                _ = try RFBClient.readExact(c, 10)                              // incremental update request
                pointer = try RFBClient.readExact(c, 6)
            } catch {}
        }.start()

        let client = try RFBClient(port: port, password: "secret12")
        defer { client.close() }
        let deadline = Date().addingTimeInterval(5)
        while client.current == nil && Date() < deadline { usleep(5000) }
        let frame = try XCTUnwrap(client.current)
        XCTAssertEqual([frame.width, frame.height, frame.sequence], [4, 2, 1])
        XCTAssertEqual(Array(frame.pixels), pixels)
        try client.pointer(buttons: 1, x: 3, y: 1)
        wait(for: [done], timeout: 5)
        XCTAssertTrue(authOK)
        XCTAssertEqual(pointer, [5, 1, 0, 3, 0, 1])
    }
}
