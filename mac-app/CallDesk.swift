import AppKit
import Combine
import Darwin
import Foundation
import SwiftUI

private let serviceDirectory = URL(fileURLWithPath: ProcessInfo.processInfo.environment["CALL_DESK_SERVICE_ROOT"] ?? (NSHomeDirectory() + "/CodingProjects/shared-call-desk"), isDirectory: true)

struct DeskSnapshot: Decodable {
    struct Overall: Decodable { let state: String; let label: String; let detail: String }
    struct Check: Decodable, Identifiable { let id: String; let label: String; let state: String; let detail: String }
    struct Incoming: Decodable {
        let enabled: Bool?
        let running: Bool?
        let phase: String?
        let allowed_callers: [String]?
        let expires_at: FlexibleTime?
    }
    struct ActiveCall: Decodable { let owner: String?; let phase: String?; let call_id: String? }
    struct BrowserOperation: Decodable { let token: String?; let owner: String?; let status: String? }
    let overall: Overall
    let checks: [Check]
    let incoming: Incoming?
    let active_call: ActiveCall?
    let browser_operation: BrowserOperation?
    let last_error: String?
}

struct FlexibleTime: Decodable {
    let date: Date?
    init(from decoder: Decoder) throws {
        let value = try decoder.singleValueContainer()
        if let epoch = try? value.decode(Double.self) { date = Date(timeIntervalSince1970: epoch) }
        else if let string = try? value.decode(String.self) {
            let formatter = ISO8601DateFormatter()
            formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
            date = formatter.date(from: string) ?? ISO8601DateFormatter().date(from: string)
        } else { date = nil }
    }
}

enum BackendFailure: Error {
    case missingBackend, launchFailed, timedOut, failed, unreadable
    case reported(String)
    case continuing(String)
    var message: String {
        switch self {
        case .missingBackend: return "Call Desk’s local backend is not installed at its configured path."
        case .launchFailed: return "The local backend could not start. Check the logs."
        case .timedOut: return "The backend did not finish in time. Its result is uncertain; check connections before repeating an action."
        case .failed: return "The backend reported a problem. Check connections or open the local logs."
        case .unreadable: return "The backend response could not be read. Connection health is unknown."
        case .reported(let message): return message
        case .continuing(let command):
            if command == "text-send" {
                return "The send result is not yet verified. The original request is still being checked; use Check send status. Do not resend it."
            }
            if command.hasPrefix("text-") {
                return "The messaging operation is still completing. Its result and browser cleanup are not yet verified."
            }
            return command == "disarm"
                ? "Audio cleanup is still running. The backend was left running so restoration can finish; its result is not yet verified."
                : "Audio setup is still running. Its result is not yet verified. Use Stop / disarm to cancel safely."
        }
    }
}

enum Backend {
    private static let pendingLock = NSLock()
    private static var continuingCommands: [UUID: String] = [:]
    private static let softTimeoutCommands: Set<String> = ["arm", "disarm", "text-send", "text-inbox", "text-read", "text-status", "text-recover-browser"]

    static var continuingMessage: String? {
        pendingLock.lock(); defer { pendingLock.unlock() }
        guard !continuingCommands.isEmpty else { return nil }
        let command = continuingCommands.values.contains("disarm") ? "disarm" : continuingCommands.values.contains("text-send") ? "text-send" : continuingCommands.values.sorted().first ?? "arm"
        return BackendFailure.continuing(command).message
    }

    static func isContinuing(_ command: String) -> Bool {
        pendingLock.lock(); defer { pendingLock.unlock() }
        return continuingCommands.values.contains(command)
    }

    private static func retainContinuing(_ command: String) -> UUID {
        let id = UUID()
        pendingLock.lock(); continuingCommands[id] = command; pendingLock.unlock()
        return id
    }

    private static func finishContinuing(_ id: UUID) {
        pendingLock.lock(); continuingCommands.removeValue(forKey: id); pendingLock.unlock()
    }

    static func run(_ command: String, request: Data? = nil, timeout: TimeInterval) async throws -> Data {
        try await withCheckedThrowingContinuation { continuation in
            DispatchQueue.global(qos: .utility).async {
                do { continuation.resume(returning: try execute(command, request: request, timeout: timeout)) }
                catch { continuation.resume(throwing: error) }
            }
        }
    }

