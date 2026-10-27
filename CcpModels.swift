// CcpModels.swift — Model discovery & deployment for CCP LiteLLM
// Provider discovery (non-UI) + SwiftUI Model Manager window.

import SwiftUI

// ---------------------------------------------------------------------------
// MARK: - ModelEntry
// ---------------------------------------------------------------------------

struct ModelEntry: Identifiable {
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

    // MARK: - Azure AI Foundry (CLI — AIServices accounts)

    private func azureAIContext() -> (rg: String, account: String, location: String)? {
        let env = EnvFileManager.shared
        guard let rg = env.get("AZURE_AI_RESOURCE_GROUP") ?? env.get("AZURE_RESOURCE_GROUP"),
              let account = env.get("AZURE_AI_ACCOUNT_NAME") ?? env.get("AZURE_ACCOUNT_NAME") else {
            return nil
        }
        let location = env.get("AZURE_AI_LOCATION") ?? env.get("AZURE_LOCATION") ?? "eastus2"
        return (rg, account, location)
    }

    func listAzureAIDeployments(completion: @escaping (Result<[ModelEntry], Error>) -> Void) {
        guard let ctx = azureAIContext() else {
            completion(.failure(NSError(domain: "ccp", code: 41,
                userInfo: [NSLocalizedDescriptionKey:
                    "Set AZURE_AI_RESOURCE_GROUP/AZURE_AI_ACCOUNT_NAME or AZURE_RESOURCE_GROUP/AZURE_ACCOUNT_NAME in .env"])))
            return
        }

        runAzCLI([
            "cognitiveservices", "account", "deployment", "list",
            "--resource-group", ctx.rg,
            "--name", ctx.account,
            "-o", "json"
        ]) { result in
            switch result {
            case .failure(let e): completion(.failure(e))
            case .success(let data):
                do {
                    let arr = try JSONSerialization.jsonObject(with: data) as? [[String: Any]] ?? []
                    var entries: [ModelEntry] = []
                    for dep in arr {
                        let props = dep["properties"] as? [String: Any] ?? [:]
                        let model = props["model"] as? [String: Any] ?? [:]
                        let sku = dep["sku"] as? [String: Any] ?? [:]
                        let name = dep["name"] as? String ?? ""
                        let modelName = model["name"] as? String ?? name
                        let version = model["version"] as? String ?? ""
                        let status = props["provisioningState"] as? String ?? props["deploymentState"] as? String ?? "unknown"
                        let capacity = sku["capacity"] as? Int ?? props["currentCapacity"] as? Int
                        let skuName = sku["name"] as? String ?? ""
                        entries.append(ModelEntry(
                            id: name, modelName: modelName, version: version,
                            kind: .deployment, provider: "azure_ai",
                            capacity: capacity, status: status, sku: skuName
                        ))
                    }
                    completion(.success(entries.sorted { $0.id < $1.id }))
                } catch {
                    completion(.failure(error))
                }
            }
        }
    }

