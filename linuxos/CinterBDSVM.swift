import Foundation
import AppKit
import Virtualization

private func describeVMError(_ error: Error) -> String {
    let ns = error as NSError
    var lines = ["domain=\(ns.domain) code=\(ns.code) description=\(ns.localizedDescription)"]
    for (key, value) in ns.userInfo { lines.append("\(key)=\(value)") }
    var underlying = ns.userInfo[NSUnderlyingErrorKey] as? NSError
    var depth = 0
    while let detail = underlying, depth < 5 {
        lines.append("underlying[\(depth)] domain=\(detail.domain) code=\(detail.code) description=\(detail.localizedDescription) userInfo=\(detail.userInfo)")
        underlying = detail.userInfo[NSUnderlyingErrorKey] as? NSError
        depth += 1
    }
    return lines.joined(separator: " | ")
}


@available(macOS 13.0, *)
final class CinterVMDelegate: NSObject, VZVirtualMachineDelegate {
    var requestedExitCode: Int32?
    // Keep the graphical window alive for the lifetime of the VM.
    var displayWindow: NSWindow?
    func guestDidStop(_ virtualMachine: VZVirtualMachine) { exit(requestedExitCode ?? 0) }
    func virtualMachine(_ virtualMachine: VZVirtualMachine, didStopWithError error: Error) {
        fputs("CINTER_VM_ERROR \(describeVMError(error))\n", stderr); exit(30)
    }
}

