// CcpGroups.swift — Server state, resource-group lifecycle API client and views
// (health / active / groups / models + deploy-teardown-redeploy picker).

import SwiftUI

// MARK: - Domain types

enum ServerState: Equatable {
    case up, degraded, down, unknown

    var label: String {
        switch self {
        case .up: "Running"
        case .degraded: "Degraded"
        case .down: "Stopped"
        case .unknown: "Checking…"
        }
    }

    var symbol: String {
        switch self {
        case .up: "checkmark.circle.fill"
        case .degraded: "exclamationmark.triangle.fill"
        case .down: "xmark.circle.fill"
        case .unknown: "circle.dotted"
        }
    }

    var color: Color {
        switch self {
        case .up: .green
        case .degraded: .orange
        case .down: .red
        case .unknown: .yellow
        }
    }
}

enum GroupStatus: String {
    case deployed, tornDown = "torn_down", undeployed, deploying, tearingDown = "tearing_down", error, unknown

    var label: String {
        switch self {
        case .deployed: "Deployed"
        case .tornDown: "Torn down"
        case .undeployed: "Not deployed"
        case .deploying: "Deploying"
        case .tearingDown: "Tearing down"
        case .error: "Error"
        case .unknown: "Unknown"
        }
    }

    var color: Color {
        switch self {
        case .deployed: .green
        case .tornDown: .gray
        case .undeployed: .orange
        case .deploying: .blue
        case .tearingDown: .orange
        case .error: .red
        case .unknown: .secondary
        }
    }

    var isTransitioning: Bool { self == .deploying || self == .tearingDown }
}

enum GroupAction: String, CaseIterable, Identifiable {
    case deploy, teardown, redeploy
    var id: String { rawValue }
    var title: String { rawValue.capitalized }
    var symbol: String {
        switch self {
        case .deploy: "arrow.up.circle"
        case .teardown: "trash.circle"
        case .redeploy: "arrow.triangle.2.circlepath.circle"
        }
    }
}

struct GroupModel: Identifiable, Hashable {
    let name: String
    let deployment: String
    let kind: String
    let sku: String
    let capacity: Int?
    /// Name a client must send to reach this copy; "<group>/<model>" when a plain name collides.
    let addressable: String
    var id: String { name }
}

struct GroupInfo: Identifiable, Hashable {
    let id: String
    let resourceGroup: String
    let region: String
    let purpose: String
    let billing: String
    let policy: String
    let idleMinutes: Int
    let models: [GroupModel]
    let status: GroupStatus
    let lastUsed: Date?
    let deployedModels: [String]

    var isOnDemand: Bool { policy == "on_demand" }

    /// Seconds until the idle reaper tears this group down (on-demand + deployed only).
    func idleRemaining(now: Date = Date()) -> TimeInterval? {
        guard isOnDemand, status == .deployed, let lastUsed else { return nil }
        return lastUsed.addingTimeInterval(Double(idleMinutes) * 60).timeIntervalSince(now)
    }
}

struct RecentUse: Hashable {
    let model: String
    let group: String?
    let lastUsed: Date?
}

struct PickerRequest: Identifiable, Hashable {
    let id = UUID()
    let groupID: String
    let action: GroupAction
}

struct OutputDoc: Identifiable {
    let id = UUID()
    let title: String
    let text: String
}

// MARK: - Parsing helpers

