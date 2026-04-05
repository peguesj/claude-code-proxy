// CcpMenuBar.swift — CCP LiteLLM Menu Bar Daemon
// Compile: swiftc -framework Cocoa -O CcpMenuBar.swift -o CcpMenuBar
// Install: bash build-menubar.sh

import Cocoa
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
let APP_VERSION = "1.1.0"

let PROXY_PLIST    = NSHomeDirectory() + "/Library/LaunchAgents/com.ccp.litellm.proxy.plist"
let MENUBAR_PLIST  = NSHomeDirectory() + "/Library/LaunchAgents/com.ccp.litellm.menubar.plist"
let PROXY_LABEL    = "com.ccp.litellm.proxy"
let MENUBAR_LABEL  = "com.ccp.litellm.menubar"

// MARK: Providers + Presets

let PROVIDERS = ["azure", "openai", "google", "anthropic"]

struct Preset {
    let name: String
    let provider: String
    let big: String
    let small: String
    let frontier: String
}

let PRESETS: [Preset] = [
    Preset(name: "Best Performance",  provider: "azure",  big: "gpt-4.1",       small: "gpt-4o",         frontier: "gpt-5.2"),
    Preset(name: "Best Reasoning",    provider: "azure",  big: "gpt-4.1",       small: "o4-mini",        frontier: "o3"),
    Preset(name: "Balanced",          provider: "azure",  big: "gpt-4o",        small: "o4-mini",        frontier: "gpt-5.2"),
    Preset(name: "Cost Efficient",    provider: "azure",  big: "o4-mini",       small: "gpt-4.1-mini",   frontier: "gpt-5"),
    Preset(name: "Speed",             provider: "azure",  big: "gpt-4o",        small: "gpt-4.1-mini",   frontier: "gpt-5"),
    Preset(name: "Coding",            provider: "azure",  big: "gpt-4.1",       small: "o4-mini",        frontier: "gpt-5.2-codex"),
    Preset(name: "OpenAI Direct",     provider: "openai", big: "gpt-4.1",       small: "gpt-4.1-mini",   frontier: "gpt-5.2"),
    Preset(name: "2026 Best Overall", provider: "azure",  big: "gpt-5.2-codex", small: "gpt-5.2-chat",   frontier: "gpt-5.2"),
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

// ---------------------------------------------------------------------------
// MARK: - ProxyStatus
// ---------------------------------------------------------------------------

enum ProxyStatus: Equatable {
    case running, stopped, unknown
}

// ---------------------------------------------------------------------------
// MARK: - SettingsWindowController
// ---------------------------------------------------------------------------

final class SettingsWindowController: NSWindowController, NSWindowDelegate {

    var providerPopup: NSPopUpButton!
    var bigField: NSTextField!
    var smallField: NSTextField!
    var frontierField: NSTextField!
    var proxyKeyField: NSSecureTextField!
    var azureKeyField: NSSecureTextField!
    var azureBaseField: NSTextField!
    var azureVersionField: NSTextField!
    var azureSubField: NSTextField!
    var azureRgField: NSTextField!
    var azureAccountField: NSTextField!
    var openaiKeyField: NSSecureTextField!
    var anthropicKeyField: NSSecureTextField!
    var geminiKeyField: NSSecureTextField!
    var restartOnSave: NSButton!

    var onSave: (() -> Void)?

    convenience init() {
        let w = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 560, height: 780),
            styleMask: [.titled, .closable, .miniaturizable],
            backing: .buffered, defer: false
        )
        w.title = "CCP LiteLLM — Settings"
        w.center()
        self.init(window: w)
        w.delegate = self
        buildUI()
        loadValues()
    }

    private func buildUI() {
        guard let content = window?.contentView else { return }

        let stack = NSStackView()
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 10
        stack.edgeInsets = NSEdgeInsets(top: 16, left: 16, bottom: 16, right: 16)
        stack.translatesAutoresizingMaskIntoConstraints = false
        content.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.topAnchor.constraint(equalTo: content.topAnchor),
            stack.leadingAnchor.constraint(equalTo: content.leadingAnchor),
            stack.trailingAnchor.constraint(equalTo: content.trailingAnchor),
            stack.bottomAnchor.constraint(equalTo: content.bottomAnchor),
        ])

        stack.addArrangedSubview(sectionHeader("Provider & Models"))

        providerPopup = NSPopUpButton(frame: .zero, pullsDown: false)
        providerPopup.addItems(withTitles: PROVIDERS)
        stack.addArrangedSubview(labeled("Preferred Provider", control: providerPopup, width: 200))

        bigField = textField()
        stack.addArrangedSubview(labeled("BIG_MODEL (sonnet →)", control: bigField))

        smallField = textField()
        stack.addArrangedSubview(labeled("SMALL_MODEL (haiku →)", control: smallField))

        frontierField = textField()
        stack.addArrangedSubview(labeled("FRONTIER_MODEL (opus →)", control: frontierField))

        stack.addArrangedSubview(spacer(8))
        stack.addArrangedSubview(sectionHeader("Proxy Auth"))

        proxyKeyField = secureField()
        stack.addArrangedSubview(labeled("PROXY_API_KEY (optional client key)", control: proxyKeyField))

        stack.addArrangedSubview(spacer(8))
        stack.addArrangedSubview(sectionHeader("Azure OpenAI"))

        azureKeyField = secureField()
        stack.addArrangedSubview(labeled("AZURE_API_KEY", control: azureKeyField))

        azureBaseField = textField()
        stack.addArrangedSubview(labeled("AZURE_API_BASE", control: azureBaseField))

        azureVersionField = textField()
        stack.addArrangedSubview(labeled("AZURE_API_VERSION", control: azureVersionField))

        stack.addArrangedSubview(spacer(6))
        stack.addArrangedSubview(sectionHeader("Azure Resource Context (for model deployment)"))

        azureSubField = textField()
        stack.addArrangedSubview(labeled("AZURE_SUBSCRIPTION_ID", control: azureSubField))

        azureRgField = textField()
        stack.addArrangedSubview(labeled("AZURE_RESOURCE_GROUP", control: azureRgField))

        azureAccountField = textField()
        stack.addArrangedSubview(labeled("AZURE_ACCOUNT_NAME (Cognitive Services account)", control: azureAccountField))

        stack.addArrangedSubview(spacer(8))
        stack.addArrangedSubview(sectionHeader("Other Backend Keys"))

        openaiKeyField = secureField()
        stack.addArrangedSubview(labeled("OPENAI_API_KEY", control: openaiKeyField))

        anthropicKeyField = secureField()
        stack.addArrangedSubview(labeled("ANTHROPIC_API_KEY", control: anthropicKeyField))

        geminiKeyField = secureField()
        stack.addArrangedSubview(labeled("GEMINI_API_KEY", control: geminiKeyField))

        stack.addArrangedSubview(spacer(12))

        restartOnSave = NSButton(checkboxWithTitle: "Restart proxy after save", target: nil, action: nil)
        restartOnSave.state = .on
        stack.addArrangedSubview(restartOnSave)

        // Buttons
        let buttons = NSStackView()
        buttons.orientation = .horizontal
        buttons.spacing = 8
        let cancel = NSButton(title: "Cancel", target: self, action: #selector(cancel))
        cancel.bezelStyle = .rounded
        cancel.keyEquivalent = "\u{1b}"
        let save = NSButton(title: "Save", target: self, action: #selector(save))
        save.bezelStyle = .rounded
        save.keyEquivalent = "\r"
        buttons.addArrangedSubview(cancel)
        buttons.addArrangedSubview(save)
        stack.addArrangedSubview(buttons)
    }

    private func sectionHeader(_ text: String) -> NSTextField {
        let l = NSTextField(labelWithString: text)
        l.font = NSFont.boldSystemFont(ofSize: 12)
        l.textColor = .secondaryLabelColor
        return l
    }

    private func labeled(_ title: String, control: NSView, width: CGFloat = 380) -> NSView {
        let row = NSStackView()
        row.orientation = .vertical
        row.alignment = .leading
        row.spacing = 2
        let l = NSTextField(labelWithString: title)
        l.font = NSFont.systemFont(ofSize: 11)
        l.textColor = .secondaryLabelColor
        row.addArrangedSubview(l)
        row.addArrangedSubview(control)
        control.translatesAutoresizingMaskIntoConstraints = false
        control.widthAnchor.constraint(equalToConstant: width).isActive = true
        return row
    }

    private func textField() -> NSTextField {
        let f = NSTextField()
        f.font = NSFont.monospacedSystemFont(ofSize: 11, weight: .regular)
        return f
    }

    private func secureField() -> NSSecureTextField {
        let f = NSSecureTextField()
        f.font = NSFont.monospacedSystemFont(ofSize: 11, weight: .regular)
        return f
    }

    private func spacer(_ h: CGFloat) -> NSView {
        let v = NSView()
        v.translatesAutoresizingMaskIntoConstraints = false
        v.heightAnchor.constraint(equalToConstant: h).isActive = true
        return v
    }

    private func loadValues() {
        let env = EnvFileManager.shared
        let prov = env.get("PREFERRED_PROVIDER") ?? "azure"
        if let idx = PROVIDERS.firstIndex(of: prov) {
            providerPopup.selectItem(at: idx)
        }
        bigField.stringValue       = env.get("BIG_MODEL") ?? ""
        smallField.stringValue     = env.get("SMALL_MODEL") ?? ""
        frontierField.stringValue  = env.get("FRONTIER_MODEL") ?? ""
        proxyKeyField.stringValue  = env.get("PROXY_API_KEY") ?? ""
        azureKeyField.stringValue  = env.get("AZURE_API_KEY") ?? ""
        azureBaseField.stringValue = env.get("AZURE_API_BASE") ?? ""
        azureVersionField.stringValue = env.get("AZURE_API_VERSION") ?? ""
        azureSubField.stringValue = env.get("AZURE_SUBSCRIPTION_ID") ?? ""
        azureRgField.stringValue = env.get("AZURE_RESOURCE_GROUP") ?? ""
        azureAccountField.stringValue = env.get("AZURE_ACCOUNT_NAME") ?? ""
        openaiKeyField.stringValue = env.get("OPENAI_API_KEY") ?? ""
        anthropicKeyField.stringValue = env.get("ANTHROPIC_API_KEY") ?? ""
        geminiKeyField.stringValue = env.get("GEMINI_API_KEY") ?? ""
    }

    @objc private func cancel() {
        window?.performClose(nil)
    }

    @objc private func save() {
        let updates: [String: String?] = [
            "PREFERRED_PROVIDER":  providerPopup.titleOfSelectedItem,
            "BIG_MODEL":           bigField.stringValue.isEmpty ? nil : bigField.stringValue,
            "SMALL_MODEL":         smallField.stringValue.isEmpty ? nil : smallField.stringValue,
            "FRONTIER_MODEL":      frontierField.stringValue.isEmpty ? nil : frontierField.stringValue,
            "PROXY_API_KEY":       proxyKeyField.stringValue.isEmpty ? nil : proxyKeyField.stringValue,
            "AZURE_API_KEY":       azureKeyField.stringValue.isEmpty ? nil : azureKeyField.stringValue,
            "AZURE_API_BASE":      azureBaseField.stringValue.isEmpty ? nil : azureBaseField.stringValue,
            "AZURE_API_VERSION":   azureVersionField.stringValue.isEmpty ? nil : azureVersionField.stringValue,
            "AZURE_SUBSCRIPTION_ID": azureSubField.stringValue.isEmpty ? nil : azureSubField.stringValue,
            "AZURE_RESOURCE_GROUP": azureRgField.stringValue.isEmpty ? nil : azureRgField.stringValue,
            "AZURE_ACCOUNT_NAME":   azureAccountField.stringValue.isEmpty ? nil : azureAccountField.stringValue,
            "OPENAI_API_KEY":      openaiKeyField.stringValue.isEmpty ? nil : openaiKeyField.stringValue,
            "ANTHROPIC_API_KEY":   anthropicKeyField.stringValue.isEmpty ? nil : anthropicKeyField.stringValue,
            "GEMINI_API_KEY":      geminiKeyField.stringValue.isEmpty ? nil : geminiKeyField.stringValue,
        ]

        if EnvFileManager.shared.patch(updates) {
            let shouldRestart = (restartOnSave.state == .on)
            window?.performClose(nil)
            onSave?()
            if shouldRestart {
                NotificationCenter.default.post(name: .init("CcpShouldRestart"), object: nil)
            }
        } else {
            let alert = NSAlert()
            alert.messageText = "Failed to save settings"
            alert.informativeText = "Could not write to \(ENV_FILE)"
            alert.runModal()
        }
    }
}

