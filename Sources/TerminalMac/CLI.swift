import AppKit
import Virtualization
import TerminalMacCore
import Darwin

@MainActor
final class CLI {
    let store = Store()
    var runtime: MachineRuntime?
    var guest: GuestRuntime?
    var executableURL: URL {
        (Bundle.main.executableURL ?? URL(fileURLWithPath: CommandLine.arguments[0]))
            .standardizedFileURL.resolvingSymlinksInPath()
    }
    var toolsURL: URL {
        executableURL
            .deletingLastPathComponent().deletingLastPathComponent().appendingPathComponent("Resources/guest-tools")
    }
    func run(_ input: [String], quietStartup: Bool = false) async throws {
        let command = input.first ?? (Bundle.main.bundleIdentifier == "dev.terminal-mac.guest" ? "guest" : (isatty(STDIN_FILENO) == 1 ? "up" : "help"))
        let tail = Array(input.dropFirst())
        switch command {
        case "help", "--help", "-h": print(Self.help)
        case "--version", "version": print("terminal-mac \(appVersion)")
        case "up", "setup": try await up(tail)
        case "doctor":
            _ = try Arguments(tail)
            try printJSON(["architecture": "arm64", "virtualizationSupported": String(VZVirtualMachine.isSupported),
                           "macOS": ProcessInfo.processInfo.operatingSystemVersionString, "dataDirectory": store.root.path,
                           "guestToolsBundled": String(FileManager.default.fileExists(atPath: toolsURL.appendingPathComponent("TerminalMacGuest.app").path)),
                           "hostControlAvailable": String(HostDisplayServer.isAvailable)])
        case "image":
            let args = try Arguments(tail, options: ["output"])
            guard args.positionals.isEmpty else { throw Failure("Usage: terminal-mac image [--output /path/to/restore.ipsw]") }
            let image = try await VZMacOSRestoreImage.latestSupported
            guard let output = args.options["output"] else {
                try printJSON(["url": image.url.absoluteString, "build": image.buildVersion, "version": "\(image.operatingSystemVersion.majorVersion).\(image.operatingSystemVersion.minorVersion).\(image.operatingSystemVersion.patchVersion)"])
                return
            }
            let destination = URL(fileURLWithPath: (output as NSString).expandingTildeInPath)
            guard !FileManager.default.fileExists(atPath: destination.path) else { throw Failure("Output already exists; it will not be overwritten") }
            stderr("Downloading macOS from Apple. Restore images are several gigabytes; this may take a while…")
            let (temporary, response) = try await URLSession.shared.download(from: image.url)
            defer { try? FileManager.default.removeItem(at: temporary) }
            guard (response as? HTTPURLResponse)?.statusCode == 200 else { throw Failure("Apple restore-image download failed") }
            try FileManager.default.moveItem(at: temporary, to: destination)
            print(destination.path)
        case "create":
            let args = try Arguments(tail, flags: ["offline", "standard", "host-control"], options: ["ipsw", "cpus", "memory", "disk", "width", "height"])
            try await VirtualMachines.create(args, store: store)
        case "ls", "list":
            _ = try Arguments(tail)
            let directory = store.root.appendingPathComponent("machines")
            let urls = (try? FileManager.default.contentsOfDirectory(at: directory, includingPropertiesForKeys: nil)) ?? []
            let machines = urls.sorted { $0.lastPathComponent < $1.lastPathComponent }.compactMap { try? store.load($0.lastPathComponent) }
            let encoder = JSONEncoder(); encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
            print(String(decoding: try encoder.encode(machines), as: UTF8.self))
        case "start":
            let args = try Arguments(tail, flags: ["window"])
            let name = try args.machineName()
            let machine = try store.load(name)
            guard machine.installed else { throw Failure("Installation did not complete. See the VM directory and README recovery instructions.") }
            let directory = try store.directory(name)
            if runtimeIsLocked(directory) {
                if args.flags.contains("window") { _ = try await host(name, action: "show") }
                if !quietStartup { try printJSON(["name": name, "state": "already-running"]) }; return
            }
            guard FileManager.default.fileExists(atPath: toolsURL.appendingPathComponent("TerminalMacGuest.app").path) else { throw Failure("Guest tools are missing. Run scripts/build.sh and use dist/terminal-mac.") }
            let log = directory.appendingPathComponent("runtime.log")
            if !FileManager.default.fileExists(atPath: log.path) { FileManager.default.createFile(atPath: log.path, contents: nil, attributes: [.posixPermissions: 0o600]) }
            let logHandle = try FileHandle(forWritingTo: log)
            try logHandle.seekToEnd()
            defer { try? logHandle.close() }
            let process = Process()
            process.executableURL = executableURL
            process.arguments = ["__run", name] + (args.flags.contains("window") ? ["--window"] : [])
            var environment = ProcessInfo.processInfo.environment; environment["TERMINAL_MAC_HOME"] = store.root.path
            process.environment = environment
            process.standardOutput = logHandle; process.standardError = logHandle; process.standardInput = FileHandle.nullDevice
            try process.run()
            // A host mailbox responds only after VZ has booted successfully.
            _ = try Mailbox(directory.appendingPathComponent("host-control"), create: true)
            do {
                let reply = try await host(name, action: "status", timeout: 30)
                if !quietStartup { try printJSON(reply.values) }
            } catch {
                throw Failure("VM did not become ready: \(error.localizedDescription). Details: \(log.path)")
            }
        case "__run":
            let args = try Arguments(tail, flags: ["window"])
            let name = try args.machineName()
            let machine = try store.load(name)
            guard machine.installed else { throw Failure("VM installation is incomplete") }
            runtime = try MachineRuntime(machine: machine, directory: store.directory(name), tools: toolsURL)
            try await runtime!.start(show: args.flags.contains("window"))
            // Keep the app event loop and runtime alive until guest shutdown.
            while true { try await Task.sleep(nanoseconds: 1_000_000_000) }
        case "status", "show", "hide", "pause", "resume":
            let args = try Arguments(tail)
            let name = try args.machineName()
            _ = try store.load(name)
            if command == "status", !runtimeIsLocked(try store.directory(name)) {
                try printJSON(["name": name, "state": "stopped"]); return
            }
            let reply = try await host(name, action: command)
            try printJSON(reply.values)
        case "stop":
            let args = try Arguments(tail, flags: ["force"], options: ["timeout"])
            let name = try args.machineName()
            _ = try store.load(name)
            let directory = try store.directory(name)
            guard runtimeIsLocked(directory) else { try printJSON(["name": name, "state": "stopped"]); return }
            let timeout = try args.integer("timeout", default: 60, range: 0...3600)
            let force = args.flags.contains("force")
            let reply = try await host(name, action: "stop", arguments: ["force": String(force)])
            if force || timeout == 0 { try printJSON(reply.values); return }
            // Wait for the VM process to exit, so callers learn whether the guest actually shut down.
            let deadline = Date().addingTimeInterval(Double(timeout))
            while runtimeIsLocked(directory) && Date() < deadline { try await Task.sleep(nanoseconds: 250_000_000) }
            guard !runtimeIsLocked(directory) else {
                throw Failure("The guest has not shut down after \(timeout) seconds; it may be showing a confirmation dialog. Check with terminal-mac view \(name), or power off with terminal-mac stop \(name) --force (unsaved guest work can be lost).")
            }
            try printJSON(["name": name, "state": "stopped"])
        case "control":
            let args = try Arguments(tail, flags: ["host", "guest"])
            let name = try args.machineName()
            var machine = try store.load(name)
            guard args.flags.count == 1 else { throw Failure("Usage: terminal-mac control [NAME] --host|--guest") }
            guard !runtimeIsLocked(try store.directory(name)) else { throw Failure("Stop the VM first: terminal-mac stop \(name). The control mode applies at the next boot.") }
            if args.flags.contains("host") && !HostDisplayServer.isAvailable { throw Failure("Host control needs Virtualization's display server, which this macOS does not have") }
            machine.control = args.flags.contains("host") ? "host" : nil
            try store.save(machine)
            try printJSON(["name": name, "control": machine.usesHostControl ? "host" : "guest"])
        case "display":
            let args = try Arguments(tail, flags: ["hidpi", "standard"])
            let name = try args.machineName()
            var machine = try store.load(name)
            guard args.flags.count == 1 else { throw Failure("Usage: terminal-mac display [NAME] --hidpi|--standard") }
            guard !runtimeIsLocked(try store.directory(name)) else { throw Failure("Stop the VM first: terminal-mac stop \(name). The display mode applies at the next boot.") }
            machine.scale = args.flags.contains("hidpi") ? 2 : 1
            try store.save(machine)
            try printJSON(["name": name, "display": "\(machine.width)×\(machine.height) points at \(machine.displayScale)x"])
        case "action": try await action(tail)
        case "mcp":
            let args = try Arguments(tail, flags: ["config"])
            let name = try args.machineName()
            if args.flags.contains("config") {
                // Point clients at the launcher, not the versioned bundle, so upgrades keep working.
                let server: [String: Any] = ["command": launcherPath, "args": name == "default" ? ["mcp"] : ["mcp", name]]
                let data = try JSONSerialization.data(withJSONObject: ["mcpServers": ["terminal-mac": server]], options: [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes])
                print(String(decoding: data, as: UTF8.self)); return
            }
            try await AgentServer(store: store, name: name).run()
        case "skills":
            let args = try Arguments(tail, options: ["dir"])
            guard let skill = bundledSkill else { throw Failure("This build has no bundled skill. Reinstall terminal-mac.") }
            switch args.positionals {
            case ["get"], []: print(try String(contentsOf: skill, encoding: .utf8), terminator: "")
            case ["path"]: print(skill.path)
            case ["install"]:
                let root = args.options["dir"].map { URL(fileURLWithPath: ($0 as NSString).expandingTildeInPath) }
                    ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".claude/skills")
                let destination = root.appendingPathComponent("terminal-mac/SKILL.md")
                if let existing = try? String(contentsOf: destination, encoding: .utf8), !existing.contains("\nname: terminal-mac\n") {
                    throw Failure("Refusing to replace a different skill at \(destination.path)")
                }
                try FileManager.default.createDirectory(at: destination.deletingLastPathComponent(), withIntermediateDirectories: true)
                try Data(contentsOf: skill).write(to: destination, options: .atomic)
                try printJSON(["installed": destination.path])
            default: throw Failure("Usage: terminal-mac skills [get|path|install [--dir ~/.claude/skills]]")
            }
        case "view":
            let args = try Arguments(tail, flags: ["read-only"], options: ["protocol"])
            let name = try args.machineName()
            let machine = try store.load(name)
            guard runtimeIsLocked(try store.directory(name)) else { throw Failure("VM '\(name)' is stopped. Start it with: terminal-mac start \(name)") }
            let viewer = try TerminalViewer(directory: store.directory(name), machine: machine, graphics: args.options["protocol"], readOnly: args.flags.contains("read-only"))
            try await viewer.run()
        case "guest":
            let args = try Arguments(tail, flags: ["check"], options: ["exchange"])
            guard args.positionals.isEmpty else { throw Failure("Usage: terminal-mac guest [--check] [--exchange PATH]") }
            try GuestGuard.requireVirtualMac()
            if args.flags.contains("check") { print("virtual-mac"); return }
            if try await GuestBootstrap.installAndRelaunchIfNeeded() { return }
            let exchange = URL(fileURLWithPath: args.options["exchange"] ?? "/Volumes/My Shared Files/control")
            guest = try GuestRuntime(exchange: exchange)
            guest!.start()
            while true { try await Task.sleep(nanoseconds: 1_000_000_000) }
        default: throw Failure("Unknown command: \(command). Run terminal-mac help.")
        }
    }
    func host(_ name: String, action: String, arguments: [String: String] = [:], timeout: Double = 5) async throws -> Reply {
        try await Mailbox(store.directory(name).appendingPathComponent("host-control")).call(Request(action: action, arguments: arguments, timeout: timeout))
    }
    func action(_ input: [String]) async throws {
        let args = try Arguments(input, flags: ["double"], options: ["x", "y", "x2", "y2", "dx", "dy", "text", "key", "button", "count", "output", "timeout"])
        let actions: Set<String> = ["status", "screenshot", "snapshot", "click", "move", "drag", "scroll", "type", "key"]
        var positions = args.positionals
        let name: String
        if let first = positions.first, actions.contains(first) { name = "default" }
        else { guard !positions.isEmpty else { throw Failure("Usage: terminal-mac action [NAME] screenshot|click|type|key|scroll|status") }; name = positions.removeFirst() }
        try validateName(name)
        _ = try store.load(name)
        guard !positions.isEmpty, actions.contains(positions[0]) else { throw Failure("Unknown or missing guest action") }
        let verb = positions.removeFirst()
        var parameters = args.options
        parameters.removeValue(forKey: "output"); parameters.removeValue(forKey: "timeout")
        if verb == "type", !positions.isEmpty { parameters["text"] = positions.joined(separator: " "); positions.removeAll() }
        if verb == "key", positions.count == 1 { parameters["key"] = positions.removeFirst() }
        if ["click", "move"].contains(verb), positions.count == 2 { parameters["x"] = positions.removeFirst(); parameters["y"] = positions.removeFirst() }
        if verb == "drag", positions.count == 4 {
            for key in ["x", "y", "x2", "y2"] { parameters[key] = positions.removeFirst() }
        }
        guard positions.isEmpty else { throw Failure("Unexpected action arguments") }
        if args.flags.contains("double") { parameters["double"] = "true" }
        let timeout = try args.integer("timeout", default: 15, range: 1...120)
        let mailbox = try Mailbox(store.directory(name).appendingPathComponent("exchange"))
        let request = Request(action: verb, arguments: parameters, timeout: Double(timeout))
        let imageName = "image-\(request.id.uuidString).png"
        defer { mailbox.remove(imageName) }
        let reply = try await mailbox.call(request)
        var values = reply.values
        if verb == "screenshot" || verb == "snapshot" {
            guard values["image"] == imageName, let png = try mailbox.read(imageName, limit: 32 * 1024 * 1024) else { throw Failure("Invalid guest image response") }
            let output = URL(fileURLWithPath: ((args.options["output"] ?? "terminal-mac-\(name).png") as NSString).expandingTildeInPath)
            try png.write(to: output, options: .atomic)
            values["image"] = output.path
        }
        try printJSON(values)
    }
    /// The agent skill shipped inside the app bundle, so its instructions match this version.
    /// Development builds outside a bundle fall back to the repository copy.
    var bundledSkill: URL? {
        let bundled = executableURL.deletingLastPathComponent().deletingLastPathComponent().appendingPathComponent("Resources/skills/terminal-mac/SKILL.md")
        if FileManager.default.fileExists(atPath: bundled.path) { return bundled }
        var directory = executableURL.deletingLastPathComponent()
        for _ in 0..<6 {
            let candidate = directory.appendingPathComponent("skills/terminal-mac/SKILL.md")
            if FileManager.default.fileExists(atPath: candidate.path) { return candidate }
            directory.deleteLastPathComponent()
        }
        return nil
    }
    /// How the user invoked us, before symlinks: the installer's stable launcher.
    var launcherPath: String {
        let invoked = CommandLine.arguments[0]
        if invoked.contains("/") { return URL(fileURLWithPath: invoked).standardizedFileURL.path }
        for directory in (ProcessInfo.processInfo.environment["PATH"] ?? "").split(separator: ":") {
            let candidate = "\(directory)/\(invoked)"
            if FileManager.default.isExecutableFile(atPath: candidate) { return candidate }
        }
        return executableURL.path
    }
    func runtimeIsLocked(_ directory: URL) -> Bool { Self.isRunning(directory) }
    static func isRunning(_ directory: URL) -> Bool {
        let fd = open(directory.appendingPathComponent("runtime.lock").path, O_RDWR | O_NOFOLLOW | O_CLOEXEC)
        guard fd >= 0 else { return false }
        defer { close(fd) }
        if flock(fd, LOCK_EX | LOCK_NB) == 0 { flock(fd, LOCK_UN); return false }
        return true
    }
    static let help = """
    terminal-mac — a separate macOS desktop for computer-use agents

    up [NAME]                   Download, install, boot and open your virtual Mac
      [--ipsw PATH]             Optional existing image; otherwise downloaded from Apple
      [--no-view | --detach]    Wait for readiness without viewing, or just boot
      [--plan] [--timeout 1800] Preview steps without changes; setup wait in seconds
    setup [NAME]                Same guided flow as up
    terminal-mac                Runs up in an interactive terminal

    image [--output PATH]         Find or download Apple's latest supported IPSW
    create [NAME] --ipsw PATH     Install a new virtual Mac (default name: default)
      [--cpus 4 --memory 4 --disk 64 --width 1280 --height 800 --offline]
      [--standard]                1x display; new VMs default to sharp 2x HiDPI
      [--host-control]            Drive the guest from the host: no guest helper or permissions
    display [NAME] --hidpi|--standard  Switch a stopped VM between 2x and 1x
    control [NAME] --host|--guest      Switch a stopped VM between host control and the guest helper
    start [NAME] [--window]       Boot in the background; show setup window if requested
    view [NAME] [--read-only]     Live terminal desktop (Ghostty, Kitty, iTerm2)
      [--protocol kitty|iterm]    Ctrl+G opens Mac shortcuts; Ctrl+] exits
    show|hide [NAME]             Show or hide the native VM window
    status|pause|resume [NAME]   Manage the VM
    stop [NAME] [--force]        Request guest shutdown and wait for it (or immediately power off)
      [--timeout 60]             Seconds to wait; 0 returns right after the request
    ls                          List installed VMs as JSON
    doctor                      Check host support and bundled guest tools

    action [NAME] status
    action [NAME] screenshot [--output PATH]
    action [NAME] click X Y [--button right] [--double]
    action [NAME] move X Y
    action [NAME] drag X Y X2 Y2
    action [NAME] type 'text'
    action [NAME] key cmd+space
    action [NAME] scroll --dy -300 [--dx 0 --x 640 --y 400]

    mcp [NAME]                  MCP server (stdio) with computer-use tools for agents:
                                claude mcp add terminal-mac -- terminal-mac mcp
      [--config]                Print an mcpServers entry for Cursor, VS Code, Codex…
    skills get|path|install     The agent skill for this version; install copies it
      [--dir ~/.claude/skills]  into an agent's skills folder

    With guest control (default), actions require TerminalMacGuest.app running inside the
    logged-in guest; Screen Recording and Accessibility are granted inside the guest only.
    With host control, the VM process reads the display and sends input itself.
    TERMINAL_MAC_HOME overrides the host data directory.
    """
}

/// The app bundle around the running executable. Bundle.main cannot find it when the CLI is
/// started through the installer's symlink, so versions would fall back to a guess.
let appBundle: Bundle = {
    let executable = (Bundle.main.executableURL ?? URL(fileURLWithPath: CommandLine.arguments[0])).standardizedFileURL.resolvingSymlinksInPath()
    return Bundle(url: executable.deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()) ?? Bundle.main
}()
var appVersion: String { appBundle.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "dev" }

func printJSON(_ value: [String: String]) throws {
    let data = try JSONSerialization.data(withJSONObject: value, options: [.prettyPrinted, .sortedKeys])
    print(String(decoding: data, as: UTF8.self))
}