enum Parse {
    private static let frac: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f
    }()
    private static let plain = ISO8601DateFormatter()

    static func date(_ any: Any?) -> Date? {
        guard let s = any as? String else { return nil }
        return frac.date(from: s) ?? plain.date(from: s)
    }

    /// Accepts "name" or {"model": "name"} or {"name": "name"}.
    static func modelName(_ any: Any?) -> String? {
        if let s = any as? String, !s.isEmpty { return s }
        if let d = any as? [String: Any] {
            return (d["model"] as? String) ?? (d["name"] as? String)
        }
        return nil
    }

    static func groups(_ data: Data) -> [GroupInfo] {
        guard let root = try? JSONSerialization.jsonObject(with: data) else { return [] }
        let arr: [[String: Any]]
        if let a = root as? [[String: Any]] { arr = a }
        else if let d = root as? [String: Any], let a = d["groups"] as? [[String: Any]] { arr = a }
        else { return [] }
        return arr.compactMap(group)
    }

    static func group(_ d: [String: Any]) -> GroupInfo? {
        guard let id = d["id"] as? String else { return nil }
        let state = (d["state"] as? [String: Any]) ?? d
        let policy = (d["policy"] as? String) ?? "always_on"
        let models: [GroupModel] = ((d["models"] as? [[String: Any]]) ?? []).compactMap { m in
            guard let name = m["name"] as? String else { return nil }
            return GroupModel(
                name: name,
                deployment: (m["deployment"] as? String) ?? name,
                kind: (m["kind"] as? String) ?? "chat",
                sku: (m["sku"] as? String) ?? "",
                capacity: m["capacity"] as? Int,
                addressable: (m["addressable"] as? String) ?? name
            )
        }
        // "unknown" stays unknown until the server reconciles against Azure.
        let status = (state["status"] as? String).flatMap(GroupStatus.init(rawValue:)) ?? .unknown
        return GroupInfo(
            id: id,
            resourceGroup: (d["resource_group"] as? String) ?? "",
            region: (d["region"] as? String) ?? "",
            purpose: (d["purpose"] as? String) ?? "other",
            billing: (d["billing"] as? String) ?? "pay_per_token",
            policy: policy,
            idleMinutes: (d["idle_teardown_minutes"] as? Int) ?? 60,
            models: models,
            status: status,
            lastUsed: date(state["last_used"]),
            deployedModels: (state["deployed_models"] as? [String]) ?? []
        )
    }
}

// MARK: - App model

@MainActor
@Observable
final class AppModel {
    static let shared = AppModel()

    // Server
    var server: ServerState = .unknown
    var slots: [String: String] = [:]
    var recent: [RecentUse] = []
    var served: [String] = []
    var groups: [GroupInfo] = []

    // UI state
    var proxyBusy: String?
    var banner: String?
    var picker: PickerRequest?
    var output: OutputDoc?
    var summary = ""
    var currentProvider = "azure"
    var currentProfile = "default"
    var profileNames: [String] = []
    var proxyAtLogin = false
    var menubarAtLogin = false

    private var pollTask: Task<Void, Never>?
    private var bannerTask: Task<Void, Never>?

    // MARK: Polling