    func listAzureAICatalog(completion: @escaping (Result<[ModelEntry], Error>) -> Void) {
        guard let ctx = azureAIContext() else {
            completion(.failure(NSError(domain: "ccp", code: 42,
                userInfo: [NSLocalizedDescriptionKey:
                    "Set AZURE_AI_LOCATION plus AZURE_AI_RESOURCE_GROUP/AZURE_AI_ACCOUNT_NAME in .env"])))
            return
        }

        runAzCLI([
            "cognitiveservices", "model", "list",
            "--location", ctx.location,
            "-o", "json"
        ]) { result in
            switch result {
            case .failure(let e): completion(.failure(e))
            case .success(let data):
                do {
                    let arr = try JSONSerialization.jsonObject(with: data) as? [[String: Any]] ?? []
                    var entries: [ModelEntry] = []
                    for item in arr {
                        let model = item["model"] as? [String: Any] ?? item
                        let name = model["name"] as? String ?? ""
                        let version = model["version"] as? String ?? ""
                        let format = model["format"] as? String ?? ""
                        let skuList = model["skus"] as? [[String: Any]] ?? []
                        let skuName = skuList.compactMap { $0["name"] as? String }.joined(separator: ",")
                        let lowered = name.lowercased()
                        guard format.lowercased().contains("openai") ||
                              format.lowercased().contains("anthropic") ||
                              lowered.contains("gpt") || lowered.contains("o3") ||
                              lowered.contains("o4") || lowered.contains("claude") ||
                              lowered.contains("deepseek") || lowered.contains("llama") ||
                              lowered.contains("mistral") || lowered.contains("cohere") ||
                              lowered.contains("grok") || lowered.contains("kimi")
                        else { continue }
                        entries.append(ModelEntry(
                            id: "\(name):\(version)", modelName: name,
                            version: version, kind: .catalog, provider: "azure_ai",
                            capacity: nil, status: format.isEmpty ? "Available" : format, sku: skuName
                        ))
                    }
                    completion(.success(entries.sorted { $0.modelName < $1.modelName }))
                } catch {
                    completion(.failure(error))
                }
            }
        }
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

    // MARK: - Azure deployment operations and usage

    func deleteAzureDeployment(_ deploymentName: String, completion: @escaping (Result<Void, Error>) -> Void) {
        let env = EnvFileManager.shared
        guard let rg = env.get("AZURE_RESOURCE_GROUP"), let account = env.get("AZURE_ACCOUNT_NAME") else {
            completion(.failure(NSError(domain: "ccp", code: 4, userInfo: [NSLocalizedDescriptionKey: "Set AZURE_RESOURCE_GROUP and AZURE_ACCOUNT_NAME in .env"])))
            return
        }
        runAzCLI(["cognitiveservices", "account", "deployment", "delete", "--resource-group", rg,
                  "--name", account, "--deployment-name", deploymentName, "--yes"]) { result in
            switch result {
            case .failure(let error): completion(.failure(error))
            case .success: completion(.success(()))
            }
        }
    }

    func azureDeploymentUsage(_ deploymentName: String, completion: @escaping (Result<String, Error>) -> Void) {
        let env = EnvFileManager.shared
        guard let rg = env.get("AZURE_RESOURCE_GROUP"), let account = env.get("AZURE_ACCOUNT_NAME") else {
            completion(.failure(NSError(domain: "ccp", code: 4, userInfo: [NSLocalizedDescriptionKey: "Set AZURE_RESOURCE_GROUP and AZURE_ACCOUNT_NAME in .env"])))
            return
        }
        runAzCLI(["cognitiveservices", "account", "show", "--resource-group", rg, "--name", account, "--query", "id", "-o", "tsv"]) { [weak self] result in
            switch result {
            case .failure(let error): completion(.failure(error))
            case .success(let data):
                guard let resourceID = String(data: data, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines), !resourceID.isEmpty else {
                    completion(.failure(NSError(domain: "ccp", code: 98, userInfo: [NSLocalizedDescriptionKey: "Could not resolve Azure resource ID"])))
                    return
                }
                let start = ISO8601DateFormatter().string(from: Date(timeIntervalSinceNow: -3600))
                let end = ISO8601DateFormatter().string(from: Date())
                self?.runAzCLI(["monitor", "metrics", "list", "--resource", resourceID, "--metric", "ModelRequests,InputTokens,OutputTokens,TotalTokens,AzureOpenAIAvailabilityRate", "--interval", "PT1H", "--start-time", start, "--end-time", end, "--aggregation", "Total", "--filter", "ModelDeploymentName eq '\(deploymentName)'", "-o", "json"]) { metricResult in
                    switch metricResult {
                    case .failure(let error): completion(.failure(error))
                    case .success(let metricData):
                        do {
                            let root = try JSONSerialization.jsonObject(with: metricData) as? [String: Any]
                            let metrics = root?["value"] as? [[String: Any]] ?? []
                            var parts: [String] = []
                            for metric in metrics {
                                let name = ((metric["name"] as? [String: Any])?["localizedValue"] as? String) ?? ((metric["name"] as? [String: Any])?["value"] as? String) ?? "Metric"
                                let values = metric["timeseries"] as? [[String: Any]] ?? []
                                let total = values.flatMap { ($0["data"] as? [[String: Any]]) ?? [] }.compactMap { $0["total"] as? NSNumber }.reduce(0.0) { $0 + $1.doubleValue }
                                parts.append("\(name): \(Int(total))")
                            }
                            completion(.success(parts.isEmpty ? "No usage in the last hour" : parts.joined(separator: "  •  ")))
                        } catch { completion(.failure(error)) }
                    }
                }
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

    func runAzCLI(_ args: [String], completion: @escaping (Result<Data, Error>) -> Void) {
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

// MARK: - Async wrappers

extension ModelsManager {
    func list(provider: String) async throws -> [ModelEntry] {
        try await withCheckedThrowingContinuation { cont in
            let done: (Result<[ModelEntry], Error>) -> Void = { cont.resume(with: $0) }
            switch provider {
            case "azure":     listAzureDeployments(completion: done)
            case "azure_ai":  listAzureAIDeployments(completion: done)
            case "openai":    listOpenAI(completion: done)
            case "anthropic": listAnthropic(completion: done)
            case "google":    listGoogle(completion: done)
            default:          cont.resume(returning: [])
            }
        }
    }

    func catalog() async throws -> [ModelEntry] {
        try await withCheckedThrowingContinuation { cont in
            listAzureCatalog { cont.resume(with: $0) }
        }
    }

    func deploy(name: String, model: String, version: String, sku: String, capacity: Int) async throws -> String {
        try await withCheckedThrowingContinuation { cont in
            deployAzureModel(deploymentName: name, modelName: model, modelVersion: version,
                             skuName: sku, skuCapacity: capacity) { cont.resume(with: $0) }
        }
    }

    func delete(deployment: String) async throws {
        try await withCheckedThrowingContinuation { cont in
            deleteAzureDeployment(deployment) { cont.resume(with: $0) }
        }
    }

    func usage(deployment: String) async throws -> String {
        try await withCheckedThrowingContinuation { cont in
            azureDeploymentUsage(deployment) { cont.resume(with: $0) }
        }
    }
}

// ---------------------------------------------------------------------------
// MARK: - Model Manager (SwiftUI)
// ---------------------------------------------------------------------------

struct ProviderTab: Identifiable, Hashable {
    let id: String
    let title: String
    static let all = [
        ProviderTab(id: "azure", title: "Azure OpenAI"),
        ProviderTab(id: "azure_ai", title: "Azure AI Foundry"),
        ProviderTab(id: "openai", title: "OpenAI"),
        ProviderTab(id: "anthropic", title: "Anthropic"),
        ProviderTab(id: "google", title: "Google"),
    ]
}

struct ModelManagerView: View {
    @Environment(AppModel.self) private var app

    @State private var provider = "azure"
    @State private var entries: [String: [ModelEntry]] = [:]
    @State private var errors: [String: String] = [:]
    @State private var loading: Set<String> = []
    @State private var selection: ModelEntry.ID?
    @State private var showDeploy = false
    @State private var confirmDelete = false
    @State private var info: String?
    @State private var status = ""

    private var rows: [ModelEntry] { entries[provider] ?? [] }
    private var selected: ModelEntry? { rows.first { $0.id == selection } }

    var body: some View {
        VStack(spacing: 0) {
            Picker("Provider", selection: $provider) {
                ForEach(ProviderTab.all) { Text($0.title).tag($0.id) }
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .padding(10)

            toolbar.padding(.horizontal, 10).padding(.bottom, 8)

            Table(rows, selection: $selection) {
                TableColumn(provider == "azure" || provider == "azure_ai" ? "Deployment" : "Model ID") { e in
                    Text(e.id).font(.system(.body, design: .monospaced))
                }
                TableColumn("Model") { Text($0.modelName) }
                TableColumn("Version") { Text($0.version) }.width(80)
                TableColumn("SKU") { Text($0.sku) }.width(110)
                TableColumn("Cap/TPM") { Text($0.capacity.map(String.init) ?? "") }.width(70)
                TableColumn("Status") { e in
                    Text(e.status).foregroundStyle(statusColor(e.status))
                }.width(90)
            }
            .overlay {
                if loading.contains(provider) { ProgressView() }
                else if let err = errors[provider] {
                    ContentUnavailableView("Could not load models", systemImage: "exclamationmark.triangle",
                                           description: Text(err))
                }
            }
        }
        .frame(minWidth: 760, minHeight: 440)
        .task(id: provider) { if entries[provider] == nil { await refresh(provider) } }
        .sheet(isPresented: $showDeploy) {
            DeployModelView { await refresh("azure") }
        }
        .confirmationDialog("Delete Azure deployment \(selected?.id ?? "")?",
                            isPresented: $confirmDelete, titleVisibility: .visible) {
            Button("Delete Deployment", role: .destructive) { Task { await deleteSelected() } }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("This permanently removes the deployment. The underlying model remains available for a future deployment.")
        }
        .alert("Azure usage", isPresented: Binding(get: { info != nil }, set: { if !$0 { info = nil } })) {
            Button("OK") { info = nil }
        } message: { Text(info ?? "") }
    }

    private var toolbar: some View {
        HStack {
            Button("Refresh") { Task { await refresh(provider) } }
            Button("Use as BIG") { useAs("BIG_MODEL") }.disabled(selected == nil)
            Button("Use as SMALL") { useAs("SMALL_MODEL") }.disabled(selected == nil)
            Button("Use as FRONTIER") { useAs("FRONTIER_MODEL") }.disabled(selected == nil)
            if provider == "azure" {
                Divider().frame(height: 16)
                Button("Deploy Model…") { showDeploy = true }
                Button("Usage") { Task { await showUsage() } }.disabled(selected == nil)
                Button("Delete Deployment") { confirmDelete = true }.disabled(selected == nil)
            }
            Spacer()
            Text(status).font(.caption).foregroundStyle(.secondary).lineLimit(1)
        }
    }

    private func statusColor(_ s: String) -> Color {
        let l = s.lowercased()
        if l.contains("succeed") || l.contains("avail") { return .green }
        if l.contains("fail") || l.contains("error") { return .red }
        return .secondary
    }

    private func refresh(_ p: String) async {
        loading.insert(p); errors[p] = nil
        defer { loading.remove(p) }
        do {
            let list = try await ModelsManager.shared.list(provider: p)
            entries[p] = list
            status = "\(list.count) models"
        } catch {
            entries[p] = []
            errors[p] = error.localizedDescription
            status = ""
        }
    }

    private func useAs(_ key: String) {
        guard let e = selected else { return }
        EnvFileManager.shared.patch([key: e.id, "PREFERRED_PROVIDER": provider])
        status = "Set \(key) = \(e.id) (\(provider)). Restarting proxy…"
        Task { await app.restartProxy() }
    }

    private func showUsage() async {
        guard let e = selected else { return }
        do { info = "\(e.id) — last hour\n\n" + (try await ModelsManager.shared.usage(deployment: e.id)) }
        catch { info = error.localizedDescription }
    }

    private func deleteSelected() async {
        guard let e = selected else { return }
        do {
            try await ModelsManager.shared.delete(deployment: e.id)
            status = "Deleted \(e.id)"
            await refresh("azure")
        } catch { info = "Delete failed: \(error.localizedDescription)" }
    }
}

struct DeployModelView: View {
    @Environment(\.dismiss) private var dismiss
    let onDone: () async -> Void

    @State private var catalog: [ModelEntry] = []
    @State private var choice: ModelEntry.ID?
    @State private var name = ""
    @State private var sku = "Standard"
    @State private var capacity = "10"
    @State private var status = "Fetching Azure model catalog…"
    @State private var busy = true
    @State private var failed = false

    private static let skus = ["Standard", "GlobalStandard", "DataZoneStandard", "ProvisionedManaged"]

    var body: some View {
        Form {
            Picker("Model from catalog", selection: $choice) {
                ForEach(catalog) { Text("\($0.modelName)  v\($0.version)").tag(Optional($0.id)) }
            }
            .onChange(of: choice) { _, new in
                if let e = catalog.first(where: { $0.id == new }) { name = "\(e.modelName)-deployment" }
            }
            TextField("Deployment name", text: $name)
            Picker("SKU", selection: $sku) { ForEach(Self.skus, id: \.self) { Text($0) } }
            TextField("Capacity (TPM ×1000)", text: $capacity)
            HStack {
                if busy { ProgressView().controlSize(.small) }
                Text(status).font(.caption).foregroundStyle(failed ? .red : .secondary)
            }
            HStack {
                Spacer()
                Button("Cancel", role: .cancel) { dismiss() }.disabled(busy && !catalog.isEmpty)
                Button("Deploy") { Task { await deploy() } }
                    .keyboardShortcut(.defaultAction)
                    .disabled(busy || choice == nil || name.trimmingCharacters(in: .whitespaces).isEmpty)
            }
        }
        .formStyle(.grouped)
        .frame(width: 500)
        .task { await load() }
    }

    private func load() async {
        do {
            catalog = try await ModelsManager.shared.catalog()
            choice = catalog.first?.id
            status = catalog.isEmpty ? "Catalog empty for this account" : "\(catalog.count) models available"
        } catch {
            status = "Error: \(error.localizedDescription)"; failed = true
        }
        busy = false
    }

    private func deploy() async {
        guard let e = catalog.first(where: { $0.id == choice }) else { return }
        let dep = name.trimmingCharacters(in: .whitespaces)
        busy = true; failed = false
        status = "Deploying \(e.modelName) v\(e.version) as \(dep)…"
        do {
            _ = try await ModelsManager.shared.deploy(name: dep, model: e.modelName, version: e.version,
                                                      sku: sku, capacity: Int(capacity) ?? 10)
            await onDone()
            dismiss()
        } catch {
            status = "Failed: \(error.localizedDescription)"; failed = true; busy = false
        }
    }
}
