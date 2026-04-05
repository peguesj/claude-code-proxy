// CcpModels.swift — Model discovery & deployment for CCP LiteLLM
// Compiled alongside CcpMenuBar.swift via build-menubar.sh

import Cocoa

// ---------------------------------------------------------------------------
// MARK: - ModelEntry
// ---------------------------------------------------------------------------

struct ModelEntry {
    enum Kind { case deployment, catalog }
    let id: String             // deployment name OR model id
    let modelName: String      // canonical model (e.g. "gpt-4o")
    let version: String        // model version (may be empty)
    let kind: Kind
    let provider: String       // azure / openai / anthropic / google
    let capacity: Int?         // TPM capacity (Azure) or context length
    let status: String         // "Succeeded", "Creating", "Available", etc.
    let sku: String            // "Standard", "GlobalStandard", or ""
}

// ---------------------------------------------------------------------------
// MARK: - ModelsManager
// ---------------------------------------------------------------------------

final class ModelsManager {
    static let shared = ModelsManager()

    // MARK: - Azure (REST — Azure OpenAI)

    /// Lists deployments in the configured Azure OpenAI account via REST API.
    /// Uses AZURE_API_KEY + AZURE_API_BASE from .env.
    func listAzureDeployments(completion: @escaping (Result<[ModelEntry], Error>) -> Void) {
        let env = EnvFileManager.shared
        guard let baseRaw = env.get("AZURE_API_BASE"),
              let key = env.get("AZURE_API_KEY") else {
            completion(.failure(NSError(domain: "ccp", code: 1,
                userInfo: [NSLocalizedDescriptionKey: "AZURE_API_BASE / AZURE_API_KEY not set in .env"])))
            return
        }
        let apiVersion = env.get("AZURE_API_VERSION") ?? "2024-10-21"
        let base = baseRaw.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        guard let url = URL(string: "\(base)/openai/deployments?api-version=\(apiVersion)") else {
            completion(.failure(NSError(domain: "ccp", code: 2,
                userInfo: [NSLocalizedDescriptionKey: "Invalid AZURE_API_BASE URL"])))
            return
        }
        var req = URLRequest(url: url, timeoutInterval: 15.0)
        req.httpMethod = "GET"
        req.setValue(key, forHTTPHeaderField: "api-key")

        URLSession.shared.dataTask(with: req) { data, response, err in
            if let err = err { completion(.failure(err)); return }
            guard let data = data else {
                completion(.failure(NSError(domain: "ccp", code: 3,
                    userInfo: [NSLocalizedDescriptionKey: "No data from Azure"])))
                return
            }
            if let http = response as? HTTPURLResponse, http.statusCode >= 400 {
                let body = String(data: data, encoding: .utf8) ?? ""
                completion(.failure(NSError(domain: "ccp", code: http.statusCode,
                    userInfo: [NSLocalizedDescriptionKey: "HTTP \(http.statusCode): \(body.prefix(300))"])))
                return
            }
            do {
                let obj = try JSONSerialization.jsonObject(with: data) as? [String: Any]
                let arr = (obj?["data"] as? [[String: Any]]) ?? []
                var entries: [ModelEntry] = []
                for dep in arr {
                    let id = (dep["id"] as? String) ?? ""
                    let modelDict = dep["model"] as? [String: Any]
                    let modelName = (modelDict?["name"] as? String)
                        ?? (dep["model"] as? String) ?? id
                    let version = (modelDict?["version"] as? String) ?? ""
                    let status = (dep["status"] as? String) ?? "unknown"
                    let scale = dep["scale_settings"] as? [String: Any]
                    let sku = (scale?["scale_type"] as? String) ?? (dep["sku"] as? [String: Any])?["name"] as? String ?? ""
                    let capacity = (dep["capacity"] as? Int) ?? (scale?["capacity"] as? Int)
                    entries.append(ModelEntry(
                        id: id, modelName: modelName, version: version,
                        kind: .deployment, provider: "azure",
                        capacity: capacity, status: status, sku: sku
                    ))
                }
                completion(.success(entries.sorted { $0.id < $1.id }))
            } catch {
                completion(.failure(error))
            }
        }.resume()
    }