    private static func execute(_ command: String, request: Data?, timeout: TimeInterval) throws -> Data {
        if isContinuing(command), softTimeoutCommands.contains(command) {
            throw BackendFailure.continuing(command)
        }
        let manager = FileManager.default
        let python = serviceDirectory.appendingPathComponent(".venv/bin/python")
        let script = serviceDirectory.appendingPathComponent("desktop_api.py")
        guard manager.isExecutableFile(atPath: python.path), manager.fileExists(atPath: script.path) else {
            throw BackendFailure.missingBackend
        }
        // A private, bounded temporary response avoids pipe-buffer deadlocks.
        // Only typed status fields are rendered; raw errors are never displayed.
        let temp = manager.temporaryDirectory.appendingPathComponent("call-desk-\(UUID().uuidString).json")
        guard manager.createFile(atPath: temp.path, contents: nil, attributes: [.posixPermissions: 0o600]) else {
            throw BackendFailure.launchFailed
        }
        var removeResponseOnReturn = true
        defer { if removeResponseOnReturn { try? manager.removeItem(at: temp) } }
        let output = try FileHandle(forWritingTo: temp)
        defer { try? output.close() }
        // A separate private input file keeps phone numbers, message text, and
        // authorization out of argv and avoids filling a pipe before launch.
        let requestURL = manager.temporaryDirectory.appendingPathComponent("call-desk-input-\(UUID().uuidString).json")
        var input: FileHandle?
        defer {
            try? input?.close()
            try? manager.removeItem(at: requestURL)
        }
        if let request {
            guard request.count <= 65_536,
                  manager.createFile(atPath: requestURL.path, contents: request, attributes: [.posixPermissions: 0o600]) else {
                throw BackendFailure.launchFailed
            }
            input = try FileHandle(forReadingFrom: requestURL)
        }
        let process = Process()
        process.executableURL = python
        process.arguments = [script.path, command]
        process.currentDirectoryURL = serviceDirectory
        process.standardInput = input ?? FileHandle.nullDevice
        process.standardOutput = output
        process.standardError = FileHandle.nullDevice
        let terminated = DispatchSemaphore(value: 0)
        process.terminationHandler = { _ in terminated.signal() }
        do { try process.run() } catch { throw BackendFailure.launchFailed }
        if terminated.wait(timeout: .now() + timeout) == .timedOut {
            if softTimeoutCommands.contains(command) {
                // These commands can be inside native route restoration. A
                // UI timeout is not permission to kill them halfway through.
                // Return control to the app, retain the process/response until
                // completion, and keep the unresolved operation visible.
                let operation = retainContinuing(command)
                removeResponseOnReturn = false
                DispatchQueue.global(qos: .utility).async {
                    terminated.wait()
                    _ = process.terminationStatus // Retain/reap this exact child.
                    try? manager.removeItem(at: temp)
                    finishContinuing(operation)
                }
                throw BackendFailure.continuing(command)
            }
            if process.isRunning { process.terminate() }
            if terminated.wait(timeout: .now() + 1) == .timedOut, process.isRunning {
                kill(process.processIdentifier, SIGKILL)
                _ = terminated.wait(timeout: .now() + 1)
            }
            throw BackendFailure.timedOut
        }
        try output.synchronize()
        let attributes = try manager.attributesOfItem(atPath: temp.path)
        guard let size = attributes[.size] as? NSNumber, size.intValue <= 1_048_576 else {
            throw BackendFailure.unreadable
        }
        let data = try Data(contentsOf: temp)
        if let body = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           let message = body["error"] as? String {
            let safe = redactError(message)
            throw BackendFailure.reported(safe)
        }
        guard process.terminationStatus == 0 else { throw BackendFailure.failed }
        return data
    }

    static func redactError(_ message: String) -> String {
        if ["op://", "sk-", "Bearer ", "api_key", "api-key"].contains(where: { message.contains($0) }) {
            return BackendFailure.failed.message
        }
        return String(message.prefix(500))
    }

    static func decode(_ data: Data) -> DeskSnapshot? {
        let decoder = JSONDecoder()
        if let snapshot = try? decoder.decode(DeskSnapshot.self, from: data) { return snapshot }
        struct Envelope: Decodable { let status: DeskSnapshot }
        return (try? decoder.decode(Envelope.self, from: data))?.status
    }
}

// A user-clicked Call Desk window can hand its active state to the enrolled
// process. A background helper cannot yield another application's activation.
struct ActivationTarget: Decodable {
    let app: String
    let window_id: String
    let pid: Int32
    let process_instance: String

