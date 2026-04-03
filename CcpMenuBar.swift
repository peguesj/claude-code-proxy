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

let PROXY_PLIST    = NSHomeDirectory() + "/Library/LaunchAgents/com.ccp.litellm.proxy.plist"
let MENUBAR_PLIST   = NSHomeDirectory() + "/Library/LaunchAgents/com.ccp.litellm.menubar.plist"
let PROXY_LABEL    = "com.ccp.litellm.proxy"
let MENUBAR_LABEL   = "com.ccp.litellm.menubar"

// ---------------------------------------------------------------------------
// MARK: - ProxyStatus
// ---------------------------------------------------------------------------

enum ProxyStatus: Equatable {
    case running, stopped, unknown
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

    // MARK: Launch

    func applicationDidFinishLaunching(_ notification: Notification) {
        // Hide from Dock
        NSApp.setActivationPolicy(.accessory)

        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        applyStatusIcon(.unknown)
        buildMenu()
        startHealthCheck()
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

        // -- Status header (updated dynamically)
        statusMenuItem = NSMenuItem(title: "Checking…", action: nil, keyEquivalent: "")
        statusMenuItem.isEnabled = false
        menu.addItem(statusMenuItem)

        // -- Model config (updated dynamically)
        modelMenuItem = NSMenuItem(title: loadModelSummary(), action: nil, keyEquivalent: "")
        modelMenuItem.isEnabled = false
        menu.addItem(modelMenuItem)

        menu.addItem(.separator())

        // -- Proxy controls
        startItem = NSMenuItem(title: "Start Proxy",
                               action: #selector(startProxy),
                               keyEquivalent: "")
        startItem.target = self
        startItem.isEnabled = true
        menu.addItem(startItem)

        stopItem = NSMenuItem(title: "Stop Proxy",
                              action: #selector(stopProxy),
                              keyEquivalent: "")
        stopItem.target = self
        stopItem.isEnabled = false
        menu.addItem(stopItem)

        restartItem = NSMenuItem(title: "Restart Proxy",
                                 action: #selector(restartProxy),
                                 keyEquivalent: "r")
        restartItem.target = self
        restartItem.isEnabled = true
        menu.addItem(restartItem)

        menu.addItem(.separator())

        // -- Log & switcher
        let copyItem = NSMenuItem(title: "Copy Client Setup",
                                   action: #selector(copyClientSetup),
                                   keyEquivalent: "c")
        copyItem.target = self
        copyItem.isEnabled = true
        menu.addItem(copyItem)

        let logItem = NSMenuItem(title: "View Log",
                                 action: #selector(viewLog),
                                 keyEquivalent: "l")
        logItem.target = self
        logItem.isEnabled = true
        menu.addItem(logItem)

        let switcherItem = NSMenuItem(title: "Open Model Switcher",
                                      action: #selector(openSwitcher),
                                      keyEquivalent: "")
        switcherItem.target = self
        switcherItem.isEnabled = true
        menu.addItem(switcherItem)

        menu.addItem(.separator())

        // -- Login toggles
        proxyAtLoginItem = NSMenuItem(title: "Start Proxy at Login",
                                       action: #selector(toggleProxyAtLogin),
                                       keyEquivalent: "")
        proxyAtLoginItem.target = self
        proxyAtLoginItem.isEnabled = true
        proxyAtLoginItem.state = isLaunchAgentLoaded(label: PROXY_LABEL) ? .on : .off
        menu.addItem(proxyAtLoginItem)

        menubarAtLoginItem = NSMenuItem(title: "Start Menu Bar at Login",
                                         action: #selector(toggleMenuBarAtLogin),
                                         keyEquivalent: "")
        menubarAtLoginItem.target = self
        menubarAtLoginItem.isEnabled = true
        menubarAtLoginItem.state = isLaunchAgentLoaded(label: MENUBAR_LABEL) ? .on : .off
        menu.addItem(menubarAtLoginItem)

        menu.addItem(.separator())

        // -- Troubleshoot
        let troubleshootItem = NSMenuItem(title: "Troubleshoot…",
                                           action: #selector(runTroubleshoot),
                                           keyEquivalent: "")
        troubleshootItem.target = self
        troubleshootItem.isEnabled = true
        menu.addItem(troubleshootItem)

        menu.addItem(.separator())

        // -- Quit
        let quitItem = NSMenuItem(title: "Quit CCP Monitor",
                                   action: #selector(NSApplication.terminate(_:)),
                                   keyEquivalent: "q")
        menu.addItem(quitItem)

        statusItem.menu = menu
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
        // Always refresh model display
        modelMenuItem?.title = loadModelSummary()

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

    // MARK: - Log

    @objc func viewLog() {
        // Try Console.app first; fall back to open -t (TextEdit)
        let logURL = URL(fileURLWithPath: LOG_FILE)
        if !FileManager.default.fileExists(atPath: LOG_FILE) {
            // Create empty file so Console.app doesn't fail
            try? "".write(to: logURL, atomically: true, encoding: .utf8)
        }
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/usr/bin/open")
        proc.arguments = ["-a", "Console", LOG_FILE]
        if (try? proc.run()) != nil { return }
        // Fallback
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
            // Fallback: reveal in Finder
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
        guard let raw = try? String(contentsOfFile: ENV_FILE, encoding: .utf8) else {
            return "Config: (unreadable)"
        }

        func get(_ key: String) -> String? {
            for line in raw.components(separatedBy: .newlines) {
                let t = line.trimmingCharacters(in: .whitespaces)
                guard !t.hasPrefix("#"), t.hasPrefix("\(key)=") else { continue }
                var v = String(t.dropFirst(key.count + 1))
                v = v.trimmingCharacters(in: CharacterSet(charactersIn: "\"'"))
                return v.isEmpty ? nil : v
            }
            return nil
        }

        let prov     = get("PREFERRED_PROVIDER") ?? "openai"
        let frontier = get("FRONTIER_MODEL")     ?? "gpt-5.2"
        let big      = get("BIG_MODEL")           ?? "gpt-4.1"
        let small    = get("SMALL_MODEL")         ?? "gpt-4.1-mini"
        let proxyKey = get("PROXY_API_KEY")

        // Auth label: show whether ANTHROPIC_API_KEY matters to the client
        let auth: String
        if prov == "anthropic" {
            auth = "⚠ ANTHROPIC_API_KEY required"
        } else if let _ = proxyKey {
            auth = "proxy-key required"
        } else {
            auth = "no Anthropic key needed"
        }

        return "\(prov): \(frontier)/\(big)/\(small)  [\(auth)]"
    }

    // MARK: - Copy Client Setup

    @objc func copyClientSetup() {
        guard let content = try? String(contentsOfFile: ENV_FILE, encoding: .utf8) else {
            copyToClipboard("ANTHROPIC_BASE_URL=http://localhost:\(PORT)")
            return
        }

        func get(_ key: String) -> String? {
            for line in content.components(separatedBy: .newlines) {
                let t = line.trimmingCharacters(in: .whitespaces)
                guard !t.hasPrefix("#"), t.hasPrefix("\(key)=") else { continue }
                var v = String(t.dropFirst(key.count + 1))
                v = v.trimmingCharacters(in: CharacterSet(charactersIn: "\"'"))
                return v.isEmpty ? nil : v
            }
            return nil
        }

        let prov     = get("PREFERRED_PROVIDER") ?? "openai"
        let proxyKey = get("PROXY_API_KEY")

        var parts = ["ANTHROPIC_BASE_URL=http://localhost:\(PORT)"]

        if prov == "anthropic" {
            // Real key needed — user must supply it; just give them the base URL
            parts.append("ANTHROPIC_API_KEY=<your-sk-ant-...>")
        } else if let key = proxyKey {
            // Proxy enforces a specific client key
            parts.append("ANTHROPIC_API_KEY=\(key)")
        } else {
            // Any value works; use a placeholder that makes the intent clear
            parts.append("ANTHROPIC_API_KEY=proxy")
        }

        copyToClipboard(parts.joined(separator: " "))

        // Brief visual confirmation in status header
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

let delegate = AppDelegate()
let app = NSApplication.shared
app.delegate = delegate
app.run()
