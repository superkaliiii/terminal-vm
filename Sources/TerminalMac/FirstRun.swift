import AppKit
import Virtualization
import TerminalMacCore

extension CLI {
    func up(_ input: [String]) async throws {
        let args = try Arguments(input, flags: ["plan", "no-view", "detach", "offline", "host-control"], options: ["ipsw", "cpus", "memory", "disk", "width", "height", "timeout", "protocol"])
        let name = try args.machineName()
        let directory = try store.directory(name)
        let existing = FileManager.default.fileExists(atPath: directory.appendingPathComponent("machine.json").path) ? try store.load(name) : nil
        let plan = SetupPlan(name: name, machine: existing, hasLocalImage: args.options["ipsw"] != nil)
        if args.flags.contains("plan") {
            let encoder = JSONEncoder(); encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
            print(String(decoding: try encoder.encode(plan), as: UTF8.self)); return
        }
        // Fail before any download or disk creation if host/package support is missing.
        guard VZVirtualMachine.isSupported else { throw Failure("This Mac cannot start a virtual Mac. Use Apple silicon and an installed, signed terminal-mac release.") }
        guard FileManager.default.fileExists(atPath: toolsURL.appendingPathComponent("TerminalMacGuest.app").path) else { throw Failure("The installation is incomplete: bundled guest tools are missing. Reinstall the terminal-mac package.") }
        let timeout = try args.integer("timeout", default: 1800, range: 1...7200)
        // Validate resource input before starting a potentially large download.
        _ = try args.integer("cpus", default: 4, range: 1...VZVirtualMachineConfiguration.maximumAllowedCPUCount)
        _ = try args.integer("memory", default: 4, range: 1...Int(VZVirtualMachineConfiguration.maximumAllowedMemorySize / 1_073_741_824))
        _ = try args.integer("disk", default: 64, range: 32...2048)
        _ = try args.integer("width", default: 1280, range: 800...3840)
        _ = try args.integer("height", default: 800, range: 600...2160)
        if let graphics = args.options["protocol"], !["kitty", "iterm"].contains(graphics) { throw Failure("--protocol must be kitty or iterm") }
        try FileManager.default.createDirectory(at: store.root, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        var setupLock: FileLock? = try FileLock(store.root.appendingPathComponent("setup-\(name).lock"))
        defer { withExtendedLifetime(setupLock) {} }
        // Re-read after acquiring the installation lock.
        let machine = FileManager.default.fileExists(atPath: directory.appendingPathComponent("machine.json").path) ? try store.load(name) : nil
        if machine?.installed != true {
            guard !runtimeIsLocked(directory) else { throw Failure("An incomplete VM is running. Stop it before retrying installation.") }
            let image: URL
            if let local = args.options["ipsw"] { image = URL(fileURLWithPath: (local as NSString).expandingTildeInPath) }
            else { image = try await cachedRestoreImage() }
            var createInput = [name, "--ipsw", image.path]
            for key in ["cpus", "memory", "disk", "width", "height"] {
                if let value = args.options[key] { createInput += ["--\(key)", value] }
            }
            for flag in ["offline", "host-control"] where args.flags.contains(flag) { createInput += ["--\(flag)"] }
            try await VirtualMachines.create(Arguments(createInput, flags: ["offline", "host-control"], options: ["ipsw", "cpus", "memory", "disk", "width", "height"]), store: store, retryIncomplete: true)
        } else if !Set(args.options.keys).intersection(["ipsw", "cpus", "memory", "disk", "width", "height"]).isEmpty || args.flags.contains("offline") || args.flags.contains("host-control") {
            throw Failure("This VM already exists. Creation options only apply to a new VM; run terminal-mac up \(name) without them (use terminal-mac control \(name) --host to switch control mode).")
        }
        try await run(["start", name], quietStartup: true)
        let lifecycle = try await host(name, action: "status", timeout: 30)
        if lifecycle.values["state"] == "paused" { _ = try await host(name, action: "resume") }
        setupLock = nil
        if args.flags.contains("detach") {
            stderr("VM started. Run 'terminal-mac up \(name)' to finish setup or open its desktop.")
            try printJSON(["name": name, "state": "started", "setup": "not-checked"])
            return
        }
        let mailbox = try Mailbox(directory.appendingPathComponent("exchange"))
        var values = try? await mailbox.call(Request(action: "status", timeout: 2)).values
        var readiness = GuestReadiness(values)
        var showedSetup = false
        // After first-time setup, the terminal viewer handles a guest at its login screen itself.
        let inline = !args.flags.contains("no-view") && (TerminalViewer.supportsCurrentTerminal || args.options["protocol"] != nil)
        if readiness != .ready, inline, FileManager.default.fileExists(atPath: TerminalViewer.readyMarker(directory).path) {
            try await TerminalViewer(directory: directory, machine: store.load(name), graphics: args.options["protocol"], readOnly: false).run()
            return
        }
        if readiness != .ready {
            _ = try await host(name, action: "show")
            showedSetup = true
            stderr("""
            Finish first-time setup in the virtual Mac window:
              1. Complete macOS setup and sign in to its desktop.
              2. Open Finder → /Volumes/My Shared Files/tools → TerminalMacGuest.app.
                 It installs itself in the guest and registers for future guest logins.
              3. Use its two buttons to allow Screen Recording and Accessibility.
            Waiting for the virtual Mac to be ready. You can rerun this command later.
            """)
        }
        let deadline = Date().addingTimeInterval(Double(timeout))
        var lastReadiness = readiness
        while readiness != .ready && Date() < deadline {
            try Task.checkCancellation()
            values = try? await mailbox.call(Request(action: "status", timeout: 2)).values
            readiness = GuestReadiness(values)
            if readiness != lastReadiness && readiness != .ready {
                stderr(readiness == .permissions ? "Guest helper connected. Waiting for its privacy approvals…" : "Waiting for the guest helper…")
                lastReadiness = readiness
            }
            if readiness != .ready { try await Task.sleep(nanoseconds: 1_000_000_000) }
        }
        guard readiness == .ready else { throw Failure("Setup is still pending. The VM is running; rerun 'terminal-mac up \(name)' when ready.") }
        stderr("Your virtual Mac is ready.")
        FileManager.default.createFile(atPath: TerminalViewer.readyMarker(directory).path, contents: nil, attributes: [.posixPermissions: 0o600])
        if args.flags.contains("no-view") {
            if showedSetup { _ = try await host(name, action: "hide") }
            try printJSON(values ?? [:]); return
        }
        if !TerminalViewer.supportsCurrentTerminal && args.options["protocol"] == nil {
            _ = try await host(name, action: "show")
            stderr("Opened the native desktop. For inline viewing use Ghostty, Kitty or iTerm2. Agent actions work from this terminal.")
            return
        }
        if showedSetup { _ = try await host(name, action: "hide") }
        try await TerminalViewer(directory: directory, machine: store.load(name), graphics: args.options["protocol"], readOnly: false).run()
    }

    private func cachedRestoreImage() async throws -> URL {
        let cache = store.root.appendingPathComponent("cache", isDirectory: true)
        try FileManager.default.createDirectory(at: cache, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        let lock = try FileLock(cache.appendingPathComponent("download.lock"))
        defer { withExtendedLifetime(lock) {} }
        stderr("Checking Apple's compatible macOS restore image…")
        let image = try await VZMacOSRestoreImage.latestSupported
        guard image.buildVersion.range(of: "^[A-Za-z0-9._-]+$", options: .regularExpression) != nil,
              image.url.scheme == "https" else { throw Failure("Unexpected Apple restore-image metadata") }
        let destination = cache.appendingPathComponent("macOS-\(image.buildVersion).ipsw")
        if FileManager.default.fileExists(atPath: destination.path) {
            if let cached = try? await VZMacOSRestoreImage.image(from: destination), cached.buildVersion == image.buildVersion, cached.mostFeaturefulSupportedConfiguration != nil {
                stderr("Using the cached macOS restore image."); return destination
            }
            stderr("The cached restore image is invalid; downloading a fresh copy.")
            try FileManager.default.removeItem(at: destination)
        }
        stderr("First run downloads macOS directly from Apple (several GB), then installs it locally. Future launches reuse this VM.")
        let download = RestoreDownload(destination: destination)
        let result = try await download.run(from: image.url)
        let verified = try await VZMacOSRestoreImage.image(from: result)
        guard verified.buildVersion == image.buildVersion, verified.mostFeaturefulSupportedConfiguration != nil else { throw Failure("Downloaded restore image is not compatible with this Mac") }
        return result
    }
}
