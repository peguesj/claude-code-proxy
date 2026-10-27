// CcpMenuBar.swift — CCP LiteLLM Menu Bar Daemon
// SwiftUI MenuBarExtra (.window style), macOS 14+. Build: bash build-menubar.sh
// Install: bash build-menubar.sh

import SwiftUI
import Darwin

// ---------------------------------------------------------------------------
// MARK: - Constants
// ---------------------------------------------------------------------------

let CCP_DIR    = "/Users/jeremiah/Developer/claude-code-proxy"
let CCP_BIN    = "/Users/jeremiah/.local/bin/ccp-litellm"
let LOG_FILE   = "/tmp/ccp-server.log"
let ENV_FILE   = CCP_DIR + "/.env"
let PORT       = 8082
let HEALTH_URL = "http://localhost:\(PORT)/"
let APP_VERSION = "2.0.0"

let PROXY_PLIST    = NSHomeDirectory() + "/Library/LaunchAgents/com.ccp.litellm.proxy.plist"
let MENUBAR_PLIST  = NSHomeDirectory() + "/Library/LaunchAgents/com.ccp.litellm.menubar.plist"
let PROXY_LABEL    = "com.ccp.litellm.proxy"
let MENUBAR_LABEL  = "com.ccp.litellm.menubar"

// MARK: Providers + Presets

let PROVIDERS = ["azure", "azure_ai", "openai", "google", "anthropic"]

struct Preset {
    let name: String
    let provider: String
    let big: String
    let small: String
    let frontier: String
}

let PRESETS: [Preset] = [
    Preset(name: "Best Performance",  provider: "azure",  big: "gpt-5.6-terra", small: "gpt-5.6-luna",   frontier: "gpt-6-astra"),
    Preset(name: "Best Reasoning",    provider: "azure",  big: "o3",            small: "gpt-5.6-luna",   frontier: "gpt-6-astra"),
    Preset(name: "Balanced",          provider: "azure",  big: "gpt-5.5",       small: "gpt-5.6-luna",   frontier: "gpt-6-astra"),
    Preset(name: "Cost Efficient",    provider: "azure",  big: "gpt-4.1",       small: "gpt-4.1-mini",    frontier: "o3-mini"),
    Preset(name: "Speed",             provider: "azure",  big: "gpt-4o",        small: "gpt-4o-mini",     frontier: "gpt-5"),
    Preset(name: "Coding",            provider: "azure",  big: "gpt-5.3-codex", small: "codex-mini",      frontier: "gpt-6-astra"),
    Preset(name: "OpenAI Direct",     provider: "openai", big: "gpt-4.1",       small: "gpt-4.1-mini",   frontier: "gpt-6-astra"),
    Preset(name: "2026 Best Overall", provider: "azure",  big: "gpt-5.6-terra", small: "gpt-5.6-luna",   frontier: "gpt-6-astra"),
]

// ---------------------------------------------------------------------------
// MARK: - EnvFileManager
// ---------------------------------------------------------------------------

final class EnvFileManager {
    static let shared = EnvFileManager()
    private let path = ENV_FILE

    func read() -> String {
        (try? String(contentsOfFile: path, encoding: .utf8)) ?? ""
    }

    func get(_ key: String) -> String? {
        for line in read().components(separatedBy: .newlines) {
            let t = line.trimmingCharacters(in: .whitespaces)
            guard !t.hasPrefix("#"), t.hasPrefix("\(key)=") else { continue }
            var v = String(t.dropFirst(key.count + 1))
            v = v.trimmingCharacters(in: CharacterSet(charactersIn: "\"'"))
            return v.isEmpty ? nil : v
        }
        return nil
    }