    var windowNumber: CGWindowID? {
        guard window_id.hasPrefix("w-"), let number = UInt32(window_id.dropFirst(2)), number > 0,
              window_id == "w-\(number)" else { return nil }
        return number
    }
}

struct ActivationEvidence {
    let app: String
    let window: CGWindowID
    let pid: Int32
    let processInstance: String
    let layer: Int
    let bundleIdentifier: String
}

enum ActivationFailure: Error {
    case invalidEnrollment, changedIdentity, unavailable, inactiveSender, requestRejected
    var message: String {
        switch self {
        case .invalidEnrollment: return "The saved Google Voice window identity is invalid. Reconnect it before continuing."
        case .changedIdentity: return "The connected Chrome window or process changed. Reconnect Google Voice before continuing."
        case .unavailable: return "The connected Google Voice window is unavailable. No replacement window was opened."
        case .inactiveSender: return "Click Call Desk’s window, then try the action again. macOS needs an active app to hand focus to Google Voice."
        case .requestRejected: return "macOS did not accept the Google Voice activation request. No browser action was sent."
        }
    }
}

enum CooperativeActivation {
    static func validate(_ target: ActivationTarget, _ evidence: ActivationEvidence) throws {
        guard target.app == "Google Chrome", target.pid > 0, let number = target.windowNumber,
              !target.process_instance.isEmpty else { throw ActivationFailure.invalidEnrollment }
        guard evidence.app == target.app, evidence.window == number, evidence.pid == target.pid,
              evidence.processInstance == target.process_instance, evidence.layer == 0,
              evidence.bundleIdentifier == "com.google.Chrome" else { throw ActivationFailure.changedIdentity }
    }

    // Injectable contract for offline tests. Revalidate after yielding and
    // immediately before activation; a yielded request is never proof of focus.
    static func handoff(_ target: ActivationTarget, evidence: () throws -> ActivationEvidence,
                        senderActive: () -> Bool, yield: () -> Void, activate: () -> Bool) throws {
        guard senderActive() else { throw ActivationFailure.inactiveSender }
        try validate(target, evidence())
        guard senderActive() else { throw ActivationFailure.inactiveSender }
        yield()
        try validate(target, evidence())
        guard activate() else { throw ActivationFailure.requestRejected }
    }

    @MainActor static func request() throws {
        let path = serviceDirectory.appendingPathComponent("state/google-voice-ui.json")
        guard let data = try? Data(contentsOf: path),
              let target = try? JSONDecoder().decode(ActivationTarget.self, from: data) else {
            throw ActivationFailure.invalidEnrollment
        }
        guard target.app == "Google Chrome", target.pid > 0,
              !target.process_instance.isEmpty, let number = target.windowNumber,
              let running = NSRunningApplication(processIdentifier: target.pid) else {
            throw ActivationFailure.unavailable
        }
        var record: [String: Any] = ["started_at": Date().timeIntervalSince1970,
            "app": target.app, "window_id": target.window_id, "pid": target.pid,
            "process_instance": target.process_instance, "yield_dispatched": false,
            "activation_dispatched": false, "exact_window_verified": false]
        defer {
            record["finished_at"] = Date().timeIntervalSince1970
            if let receipt = try? JSONSerialization.data(withJSONObject: record, options: [.sortedKeys]),
               (try? receipt.write(to: serviceDirectory.appendingPathComponent("state/last-activation-handoff.json"), options: .atomic)) != nil {
                try? FileManager.default.setAttributes([.posixPermissions: 0o600],
                    ofItemAtPath: serviceDirectory.appendingPathComponent("state/last-activation-handoff.json").path)
            }
        }
        do {
            try handoff(target, evidence: {
                guard !running.isTerminated,
                      let rows = CGWindowListCopyWindowInfo(.optionIncludingWindow, number) as? [[String: Any]] else {
                    throw ActivationFailure.unavailable
                }
                let matches = rows.filter { ($0[kCGWindowNumber as String] as? NSNumber)?.uint32Value == number }
                guard matches.count == 1, let row = matches.first,
                      let owner = row[kCGWindowOwnerName as String] as? String,
                      let pid = row[kCGWindowOwnerPID as String] as? NSNumber,
                      let layer = row[kCGWindowLayer as String] as? NSNumber else {
                    throw ActivationFailure.unavailable
                }
                var info = proc_bsdinfo()
                let size = Int32(MemoryLayout<proc_bsdinfo>.size)
                guard proc_pidinfo(target.pid, PROC_PIDTBSDINFO, 0, &info, size) == size,
                      info.pbi_pid == UInt32(target.pid) else { throw ActivationFailure.unavailable }
                return ActivationEvidence(app: owner, window: number, pid: pid.int32Value,
                    processInstance: "macos-proc-v1:\(info.pbi_start_tvsec):\(info.pbi_start_tvusec)",
                    layer: layer.intValue, bundleIdentifier: running.bundleIdentifier ?? "")
            }, senderActive: { NSApplication.shared.isActive }, yield: {
                NSApplication.shared.yieldActivation(to: running)
                record["yield_dispatched"] = true
            }, activate: {
                record["activation_dispatched"] = true
                return running.activate(from: .current, options: [])
            })
            record["status"] = "requested"
            // Exact focus, current origin/account and controls remain the
            // backend helper's responsibility before any browser action.
        } catch {
            record["status"] = "failed"
            record["error"] = String(describing: error)
            throw error
        }
    }

