import AppKit
import Foundation
import SwiftUI

struct TextThread: Decodable, Identifiable {
    let conversation_id: String
    let recipient: String?
    let can_reply: Bool?
    let display_name: String?
    let preview: String?
    let timestamp: FlexibleTime?
    let unread: Bool?
    var id: String { conversation_id }
}

struct TextInbox: Decodable {
    let threads: [TextThread]
    let next_cursor: String?
    let cached_at: FlexibleTime?
    let source: String?
}

struct TextMessage: Decodable, Identifiable {
    let message_id: String
    let direction: String
    let body: String
    let timestamp: FlexibleTime?
    let status: String?
    var id: String { message_id }
}

struct TextConversation: Decodable {
    let conversation_id: String
    let recipient: String
    let messages: [TextMessage]
    let next_before: String?
    let cached_at: FlexibleTime?
    let source: String?
}

struct TextDraft: Decodable {
    let draft_id: String
    let owner: String
    let recipient: String
    let body: String
    let reply_to_message_id: String?
    let digest: String
    let phase: String
    let browser_cleanup_verified: Bool?
    let browser_cleanup_error_code: String?
    let error_code: String?
    let delivery_status: String?
}

struct TextBrowserRecovery: Decodable {
    let released: Bool
    let operation_token: String
    let unknown_messages_unmodified: Bool
}

enum MessageSafety {
    static let owner = "call-desk-ui"

    static func canRead(_ thread: TextThread) -> Bool {
        thread.recipient?.isEmpty == false && thread.can_reply != false
    }

    static func recovered(_ result: TextBrowserRecovery, expectedToken: String) -> Bool {
        result.released && result.operation_token == expectedToken && result.unknown_messages_unmodified
    }

    static func matches(_ draft: TextDraft, recipient: String, body: String, reply: String?) -> Bool {
        draft.owner == owner && draft.recipient == recipient && draft.body == body &&
        draft.reply_to_message_id == reply && !draft.digest.isEmpty && !draft.draft_id.isEmpty
    }

    static func sendable(_ draft: TextDraft, recipient: String, body: String, reply: String?) -> Bool {
        draft.phase == "draft" && matches(draft, recipient: recipient, body: body, reply: reply)
    }

    static func request(_ payload: [String: Any]) throws -> Data {
        let data = try JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys])
        guard data.count <= 65_536 else { throw BackendFailure.reported("The message request is too large.") }
        return data
    }

    static func statusLabel(_ phase: String) -> String {
        switch phase {
        case "draft": return "Draft ready for review"
        case "preflight": return "Checking the recipient and browser"
        case "send_intent": return "Send requested · result not yet verified"
        case "sent_confirmed": return "Sent · independently confirmed"
        case "sent_observed": return "Sent in Google Voice · delivery not verified"
        case "send_unknown": return "Send result uncertain · check status before any retry"
        case "failed_before_send": return "Stopped before sending"
        case "cancelled": return "Cancelled"
        default: return "Status unknown"
        }
    }

    static func selfTest() throws {
        let raw: [String: Any] = ["draft_id":"draft-test", "owner":owner, "recipient":"+15555550123",
                                  "body":"Line one\nExact \"text\" \\ $()", "digest":"digest-test", "phase":"draft",
                                  "delivery_status":"not_verified"]
        let data = try request(raw)
        let draft = try JSONDecoder().decode(TextDraft.self, from: data)
        precondition(matches(draft, recipient: draft.recipient, body: draft.body, reply: nil))
        precondition(!matches(draft, recipient: "+15555550124", body: draft.body, reply: nil))
        precondition(!matches(draft, recipient: draft.recipient, body: draft.body + " ", reply: nil))
        precondition(!matches(draft, recipient: draft.recipient, body: draft.body, reply: "different-message"))
        precondition(sendable(draft, recipient: draft.recipient, body: draft.body, reply: nil))
        for phase in ["preflight", "send_intent", "sent_observed", "sent_confirmed", "send_unknown", "failed_before_send", "cancelled"] {
            var changed = raw; changed["phase"] = phase
            let receipt = try JSONDecoder().decode(TextDraft.self, from: request(changed))
            precondition(!sendable(receipt, recipient: receipt.recipient, body: receipt.body, reply: nil))
        }
        let decoded = try JSONSerialization.jsonObject(with: data) as! [String: Any]
        precondition(decoded["body"] as? String == raw["body"] as? String)
        do {
            _ = try request(["body":String(repeating:"a", count:65_537)])
            preconditionFailure("Oversized request accepted")
        } catch BackendFailure.reported {}
        let unresolved = try JSONDecoder().decode(TextThread.self, from:request([
            "conversation_id":"unresolved", "recipient":NSNull(), "display_name":"Contact", "can_reply":false]))
        precondition(!canRead(unresolved))
        let resolved = try JSONDecoder().decode(TextThread.self, from:request([
            "conversation_id":"resolved", "recipient":"+15555550123", "can_reply":true]))
        precondition(canRead(resolved))
        let disabled = try JSONDecoder().decode(TextThread.self, from:request([
            "conversation_id":"disabled", "recipient":"+15555550123", "can_reply":false]))
        precondition(!canRead(disabled))
        let recovery = TextBrowserRecovery(released:true, operation_token:"expected", unknown_messages_unmodified:true)
        precondition(recovered(recovery, expectedToken:"expected"))
        precondition(!recovered(recovery, expectedToken:"other"))
        precondition(!recovered(TextBrowserRecovery(released:true, operation_token:"expected", unknown_messages_unmodified:false), expectedToken:"expected"))
        print("{\"ok\":true,\"messaging_fixture_tests\":19,\"native_actions\":0,\"messages_sent\":0}")
    }
}