    /// Patch one or more key=value pairs, preserving comments + order.
    /// Keys not present are appended at the end.
    @discardableResult
    func patch(_ updates: [String: String?]) -> Bool {
        var content = read()
        if content.isEmpty, FileManager.default.fileExists(atPath: path) == false {
            // create empty file
        }
        var lines = content.components(separatedBy: "\n")
        var remaining = updates

        for (i, line) in lines.enumerated() {
            let t = line.trimmingCharacters(in: .whitespaces)
            if t.hasPrefix("#") || t.isEmpty { continue }
            guard let eq = t.firstIndex(of: "=") else { continue }
            let key = String(t[..<eq])
            if let val = remaining[key] {
                if let v = val {
                    // quote if contains spaces or special chars
                    let quoted = needsQuoting(v) ? "\"\(escape(v))\"" : v
                    lines[i] = "\(key)=\(quoted)"
                } else {
                    // remove by commenting out
                    lines[i] = "# \(line)"
                }
                remaining.removeValue(forKey: key)
            }
        }

        // Append keys that weren't found
        for (key, val) in remaining {
            guard let v = val else { continue }
            let quoted = needsQuoting(v) ? "\"\(escape(v))\"" : v
            lines.append("\(key)=\(quoted)")
        }

        content = lines.joined(separator: "\n")
        do {
            try content.write(toFile: path, atomically: true, encoding: .utf8)
            return true
        } catch {
            return false
        }
    }

    private func needsQuoting(_ s: String) -> Bool {
        s.contains(" ") || s.contains("#") || s.contains("\"") || s.contains("{") || s.contains("}")
    }

    private func escape(_ s: String) -> String {
        s.replacingOccurrences(of: "\"", with: "\\\"")
    }
}

final class ProfileFileManager {
    static let shared = ProfileFileManager()

    func profilesPath() -> String {
        EnvFileManager.shared.get("CCP_PROFILES_FILE") ?? (CCP_DIR + "/profiles.json")
    }

    func currentProfile() -> String {
        EnvFileManager.shared.get("CCP_PROFILE")
            ?? EnvFileManager.shared.get("ACTIVE_PROFILE")
            ?? activeProfileFromFile()
            ?? "default"
    }

    func profileNames() -> [String] {
        guard let data = FileManager.default.contents(atPath: profilesPath()),
              let root = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let profiles = root["profiles"] as? [String: Any] else {
            return []
        }
        return profiles.keys.sorted()
    }

    private func activeProfileFromFile() -> String? {
        guard let data = FileManager.default.contents(atPath: profilesPath()),
              let root = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            return nil
        }
        return root["active_profile"] as? String
    }
}

// ---------------------------------------------------------------------------
// MARK: - Proxy / launchctl / shell actions
// ---------------------------------------------------------------------------

private func runProcess(_ path: String, _ args: [String]) -> (output: String, code: Int32) {
    let proc = Process()
    proc.executableURL = URL(fileURLWithPath: path)
    proc.arguments = args
    let pipe = Pipe()
    proc.standardOutput = pipe
    proc.standardError = pipe
    do { try proc.run() } catch { return ("Failed to run \(path): \(error)", 1) }
    let data = pipe.fileHandleForReading.readDataToEndOfFile()
    proc.waitUntilExit()
    return (String(data: data, encoding: .utf8)?.trimmingCharacters(in: .newlines) ?? "", proc.terminationStatus)
}

extension AppModel {
    func ccp(_ args: [String]) async -> (output: String, code: Int32) {
        await Task.detached(priority: .userInitiated) { runProcess(CCP_BIN, args) }.value
    }

    func refreshEnv() {
        let env = EnvFileManager.shared
        let prov = env.get("PREFERRED_PROVIDER") ?? "azure"
        let profile = ProfileFileManager.shared.currentProfile()
        currentProvider = prov
        currentProfile = profile
        profileNames = ProfileFileManager.shared.profileNames()
        guard !env.read().isEmpty else { summary = "Config: (unreadable)"; return }
        let frontier = env.get("FRONTIER_MODEL") ?? "gpt-6-astra"
        let big = env.get("BIG_MODEL") ?? "gpt-5.5"
        let small = env.get("SMALL_MODEL") ?? "gpt-5-mini"
        let auth: String
        if prov == "anthropic" { auth = "ANTHROPIC_API_KEY required" }
        else if env.get("PROXY_API_KEY") != nil { auth = "proxy-key required" }
        else { auth = "no Anthropic key needed" }
        summary = "\(profile) • \(prov): \(frontier)/\(big)/\(small)  [\(auth)]"
    }