    static func selfTest() throws {
        let target = ActivationTarget(app: "Google Chrome", window_id: "w-123", pid: 777,
                                      process_instance: "macos-proc-v1:100:200")
        let valid = ActivationEvidence(app: target.app, window: 123, pid: 777,
                                       processInstance: target.process_instance, layer: 0,
                                       bundleIdentifier: "com.google.Chrome")
        var events: [String] = []
        try handoff(target, evidence: { events.append("identity"); return valid },
                    senderActive: { true }, yield: { events.append("yield") },
                    activate: { events.append("activate"); return true })
        precondition(events == ["identity", "yield", "identity", "activate"])
        events = []
        do {
            try handoff(target, evidence: { events.append("identity"); return valid },
                        senderActive: { false }, yield: { events.append("yield") },
                        activate: { events.append("activate"); return true })
            preconditionFailure("Inactive sender accepted")
        } catch ActivationFailure.inactiveSender {}
        precondition(events.isEmpty)
        for evidence in [
            ActivationEvidence(app: valid.app, window: 124, pid: 777, processInstance: valid.processInstance, layer: 0, bundleIdentifier: valid.bundleIdentifier),
            ActivationEvidence(app: valid.app, window: 123, pid: 778, processInstance: valid.processInstance, layer: 0, bundleIdentifier: valid.bundleIdentifier),
            ActivationEvidence(app: valid.app, window: 123, pid: 777, processInstance: "recycled", layer: 0, bundleIdentifier: valid.bundleIdentifier),
            ActivationEvidence(app: "Other", window: 123, pid: 777, processInstance: valid.processInstance, layer: 0, bundleIdentifier: valid.bundleIdentifier),
            ActivationEvidence(app: valid.app, window: 123, pid: 777, processInstance: valid.processInstance, layer: 1, bundleIdentifier: valid.bundleIdentifier),
            ActivationEvidence(app: valid.app, window: 123, pid: 777, processInstance: valid.processInstance, layer: 0, bundleIdentifier: "other")
        ] {
            events = []
            do {
                try handoff(target, evidence: { evidence }, senderActive: { true },
                            yield: { events.append("yield") }, activate: { events.append("activate"); return true })
                preconditionFailure("Changed identity accepted")
            } catch ActivationFailure.changedIdentity {}
            precondition(events.isEmpty)
        }
        events = []
        var reads = 0
        do {
            try handoff(target, evidence: {
                reads += 1
                if reads == 2 { throw ActivationFailure.changedIdentity }
                return valid
            }, senderActive: { true }, yield: { events.append("yield") },
               activate: { events.append("activate"); return true })
            preconditionFailure("Post-yield identity change accepted")
        } catch ActivationFailure.changedIdentity {}
        precondition(events == ["yield"])
        do {
            try handoff(target, evidence: { valid }, senderActive: { true }, yield: {}, activate: { false })
            preconditionFailure("Rejected activation accepted")
        } catch ActivationFailure.requestRejected {}
        print("{\"ok\":true,\"activation_fixture_tests\":10,\"native_actions\":0}")
    }
}

