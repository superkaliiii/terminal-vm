import AppKit
import Virtualization
import TerminalMacCore
import Darwin

@MainActor
enum VirtualMachines {
    static func configuration(_ machine: Machine, at directory: URL, tools: URL?) throws -> VZVirtualMachineConfiguration {
        let config = VZVirtualMachineConfiguration()
        config.bootLoader = VZMacOSBootLoader()
        config.cpuCount = machine.cpu
        config.memorySize = UInt64(machine.memoryGiB) * 1024 * 1024 * 1024
        let platform = VZMacPlatformConfiguration()
        guard let hardware = VZMacHardwareModel(dataRepresentation: try Data(contentsOf: directory.appendingPathComponent("hardware.bin"))), hardware.isSupported,
              let identity = VZMacMachineIdentifier(dataRepresentation: try Data(contentsOf: directory.appendingPathComponent("identity.bin"))) else {
            throw Failure("This VM hardware model is not supported on this Mac")
        }
        platform.hardwareModel = hardware
        platform.machineIdentifier = identity
        platform.auxiliaryStorage = VZMacAuxiliaryStorage(contentsOf: directory.appendingPathComponent("auxiliary.bin"))
        config.platform = platform
        let attachment = try VZDiskImageStorageDeviceAttachment(url: directory.appendingPathComponent("disk.img"), readOnly: false)
        config.storageDevices = [VZVirtioBlockDeviceConfiguration(attachment: attachment)]
        let graphics = VZMacGraphicsDeviceConfiguration()
        // At 220 ppi macOS picks a 2x Retina mode, so the desktop keeps its point size with sharp text.
        let scale = machine.displayScale
        graphics.displays = [VZMacGraphicsDisplayConfiguration(widthInPixels: machine.width * scale, heightInPixels: machine.height * scale, pixelsPerInch: scale == 2 ? 220 : 80)]
        config.graphicsDevices = [graphics]
        config.keyboards = [VZUSBKeyboardConfiguration()]
        config.pointingDevices = [VZUSBScreenCoordinatePointingDeviceConfiguration()]
        config.entropyDevices = [VZVirtioEntropyDeviceConfiguration()]
        if machine.network {
            let network = VZVirtioNetworkDeviceConfiguration()
            network.attachment = VZNATNetworkDeviceAttachment()
            config.networkDevices = [network]
        }
        // Host control answers the mailbox itself, so the guest gets no shares and an installed helper stays idle.
        if !machine.usesHostControl {
            var shares: [String: VZSharedDirectory] = ["control": VZSharedDirectory(url: directory.appendingPathComponent("exchange"), readOnly: false)]
            if let tools { shares["tools"] = VZSharedDirectory(url: tools, readOnly: true) }
            let filesystem = VZVirtioFileSystemDeviceConfiguration(tag: VZVirtioFileSystemDeviceConfiguration.macOSGuestAutomountTag)
            filesystem.share = VZMultipleDirectoryShare(directories: shares)
            config.directorySharingDevices = [filesystem]
        }
        try config.validate()
        return config
    }