    func startProxy() async {
        proxyBusy = "Starting…"
        _ = await ccp(["start"])
        try? await Task.sleep(for: .seconds(2.5))
        proxyBusy = nil
        await refresh()
    }

    func stopProxy() async {
        proxyBusy = "Stopping…"
        _ = await ccp(["stop"])
        try? await Task.sleep(for: .seconds(1.5))
        proxyBusy = nil
        await refresh()
    }

    func restartProxy() async {
        proxyBusy = "Restarting…"
        _ = await ccp(["restart"])
        try? await Task.sleep(for: .seconds(4))
        proxyBusy = nil
        await refresh()
    }

    func setProvider(_ p: String) async {
        EnvFileManager.shared.patch(["PREFERRED_PROVIDER": p])
        refreshEnv()
        await restartProxy()
    }

    func setProfile(_ p: String) async {
        EnvFileManager.shared.patch(["CCP_PROFILE": p])
        refreshEnv()
        await restartProxy()
    }

    func applyPreset(_ p: Preset) async {
        EnvFileManager.shared.patch([
            "PREFERRED_PROVIDER": p.provider, "BIG_MODEL": p.big,
            "SMALL_MODEL": p.small, "FRONTIER_MODEL": p.frontier,
        ])
        refreshEnv()
        await restartProxy()
    }

    // LaunchAgents

    func refreshLoginState() async {
        let (p, m) = await Task.detached {
            (launchAgentLoaded(PROXY_LABEL), launchAgentLoaded(MENUBAR_LABEL))
        }.value
        proxyAtLogin = p
        menubarAtLogin = m
    }

    func toggleLaunchAgent(label: String, plist: String) async {
        await Task.detached {
            if launchAgentLoaded(label) {
                _ = runProcess("/bin/launchctl", ["bootout", "gui/\(getuid())/\(label)"])
            } else {
                _ = runProcess("/bin/launchctl", ["bootstrap", "gui/\(getuid())", plist])
            }
        }.value
        await refreshLoginState()
    }

    func troubleshoot() async {
        let r = await ccp(["troubleshoot"])
        output = OutputDoc(title: "CCP LiteLLM — Troubleshoot", text: r.output.isEmpty ? "(no output)" : r.output)
    }

    func about() {
        output = OutputDoc(title: "CCP LiteLLM Menu Bar", text: """
        Version \(APP_VERSION)

        Claude Code Proxy manager for the FastAPI + LiteLLM server on port \(PORT).
        Translates Anthropic API requests to Azure OpenAI / OpenAI / Google / Anthropic.

        Project: \(CCP_DIR)
        Log: \(LOG_FILE)
        Health: http://localhost:\(PORT)/ccp/health

        CLI: ccp  •  Slash: /ccp-litellm
        """)
    }

    func copyClientSetup() {
        let env = EnvFileManager.shared
        var parts = ["ANTHROPIC_BASE_URL=http://localhost:\(PORT)"]
        if (env.get("PREFERRED_PROVIDER") ?? "openai") == "anthropic" {
            parts.append("ANTHROPIC_API_KEY=<your-sk-ant-...>")
        } else {
            parts.append("ANTHROPIC_API_KEY=\(env.get("PROXY_API_KEY") ?? "proxy")")
        }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(parts.joined(separator: " "), forType: .string)
        flash("Client setup copied to clipboard")
    }

    func openSwitcher() {
        let script = """
        tell application "Terminal"
            activate
            do script "\(CCP_DIR)/ccp-switcher"
        end tell
        """
        var err: NSDictionary?
        _ = NSAppleScript(source: script)?.executeAndReturnError(&err)
        if err != nil { NSWorkspace.shared.open(URL(fileURLWithPath: CCP_DIR)) }
    }