@MainActor final class DeskModel: ObservableObject {
    @Published var snapshot: DeskSnapshot?
    @Published var refreshedAt: Date?
    @Published var activity: String?
    @Published var failure: String?
    @Published var refreshing = false
    @Published var stopping = false
    @Published var backendFinishing = false
    @Published var messagingBusy = false
    private var timer: Timer?
    private var primaryAction: Task<Void, Never>?
    private var generation = 0

    var isBusy: Bool { activity != nil || stopping || backendFinishing || messagingBusy }
    var trayState: String {
        if failure != nil { return "attention" }
        if isBusy { return "busy" }
        return snapshot?.overall.state ?? "unknown"
    }

    func start() {
        Task { await refresh() }
        timer = Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { [weak self] _ in
            Task { @MainActor in
                // A soft-timed-out mutation continues in the background. Keep
                // polling its authoritative result instead of freezing its UI.
                guard let self, self.activity == nil, !self.stopping else { return }
                await self.refresh()
            }
        }
    }

    func refresh() async {
        guard !refreshing else { return }
        backendFinishing = Backend.continuingMessage != nil
        refreshing = true
        let version = generation
        defer { refreshing = false }
        do {
            let data = try await Backend.run("status", timeout: 20)
            guard version == generation else { return }
            guard let value = Backend.decode(data) else { throw BackendFailure.unreadable }
            snapshot = value; refreshedAt = Date()
            backendFinishing = Backend.continuingMessage != nil
            failure = Backend.continuingMessage ?? value.last_error.map(Backend.redactError)
        } catch {
            guard version == generation else { return }
            backendFinishing = Backend.continuingMessage != nil
            failure = Backend.continuingMessage ?? (error as? BackendFailure)?.message ?? BackendFailure.failed.message
        }
    }

    func perform(_ command: String) {
        if command == "disarm" { stop(); return }
        guard !isBusy else { return }
        generation += 1
        let version = generation
        switch command {
        case "arm": activity = "Preparing one test call…"
        case "show-voice": activity = "Showing Google Voice…"
        default: activity = "Checking connections…"
        }
        failure = nil
        primaryAction = Task {
            do {
                if command == "arm" || command == "show-voice" {
                    do { try CooperativeActivation.request() }
                    catch let error as ActivationFailure { throw BackendFailure.reported(error.message) }
                }
                guard version == generation else { throw CancellationError() }
                let data = try await Backend.run(command, timeout: command == "arm" ? 90 : 20)
                if version == generation, let value = Backend.decode(data) {
                    snapshot = value; refreshedAt = Date()
                }
            } catch {
                if version == generation { failure = (error as? BackendFailure)?.message ?? BackendFailure.failed.message }
            }
            if version == generation { activity = nil }
            primaryAction = nil
            // A mutation response is optional. Refresh authoritative health.
            if !stopping { activity = nil; await refresh() }
        }
    }

    private func stop() {
        guard !stopping else { return }
        if Backend.isContinuing("disarm") {
            backendFinishing = true
            failure = Backend.continuingMessage
            return
        }
        // Stop can be requested during a slow arm; backend owns serialization.
        // Do not terminate the arm subprocess or assume it was rolled back.
        generation += 1
        stopping = true
        activity = "Stopping / disarming…"
        failure = nil
        Task {
            // Focus trouble must never prevent a stop/cancellation request.
            // The backend still verifies exact focus before browser cleanup.
            var handoffFailure: String?
            do { try CooperativeActivation.request() }
            catch let error as ActivationFailure { handoffFailure = error.message }
            catch { handoffFailure = "The Google Voice activation handoff failed." }
            do {
                let data = try await Backend.run("disarm", timeout: 90)
                if let value = Backend.decode(data) { snapshot = value; refreshedAt = Date() }
            } catch { failure = (error as? BackendFailure)?.message ?? BackendFailure.failed.message }
            stopping = false
            activity = primaryAction == nil ? nil : "Waiting for prior operation to finish…"
            await refresh()
            if let handoffFailure, failure == nil, snapshot?.overall.state == "attention" {
                failure = handoffFailure
            }
        }
    }
}

private func stateColor(_ state: String) -> Color {
    switch state {
    case "ok", "ready": return .green
    case "attention": return .orange
    case "busy": return .blue
    default: return .secondary
    }
}