    func startPolling() {
        guard pollTask == nil else { return }
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                await self?.refresh()
                try? await Task.sleep(for: .seconds(5))
            }
        }
    }

    func refresh() async {
        refreshEnv()
        do {
            let (code, data) = try await http("/ccp/health", timeout: 3)
            if code == 200 {
                let ok = (try? JSONSerialization.jsonObject(with: data) as? [String: Any])?["ok"] as? Bool
                server = (ok ?? true) ? .up : .degraded
            } else if code == 404 {
                // Older server without /ccp/health: fall back to root.
                let (rootCode, _) = try await http("/", timeout: 3)
                server = rootCode == 200 ? .degraded : .down
            } else {
                server = .degraded
            }
        } catch {
            server = .down
            return
        }

        await loadActive()
        await loadGroups()
        await loadServed()
        if server == .up, groups.contains(where: { $0.status == .error }) { server = .degraded }
    }

    private func loadActive() async {
        guard let (code, data) = try? await http("/ccp/active", timeout: 3), code == 200,
              let d = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return }
        var s: [String: String] = [:]
        for k in ["big", "small", "frontier"] { if let m = Parse.modelName(d[k]) { s[k] = m } }
        slots = s
        recent = ((d["recent"] as? [[String: Any]]) ?? []).compactMap { r in
            guard let m = r["model"] as? String else { return nil }
            return RecentUse(model: m, group: r["group"] as? String, lastUsed: Parse.date(r["last_used"]))
        }
    }

    private func loadGroups() async {
        guard let (code, data) = try? await http("/ccp/groups", timeout: 4), code == 200 else { return }
        groups = Parse.groups(data)
    }

    private func loadServed() async {
        guard let (code, data) = try? await http("/v1/models", timeout: 4), code == 200,
              let d = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let arr = d["data"] as? [[String: Any]] else { return }
        served = arr.compactMap { $0["id"] as? String }
    }

    // MARK: HTTP

    func http(_ path: String, method: String = "GET", body: [String: Any]? = nil,
              timeout: TimeInterval = 10) async throws -> (Int, Data) {
        guard let url = URL(string: "http://localhost:\(PORT)\(path)") else { throw URLError(.badURL) }
        var req = URLRequest(url: url, timeoutInterval: timeout)
        req.httpMethod = method
        if let key = EnvFileManager.shared.get("PROXY_API_KEY") {
            req.setValue("Bearer \(key)", forHTTPHeaderField: "Authorization")
        }
        if let body {
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try JSONSerialization.data(withJSONObject: body)
        }
        let (data, resp) = try await URLSession.shared.data(for: req)
        return ((resp as? HTTPURLResponse)?.statusCode ?? 0, data)
    }

    // MARK: Derived

    /// Models served: /v1/models + every group model + slot assignments, de-duplicated.
    var allModels: [String] {
        var set = Set(served)
        for g in groups { for m in g.models { set.insert(m.addressable) } }
        for m in slots.values { set.insert(m) }
        return set.sorted()
    }

    private func bare(_ s: String) -> String { s.split(separator: "/").last.map(String.init) ?? s }

    func slotLabels(for model: String) -> [String] {
        slots.filter { bare($0.value) == bare(model) }.map { $0.key.uppercased() }.sorted()
    }

    func isRecent(_ model: String) -> Bool {
        recent.contains { bare($0.model) == bare(model) }
    }

    func group(for model: String) -> GroupInfo? {
        groups.first { g in g.models.contains { $0.addressable == model } }
            ?? groups.first { g in g.models.contains { bare($0.name) == bare(model) } }
    }

    // MARK: Group lifecycle

    func flash(_ message: String) {
        banner = message
        bannerTask?.cancel()
        bannerTask = Task { [weak self] in
            try? await Task.sleep(for: .seconds(6))
            if !Task.isCancelled { self?.banner = nil }
        }
    }

    func groupAction(_ groupID: String, _ action: GroupAction, models: [String]?) async {
        let body: [String: Any]? = models.map { ["models": $0] }
        do {
            let (code, data) = try await http("/ccp/groups/\(groupID)/\(action.rawValue)",
                                              method: "POST", body: body, timeout: 15)
            if (200..<300).contains(code) {
                let n = models.map { "\($0.count) model(s)" } ?? "all models"
                flash("\(action.title) requested for \(groupID) (\(n))")
            } else {
                let msg = String(data: data, encoding: .utf8) ?? ""
                flash("\(action.title) \(groupID) failed: HTTP \(code) \(msg.prefix(160))")
            }
        } catch {
            flash("\(action.title) \(groupID) failed: \(error.localizedDescription)")
        }
        await refresh()
    }

    func presentPicker(_ groupID: String, _ action: GroupAction) {
        picker = PickerRequest(groupID: groupID, action: action)
    }
}

// MARK: - Menu sections

struct ModelsSection: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            SectionHeader(title: "Models", trailing: "\(model.allModels.count)")
            if model.allModels.isEmpty {
                Text(model.server == .down ? "Server not running" : "No models reported")
                    .font(.caption).foregroundStyle(.secondary)
            } else {
                ScrollView {
                    VStack(alignment: .leading, spacing: 2) {
                        ForEach(model.allModels, id: \.self) { name in
                            ModelRow(name: name)
                        }
                    }
                }
                .frame(maxHeight: 140)
            }
        }
    }
}

struct ModelRow: View {
    @Environment(AppModel.self) private var model
    let name: String

    var body: some View {
        let slots = model.slotLabels(for: name)
        let recent = model.isRecent(name)
        let group = model.group(for: name)
        let down = group.map { $0.isOnDemand && $0.status == .tornDown } ?? false
        HStack(spacing: 6) {
            Image(systemName: icon(inSlot: !slots.isEmpty, recent: recent, down: down))
                .foregroundStyle(slots.isEmpty ? (recent ? Color.blue : (down ? .gray : .secondary)) : Color.yellow)
                .frame(width: 16)
                .accessibilityHidden(true)
            Text(name).font(.system(.caption, design: .monospaced)).lineLimit(1)
            Spacer(minLength: 4)
            if !slots.isEmpty {
                Text(slots.joined(separator: "/")).font(.caption2.bold()).foregroundStyle(.yellow)
            } else if down {
                Text("torn down").font(.caption2).foregroundStyle(.secondary)
            } else if recent {
                Text("recent").font(.caption2).foregroundStyle(.blue)
            }
        }
        .accessibilityElement(children: .combine)
    }