@MainActor final class MessagesModel: ObservableObject {
    @Published var inbox: TextInbox?
    @Published var conversation: TextConversation?
    @Published var selectedConversation: String?
    @Published var recipient = ""
    @Published var body = ""
    @Published var replyTo: String?
    @Published var draft: TextDraft?
    @Published var lastReceipt: TextDraft?
    @Published var busy = false
    @Published var activity: String?
    @Published var failure: String?
    @Published var recoveryEvidence = ""
    private weak var desk: DeskModel?
    private var requestKey = UUID().uuidString

    init(desk: DeskModel) { self.desk = desk }

    var canSend: Bool {
        guard !busy, desk?.isBusy != true, let draft else { return false }
        return MessageSafety.sendable(draft, recipient: recipient, body: body, reply: replyTo)
    }

    func edited() {
        if let draft, !MessageSafety.matches(draft, recipient: recipient, body: body, reply: replyTo) {
            self.draft = nil
            requestKey = UUID().uuidString
        }
    }

    private func perform(_ command: String, payload: [String: Any], live: Bool,
                         activate: Bool = true, activity: String, done: @escaping (Data) throws -> Void) {
        guard !busy, !live || desk?.isBusy != true else { return }
        busy = true; self.activity = activity; failure = nil
        if live { desk?.messagingBusy = true }
        Task {
            defer {
                busy = false; self.activity = nil
                if live { desk?.messagingBusy = false }
            }
            do {
                let request = try MessageSafety.request(payload)
                if live && activate {
                    do { try CooperativeActivation.request() }
                    catch let error as ActivationFailure { throw BackendFailure.reported(error.message) }
                }
                let data = try await Backend.run(command, request: request, timeout: live ? 90 : 30)
                try done(data)
            } catch {
                failure = (error as? BackendFailure)?.message ?? "The message result could not be read. Check status before repeating an action."
            }
        }
    }