    /// Lists the model catalog available for deployment in the configured
    /// Azure account. Shells `az cognitiveservices account list-models`.
    func listAzureCatalog(completion: @escaping (Result<[ModelEntry], Error>) -> Void) {
        let env = EnvFileManager.shared
        guard let rg = env.get("AZURE_RESOURCE_GROUP"),
              let account = env.get("AZURE_ACCOUNT_NAME") else {
            completion(.failure(NSError(domain: "ccp", code: 4,
                userInfo: [NSLocalizedDescriptionKey:
                    "Set AZURE_RESOURCE_GROUP and AZURE_ACCOUNT_NAME in .env to browse catalog"])))
            return
        }
        runAzCLI([
            "cognitiveservices", "account", "list-models",
            "--resource-group", rg,
            "--name", account,
            "-o", "json"
        ]) { result in
            switch result {
            case .failure(let e): completion(.failure(e))
            case .success(let data):
                do {
                    let arr = try JSONSerialization.jsonObject(with: data) as? [[String: Any]] ?? []
                    var entries: [ModelEntry] = []
                    for m in arr {
                        let model = m["model"] as? [String: Any] ?? m
                        let name = (model["name"] as? String) ?? ""
                        let version = (model["version"] as? String) ?? ""
                        let format = (model["format"] as? String) ?? ""
                        let skuList = (model["skus"] as? [[String: Any]]) ?? []
                        let skuName = (skuList.first?["name"] as? String) ?? ""
                        let caps = skuList.first?["capacity"] as? [String: Any]
                        let capacity = (caps?["default"] as? Int)
                        guard format.lowercased().contains("openai") ||
                              format.isEmpty ||
                              name.lowercased().contains("gpt") ||
                              name.lowercased().contains("o3") ||
                              name.lowercased().contains("o4")
                        else { continue }
                        entries.append(ModelEntry(
                            id: "\(name):\(version)", modelName: name,
                            version: version, kind: .catalog, provider: "azure",
                            capacity: capacity, status: "Available", sku: skuName
                        ))
                    }
                    completion(.success(entries.sorted { $0.modelName < $1.modelName }))
                } catch {
                    completion(.failure(error))
                }
            }
        }
    }

    /// Creates a new Azure OpenAI deployment via `az` CLI.
    func deployAzureModel(
        deploymentName: String, modelName: String, modelVersion: String,
        skuName: String, skuCapacity: Int,
        completion: @escaping (Result<String, Error>) -> Void
    ) {
        let env = EnvFileManager.shared
        guard let rg = env.get("AZURE_RESOURCE_GROUP"),
              let account = env.get("AZURE_ACCOUNT_NAME") else {
            completion(.failure(NSError(domain: "ccp", code: 4,
                userInfo: [NSLocalizedDescriptionKey:
                    "Set AZURE_RESOURCE_GROUP and AZURE_ACCOUNT_NAME in .env"])))
            return
        }
        runAzCLI([
            "cognitiveservices", "account", "deployment", "create",
            "--resource-group", rg,
            "--name", account,
            "--deployment-name", deploymentName,
            "--model-name", modelName,
            "--model-version", modelVersion,
            "--model-format", "OpenAI",
            "--sku-name", skuName,
            "--sku-capacity", String(skuCapacity),
            "-o", "json"
        ]) { result in
            switch result {
            case .failure(let e): completion(.failure(e))
            case .success(let data):
                completion(.success(String(data: data, encoding: .utf8) ?? ""))
            }
        }
    }

    // MARK: - OpenAI

    func listOpenAI(completion: @escaping (Result<[ModelEntry], Error>) -> Void) {
        guard let key = EnvFileManager.shared.get("OPENAI_API_KEY") else {
            completion(.failure(NSError(domain: "ccp", code: 5,
                userInfo: [NSLocalizedDescriptionKey: "OPENAI_API_KEY not set"])))
            return
        }
        guard let url = URL(string: "https://api.openai.com/v1/models") else { return }
        var req = URLRequest(url: url, timeoutInterval: 15)
        req.setValue("Bearer \(key)", forHTTPHeaderField: "Authorization")
        URLSession.shared.dataTask(with: req) { data, response, err in
            if let err = err { completion(.failure(err)); return }
            guard let data = data else { return }
            if let http = response as? HTTPURLResponse, http.statusCode >= 400 {
                let body = String(data: data, encoding: .utf8) ?? ""
                completion(.failure(NSError(domain: "ccp", code: http.statusCode,
                    userInfo: [NSLocalizedDescriptionKey: "HTTP \(http.statusCode): \(body.prefix(300))"])))
                return
            }
            do {
                let obj = try JSONSerialization.jsonObject(with: data) as? [String: Any]
                let arr = (obj?["data"] as? [[String: Any]]) ?? []
                var entries: [ModelEntry] = []
                for m in arr {
                    let id = (m["id"] as? String) ?? ""
                    entries.append(ModelEntry(
                        id: id, modelName: id, version: "", kind: .deployment,
                        provider: "openai", capacity: nil, status: "Available", sku: ""
                    ))
                }
                completion(.success(entries.sorted { $0.id < $1.id }))
            } catch {
                completion(.failure(error))
            }
        }.resume()
    }