    private func icon(inSlot: Bool, recent: Bool, down: Bool) -> String {
        if inSlot { return "star.fill" }
        if recent { return "clock.fill" }
        if down { return "moon.zzz" }
        return "circle"
    }
}

struct GroupsSection: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            SectionHeader(title: "Resource Groups", trailing: "\(model.groups.count)")
            if model.groups.isEmpty {
                Text(model.server == .down ? "Server not running" : "No groups configured")
                    .font(.caption).foregroundStyle(.secondary)
            }
            ForEach(model.groups) { g in GroupRow(group: g) }
        }
    }
}

struct GroupRow: View {
    @Environment(AppModel.self) private var model
    @Environment(\.openWindow) private var openWindow
    let group: GroupInfo

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 6) {
                    Text(group.id).font(.callout.weight(.medium))
                    StatusBadge(status: group.status)
                }
                Text("\(group.policy == "on_demand" ? "on-demand" : "always on") · \(group.region) · \(group.models.count) models")
                    .font(.caption2).foregroundStyle(.secondary)
                if group.isOnDemand, group.status == .deployed {
                    TimelineView(.periodic(from: .now, by: 30)) { ctx in
                        Text(countdown(group.idleRemaining(now: ctx.date)))
                            .font(.caption2).foregroundStyle(.orange)
                    }
                }
            }
            Spacer()
            actionMenu
        }
        .accessibilityElement(children: .contain)
    }

    private var actionMenu: some View {
        Menu {
            ForEach(GroupAction.allCases) { action in
                Button { run(action, nil) } label: { Label(action.title, systemImage: action.symbol) }
                Button { pick(action) } label: { Text("\(action.title) with selected models…") }
                if action != .redeploy { Divider() }
            }
        } label: {
            Image(systemName: "ellipsis.circle").imageScale(.large)
        }
        .menuStyle(.borderlessButton)
        .menuIndicator(.hidden)
        .fixedSize()
        .disabled(group.status.isTransitioning)
        .accessibilityLabel("Actions for \(group.id)")
    }

    private func countdown(_ remaining: TimeInterval?) -> String {
        guard let r = remaining else { return "idle timer: n/a" }
        if r <= 0 { return "idle teardown due" }
        let m = Int(r / 60)
        return m >= 60 ? "idle teardown in \(m / 60)h \(m % 60)m" : "idle teardown in \(max(m, 1))m"
    }

    private func run(_ action: GroupAction, _ models: [String]?) {
        let id = group.id
        Task { await model.groupAction(id, action, models: models) }
    }

    private func pick(_ action: GroupAction) {
        model.presentPicker(group.id, action)
        openWindow(id: "picker")
        NSApplication.shared.activate(ignoringOtherApps: true)
    }
}

struct StatusBadge: View {
    let status: GroupStatus
    var body: some View {
        HStack(spacing: 3) {
            if status.isTransitioning { ProgressView().controlSize(.mini) }
            Text(status.label)
        }
        .font(.caption2.weight(.semibold))
        .padding(.horizontal, 6).padding(.vertical, 1)
        .background(status.color.opacity(0.18), in: Capsule())
        .foregroundStyle(status.color)
    }
}

struct SectionHeader: View {
    let title: String
    var trailing: String?
    var body: some View {
        HStack {
            Text(title.uppercased()).font(.caption.weight(.semibold)).foregroundStyle(.secondary)
            Spacer()
            if let trailing { Text(trailing).font(.caption).foregroundStyle(.tertiary) }
        }
    }
}

// MARK: - Model picker (multi-select with sections, search, select/deselect all)

struct ModelPickerView: View {
    @Environment(AppModel.self) private var model
    @Environment(\.dismissWindow) private var dismissWindow
    let request: PickerRequest

    @State private var selected: Set<String> = []
    @State private var search = ""
    @State private var seeded = false
    @State private var submitting = false

    private static let kindOrder = ["chat", "reasoning", "multimodal", "audio", "image", "embedding", "router"]

    private var group: GroupInfo? { model.groups.first { $0.id == request.groupID } }
    private var allModels: [GroupModel] { group?.models ?? [] }

    private var filtered: [GroupModel] {
        let q = search.trimmingCharacters(in: .whitespaces).lowercased()
        guard !q.isEmpty else { return allModels }
        return allModels.filter {
            $0.name.lowercased().contains(q) || $0.kind.lowercased().contains(q)
                || $0.deployment.lowercased().contains(q)
        }
    }