    static func create(_ args: Arguments, store: Store, retryIncomplete: Bool = false) async throws {
        guard VZVirtualMachine.isSupported else { throw Failure("Virtualization is unavailable. Use an Apple silicon Mac and the signed build.") }
        let name = try args.machineName()
        guard let ipsw = args.options["ipsw"] else { throw Failure("Provide --ipsw /path/to/restore.ipsw. Use terminal-mac image to find an Apple restore image.") }
        let imageURL = URL(fileURLWithPath: (ipsw as NSString).expandingTildeInPath)
        guard FileManager.default.fileExists(atPath: imageURL.path) else { throw Failure("Restore image does not exist: \(imageURL.path)") }
        let directory = try store.directory(name)
        let image = try await VZMacOSRestoreImage.image(from: imageURL)
        guard let requirements = image.mostFeaturefulSupportedConfiguration else { throw Failure("This restore image is not supported by this Mac") }
        if FileManager.default.fileExists(atPath: directory.path) {
            guard retryIncomplete, let partial = try? store.load(name), !partial.installed,
                  try Data(contentsOf: directory.appendingPathComponent("hardware.bin")) == requirements.hardwareModel.dataRepresentation else {
                throw Failure("VM directory already exists and cannot be automatically retried: \(directory.path). It will not be overwritten.")
            }
            stderr("Retrying the incomplete macOS installation for '\(name)' with its existing hardware and disk.")
            try await install(partial, at: directory, imageURL: imageURL, store: store)
            return
        }
        let minMemory = Int((requirements.minimumSupportedMemorySize + 1_073_741_823) / 1_073_741_824)
        let cpu = try args.integer("cpus", default: max(requirements.minimumSupportedCPUCount, min(4, ProcessInfo.processInfo.activeProcessorCount / 2)), range: requirements.minimumSupportedCPUCount...VZVirtualMachineConfiguration.maximumAllowedCPUCount)
        let memory = try args.integer("memory", default: max(4, minMemory), range: minMemory...Int(VZVirtualMachineConfiguration.maximumAllowedMemorySize / 1_073_741_824))
        let disk = try args.integer("disk", default: 64, range: 32...2048)
        let width = try args.integer("width", default: 1280, range: 800...3840)
        let height = try args.integer("height", default: 800, range: 600...2160)
        let machine = Machine(name: name, cpu: cpu, memoryGiB: memory, diskGiB: disk, width: width, height: height, network: !args.flags.contains("offline"),
                              scale: args.flags.contains("standard") ? 1 : 2, control: args.flags.contains("host-control") ? "host" : nil)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        _ = try Mailbox(directory.appendingPathComponent("exchange"), create: true)
        try requirements.hardwareModel.dataRepresentation.write(to: directory.appendingPathComponent("hardware.bin"))
        try VZMacMachineIdentifier().dataRepresentation.write(to: directory.appendingPathComponent("identity.bin"))
        _ = try VZMacAuxiliaryStorage(creatingStorageAt: directory.appendingPathComponent("auxiliary.bin"), hardwareModel: requirements.hardwareModel, options: [])
        let diskURL = directory.appendingPathComponent("disk.img")
        guard FileManager.default.createFile(atPath: diskURL.path, contents: nil, attributes: [.posixPermissions: 0o600]) else { throw Failure("Cannot create disk image") }
        let file = try FileHandle(forWritingTo: diskURL)
        try file.truncate(atOffset: UInt64(disk) * 1_073_741_824)
        try file.close()
        try store.save(machine)
        try await install(machine, at: directory, imageURL: imageURL, store: store)
    }
    private static func install(_ original: Machine, at directory: URL, imageURL: URL, store: Store) async throws {
        var machine = original
        let vm = VZVirtualMachine(configuration: try configuration(machine, at: directory, tools: nil))
        let installer = VZMacOSInstaller(virtualMachine: vm, restoringFromImageAt: imageURL)
        stderr("Installing macOS into '\(machine.name)' (\(machine.cpu) CPUs, \(machine.memoryGiB) GiB RAM, \(machine.diskGiB) GiB sparse disk)…")
        let observation = installer.progress.observe(\.fractionCompleted, options: [.new]) { progress, _ in
            stderr(String(format: "Installation: %.0f%%", progress.fractionCompleted * 100))
        }
        defer { observation.invalidate() }
        try await installer.install()
        machine.installed = true
        try store.save(machine)
        stderr("macOS is installed in '\(machine.name)'.")
    }
}

@MainActor
final class MachineRuntime: NSObject, VZVirtualMachineDelegate, NSWindowDelegate {
    let vm: VZVirtualMachine
    let machine: Machine
    let directory: URL
    let mailbox: Mailbox
    let lock: Int32
    var window: NSWindow?
    var loop: Task<Void, Never>?
    var stopping = false
    var stopDeadline: Date?
    var hostControl: HostControl?
    var controlState = "guest"