    // MARK: - Anthropic

    func listAnthropic(completion: @escaping (Result<[ModelEntry], Error>) -> Void) {
        guard let key = EnvFileManager.shared.get("ANTHROPIC_API_KEY") else {
            completion(.failure(NSError(domain: "ccp", code: 6,
                userInfo: [NSLocalizedDescriptionKey: "ANTHROPIC_API_KEY not set"])))
            return
        }
        guard let url = URL(string: "https://api.anthropic.com/v1/models") else { return }
        var req = URLRequest(url: url, timeoutInterval: 15)
        req.setValue(key, forHTTPHeaderField: "x-api-key")
        req.setValue("2023-06-01", forHTTPHeaderField: "anthropic-version")
        URLSession.shared.dataTask(with: req) { data, response, err in
            if let err = err { completion(.failure(err)); return }
            guard let data = data else { return }
            if let http = response as? HTTPURLResponse, http.statusCode >= 400 {
                let body = String(data: data, encoding: .utf8) ?? ""
                completion(.failure(NSError(domain: "ccp", code: http.statusCode,
                    userInfo: [NSLocalizedDescriptionKey: "HTTP \(http.statusCode): \(body.prefix(300))"])))
                return
            }
            do {
                let obj = try JSONSerialization.jsonObject(with: data) as? [String: Any]
                let arr = (obj?["data"] as? [[String: Any]]) ?? []
                var entries: [ModelEntry] = []
                for m in arr {
                    let id = (m["id"] as? String) ?? ""
                    let name = (m["display_name"] as? String) ?? id
                    entries.append(ModelEntry(
                        id: id, modelName: name, version: "", kind: .deployment,
                        provider: "anthropic", capacity: nil, status: "Available", sku: ""
                    ))
                }
                completion(.success(entries.sorted { $0.id < $1.id }))
            } catch {
                completion(.failure(error))
            }
        }.resume()
    }

    // MARK: - Google

    func listGoogle(completion: @escaping (Result<[ModelEntry], Error>) -> Void) {
        guard let key = EnvFileManager.shared.get("GEMINI_API_KEY") else {
            completion(.failure(NSError(domain: "ccp", code: 7,
                userInfo: [NSLocalizedDescriptionKey: "GEMINI_API_KEY not set"])))
            return
        }
        guard let url = URL(string: "https://generativelanguage.googleapis.com/v1beta/models?key=\(key)") else { return }
        let req = URLRequest(url: url, timeoutInterval: 15)
        URLSession.shared.dataTask(with: req) { data, response, err in
            if let err = err { completion(.failure(err)); return }
            guard let data = data else { return }
            if let http = response as? HTTPURLResponse, http.statusCode >= 400 {
                let body = String(data: data, encoding: .utf8) ?? ""
                completion(.failure(NSError(domain: "ccp", code: http.statusCode,
                    userInfo: [NSLocalizedDescriptionKey: "HTTP \(http.statusCode): \(body.prefix(300))"])))
                return
            }
            do {
                let obj = try JSONSerialization.jsonObject(with: data) as? [String: Any]
                let arr = (obj?["models"] as? [[String: Any]]) ?? []
                var entries: [ModelEntry] = []
                for m in arr {
                    let fullName = (m["name"] as? String) ?? ""
                    let id = fullName.replacingOccurrences(of: "models/", with: "")
                    let display = (m["displayName"] as? String) ?? id
                    let ctx = m["inputTokenLimit"] as? Int
                    entries.append(ModelEntry(
                        id: id, modelName: display, version: "",
                        kind: .deployment, provider: "google",
                        capacity: ctx, status: "Available", sku: ""
                    ))
                }
                completion(.success(entries.sorted { $0.id < $1.id }))
            } catch {
                completion(.failure(error))
            }
        }.resume()
    }