    func loadInbox(refresh: Bool, more: Bool = false) {
        var payload: [String: Any] = ["limit":20, "refresh":refresh]
        if more, let cursor = inbox?.next_cursor { payload["cursor"] = cursor }
        perform("text-inbox", payload: payload, live: refresh, activity: refresh ? "Refreshing inbox…" : "Reading cached inbox…") { [self] data in
            let value = try JSONDecoder().decode(TextInbox.self, from: data)
            if more, let previous = inbox {
                let known = Set(previous.threads.map(\.id))
                inbox = TextInbox(threads: previous.threads + value.threads.filter { !known.contains($0.id) },
                                  next_cursor:value.next_cursor, cached_at:value.cached_at, source:value.source)
            } else { inbox = value }
        }
    }

    func select(_ thread: TextThread) {
        guard !busy, MessageSafety.canRead(thread) else { return }
        selectedConversation = thread.conversation_id
        conversation = nil
        loadConversation(refresh: false)
    }

    func loadConversation(refresh: Bool, earlier: Bool = false) {
        guard let id = selectedConversation else { return }
        var payload: [String: Any] = ["conversation_id":id, "limit":50, "refresh":refresh]
        if earlier, let before = conversation?.next_before { payload["before"] = before }
        perform("text-read", payload:payload, live:refresh,
                activity:refresh ? "Refreshing conversation…" : "Reading cached conversation…") { [self] data in
            let value = try JSONDecoder().decode(TextConversation.self, from:data)
            guard value.conversation_id == id, selectedConversation == id else {
                throw BackendFailure.reported("The conversation changed. Select it again before replying.")
            }
            if earlier, let previous = conversation {
                let known = Set(previous.messages.map(\.id))
                conversation = TextConversation(conversation_id:id, recipient:value.recipient,
                    messages:value.messages.filter { !known.contains($0.id) } + previous.messages,
                    next_before:value.next_before, cached_at:value.cached_at, source:value.source)
            } else { conversation = value }
        }
    }

    func newMessage() {
        guard !busy else { return }
        recipient = ""; body = ""; replyTo = nil; draft = nil
        requestKey = UUID().uuidString; failure = nil
    }

    func reply(to message: TextMessage) {
        guard !busy, message.direction == "incoming", let conversation else { return }
        recipient = conversation.recipient; body = ""; replyTo = message.message_id; draft = nil
        requestKey = UUID().uuidString; failure = nil
    }

    func prepareDraft() {
        let to = recipient, text = body, reply = replyTo
        var payload: [String: Any] = ["owner":MessageSafety.owner, "request_key":requestKey,
                                      "recipient":to, "body":text]
        if let reply { payload["reply_to_message_id"] = reply }
        perform("text-draft", payload:payload, live:false, activity:"Preparing draft…") { [self] data in
            let value = try JSONDecoder().decode(TextDraft.self, from:data)
            guard MessageSafety.matches(value, recipient:recipient, body:body, reply:replyTo),
                  to == recipient, text == body, reply == replyTo else {
                throw BackendFailure.reported("The draft changed during review. Review the current text again.")
            }
            draft = value
        }
    }

    func sendReviewed() {
        guard canSend, let reviewed = draft else { return }
        // Retain the immutable draft before dispatch. Even a lost response leaves
        // its ID available for status; the UI never starts a replacement send.
        lastReceipt = reviewed
        draft = nil
        perform("text-send", payload:["draft_id":reviewed.draft_id, "owner":MessageSafety.owner,
                                       "expected_digest":reviewed.digest,
                                       "authorization":"User clicked Send reviewed message in Call Desk for draft \(reviewed.draft_id), digest \(reviewed.digest)."],
                live:true, activity:"Sending reviewed message…") { [self] data in
            let value = try JSONDecoder().decode(TextDraft.self, from:data)
            guard value.draft_id == reviewed.draft_id, value.digest == reviewed.digest else {
                throw BackendFailure.reported("The send receipt did not match this draft. Check its status before doing anything else.")
            }
            lastReceipt = value
        }
    }

    func checkSendStatus(refresh: Bool) {
        guard let receipt = lastReceipt else { return }
        perform("text-status", payload:["draft_id":receipt.draft_id, "refresh":refresh], live:refresh,
                activity:refresh ? "Verifying in Google Voice…" : "Reading send status…") { [self] data in
            let value = try JSONDecoder().decode(TextDraft.self, from:data)
            guard value.draft_id == receipt.draft_id, value.digest == receipt.digest else {
                throw BackendFailure.reported("The receipt did not match the original draft.")
            }
            lastReceipt = value
        }
    }