struct DeskView: View {
    @ObservedObject var model: DeskModel
    var openMessages: () -> Void = {}

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack(spacing: 10) {
                Image(systemName: "phone.fill").font(.title2)
                VStack(alignment: .leading, spacing: 2) {
                    Text("Call Desk").font(.title3.bold())
                    Text("Shared calling on this Mac").font(.caption).foregroundStyle(.secondary)
                }
                Spacer()
                if model.refreshing || model.isBusy { ProgressView().controlSize(.small) }
            }
            VStack(alignment: .leading, spacing: 6) {
                HStack(spacing: 7) {
                    Circle().fill(stateColor(model.trayState)).frame(width: 8, height: 8)
                    Text(model.activity ?? (model.failure == nil ? model.snapshot?.overall.label ?? "Checking status…" : "Needs attention")).font(.headline)
                }
                Text(model.failure ?? model.snapshot?.overall.detail ?? "Reading the local connection status.")
                    .font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
            if let snapshot = model.snapshot {
                ScrollView {
                    VStack(alignment: .leading, spacing: 13) {
                        ForEach(snapshot.checks) { check in
                            HStack(alignment: .top, spacing: 9) {
                                Image(systemName: check.state == "ok" ? "checkmark.circle.fill" : check.state == "attention" ? "exclamationmark.circle.fill" : "circle.dashed")
                                    .foregroundStyle(stateColor(check.state)).frame(width: 16)
                                VStack(alignment: .leading, spacing: 3) {
                                    Text(check.label).font(.callout.weight(.medium))
                                    Text(check.detail).font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                                }
                            }
                        }
                        if let incoming = snapshot.incoming {
                            Divider()
                            Text("Incoming calls").font(.callout.bold())
                            Text(incoming.enabled == true ? incoming.running == true ? "Armed · watcher running" : "Armed · watcher not confirmed" : "Automatic answering is off")
                                .font(.caption).foregroundStyle(.secondary)
                            if let callers = incoming.allowed_callers, !callers.isEmpty {
                                Text("Allowed: " + callers.joined(separator: ", ")).font(.caption).foregroundStyle(.secondary)
                            }
                            if incoming.enabled == true, let expires = incoming.expires_at?.date {
                                Text("Expires " + expires.formatted(date: .omitted, time: .shortened)).font(.caption).foregroundStyle(.secondary)
                            }
                        }
                        if let call = snapshot.active_call {
                            Divider()
                            Text("Current call").font(.callout.bold())
                            Text([call.owner, call.phase].compactMap { $0 }.joined(separator: " · "))
                                .font(.caption).foregroundStyle(.secondary)
                        }
                    }.frame(maxWidth: .infinity, alignment: .leading).padding(.trailing, 3)
                }.frame(maxHeight: 315)
            }
            Divider()
            VStack(spacing: 8) {
                HStack {
                    Button("Check connections") { model.perform("diagnose") }
                        .disabled(model.isBusy || model.snapshot?.browser_operation != nil)
                    Spacer()
                    Button("Arm one test call") { model.perform("arm") }
                        .disabled(model.isBusy || model.snapshot == nil || model.snapshot?.incoming?.enabled == true || model.snapshot?.active_call != nil || model.snapshot?.browser_operation != nil)
                }
                HStack {
                    Button("Stop / disarm") { model.perform("disarm") }.disabled(model.stopping)
                    Spacer()
                    Button("Show Google Voice") { model.perform("show-voice") }
                        .disabled(model.isBusy || model.snapshot?.browser_operation != nil)
                }
                Button("Messages", action: openMessages)
                Text("Stops voice; browser cleanup status remains visible.")
                    .font(.caption2).foregroundStyle(.secondary).frame(maxWidth: .infinity, alignment: .leading)
            }.controlSize(.small)
            HStack {
                Text(model.refreshedAt.map { "Last checked " + $0.formatted(date: .omitted, time: .standard) } ?? "No successful status check yet")
                    .font(.caption2).foregroundStyle(.secondary)
                Spacer()
                Button("Open logs") { NSWorkspace.shared.open(serviceDirectory.appendingPathComponent("state", isDirectory: true)) }
                    .buttonStyle(.link).font(.caption)
                Button("Quit") { NSApplication.shared.terminate(nil) }.buttonStyle(.link).font(.caption)
            }
        }
        .padding(18)
        .frame(width: 410)
        .frame(minHeight: 290, maxHeight: 680)
    }
}