    // MARK: - Helpers

    private func runAzCLI(_ args: [String], completion: @escaping (Result<Data, Error>) -> Void) {
        DispatchQueue.global(qos: .userInitiated).async {
            // Resolve `az` absolute path
            let paths = ["/opt/homebrew/bin/az", "/usr/local/bin/az", "/usr/bin/az"]
            guard let azPath = paths.first(where: { FileManager.default.isExecutableFile(atPath: $0) }) else {
                completion(.failure(NSError(domain: "ccp", code: 99,
                    userInfo: [NSLocalizedDescriptionKey: "az CLI not found. Install with: brew install azure-cli"])))
                return
            }
            let proc = Process()
            proc.executableURL = URL(fileURLWithPath: azPath)
            proc.arguments = args
            let out = Pipe(), errp = Pipe()
            proc.standardOutput = out
            proc.standardError = errp
            var env = ProcessInfo.processInfo.environment
            env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
            proc.environment = env
            do { try proc.run() } catch {
                completion(.failure(error)); return
            }
            proc.waitUntilExit()
            let outData = out.fileHandleForReading.readDataToEndOfFile()
            let errData = errp.fileHandleForReading.readDataToEndOfFile()
            if proc.terminationStatus != 0 {
                let msg = String(data: errData, encoding: .utf8) ?? "az failed"
                completion(.failure(NSError(domain: "ccp", code: Int(proc.terminationStatus),
                    userInfo: [NSLocalizedDescriptionKey: msg])))
                return
            }
            completion(.success(outData))
        }
    }
}

// ---------------------------------------------------------------------------
// MARK: - ModelsWindowController
// ---------------------------------------------------------------------------

final class ModelsWindowController: NSWindowController, NSWindowDelegate {

    // One tab = one provider
    struct Tab {
        let provider: String
        let title: String
        weak var tableView: NSTableView?
        weak var statusLabel: NSTextField?
        var entries: [ModelEntry] = []
    }

    var tabs: [String: Tab] = [:]
    var tabView: NSTabView!