    func recoverBrowser(token: String, owner: String) {
        guard !recoveryEvidence.trimmingCharacters(in:.whitespacesAndNewlines).isEmpty else { return }
        perform("text-recover-browser", payload:["operation_token":token,"owner":owner,
                                                  "termination_evidence":recoveryEvidence],
                live:true, activate:false, activity:"Verifying idle browser before release…") { [self] data in
            let result = try JSONDecoder().decode(TextBrowserRecovery.self, from:data)
            guard MessageSafety.recovered(result, expectedToken:token) else {
                throw BackendFailure.reported("Browser release was not verified. The message outcome is unchanged.")
            }
            recoveryEvidence = ""
            if let desk { Task { await desk.refresh() } }
        }
    }
}

struct MessagesView: View {
    @ObservedObject var model: MessagesModel
    @ObservedObject var desk: DeskModel
    private var liveBlocked: Bool { model.busy || desk.isBusy }

    var body: some View {
        VStack(alignment:.leading, spacing:12) {
            HStack {
                Text("Messages").font(.title2.bold())
                Spacer()
                if model.busy { ProgressView().controlSize(.small) }
                Button("Refresh inbox") { model.loadInbox(refresh:true) }.disabled(liveBlocked)
                Button("New message") { model.newMessage() }.disabled(model.busy)
                Button("Stop / disarm calls") { desk.perform("disarm") }.disabled(desk.stopping)
            }
            Text("Shows messages observed so far, not necessarily all history. Refresh reads Google Voice and may mark a conversation read.")
                .font(.caption).foregroundStyle(.secondary)
            if let text = model.failure ?? model.activity {
                Text(text).font(.callout).foregroundStyle(model.failure == nil ? Color.secondary : Color.orange)
            }
            HSplitView {
                VStack(alignment:.leading) {
                    List(model.inbox?.threads ?? []) { thread in
                        Button { model.select(thread) } label: {
                            VStack(alignment:.leading, spacing:4) {
                                HStack {
                                    Text(thread.display_name?.isEmpty == false ? thread.display_name! : thread.recipient ?? "Number unresolved").fontWeight(thread.unread == true ? .semibold : .regular)
                                    if thread.unread == true { Circle().frame(width:6,height:6).foregroundStyle(.blue) }
                                }
                                Text(thread.preview ?? "").font(.caption).lineLimit(2).foregroundStyle(.secondary)
                                if !MessageSafety.canRead(thread) {
                                    Text("Number unresolved · cannot reply").font(.caption2).foregroundStyle(.secondary)
                                }
                            }.frame(maxWidth:.infinity, alignment:.leading).padding(.vertical,3)
                        }.buttonStyle(.plain).disabled(model.busy || !MessageSafety.canRead(thread))
                    }
                    if model.inbox?.threads.isEmpty != false {
                        Text("No cached conversations. Use Refresh inbox to check Google Voice.").font(.caption).foregroundStyle(.secondary).padding(8)
                    }
                    if model.inbox?.next_cursor != nil {
                        Button("More cached conversations") { model.loadInbox(refresh:false,more:true) }.disabled(model.busy)
                    }
                }.frame(minWidth:200,idealWidth:240,maxWidth:300)
                VStack(alignment:.leading, spacing:10) {
                    HStack {
                        Text(model.conversation?.recipient ?? "Select a conversation").font(.headline)
                        Spacer()
                        Button("Refresh conversation") { model.loadConversation(refresh:true) }
                            .disabled(liveBlocked || model.selectedConversation == nil)
                    }
                    if model.conversation?.next_before != nil {
                        Button("Earlier cached messages") { model.loadConversation(refresh:false,earlier:true) }.disabled(model.busy)
                    }
                    ScrollView {
                        LazyVStack(alignment:.leading, spacing:12) {
                            ForEach(model.conversation?.messages ?? []) { message in
                                VStack(alignment:.leading, spacing:4) {
                                    HStack {
                                        Text(message.direction == "incoming" ? "Received" : "Outgoing").font(.caption.bold())
                                        if let date = message.timestamp?.date { Text(date.formatted()).font(.caption).foregroundStyle(.secondary) }
                                        Spacer()
                                        if message.direction == "incoming" {
                                            Button("Reply") { model.reply(to:message) }.font(.caption).disabled(model.busy)
                                        }
                                    }
                                    Text(message.body).textSelection(.enabled).frame(maxWidth:.infinity,alignment:.leading)
                                }.padding(10).background(Color.secondary.opacity(0.07)).cornerRadius(6)
                            }
                        }
                    }.frame(minHeight:130)
                    Divider()
                    Text(model.replyTo == nil ? "New message" : "Reply to the selected received message").font(.headline)
                    TextField("Recipient in international format, e.g. +15555550123", text:$model.recipient)
                        .textFieldStyle(.roundedBorder).disabled(model.busy || model.replyTo != nil)
                    TextEditor(text:$model.body).font(.body).frame(minHeight:80,maxHeight:130)
                        .overlay(RoundedRectangle(cornerRadius:4).stroke(Color.secondary.opacity(0.3))).disabled(model.busy)
                    if let draft = model.draft {
                        VStack(alignment:.leading,spacing:4) {
                            Text("Review · \(draft.recipient)").font(.subheadline.bold())
                            ScrollView { Text(draft.body).textSelection(.enabled).frame(maxWidth:.infinity,alignment:.leading) }.frame(maxHeight:100)
                        }.padding(10).background(Color.blue.opacity(0.07)).cornerRadius(6)
                    }
                    HStack {
                        Button("Review draft") { model.prepareDraft() }.disabled(model.busy || model.recipient.isEmpty || model.body.isEmpty)
                        Spacer()
                        Button("Send reviewed message") { model.sendReviewed() }.disabled(!model.canSend)
                    }
                    if let receipt = model.lastReceipt {
                        Divider()
                        Text(MessageSafety.statusLabel(receipt.phase)).font(.subheadline.bold())
                        if receipt.browser_cleanup_verified == false {
                            Text("Browser cleanup needs attention. Calls remain blocked until the return to the Calls page is verified.")
                                .font(.caption).foregroundStyle(.orange)
                        }
                        HStack {
                            Button("Check send status") { model.checkSendStatus(refresh:false) }.disabled(model.busy)
                            Button("Verify in Google Voice") { model.checkSendStatus(refresh:true) }.disabled(liveBlocked)
                        }
                    }
                    if let operation = desk.snapshot?.browser_operation,
                       let token = operation.token, let owner = operation.owner {
                        DisclosureGroup("Repair a stuck browser reservation") {
                            VStack(alignment:.leading,spacing:6) {
                                Text("First verify that the prior task and its native helper processes ended. Recovery then checks the already-open Calls page is idle; it does not resend or settle uncertain texts.")
                                    .font(.caption).foregroundStyle(.secondary)
                                TextField("Observed termination evidence or local proof path",text:$model.recoveryEvidence)
                                    .textFieldStyle(.roundedBorder)
                                Button("Verify idle and release browser") { model.recoverBrowser(token:token,owner:owner) }
                                    .disabled(liveBlocked || model.recoveryEvidence.trimmingCharacters(in:.whitespacesAndNewlines).isEmpty)
                            }
                        }.font(.caption)
                    }
                }.padding(.leading,10).frame(minWidth:460)
            }
        }.padding(18)
        .onChange(of:model.recipient) { model.edited() }
        .onChange(of:model.body) { model.edited() }
        .onChange(of:model.replyTo) { model.edited() }
        .onChange(of:desk.snapshot?.browser_operation?.token) { model.recoveryEvidence = "" }
    }
}