    private var sections: [(kind: String, models: [GroupModel])] {
        let grouped = Dictionary(grouping: filtered, by: \.kind)
        let ordered = grouped.keys.sorted { a, b in
            let ia = Self.kindOrder.firstIndex(of: a) ?? Int.max
            let ib = Self.kindOrder.firstIndex(of: b) ?? Int.max
            return ia == ib ? a < b : ia < ib
        }
        return ordered.map { ($0, grouped[$0]!.sorted { $0.name < $1.name }) }
    }

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            if allModels.isEmpty {
                ContentUnavailableView("No models", systemImage: "tray",
                                       description: Text("Group \(request.groupID) lists no models."))
            } else if filtered.isEmpty {
                ContentUnavailableView.search(text: search)
            } else {
                List {
                    ForEach(sections, id: \.kind) { section in
                        Section(section.kind.capitalized) {
                            ForEach(section.models) { m in
                                Toggle(isOn: binding(for: m.name)) {
                                    HStack {
                                        Text(m.addressable).font(.system(.body, design: .monospaced))
                                        Spacer()
                                        if group?.deployedModels.contains(m.name) == true {
                                            Text("deployed").font(.caption2).foregroundStyle(.green)
                                        }
                                        if !m.sku.isEmpty {
                                            Text(m.sku).font(.caption2).foregroundStyle(.secondary)
                                        }
                                    }
                                }
                                .toggleStyle(.checkbox)
                            }
                        }
                    }
                }
            }
            Divider()
            footer
        }
        .frame(minWidth: 420, minHeight: 460)
        .onAppear(perform: seed)
        .onChange(of: group?.models.count) { _, _ in seed() }
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Image(systemName: request.action.symbol).foregroundStyle(.tint)
                Text("\(request.action.title) · \(request.groupID)").font(.headline)
                Spacer()
                Text("\(selected.count) of \(allModels.count) selected")
                    .font(.caption).foregroundStyle(.secondary)
            }
            HStack {
                TextField("Search models", text: $search)
                    .textFieldStyle(.roundedBorder)
                Button("Select All") { selected.formUnion(filtered.map(\.name)) }
                Button("Deselect All") { selected.subtract(filtered.map(\.name)) }
            }
        }
        .padding(12)
    }

    private var footer: some View {
        HStack {
            if request.action == .teardown {
                Label("Stops billing for the selected deployments.", systemImage: "info.circle")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Spacer()
            Button("Cancel", role: .cancel) { dismissWindow(id: "picker") }
                .keyboardShortcut(.cancelAction)
            Button(confirmTitle) { confirm() }
                .keyboardShortcut(.defaultAction)
                .buttonStyle(.borderedProminent)
                .disabled(selected.isEmpty || submitting)
        }
        .padding(12)
    }

    private var confirmTitle: String {
        "\(request.action.title) \(selected.count) model\(selected.count == 1 ? "" : "s")"
    }

    private func binding(for name: String) -> Binding<Bool> {
        Binding(
            get: { selected.contains(name) },
            set: { on in if on { selected.insert(name) } else { selected.remove(name) } }
        )
    }

    private func seed() {
        guard !seeded, let g = group, !g.models.isEmpty else { return }
        seeded = true
        let deployed = Set(g.deployedModels)
        switch request.action {
        case .deploy:
            let missing = g.models.map(\.name).filter { !deployed.contains($0) }
            selected = Set(missing.isEmpty ? g.models.map(\.name) : missing)
        case .teardown, .redeploy:
            let live = g.models.map(\.name).filter { deployed.contains($0) }
            selected = Set(live.isEmpty ? g.models.map(\.name) : live)
        }
    }

    private func confirm() {
        submitting = true
        let names = allModels.map(\.name).filter { selected.contains($0) }
        let id = request.groupID, action = request.action
        Task {
            await model.groupAction(id, action, models: names)
            dismissWindow(id: "picker")
        }
    }
}

struct PickerHost: View {
    @Environment(AppModel.self) private var model
    var body: some View {
        if let req = model.picker {
            ModelPickerView(request: req).id(req.id)
        } else {
            ContentUnavailableView("No request", systemImage: "checklist")
                .frame(minWidth: 420, minHeight: 300)
        }
    }
}
