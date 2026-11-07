import Foundation

public struct Failure: LocalizedError {
    public let message: String
    public init(_ message: String) { self.message = message }
    public var errorDescription: String? { message }
}

public struct Arguments {
    public var positionals: [String] = []
    public var options: [String: String] = [:]
    public var flags: Set<String> = []
    public init(_ input: [String], flags allowedFlags: Set<String> = [], options allowedOptions: Set<String> = []) throws {
        var i = 0
        while i < input.count {
            let item = input[i]
            if item == "--" { positionals += input.dropFirst(i + 1); break }
            if item.hasPrefix("--") {
                let name = String(item.dropFirst(2))
                guard !flags.contains(name), options[name] == nil else { throw Failure("Duplicate option: \(item)") }
                if allowedFlags.contains(name) { flags.insert(name) }
                else if allowedOptions.contains(name) {
                    i += 1
                    guard i < input.count, !input[i].hasPrefix("--") else { throw Failure("Missing value for \(item)") }
                    options[name] = input[i]
                } else { throw Failure("Unknown option: \(item)") }
            } else { positionals.append(item) }
            i += 1
        }
    }
    public func integer(_ key: String, default fallback: Int, range: ClosedRange<Int>) throws -> Int {
        guard let raw = options[key] else { return fallback }
        guard let value = Int(raw), range.contains(value) else { throw Failure("--\(key) must be in \(range)") }
        return value
    }
    public func machineName() throws -> String {
        guard positionals.count <= 1 else { throw Failure("Expected one VM name") }
        let name = positionals.first ?? "default"
        try validateName(name)
        return name
    }
}

public func validateName(_ name: String) throws {
    guard name.range(of: "^[a-zA-Z0-9][a-zA-Z0-9_-]{0,47}$", options: .regularExpression) != nil else {
        throw Failure("VM names must be 1–48 letters, numbers, underscores or hyphens, starting with a letter or number")
    }
}

public struct Machine: Codable {
    public var name: String
    public var cpu: Int
    public var memoryGiB: Int
    public var diskGiB: Int
    public var width: Int
    public var height: Int
    public var network: Bool
    public var installed: Bool
    /// 2 renders the guest at Retina density: twice the pixels for the same point size. Older VMs have none.
    public var scale: Int?
    public var displayScale: Int { scale == 2 ? 2 : 1 }
    /// "host" drives the guest from the VM process (no guest helper); nil or "guest" uses TerminalMacGuest.
    public var control: String?
    public var usesHostControl: Bool { control == "host" }
    public init(name: String, cpu: Int, memoryGiB: Int, diskGiB: Int, width: Int = 1280, height: Int = 800, network: Bool = true, installed: Bool = false, scale: Int? = nil, control: String? = nil) {
        self.name = name; self.cpu = cpu; self.memoryGiB = memoryGiB; self.diskGiB = diskGiB
        self.width = width; self.height = height; self.network = network; self.installed = installed; self.scale = scale
        self.control = control
    }
}

public struct Request: Codable {
    public let id: UUID
    public let action: String
    public let arguments: [String: String]
    public let deadline: Date
    public init(action: String, arguments: [String: String] = [:], timeout: TimeInterval = 15) {
        id = UUID(); self.action = action; self.arguments = arguments; deadline = Date().addingTimeInterval(timeout)
    }
}

public struct Reply: Codable {
    public let id: UUID
    public let ok: Bool
    public let values: [String: String]
    public let error: String?
    public init(id: UUID, values: [String: String] = [:], error: String? = nil) {
        self.id = id; ok = error == nil; self.values = values; self.error = error
    }
}

public struct Store {
    public let root: URL
    public init(root: URL? = nil) {
        self.root = root ?? ProcessInfo.processInfo.environment["TERMINAL_MAC_HOME"].map { URL(fileURLWithPath: $0) }
            ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/terminal-mac")
    }
    public func directory(_ name: String) throws -> URL {
        try validateName(name)
        return root.appendingPathComponent("machines").appendingPathComponent(name)
    }
    public func load(_ name: String) throws -> Machine {
        let url = try directory(name).appendingPathComponent("machine.json")
        guard FileManager.default.fileExists(atPath: url.path) else { throw Failure("VM '\(name)' does not exist. Run terminal-mac create \(name) --ipsw /path/to/restore.ipsw") }
        return try JSONDecoder().decode(Machine.self, from: Data(contentsOf: url))
    }
    public func save(_ machine: Machine) throws {
        let data = try JSONEncoder().encode(machine)
        try data.write(to: directory(machine.name).appendingPathComponent("machine.json"), options: .atomic)
    }
}