// ---------------------------------------------------------------------------
// MARK: - AppDelegate
// ---------------------------------------------------------------------------

class AppDelegate: NSObject, NSApplicationDelegate {

    var statusItem: NSStatusItem!
    var healthTimer: Timer?
    var currentStatus: ProxyStatus = .unknown

    // Dynamic menu item refs
    var statusMenuItem: NSMenuItem!
    var modelMenuItem: NSMenuItem!
    var startItem: NSMenuItem!
    var stopItem: NSMenuItem!
    var restartItem: NSMenuItem!
    var proxyAtLoginItem: NSMenuItem!
    var menubarAtLoginItem: NSMenuItem!
    var providerSubmenu: NSMenu!
    var presetsSubmenu: NSMenu!

    var settingsWC: SettingsWindowController?
    var modelsWC: ModelsWindowController?

    // MARK: Launch

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        applyStatusIcon(.unknown)
        buildMenu()
        startHealthCheck()

        NotificationCenter.default.addObserver(
            self, selector: #selector(handleShouldRestart),
            name: .init("CcpShouldRestart"), object: nil
        )
    }

    @objc func handleShouldRestart() {
        restartProxy()
    }

    // MARK: - Status Icon

    func applyStatusIcon(_ status: ProxyStatus) {
        guard let button = statusItem.button else { return }

        let symbolName: String
        let color: NSColor
        let fallback: String

        switch status {
        case .running:
            symbolName = "circle.fill"
            color      = .systemGreen
            fallback   = "●"
        case .stopped:
            symbolName = "circle.fill"
            color      = .systemRed
            fallback   = "○"
        case .unknown:
            symbolName = "circle.dotted"
            color      = .systemYellow
            fallback   = "◌"
        }

        if let img = NSImage(systemSymbolName: symbolName,
                             accessibilityDescription: "\(status)") {
            let cfg = NSImage.SymbolConfiguration(paletteColors: [color])
            button.image = img.withSymbolConfiguration(cfg)
            button.title = ""
        } else {
            button.image = nil
            button.title = fallback
        }
    }

    // MARK: - Menu Construction

    func buildMenu() {
        let menu = NSMenu()
        menu.autoenablesItems = false

        // Status header
        statusMenuItem = NSMenuItem(title: "Checking…", action: nil, keyEquivalent: "")
        statusMenuItem.isEnabled = false
        menu.addItem(statusMenuItem)

        // Model config — clickable to open settings
        modelMenuItem = NSMenuItem(title: loadModelSummary(),
                                   action: #selector(openSettings),
                                   keyEquivalent: "")
        modelMenuItem.target = self
        menu.addItem(modelMenuItem)

        menu.addItem(.separator())

        // Proxy controls
        startItem = NSMenuItem(title: "Start Proxy",
                               action: #selector(startProxy), keyEquivalent: "")
        startItem.target = self
        menu.addItem(startItem)

        stopItem = NSMenuItem(title: "Stop Proxy",
                              action: #selector(stopProxy), keyEquivalent: "")
        stopItem.target = self
        stopItem.isEnabled = false
        menu.addItem(stopItem)

        restartItem = NSMenuItem(title: "Restart Proxy",
                                 action: #selector(restartProxy), keyEquivalent: "r")
        restartItem.target = self
        menu.addItem(restartItem)

        menu.addItem(.separator())

        // Provider submenu
        let providerMenu = NSMenuItem(title: "Provider", action: nil, keyEquivalent: "")
        providerSubmenu = NSMenu()
        for p in PROVIDERS {
            let item = NSMenuItem(title: p, action: #selector(selectProvider(_:)), keyEquivalent: "")
            item.target = self
            item.representedObject = p
            providerSubmenu.addItem(item)
        }
        providerMenu.submenu = providerSubmenu
        menu.addItem(providerMenu)
        refreshProviderCheckmarks()

        // Presets submenu
        let presetsMenu = NSMenuItem(title: "Presets", action: nil, keyEquivalent: "")
        presetsSubmenu = NSMenu()
        for (i, preset) in PRESETS.enumerated() {
            let title = "\(preset.name)  —  \(preset.provider): \(preset.frontier)/\(preset.big)/\(preset.small)"
            let item = NSMenuItem(title: title, action: #selector(applyPreset(_:)), keyEquivalent: "")
            item.target = self
            item.tag = i
            presetsSubmenu.addItem(item)
        }
        presetsMenu.submenu = presetsSubmenu
        menu.addItem(presetsMenu)

        // Settings
        let settingsItem = NSMenuItem(title: "Settings…",
                                      action: #selector(openSettings), keyEquivalent: ",")
        settingsItem.target = self
        menu.addItem(settingsItem)

        let modelsItem = NSMenuItem(title: "Manage Models…",
                                    action: #selector(openModels), keyEquivalent: "m")
        modelsItem.target = self
        menu.addItem(modelsItem)

        let editEnvItem = NSMenuItem(title: "Edit .env…",
                                     action: #selector(editEnv), keyEquivalent: "")
        editEnvItem.target = self
        menu.addItem(editEnvItem)

        menu.addItem(.separator())

        // Client + log
        let copyItem = NSMenuItem(title: "Copy Client Setup",
                                  action: #selector(copyClientSetup), keyEquivalent: "c")
        copyItem.target = self
        menu.addItem(copyItem)

        let logItem = NSMenuItem(title: "View Log",
                                 action: #selector(viewLog), keyEquivalent: "l")
        logItem.target = self
        menu.addItem(logItem)

        let switcherItem = NSMenuItem(title: "Open Model Switcher (TUI)",
                                      action: #selector(openSwitcher), keyEquivalent: "")
        switcherItem.target = self
        menu.addItem(switcherItem)

        let revealItem = NSMenuItem(title: "Reveal Project in Finder",
                                    action: #selector(revealInFinder), keyEquivalent: "")
        revealItem.target = self
        menu.addItem(revealItem)

        menu.addItem(.separator())

        // Login toggles
        proxyAtLoginItem = NSMenuItem(title: "Start Proxy at Login",
                                      action: #selector(toggleProxyAtLogin), keyEquivalent: "")
        proxyAtLoginItem.target = self
        proxyAtLoginItem.state = isLaunchAgentLoaded(label: PROXY_LABEL) ? .on : .off
        menu.addItem(proxyAtLoginItem)

        menubarAtLoginItem = NSMenuItem(title: "Start Menu Bar at Login",
                                        action: #selector(toggleMenuBarAtLogin), keyEquivalent: "")
        menubarAtLoginItem.target = self
        menubarAtLoginItem.state = isLaunchAgentLoaded(label: MENUBAR_LABEL) ? .on : .off
        menu.addItem(menubarAtLoginItem)

        menu.addItem(.separator())

        // Troubleshoot + About
        let troubleshootItem = NSMenuItem(title: "Troubleshoot…",
                                          action: #selector(runTroubleshoot), keyEquivalent: "")
        troubleshootItem.target = self
        menu.addItem(troubleshootItem)

        let aboutItem = NSMenuItem(title: "About CCP LiteLLM",
                                   action: #selector(showAbout), keyEquivalent: "")
        aboutItem.target = self
        menu.addItem(aboutItem)

        menu.addItem(.separator())

        menu.addItem(NSMenuItem(title: "Quit CCP Monitor",
                                action: #selector(NSApplication.terminate(_:)),
                                keyEquivalent: "q"))

        statusItem.menu = menu
    }

    func refreshProviderCheckmarks() {
        let current = EnvFileManager.shared.get("PREFERRED_PROVIDER") ?? "azure"
        for item in providerSubmenu.items {
            item.state = (item.representedObject as? String == current) ? .on : .off
        }
    }

    // MARK: - Health Check

    func startHealthCheck() {
        performHealthCheck()
        healthTimer = Timer.scheduledTimer(withTimeInterval: 5.0, repeats: true) { [weak self] _ in
            self?.performHealthCheck()
        }
    }

    func performHealthCheck() {
        guard let url = URL(string: HEALTH_URL) else { return }
        var req = URLRequest(url: url, timeoutInterval: 3.0)
        req.httpMethod = "GET"
        URLSession.shared.dataTask(with: req) { [weak self] _, response, _ in
            let ok = (response as? HTTPURLResponse)?.statusCode == 200
            DispatchQueue.main.async {
                self?.applyUpdate(ok ? .running : .stopped)
            }
        }.resume()
    }

    func applyUpdate(_ status: ProxyStatus) {
        modelMenuItem?.title = loadModelSummary()
        refreshProviderCheckmarks()

        guard status != currentStatus else { return }
        currentStatus = status
        applyStatusIcon(status)

        switch status {
        case .running:
            statusMenuItem.title = "● Running on :\(PORT)"
            startItem.isEnabled   = false
            stopItem.isEnabled    = true
            restartItem.isEnabled = true
        case .stopped:
            statusMenuItem.title = "○ Stopped"
            startItem.isEnabled   = true
            stopItem.isEnabled    = false
            restartItem.isEnabled = false
        case .unknown:
            statusMenuItem.title = "◌ Checking…"
            startItem.isEnabled   = true
            stopItem.isEnabled    = true
            restartItem.isEnabled = true
        }
    }

    // MARK: - Shell Runner

    @discardableResult
    func shell(_ args: [String]) -> (output: String, code: Int32) {
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: CCP_BIN)
        proc.arguments = args
        let pipe = Pipe()
        proc.standardOutput = pipe
        proc.standardError  = pipe
        do { try proc.run(); proc.waitUntilExit() }
        catch { return ("Failed to run \(CCP_BIN): \(error)", 1) }
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        return (String(data: data, encoding: .utf8)?.trimmingCharacters(in: .newlines) ?? "",
                proc.terminationStatus)
    }

    // MARK: - Proxy Actions

    @objc func startProxy() {
        startItem.isEnabled = false
        startItem.title     = "Starting…"
        DispatchQueue.global(qos: .userInitiated).async {
            self.shell(["start"])
            DispatchQueue.main.asyncAfter(deadline: .now() + 2.5) {
                self.startItem.title = "Start Proxy"
                self.performHealthCheck()
            }
        }
    }

    @objc func stopProxy() {
        stopItem.isEnabled = false
        stopItem.title     = "Stopping…"
        DispatchQueue.global(qos: .userInitiated).async {
            self.shell(["stop"])
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) {
                self.stopItem.title = "Stop Proxy"
                self.performHealthCheck()
            }
        }
    }

    @objc func restartProxy() {
        restartItem.isEnabled = false
        restartItem.title     = "Restarting…"
        DispatchQueue.global(qos: .userInitiated).async {
            self.shell(["restart"])
            DispatchQueue.main.asyncAfter(deadline: .now() + 4.0) {
                self.restartItem.title = "Restart Proxy"
                self.performHealthCheck()
            }
        }
    }

    // MARK: - Provider + Presets

    @objc func selectProvider(_ sender: NSMenuItem) {
        guard let prov = sender.representedObject as? String else { return }
        EnvFileManager.shared.patch(["PREFERRED_PROVIDER": prov])
        refreshProviderCheckmarks()
        modelMenuItem?.title = loadModelSummary()
        restartProxy()
    }

    @objc func applyPreset(_ sender: NSMenuItem) {
        let idx = sender.tag
        guard idx >= 0, idx < PRESETS.count else { return }
        let p = PRESETS[idx]
        EnvFileManager.shared.patch([
            "PREFERRED_PROVIDER": p.provider,
            "BIG_MODEL":          p.big,
            "SMALL_MODEL":        p.small,
            "FRONTIER_MODEL":     p.frontier,
        ])
        refreshProviderCheckmarks()
        modelMenuItem?.title = loadModelSummary()
        restartProxy()
    }

    // MARK: - Settings Window

    @objc func openSettings() {
        if settingsWC == nil {
            settingsWC = SettingsWindowController()
            settingsWC?.onSave = { [weak self] in
                self?.refreshProviderCheckmarks()
                self?.modelMenuItem?.title = self?.loadModelSummary() ?? ""
            }
        }
        NSApp.activate(ignoringOtherApps: true)
        settingsWC?.showWindow(nil)
    }

    @objc func openModels() {
        if modelsWC == nil {
            modelsWC = ModelsWindowController()
        }
        NSApp.activate(ignoringOtherApps: true)
        modelsWC?.showWindow(nil)
    }

    @objc func editEnv() {
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/usr/bin/open")
        proc.arguments = ["-t", ENV_FILE]
        try? proc.run()
    }

    @objc func revealInFinder() {
        NSWorkspace.shared.selectFile(ENV_FILE, inFileViewerRootedAtPath: CCP_DIR)
    }

    // MARK: - About

    @objc func showAbout() {
        let alert = NSAlert()
        alert.messageText = "CCP LiteLLM Menu Bar"
        alert.informativeText = """
        Version \(APP_VERSION)

        Claude Code Proxy manager for the FastAPI + LiteLLM server on port \(PORT).
        Translates Anthropic API requests to Azure OpenAI / OpenAI / Google / Anthropic.

        Project: \(CCP_DIR)
        Log: \(LOG_FILE)
        Health: \(HEALTH_URL)

        CLI: ccp-litellm  •  Slash: /ccp-litellm
        """
        alert.addButton(withTitle: "OK")
        alert.addButton(withTitle: "Open Project")
        let resp = alert.runModal()
        if resp == .alertSecondButtonReturn {
            revealInFinder()
        }
    }

    // MARK: - Log

    @objc func viewLog() {
        let logURL = URL(fileURLWithPath: LOG_FILE)
        if !FileManager.default.fileExists(atPath: LOG_FILE) {
            try? "".write(to: logURL, atomically: true, encoding: .utf8)
        }
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/usr/bin/open")
        proc.arguments = ["-a", "Console", LOG_FILE]
        if (try? proc.run()) != nil { return }
        NSWorkspace.shared.open(logURL)
    }

    // MARK: - Model Switcher

    @objc func openSwitcher() {
        let script = """
        tell application "Terminal"
            activate
            do script "\(CCP_DIR)/ccp-switcher"
        end tell
        """
        var err: NSDictionary?
        _ = NSAppleScript(source: script)?.executeAndReturnError(&err)
        if err != nil {
            NSWorkspace.shared.open(URL(fileURLWithPath: CCP_DIR))
        }
    }

    // MARK: - LaunchAgent Management

    func uid() -> String { String(Darwin.getuid()) }

    func isLaunchAgentLoaded(label: String) -> Bool {
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/bin/launchctl")
        proc.arguments     = ["list", label]
        proc.standardOutput = FileHandle.nullDevice
        proc.standardError  = FileHandle.nullDevice
        try? proc.run()
        proc.waitUntilExit()
        return proc.terminationStatus == 0
    }

    func launchAgentLoad(plist: String) {
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/bin/launchctl")
        proc.arguments     = ["bootstrap", "gui/\(uid())", plist]
        try? proc.run()
        proc.waitUntilExit()
    }

    func launchAgentUnload(label: String) {
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/bin/launchctl")
        proc.arguments     = ["bootout", "gui/\(uid())/\(label)"]
        try? proc.run()
        proc.waitUntilExit()
    }

    @objc func toggleProxyAtLogin() {
        if isLaunchAgentLoaded(label: PROXY_LABEL) {
            launchAgentUnload(label: PROXY_LABEL)
            proxyAtLoginItem.state = .off
        } else {
            launchAgentLoad(plist: PROXY_PLIST)
            proxyAtLoginItem.state = .on
        }
    }

    @objc func toggleMenuBarAtLogin() {
        if isLaunchAgentLoaded(label: MENUBAR_LABEL) {
            launchAgentUnload(label: MENUBAR_LABEL)
            menubarAtLoginItem.state = .off
        } else {
            launchAgentLoad(plist: MENUBAR_PLIST)
            menubarAtLoginItem.state = .on
        }
    }

    // MARK: - Troubleshoot

    @objc func runTroubleshoot() {
        DispatchQueue.global(qos: .userInitiated).async {
            let result = self.shell(["troubleshoot"])
            DispatchQueue.main.async {
                self.showScrollableAlert(
                    title: "CCP LiteLLM — Troubleshoot",
                    text: result.output.isEmpty ? "(no output)" : result.output
                )
            }
        }
    }

    func showScrollableAlert(title: String, text: String) {
        let alert = NSAlert()
        alert.messageText = title
        alert.addButton(withTitle: "OK")

        let scroll = NSScrollView(frame: NSRect(x: 0, y: 0, width: 520, height: 260))
        scroll.hasVerticalScroller   = true
        scroll.hasHorizontalScroller = false
        scroll.autohidesScrollers    = true
        scroll.borderType            = .bezelBorder

        let tv = NSTextView(frame: scroll.bounds)
        tv.isEditable              = false
        tv.isSelectable            = true
        tv.string                  = text
        tv.font                    = NSFont.monospacedSystemFont(ofSize: 11, weight: .regular)
        tv.backgroundColor         = NSColor.textBackgroundColor
        tv.textContainerInset      = NSSize(width: 4, height: 4)
        tv.isVerticallyResizable   = true
        tv.maxSize                 = NSSize(width: CGFloat.greatestFiniteMagnitude,
                                             height: CGFloat.greatestFiniteMagnitude)
        tv.textContainer?.widthTracksTextView = true
        tv.autoresizingMask        = .width

        scroll.documentView = tv
        alert.accessoryView = scroll
        alert.runModal()
    }

    // MARK: - Env Parsing

    func loadModelSummary() -> String {
        let env = EnvFileManager.shared
        guard !env.read().isEmpty else { return "Config: (unreadable)" }

        let prov     = env.get("PREFERRED_PROVIDER") ?? "openai"
        let frontier = env.get("FRONTIER_MODEL")     ?? "gpt-5.2"
        let big      = env.get("BIG_MODEL")           ?? "gpt-4.1"
        let small    = env.get("SMALL_MODEL")         ?? "gpt-4.1-mini"
        let proxyKey = env.get("PROXY_API_KEY")

        let auth: String
        if prov == "anthropic" {
            auth = "⚠ ANTHROPIC_API_KEY required"
        } else if proxyKey != nil {
            auth = "proxy-key required"
        } else {
            auth = "no Anthropic key needed"
        }

        return "\(prov): \(frontier)/\(big)/\(small)  [\(auth)]"
    }

    // MARK: - Copy Client Setup

    @objc func copyClientSetup() {
        let env = EnvFileManager.shared
        let prov     = env.get("PREFERRED_PROVIDER") ?? "openai"
        let proxyKey = env.get("PROXY_API_KEY")

        var parts = ["ANTHROPIC_BASE_URL=http://localhost:\(PORT)"]
        if prov == "anthropic" {
            parts.append("ANTHROPIC_API_KEY=<your-sk-ant-...>")
        } else if let key = proxyKey {
            parts.append("ANTHROPIC_API_KEY=\(key)")
        } else {
            parts.append("ANTHROPIC_API_KEY=proxy")
        }

        copyToClipboard(parts.joined(separator: " "))

        let saved = statusMenuItem.title
        statusMenuItem.title = "Copied to clipboard!"
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) {
            self.statusMenuItem.title = saved
        }
    }

    func copyToClipboard(_ text: String) {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
    }
}

// ---------------------------------------------------------------------------
// MARK: - Entry Point
// ---------------------------------------------------------------------------

@main
enum CcpMenuBarMain {
    static func main() {
        let delegate = AppDelegate()
        let app = NSApplication.shared
        app.delegate = delegate
        // Keep a strong reference to prevent deallocation
        withExtendedLifetime(delegate) {
            app.run()
        }
    }
}