    func viewLog() {
        if !FileManager.default.fileExists(atPath: LOG_FILE) {
            try? "".write(toFile: LOG_FILE, atomically: true, encoding: .utf8)
        }
        NSWorkspace.shared.open(URL(fileURLWithPath: LOG_FILE))
    }
}

func launchAgentLoaded(_ label: String) -> Bool {
    runProcess("/bin/launchctl", ["list", label]).code == 0
}

// ---------------------------------------------------------------------------
// MARK: - App
// ---------------------------------------------------------------------------

@main
struct CcpMenuBarApp: App {
    @State private var model = AppModel.shared

    init() {
        AppModel.shared.startPolling()
    }

    var body: some Scene {
        MenuBarExtra {
            MenuContentView()
                .environment(model)
        } label: {
            Image(systemName: model.server.symbol)
                .accessibilityLabel("CCP LiteLLM: \(model.server.label)")
        }
        .menuBarExtraStyle(.window)

        Window("Select Models", id: "picker") {
            PickerHost().environment(model)
        }
        .windowResizability(.contentMinSize)

        Window("CCP LiteLLM — Model Manager", id: "models") {
            ModelManagerView().environment(model)
        }

        Window("CCP LiteLLM — Settings", id: "settings") {
            SettingsView().environment(model)
        }
        .windowResizability(.contentSize)

        Window("CCP LiteLLM", id: "output") {
            OutputView().environment(model)
        }
    }
}

// ---------------------------------------------------------------------------
// MARK: - Menu content
// ---------------------------------------------------------------------------

struct MenuContentView: View {
    @Environment(AppModel.self) private var model
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            header
            Divider()
            slotsSummary
            Divider()
            ModelsSection()
            Divider()
            GroupsSection()
            if let banner = model.banner {
                Text(banner).font(.caption).foregroundStyle(.blue)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            Divider()
            controls
        }
        .padding(12)
        .frame(width: 380)
        .task { await model.refreshLoginState() }
    }

    private var header: some View {
        HStack(spacing: 8) {
            Image(systemName: model.server.symbol).foregroundStyle(model.server.color).imageScale(.large)
            VStack(alignment: .leading, spacing: 1) {
                Text(model.proxyBusy ?? (model.server == .up ? "Running on :\(PORT)" : model.server.label))
                    .font(.headline)
                Text(model.summary).font(.caption2).foregroundStyle(.secondary).lineLimit(2)
            }
            Spacer()
            if model.proxyBusy != nil { ProgressView().controlSize(.small) }
        }
    }

    private var slotsSummary: some View {
        VStack(alignment: .leading, spacing: 2) {
            ForEach(["big", "small", "frontier"], id: \.self) { k in
                HStack {
                    Text(k.uppercased()).font(.caption2.bold()).foregroundStyle(.secondary).frame(width: 64, alignment: .leading)
                    Text(model.slots[k] ?? EnvFileManager.shared.get("\(k.uppercased())_MODEL") ?? "—")
                        .font(.system(.caption, design: .monospaced))
                    Spacer()
                }
            }
        }
    }

    private var controls: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Button("Start") { Task { await model.startProxy() } }
                    .disabled(model.server == .up || model.proxyBusy != nil)
                Button("Stop") { Task { await model.stopProxy() } }
                    .disabled(model.server == .down || model.proxyBusy != nil)
                Button("Restart") { Task { await model.restartProxy() } }
                    .disabled(model.server == .down || model.proxyBusy != nil)
                Spacer()
                Button { open("settings") } label: { Image(systemName: "gearshape") }
                    .help("Settings").accessibilityLabel("Settings")
            }
            HStack {
                Menu("Profile") {
                    if model.profileNames.isEmpty { Text("No profiles found") }
                    ForEach(model.profileNames, id: \.self) { p in
                        Button { Task { await model.setProfile(p) } } label: {
                            if p == model.currentProfile { Label(p, systemImage: "checkmark") } else { Text(p) }
                        }
                    }
                }
                Menu("Provider") {
                    ForEach(PROVIDERS, id: \.self) { p in
                        Button { Task { await model.setProvider(p) } } label: {
                            if p == model.currentProvider { Label(p, systemImage: "checkmark") } else { Text(p) }
                        }
                    }
                }
                Menu("Presets") {
                    ForEach(PRESETS, id: \.name) { p in
                        Button("\(p.name) — \(p.provider): \(p.frontier)/\(p.big)/\(p.small)") {
                            Task { await model.applyPreset(p) }
                        }
                    }
                }
                Menu("More") { moreItems }
            }
            .menuStyle(.button)
        }
    }

    @ViewBuilder private var moreItems: some View {
        Button("Manage Models…") { open("models") }
        Button("Edit .env…") { NSWorkspace.shared.open(URL(fileURLWithPath: ENV_FILE)) }
        Divider()
        Button("Copy Client Setup") { model.copyClientSetup() }
        Button("View Log") { model.viewLog() }
        Button("Open Model Switcher (TUI)") { model.openSwitcher() }
        Button("Reveal Project in Finder") {
            NSWorkspace.shared.selectFile(ENV_FILE, inFileViewerRootedAtPath: CCP_DIR)
        }
        Divider()
        Toggle("Start Proxy at Login", isOn: Binding(
            get: { model.proxyAtLogin },
            set: { _ in Task { await model.toggleLaunchAgent(label: PROXY_LABEL, plist: PROXY_PLIST) } }))
        Toggle("Start Menu Bar at Login", isOn: Binding(
            get: { model.menubarAtLogin },
            set: { _ in Task { await model.toggleLaunchAgent(label: MENUBAR_LABEL, plist: MENUBAR_PLIST) } }))
        Divider()
        Button("Troubleshoot…") {
            Task { await model.troubleshoot(); open("output") }
        }
        Button("About CCP LiteLLM") { model.about(); open("output") }
        Divider()
        Button("Quit CCP Monitor") { NSApplication.shared.terminate(nil) }
    }

    private func open(_ id: String) {
        openWindow(id: id)
        NSApplication.shared.activate(ignoringOtherApps: true)
    }
}