    init(machine: Machine, directory: URL, tools: URL) throws {
        self.machine = machine
        self.directory = directory
        lock = open(directory.appendingPathComponent("runtime.lock").path, O_CREAT | O_RDWR | O_NOFOLLOW | O_CLOEXEC, 0o600)
        guard lock >= 0 else { throw Failure("Cannot open VM lock") }
        guard flock(lock, LOCK_EX | LOCK_NB) == 0 else { close(lock); throw Failure("VM is already running") }
        mailbox = try Mailbox(directory.appendingPathComponent("host-control"), create: true)
        vm = VZVirtualMachine(configuration: try VirtualMachines.configuration(machine, at: directory, tools: tools))
        super.init()
        vm.delegate = self
    }
    deinit { close(lock) }
    func start(show: Bool) async throws {
        if show { showWindow() }
        try await vm.start()
        stderr("VM '\(machine.name)' is running (PID \(getpid())).")
        if machine.usesHostControl {
            // A failure leaves the VM running (the native window still works) and is reported by status.
            do {
                let control = try await HostControl(vm: vm, exchange: directory.appendingPathComponent("exchange"), scale: machine.displayScale)
                control.start()
                hostControl = control
                controlState = "host"
            } catch {
                controlState = "host-unavailable"
                stderr("Host control is unavailable: \(error.localizedDescription)")
            }
        }
        loop = Task { @MainActor [weak self] in
            while let self, !Task.isCancelled {
                do { for request in try mailbox.requests() { await handle(request) } }
                catch { stderr(error.localizedDescription) }
                if let stopDeadline, Date() > stopDeadline {
                    stderr("Guest shutdown is taking longer than 60 seconds. Use stop --force if needed.")
                    self.stopDeadline = nil
                }
                try? await Task.sleep(nanoseconds: 100_000_000)
            }
        }
    }
    func showWindow() {
        if let window { window.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true); return }
        NSApp.setActivationPolicy(.regular)
        let view = VZVirtualMachineView(frame: NSRect(x: 0, y: 0, width: machine.width, height: machine.height))
        view.virtualMachine = vm
        view.capturesSystemKeys = true
        let window = NSWindow(contentRect: view.frame, styleMask: [.titled, .closable, .resizable, .miniaturizable], backing: .buffered, defer: false)
        window.title = "terminal-mac · \(machine.name)"
        window.contentView = view
        window.delegate = self
        window.isReleasedWhenClosed = false
        window.center(); window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        self.window = window
    }
    func windowShouldClose(_ sender: NSWindow) -> Bool { sender.orderOut(nil); return false }
    func handle(_ request: Request) async {
        do {
            var values: [String: String] = [:]
            switch request.action {
            case "status": values = ["name": machine.name, "state": stateName, "pid": String(getpid()), "control": controlState]
            case "show": showWindow()
            case "hide":
                // Hiding the app hands focus back to whatever was in front before, usually the terminal.
                window?.orderOut(nil); NSApp.hide(nil)
            case "pause": try await vm.pause()
            case "resume": try await vm.resume()
            case "stop":
                if request.arguments["force"] == "true" {
                    try await vm.stop()
                    try mailbox.respond(Reply(id: request.id, values: ["state": "stopped"]))
                    exit(0)
                }
                guard !stopping else { throw Failure("Shutdown is already in progress") }
                try vm.requestStop(); stopping = true; stopDeadline = Date().addingTimeInterval(60)
                values["state"] = "shutdown-requested"
            default: throw Failure("Unknown lifecycle action")
            }
            try mailbox.respond(Reply(id: request.id, values: values))
        } catch { try? mailbox.respond(Reply(id: request.id, error: error.localizedDescription)) }
    }
    var stateName: String {
        switch vm.state {
        case .stopped: return "stopped"
        case .running: return stopping ? "stopping" : "running"
        case .paused: return "paused"
        case .starting: return "starting"
        case .pausing: return "pausing"
        case .resuming: return "resuming"
        case .stopping: return "stopping"
        case .saving: return "saving"
        case .restoring: return "restoring"
        case .error: return "error"
        @unknown default: return "unknown"
        }
    }
    nonisolated func guestDidStop(_ virtualMachine: VZVirtualMachine) { stderr("Guest shut down."); exit(0) }
    nonisolated func virtualMachine(_ virtualMachine: VZVirtualMachine, didStopWithError error: Error) { stderr(error.localizedDescription); exit(1) }
}

func stderr(_ message: String) { FileHandle.standardError.write(Data((message + "\n").utf8)) }