@MainActor final class AppDelegate: NSObject, NSApplicationDelegate {
    private let model = DeskModel()
    private var statusItem: NSStatusItem!
    private let popover = NSPopover()
    private var statusWindow: NSWindow?
    private var statusWindowController: NSWindowController?
    private var messagesWindowController: NSWindowController?
    private lazy var messagesModel = MessagesModel(desk: model)
    private var subscriptions = Set<AnyCancellable>()

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApplication.shared.setActivationPolicy(.accessory)
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let button = statusItem.button {
            button.image = NSImage(systemSymbolName: "phone.fill", accessibilityDescription: "Call Desk")
            button.image?.isTemplate = true
            button.imagePosition = .imageLeading
            button.target = self; button.action = #selector(togglePopover)
        }
        popover.behavior = .transient
        popover.contentViewController = NSHostingController(rootView: DeskView(model: model, openMessages: { [weak self] in self?.showMessagesWindow() }))
        popover.contentSize = NSSize(width: 410, height: 640)
        Publishers.CombineLatest3(model.$snapshot, model.$activity, model.$failure)
            .receive(on: RunLoop.main).sink { [weak self] _, _, _ in
                DispatchQueue.main.async { self?.updateBadge() }
            }.store(in: &subscriptions)
        updateBadge(); model.start()
        if CommandLine.arguments.contains("--show-once") {
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) { [weak self] in
                self?.showStatusWindow()
            }
        }
    }

    private func updateBadge() {
        let state = model.trayState
        let color: NSColor
        switch state {
        case "ok", "ready": color = .systemGreen
        case "attention": color = .systemOrange
        case "busy": color = .systemBlue
        default: color = .secondaryLabelColor
        }
        statusItem.button?.attributedTitle = NSAttributedString(string: " ●", attributes: [.foregroundColor: color, .font: NSFont.systemFont(ofSize: 11)])
        statusItem.button?.toolTip = "Call Desk — " + (model.failure == nil ? model.activity ?? model.snapshot?.overall.label ?? "Status unknown" : "Needs attention")
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        showStatusWindow()
        return true
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }

    private func showStatusWindow() {
        popover.performClose(nil)
        if statusWindow == nil {
            let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 410, height: 640),
                                  styleMask: [.titled, .closable, .miniaturizable],
                                  backing: .buffered, defer: false)
            window.title = "Call Desk"
            window.isReleasedWhenClosed = false
            window.collectionBehavior = [.moveToActiveSpace]
            window.contentViewController = NSHostingController(rootView: DeskView(model: model, openMessages: { [weak self] in self?.showMessagesWindow() }))
            window.setContentSize(NSSize(width: 410, height: 640))
            window.center()
            statusWindow = window
            statusWindowController = NSWindowController(window: window)
        }
        statusWindowController?.showWindow(nil)
        statusWindow?.makeKeyAndOrderFront(nil)
        NSApplication.shared.activate(ignoringOtherApps: true)
    }

    private func showMessagesWindow() {
        popover.performClose(nil)
        if messagesWindowController == nil {
            let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 900, height: 700),
                                  styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
            window.title = "Call Desk — Messages"
            window.isReleasedWhenClosed = false
            window.minSize = NSSize(width: 760, height: 580)
            window.collectionBehavior = [.moveToActiveSpace]
            window.contentViewController = NSHostingController(rootView: MessagesView(model: messagesModel, desk: model))
            window.center()
            messagesWindowController = NSWindowController(window: window)
        }
        messagesWindowController?.showWindow(nil)
        messagesWindowController?.window?.makeKeyAndOrderFront(nil)
        NSApplication.shared.activate(ignoringOtherApps: true)
        messagesModel.loadInbox(refresh: false)
    }

    @objc private func togglePopover() {
        if popover.isShown { popover.performClose(nil) }
        else if let button = statusItem.button, button.window != nil, button.bounds.width > 0 {
            popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
            NSApplication.shared.activate(ignoringOtherApps: true)
        } else {
            showStatusWindow()
        }
    }
}

@main struct CallDeskMain {
    @MainActor static func main() {
        if CommandLine.arguments.contains("--activation-self-test") {
            do { try CooperativeActivation.selfTest() }
            catch { fputs("Activation self-test failed\n", stderr); exit(1) }
            return
        }
        if CommandLine.arguments.contains("--messaging-self-test") {
            do { try MessageSafety.selfTest() }
            catch { fputs("Messaging self-test failed\n", stderr); exit(1) }
            return
        }
        let app = NSApplication.shared
        let delegate = AppDelegate()
        app.delegate = delegate
        withExtendedLifetime(delegate) { app.run() }
    }
}