// ---------------------------------------------------------------------------
// MARK: - Output window (troubleshoot / about)
// ---------------------------------------------------------------------------

struct OutputView: View {
    @Environment(AppModel.self) private var model
    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(model.output?.title ?? "").font(.headline)
            ScrollView {
                Text(model.output?.text ?? "")
                    .font(.system(.caption, design: .monospaced))
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            .padding(8)
            .background(.background.secondary, in: .rect(cornerRadius: 6))
        }
        .padding(12)
        .frame(minWidth: 520, minHeight: 280)
    }
}

// ---------------------------------------------------------------------------
// MARK: - Settings
// ---------------------------------------------------------------------------

private struct SettingsField: Identifiable {
    let key: String
    let label: String
    var secure = false
    var id: String { key }
}

private let SETTINGS_SECTIONS: [(title: String, fields: [SettingsField])] = [
    ("Profiles", [
        SettingsField(key: "CCP_PROFILE", label: "CCP_PROFILE"),
        SettingsField(key: "CCP_PROFILES_FILE", label: "CCP_PROFILES_FILE"),
    ]),
    ("Models", [
        SettingsField(key: "BIG_MODEL", label: "BIG_MODEL (sonnet →)"),
        SettingsField(key: "SMALL_MODEL", label: "SMALL_MODEL (haiku →)"),
        SettingsField(key: "FRONTIER_MODEL", label: "FRONTIER_MODEL (opus →)"),
    ]),
    ("Proxy Auth", [
        SettingsField(key: "PROXY_API_KEY", label: "PROXY_API_KEY (optional client key)", secure: true),
    ]),
    ("Azure OpenAI", [
        SettingsField(key: "AZURE_API_KEY", label: "AZURE_API_KEY", secure: true),
        SettingsField(key: "AZURE_API_BASE", label: "AZURE_API_BASE"),
        SettingsField(key: "AZURE_API_VERSION", label: "AZURE_API_VERSION"),
    ]),
    ("Azure AI Foundry", [
        SettingsField(key: "AZURE_AI_API_KEY", label: "AZURE_AI_API_KEY", secure: true),
        SettingsField(key: "AZURE_AI_API_BASE", label: "AZURE_AI_API_BASE"),
        SettingsField(key: "AZURE_AI_API_VERSION", label: "AZURE_AI_API_VERSION"),
        SettingsField(key: "AZURE_AI_RESOURCE_GROUP", label: "AZURE_AI_RESOURCE_GROUP"),
        SettingsField(key: "AZURE_AI_ACCOUNT_NAME", label: "AZURE_AI_ACCOUNT_NAME"),
        SettingsField(key: "AZURE_AI_LOCATION", label: "AZURE_AI_LOCATION"),
    ]),
    ("Azure Resource Context (for model deployment)", [
        SettingsField(key: "AZURE_SUBSCRIPTION_ID", label: "AZURE_SUBSCRIPTION_ID"),
        SettingsField(key: "AZURE_RESOURCE_GROUP", label: "AZURE_RESOURCE_GROUP"),
        SettingsField(key: "AZURE_ACCOUNT_NAME", label: "AZURE_ACCOUNT_NAME (Cognitive Services account)"),
    ]),
    ("Other Backend Keys", [
        SettingsField(key: "OPENAI_API_KEY", label: "OPENAI_API_KEY", secure: true),
        SettingsField(key: "ANTHROPIC_API_KEY", label: "ANTHROPIC_API_KEY", secure: true),
        SettingsField(key: "GEMINI_API_KEY", label: "GEMINI_API_KEY", secure: true),
    ]),
]