@available(macOS 13.0, *)
func runCinterBDSVM() throws {
    let env = ProcessInfo.processInfo.environment
    guard let kernel = env["CINTER_VM_KERNEL"], let initrd = env["CINTER_VM_INITRD"],
          let cmdlinePath = env["CINTER_VM_CMDLINE"], let bds = env["CINTER_BDS_HOST_DIR"] else {
        throw NSError(domain:"CinterBDSVM", code:2, userInfo:[NSLocalizedDescriptionKey:
          "CINTER_VM_KERNEL, CINTER_VM_INITRD, CINTER_VM_CMDLINE and CINTER_BDS_HOST_DIR are required"])
    }
    if VZLinuxRosettaDirectoryShare.availability == .notInstalled {
        let install = DispatchSemaphore(value: 0)
        var installError: Error?
        VZLinuxRosettaDirectoryShare.installRosetta { error in installError = error; install.signal() }
        install.wait()
        if let error = installError { throw error }
    }
    guard VZLinuxRosettaDirectoryShare.availability == .installed else {
        throw NSError(domain:"CinterBDSVM", code:3, userInfo:[NSLocalizedDescriptionKey:"Rosetta for Linux is unavailable on this Mac"])
    }
    // Startup-only probes isolate host Virtualization.framework attachments.
    // They never run the Bedrock service or modify its persistent content.
    let probe = env["CINTER_VM_PROBE_STAGE"]
    let validProbes = ["kernel", "initrd", "serial", "network", "entropy", "boot", "disk", "builds", "rosetta", "full"]
    if let probe = probe, !validProbes.contains(probe) {
        throw NSError(domain: "CinterBDSVM", code: 7, userInfo: [NSLocalizedDescriptionKey: "Unknown VM probe stage: \(probe)"])
    }
    let config = VZVirtualMachineConfiguration()
    config.cpuCount = max(2, min(4, VZVirtualMachineConfiguration.maximumAllowedCPUCount))
    config.memorySize = 4 * 1024 * 1024 * 1024
    let boot = VZLinuxBootLoader(kernelURL: URL(fileURLWithPath: kernel))
    if probe != "kernel" { boot.initialRamdiskURL = URL(fileURLWithPath: initrd) }
    let generatedCommandLine = try String(contentsOfFile: cmdlinePath, encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines)
    guard !generatedCommandLine.isEmpty else {
        throw NSError(domain:"CinterBDSVM", code:4, userInfo:[NSLocalizedDescriptionKey:"generated LinuxKit kernel command line is empty"])
    }
    // LinuxKit runs only the server appliance; Ubuntu owns compiler workloads.
    boot.commandLine = generatedCommandLine + " cinter.mode=server-appliance"
    config.bootLoader = boot
    let console = VZVirtioConsoleDeviceSerialPortConfiguration()
    console.attachment = VZFileHandleSerialPortAttachment(fileHandleForReading: .standardInput,
                                                           fileHandleForWriting: .standardOutput)
    config.serialPorts = (probe == "kernel" || probe == "initrd") ? [] : [console]
    let sharedRoot = bds
    let bdsShare = VZSharedDirectory(url: URL(fileURLWithPath: sharedRoot), readOnly: false)
    let bdsFS = VZVirtioFileSystemDeviceConfiguration(tag: "cinter-bds")
    bdsFS.share = VZSingleDirectoryShare(directory: bdsShare)
    let rosettaFS = VZVirtioFileSystemDeviceConfiguration(tag: "rosetta")
    rosettaFS.share = try VZLinuxRosettaDirectoryShare()
    if ["kernel", "initrd", "serial", "network", "entropy", "boot", "disk"].contains(probe ?? "") {
        config.directorySharingDevices = []
    } else if probe == "builds" {
        config.directorySharingDevices = [bdsFS]
    } else {
        config.directorySharingDevices = [bdsFS, rosettaFS]
    }
    fputs("CINTER_VM_SHARE tag=cinter-bds root=\(sharedRoot)\n", stderr)
    fputs("CINTER_VM_SHARE tag=rosetta\n", stderr)
    fputs("CINTER_VM_MODE server-appliance\n", stderr)
    fflush(stderr)
    let network = VZVirtioNetworkDeviceConfiguration()
    // The VZ bridged attachment requires Apple's restricted com.apple.vm.networking
    // entitlement and is unavailable to a locally ad-hoc signed binary.
    // Use the original Virtualization.framework guest network. The existing
    // user-space LAN relay exposes the supported Bedrock ports on the host.
    network.attachment = VZNATNetworkDeviceAttachment()
    fputs("CINTER_VM_NETWORK_MODE nat-with-host-lan-relay\n", stderr)
    config.networkDevices = ["kernel", "initrd", "serial"].contains(probe ?? "") ? [] : [network]
    config.entropyDevices = ["kernel", "initrd", "serial", "network"].contains(probe ?? "") ? [] : [VZVirtioEntropyDeviceConfiguration()]
    // Opt-in native host window. This is a VirtIO framebuffer, NOT a claim
    // of accelerated OpenGL/Vulkan in the Linux guest. Guest DRM/KMS and
    // a Metal-backed rendering path must be implemented separately.
    let graphical = env["CINTER_VM_GRAPHICAL"] == "1"
    if graphical {
        guard probe == nil else {
            throw NSError(domain: "CinterBDSVM", code: 8, userInfo: [NSLocalizedDescriptionKey:
                "CINTER_VM_GRAPHICAL cannot be combined with attachment probes"])
        }
        let display = VZVirtioGraphicsDeviceConfiguration()
        display.scanouts = [VZVirtioGraphicsScanoutConfiguration(widthInPixels: 1280, heightInPixels: 800)]
        config.graphicsDevices = [display]
        let keyboard = VZUSBKeyboardConfiguration()
        let pointer = VZUSBScreenCoordinatePointingDeviceConfiguration()
        config.keyboards = [keyboard]
        config.pointingDevices = [pointer]
    }
    try config.validate()
    fputs("CINTER_VM_PROBE stage=\(probe ?? "normal")\n", stderr)
    fputs("CINTER_VM_CONFIG cpu=\(config.cpuCount) memory=\(config.memorySize) storage=\(config.storageDevices.count) virtiofs=\(config.directorySharingDevices.count) serial=\(config.serialPorts.count) network=\(config.networkDevices.count) entropy=\(config.entropyDevices.count) kernel=\(kernel) initrd=\(initrd)\n", stderr)
    fflush(stderr)
    let vm = VZVirtualMachine(configuration: config)
    let delegate = CinterVMDelegate(); vm.delegate = delegate
    if graphical {
        // AppKit must run on the main thread; the VM's serial console and
        // existing stop-request protocol remain available independently.
        let app = NSApplication.shared
        app.setActivationPolicy(.regular)
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1280, height: 800),
                              styleMask: [.titled, .closable, .miniaturizable, .resizable],
                              backing: .buffered, defer: false)
        window.title = "CinterForge Linux VM"
        let view = VZVirtualMachineView(frame: window.contentView!.bounds)
        view.autoresizingMask = [.width, .height]
        view.virtualMachine = vm
        window.contentView = view
        window.center()
        window.makeKeyAndOrderFront(nil)
        app.activate(ignoringOtherApps: true)
        delegate.displayWindow = window
        fputs("CINTER_VM_DISPLAY native-window=1 virtio-framebuffer=1 gpu-acceleration=unverified\n", stderr)
    }
    let stopRequestURL = URL(fileURLWithPath: bds).appendingPathComponent(".cinter/vm-stop-request")
    try? FileManager.default.removeItem(at: stopRequestURL)
    var stopRequested = false
    let stopWatcher = DispatchSource.makeTimerSource(queue: .main)
    stopWatcher.schedule(deadline: .now(), repeating: .milliseconds(100))
    stopWatcher.setEventHandler {
        guard !stopRequested, let raw = try? String(contentsOf: stopRequestURL, encoding: .utf8),
              let code = Int32(raw.trimmingCharacters(in: .whitespacesAndNewlines)) else { return }
        stopRequested = true
        delegate.requestedExitCode = code
        fputs("CINTER_VM_STOP_REQUESTED bds_rc=\(code)\n", stderr); fflush(stderr)
        vm.stop { error in
            if let error = error {
                fputs("CINTER_VM_ERROR stop failed: \(error)\n", stderr); fflush(stderr); exit(32)
            }
            fputs("CINTER_VM_STOPPED bds_rc=\(code)\n", stderr); fflush(stderr)
            exit(code)
        }
    }
    stopWatcher.resume()
    if probe != nil {
        // A successful start is sufficient for this host-attachment probe.
        // Bound the attempt even when Virtualization.framework never calls back.
        DispatchQueue.main.asyncAfter(deadline: .now() + 25) {
            fputs("CINTER_VM_PROBE_TIMEOUT stage=\(probe!)\n", stderr); fflush(stderr); exit(33)
        }
    }
    vm.start { result in
        switch result {
        case .success:
            fputs("CINTER_VM_STARTED\n", stderr); fflush(stderr)
            if let probe = probe {
                fputs("CINTER_VM_PROBE_SUCCESS stage=\(probe)\n", stderr); fflush(stderr)
                // Terminate this disposable probe VM; never run a real job.
                vm.stop { error in
                    if let error = error {
                        fputs("CINTER_VM_PROBE_STOP_ERROR \(describeVMError(error))\n", stderr)
                    }
                    fflush(stderr); exit(0)
                }
            }
        case .failure(let error):
            fputs("CINTER_VM_ERROR \(describeVMError(error))\n", stderr); fflush(stderr); exit(31)
        }
    }
    // dispatchMain() runs a dispatch queue but does not pump AppKit events.
    // Without NSApplication.run(), the VirtIO display may appear frozen and
    // keyboard/mouse events never reach the guest. Headless jobs retain the
    // original dispatch-only lifecycle.
    if graphical {
        NSApplication.shared.run()
    } else {
        dispatchMain()
    }
}

if #available(macOS 13.0, *) {
    do { try runCinterBDSVM() } catch { fputs("CINTER_VM_ERROR \(error)\n", stderr); exit(1) }
} else { fputs("CINTER_VM_ERROR macOS 13 or newer required\n", stderr); exit(1) }