    convenience init() {
        let w = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 820, height: 520),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered, defer: false
        )
        w.title = "CCP LiteLLM — Model Manager"
        w.center()
        w.minSize = NSSize(width: 720, height: 420)
        self.init(window: w)
        w.delegate = self
        buildUI()
        refreshAll()
    }

    private func buildUI() {
        guard let content = window?.contentView else { return }
        tabView = NSTabView(frame: content.bounds)
        tabView.autoresizingMask = [.width, .height]
        content.addSubview(tabView)

        addTab(provider: "azure",     title: "Azure OpenAI")
        addTab(provider: "openai",    title: "OpenAI")
        addTab(provider: "anthropic", title: "Anthropic")
        addTab(provider: "google",    title: "Google")
    }

    private func addTab(provider: String, title: String) {
        let item = NSTabViewItem(identifier: provider)
        item.label = title

        let view = NSView()
        item.view = view

        // Toolbar row
        let toolbar = NSStackView()
        toolbar.orientation = .horizontal
        toolbar.spacing = 8
        toolbar.translatesAutoresizingMaskIntoConstraints = false

        let refreshBtn = NSButton(title: "Refresh", target: self, action: #selector(refreshTab(_:)))
        refreshBtn.bezelStyle = .rounded
        refreshBtn.identifier = NSUserInterfaceItemIdentifier(provider)
        toolbar.addArrangedSubview(refreshBtn)

        let useBig = NSButton(title: "Use as BIG", target: self, action: #selector(useAsBig(_:)))
        useBig.bezelStyle = .rounded
        useBig.identifier = NSUserInterfaceItemIdentifier(provider)
        toolbar.addArrangedSubview(useBig)

        let useSmall = NSButton(title: "Use as SMALL", target: self, action: #selector(useAsSmall(_:)))
        useSmall.bezelStyle = .rounded
        useSmall.identifier = NSUserInterfaceItemIdentifier(provider)
        toolbar.addArrangedSubview(useSmall)

        let useFrontier = NSButton(title: "Use as FRONTIER", target: self, action: #selector(useAsFrontier(_:)))
        useFrontier.bezelStyle = .rounded
        useFrontier.identifier = NSUserInterfaceItemIdentifier(provider)
        toolbar.addArrangedSubview(useFrontier)

        if provider == "azure" {
            let deployBtn = NSButton(title: "Deploy Model…", target: self, action: #selector(openDeployDialog(_:)))
            deployBtn.bezelStyle = .rounded
            deployBtn.identifier = NSUserInterfaceItemIdentifier(provider)
            toolbar.addArrangedSubview(deployBtn)
        }

        let spacer = NSView()
        spacer.translatesAutoresizingMaskIntoConstraints = false
        spacer.setContentHuggingPriority(.defaultLow, for: .horizontal)
        toolbar.addArrangedSubview(spacer)

        let status = NSTextField(labelWithString: "Loading…")
        status.font = NSFont.systemFont(ofSize: 11)
        status.textColor = .secondaryLabelColor
        toolbar.addArrangedSubview(status)

        view.addSubview(toolbar)

        // Table
        let scroll = NSScrollView()
        scroll.translatesAutoresizingMaskIntoConstraints = false
        scroll.hasVerticalScroller = true
        scroll.borderType = .bezelBorder
        let table = NSTableView()
        table.usesAlternatingRowBackgroundColors = true
        table.rowHeight = 20
        table.gridStyleMask = .solidHorizontalGridLineMask
        table.identifier = NSUserInterfaceItemIdentifier("table-\(provider)")

        // Columns
        let cols: [(String, String, CGFloat)] = [
            ("id",       provider == "azure" ? "Deployment" : "Model ID", 220),
            ("model",    "Model",   160),
            ("version",  "Version", 100),
            ("sku",      "SKU",      90),
            ("capacity", "Cap/TPM", 70),
            ("status",   "Status",  100),
        ]
        for (key, title, width) in cols {
            let col = NSTableColumn(identifier: NSUserInterfaceItemIdentifier(key))
            col.title = title
            col.width = width
            col.minWidth = 50
            col.resizingMask = [.userResizingMask]
            table.addTableColumn(col)
        }
        table.delegate = self
        table.dataSource = self
        scroll.documentView = table
        view.addSubview(scroll)

        NSLayoutConstraint.activate([
            toolbar.topAnchor.constraint(equalTo: view.topAnchor, constant: 10),
            toolbar.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: 12),
            toolbar.trailingAnchor.constraint(equalTo: view.trailingAnchor, constant: -12),
            scroll.topAnchor.constraint(equalTo: toolbar.bottomAnchor, constant: 10),
            scroll.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: 12),
            scroll.trailingAnchor.constraint(equalTo: view.trailingAnchor, constant: -12),
            scroll.bottomAnchor.constraint(equalTo: view.bottomAnchor, constant: -12),
        ])

        tabs[provider] = Tab(provider: provider, title: title, tableView: table,
                             statusLabel: status, entries: [])
        tabView.addTabViewItem(item)
    }

    private func table(for provider: String) -> NSTableView? { tabs[provider]?.tableView }

    // MARK: - Refresh

    @objc func refreshTab(_ sender: NSButton) {
        guard let prov = sender.identifier?.rawValue else { return }
        refresh(provider: prov)
    }

    func refreshAll() { for k in tabs.keys { refresh(provider: k) } }

    func refresh(provider: String) {
        tabs[provider]?.statusLabel?.stringValue = "Loading…"
        tabs[provider]?.statusLabel?.textColor = .secondaryLabelColor

        let onResult: (Result<[ModelEntry], Error>) -> Void = { [weak self] result in
            DispatchQueue.main.async {
                guard let self = self, var tab = self.tabs[provider] else { return }
                switch result {
                case .success(let entries):
                    tab.entries = entries
                    self.tabs[provider] = tab
                    tab.tableView?.reloadData()
                    tab.statusLabel?.stringValue = "\(entries.count) models"
                    tab.statusLabel?.textColor = .secondaryLabelColor
                case .failure(let err):
                    tab.entries = []
                    self.tabs[provider] = tab
                    tab.tableView?.reloadData()
                    tab.statusLabel?.stringValue = "Error: \(err.localizedDescription)"
                    tab.statusLabel?.textColor = .systemRed
                }
            }
        }
        switch provider {
        case "azure":     ModelsManager.shared.listAzureDeployments(completion: onResult)
        case "openai":    ModelsManager.shared.listOpenAI(completion: onResult)
        case "anthropic": ModelsManager.shared.listAnthropic(completion: onResult)
        case "google":    ModelsManager.shared.listGoogle(completion: onResult)
        default: break
        }
    }

    // MARK: - Use As

    private func selectedEntry(in provider: String) -> ModelEntry? {
        guard let tab = tabs[provider],
              let table = tab.tableView,
              table.selectedRow >= 0,
              table.selectedRow < tab.entries.count else { return nil }
        return tab.entries[table.selectedRow]
    }

    @objc func useAsBig(_ sender: NSButton)      { useAs(key: "BIG_MODEL",      sender: sender) }
    @objc func useAsSmall(_ sender: NSButton)    { useAs(key: "SMALL_MODEL",    sender: sender) }
    @objc func useAsFrontier(_ sender: NSButton) { useAs(key: "FRONTIER_MODEL", sender: sender) }

    private func useAs(key: String, sender: NSButton) {
        guard let prov = sender.identifier?.rawValue,
              let entry = selectedEntry(in: prov) else {
            shake(); return
        }
        let val = entry.id  // deployment name for Azure, model id elsewhere
        EnvFileManager.shared.patch([key: val, "PREFERRED_PROVIDER": prov])
        tabs[prov]?.statusLabel?.stringValue = "Set \(key) = \(val) (\(prov)). Restarting proxy…"
        tabs[prov]?.statusLabel?.textColor = .systemGreen
        NotificationCenter.default.post(name: .init("CcpShouldRestart"), object: nil)
    }

    private func shake() {
        guard let w = window else { return }
        let anim = CAKeyframeAnimation(keyPath: "frameOrigin")
        let origin = w.frame.origin
        anim.values = [0, -8, 8, -4, 4, 0].map {
            NSValue(point: NSPoint(x: origin.x + CGFloat($0), y: origin.y))
        }
        anim.duration = 0.3
        w.animations = ["frameOrigin": anim]
        w.animator().setFrameOrigin(origin)
    }

    // MARK: - Deploy

    @objc func openDeployDialog(_ sender: NSButton) {
        let dlg = DeployDialog()
        dlg.onComplete = { [weak self] success, message in
            DispatchQueue.main.async {
                if success {
                    self?.tabs["azure"]?.statusLabel?.stringValue = "Deploy OK: \(message)"
                    self?.tabs["azure"]?.statusLabel?.textColor = .systemGreen
                    self?.refresh(provider: "azure")
                } else {
                    let alert = NSAlert()
                    alert.messageText = "Deployment failed"
                    alert.informativeText = message
                    alert.alertStyle = .warning
                    alert.runModal()
                }
            }
        }
        window?.beginSheet(dlg.window!, completionHandler: nil)
    }
}

// MARK: - Table DataSource/Delegate

extension ModelsWindowController: NSTableViewDataSource, NSTableViewDelegate {
    func numberOfRows(in tableView: NSTableView) -> Int {
        guard let id = tableView.identifier?.rawValue else { return 0 }
        let prov = String(id.dropFirst("table-".count))
        return tabs[prov]?.entries.count ?? 0
    }

    func tableView(_ tableView: NSTableView, viewFor tableColumn: NSTableColumn?, row: Int) -> NSView? {
        guard let id = tableView.identifier?.rawValue else { return nil }
        let prov = String(id.dropFirst("table-".count))
        guard let entry = tabs[prov]?.entries[safe: row],
              let col = tableColumn?.identifier.rawValue else { return nil }

        let cell = NSTableCellView()
        let text = NSTextField(labelWithString: "")
        text.font = NSFont.monospacedSystemFont(ofSize: 11, weight: .regular)
        text.translatesAutoresizingMaskIntoConstraints = false
        text.lineBreakMode = .byTruncatingTail
        cell.addSubview(text)
        cell.textField = text
        NSLayoutConstraint.activate([
            text.leadingAnchor.constraint(equalTo: cell.leadingAnchor, constant: 2),
            text.trailingAnchor.constraint(equalTo: cell.trailingAnchor, constant: -2),
            text.centerYAnchor.constraint(equalTo: cell.centerYAnchor),
        ])

        switch col {
        case "id":       text.stringValue = entry.id
        case "model":    text.stringValue = entry.modelName
        case "version":  text.stringValue = entry.version
        case "sku":      text.stringValue = entry.sku
        case "capacity": text.stringValue = entry.capacity.map { String($0) } ?? ""
        case "status":
            text.stringValue = entry.status
            if entry.status.lowercased().contains("succeed") ||
               entry.status.lowercased().contains("avail") {
                text.textColor = .systemGreen
            } else if entry.status.lowercased().contains("fail") ||
                      entry.status.lowercased().contains("error") {
                text.textColor = .systemRed
            } else {
                text.textColor = .secondaryLabelColor
            }
        default: break
        }
        return cell
    }
}

// ---------------------------------------------------------------------------
// MARK: - DeployDialog (sheet)
// ---------------------------------------------------------------------------

final class DeployDialog: NSWindowController {

    var onComplete: ((Bool, String) -> Void)?

    var catalogPopup: NSPopUpButton!
    var deployNameField: NSTextField!
    var skuPopup: NSPopUpButton!
    var capacityField: NSTextField!
    var deployBtn: NSButton!
    var cancelBtn: NSButton!
    var statusLabel: NSTextField!
    var spinner: NSProgressIndicator!

    var catalog: [ModelEntry] = []

    convenience init() {
        let w = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 500, height: 320),
            styleMask: [.titled],
            backing: .buffered, defer: false
        )
        w.title = "Deploy Azure OpenAI Model"
        self.init(window: w)
        buildUI()
        loadCatalog()
    }

    private func buildUI() {
        guard let content = window?.contentView else { return }
        let stack = NSStackView()
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 8
        stack.edgeInsets = NSEdgeInsets(top: 16, left: 16, bottom: 16, right: 16)
        stack.translatesAutoresizingMaskIntoConstraints = false
        content.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.topAnchor.constraint(equalTo: content.topAnchor),
            stack.leadingAnchor.constraint(equalTo: content.leadingAnchor),
            stack.trailingAnchor.constraint(equalTo: content.trailingAnchor),
            stack.bottomAnchor.constraint(equalTo: content.bottomAnchor),
        ])

        func label(_ s: String) -> NSTextField {
            let l = NSTextField(labelWithString: s)
            l.font = NSFont.systemFont(ofSize: 11)
            l.textColor = .secondaryLabelColor
            return l
        }
        func wide(_ v: NSView, w: CGFloat = 450) -> NSView {
            v.translatesAutoresizingMaskIntoConstraints = false
            v.widthAnchor.constraint(equalToConstant: w).isActive = true
            return v
        }

        stack.addArrangedSubview(label("Model from catalog"))
        catalogPopup = NSPopUpButton(frame: .zero, pullsDown: false)
        catalogPopup.addItem(withTitle: "Loading catalog…")
        stack.addArrangedSubview(wide(catalogPopup))

        stack.addArrangedSubview(label("Deployment name (used as model ID in .env)"))
        deployNameField = NSTextField()
        deployNameField.font = NSFont.monospacedSystemFont(ofSize: 11, weight: .regular)
        stack.addArrangedSubview(wide(deployNameField))

        let row = NSStackView()
        row.orientation = .horizontal
        row.spacing = 12
        let skuCol = NSStackView()
        skuCol.orientation = .vertical
        skuCol.alignment = .leading
        skuCol.spacing = 2
        skuCol.addArrangedSubview(label("SKU"))
        skuPopup = NSPopUpButton(frame: .zero, pullsDown: false)
        skuPopup.addItems(withTitles: ["Standard", "GlobalStandard", "DataZoneStandard", "ProvisionedManaged"])
        skuCol.addArrangedSubview(wide(skuPopup, w: 200))
        row.addArrangedSubview(skuCol)

        let capCol = NSStackView()
        capCol.orientation = .vertical
        capCol.alignment = .leading
        capCol.spacing = 2
        capCol.addArrangedSubview(label("Capacity (TPM ×1000)"))
        capacityField = NSTextField(string: "10")
        capacityField.font = NSFont.monospacedSystemFont(ofSize: 11, weight: .regular)
        capCol.addArrangedSubview(wide(capacityField, w: 100))
        row.addArrangedSubview(capCol)
        stack.addArrangedSubview(row)

        stack.addArrangedSubview(NSView())

        spinner = NSProgressIndicator()
        spinner.style = .spinning
        spinner.controlSize = .small
        spinner.isDisplayedWhenStopped = false

        statusLabel = NSTextField(labelWithString: "")
        statusLabel.font = NSFont.systemFont(ofSize: 11)
        statusLabel.textColor = .secondaryLabelColor

        let statusRow = NSStackView(views: [spinner, statusLabel])
        statusRow.spacing = 6
        stack.addArrangedSubview(statusRow)

        let buttonRow = NSStackView()
        buttonRow.orientation = .horizontal
        buttonRow.spacing = 8
        cancelBtn = NSButton(title: "Cancel", target: self, action: #selector(cancel))
        cancelBtn.bezelStyle = .rounded
        cancelBtn.keyEquivalent = "\u{1b}"
        deployBtn = NSButton(title: "Deploy", target: self, action: #selector(deploy))
        deployBtn.bezelStyle = .rounded
        deployBtn.keyEquivalent = "\r"
        deployBtn.isEnabled = false
        buttonRow.addArrangedSubview(cancelBtn)
        buttonRow.addArrangedSubview(deployBtn)
        stack.addArrangedSubview(buttonRow)
    }

    private func loadCatalog() {
        statusLabel.stringValue = "Fetching Azure model catalog…"
        spinner.startAnimation(nil)
        ModelsManager.shared.listAzureCatalog { [weak self] result in
            DispatchQueue.main.async {
                guard let self = self else { return }
                self.spinner.stopAnimation(nil)
                self.catalogPopup.removeAllItems()
                switch result {
                case .failure(let err):
                    self.statusLabel.stringValue = "Error: \(err.localizedDescription)"
                    self.statusLabel.textColor = .systemRed
                    self.catalogPopup.addItem(withTitle: "(no models)")
                case .success(let entries):
                    self.catalog = entries
                    if entries.isEmpty {
                        self.catalogPopup.addItem(withTitle: "(no models available)")
                        self.statusLabel.stringValue = "Catalog empty for this account"
                    } else {
                        for e in entries {
                            self.catalogPopup.addItem(withTitle: "\(e.modelName)  v\(e.version)")
                        }
                        self.deployBtn.isEnabled = true
                        self.statusLabel.stringValue = "\(entries.count) models available"
                        // Default deployment name
                        if let first = entries.first {
                            self.deployNameField.stringValue = "\(first.modelName)-deployment"
                        }
                    }
                }
            }
        }
    }

    @objc private func cancel() {
        guard let w = window else { return }
        w.sheetParent?.endSheet(w)
    }

    @objc private func deploy() {
        let idx = catalogPopup.indexOfSelectedItem
        guard idx >= 0, idx < catalog.count else { return }
        let model = catalog[idx]
        let name = deployNameField.stringValue.trimmingCharacters(in: .whitespaces)
        guard !name.isEmpty else {
            statusLabel.stringValue = "Deployment name required"
            statusLabel.textColor = .systemRed
            return
        }
        let sku = skuPopup.titleOfSelectedItem ?? "Standard"
        let cap = Int(capacityField.stringValue) ?? 10

        deployBtn.isEnabled = false
        cancelBtn.isEnabled = false
        spinner.startAnimation(nil)
        statusLabel.stringValue = "Deploying \(model.modelName) v\(model.version) as \(name)…"
        statusLabel.textColor = .secondaryLabelColor

        ModelsManager.shared.deployAzureModel(
            deploymentName: name,
            modelName: model.modelName,
            modelVersion: model.version,
            skuName: sku,
            skuCapacity: cap
        ) { [weak self] result in
            DispatchQueue.main.async {
                guard let self = self, let w = self.window else { return }
                self.spinner.stopAnimation(nil)
                switch result {
                case .success:
                    w.sheetParent?.endSheet(w)
                    self.onComplete?(true, "deployed \(name)")
                case .failure(let err):
                    self.deployBtn.isEnabled = true
                    self.cancelBtn.isEnabled = true
                    self.statusLabel.stringValue = "Failed"
                    self.statusLabel.textColor = .systemRed
                    self.onComplete?(false, err.localizedDescription)
                }
            }
        }
    }
}

// ---------------------------------------------------------------------------
// MARK: - Safe Array
// ---------------------------------------------------------------------------

extension Array {
    subscript(safe index: Int) -> Element? {
        indices.contains(index) ? self[index] : nil
    }
}