struct SettingsView: View {
    @Environment(AppModel.self) private var app
    @Environment(\.dismissWindow) private var dismissWindow

    @State private var values: [String: String] = [:]
    @State private var provider = "azure"
    @State private var restartOnSave = true
    @State private var failed = false

    var body: some View {
        Form {
            Section("Provider") {
                Picker("Preferred Provider", selection: $provider) {
                    ForEach(PROVIDERS, id: \.self) { Text($0) }
                }
            }
            ForEach(SETTINGS_SECTIONS, id: \.title) { section in
                Section(section.title) {
                    ForEach(section.fields) { f in
                        if f.secure {
                            SecureField(f.label, text: binding(f.key))
                        } else {
                            TextField(f.label, text: binding(f.key))
                        }
                    }
                }
            }
            Section {
                Toggle("Restart proxy after save", isOn: $restartOnSave)
                HStack {
                    Spacer()
                    Button("Cancel", role: .cancel) { dismissWindow(id: "settings") }
                        .keyboardShortcut(.cancelAction)
                    Button("Save") { save() }.keyboardShortcut(.defaultAction)
                }
            }
        }
        .formStyle(.grouped)
        .frame(width: 600, height: 760)
        .onAppear(perform: load)
        .alert("Failed to save settings", isPresented: $failed) {
            Button("OK") {}
        } message: { Text("Could not write to \(ENV_FILE)") }
    }

    private func binding(_ key: String) -> Binding<String> {
        Binding(get: { values[key] ?? "" }, set: { values[key] = $0 })
    }

    private func load() {
        let env = EnvFileManager.shared
        provider = env.get("PREFERRED_PROVIDER") ?? "azure"
        var v: [String: String] = [:]
        for s in SETTINGS_SECTIONS { for f in s.fields { v[f.key] = env.get(f.key) ?? "" } }
        values = v
    }

    private func save() {
        var updates: [String: String?] = ["PREFERRED_PROVIDER": provider]
        for (k, v) in values { updates[k] = v.isEmpty ? nil : v }
        if EnvFileManager.shared.patch(updates) {
            app.refreshEnv()
            dismissWindow(id: "settings")
            if restartOnSave { Task { await app.restartProxy() } }
        } else {
            failed = true
        }
    }
}
