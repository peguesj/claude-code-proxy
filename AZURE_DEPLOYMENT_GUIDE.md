# Azure ML Deployment Guide for Open-Source LLMs
**Target**: Deploy Qwen, Phi, and Deepseek models to Azure ML

---

## Prerequisites

### Azure Setup
```bash
# Install Azure CLI
brew install azure-cli

# Login to Azure
az login

# Set default subscription
az account set --subscription "Your Subscription ID"

# Create resource group
az group create \
  --name ccp-ml-rg \
  --location eastus

# Register required resource providers
az provider register --namespace Microsoft.MachineLearningServices
az provider register --namespace Microsoft.Compute
az provider register --namespace Microsoft.Storage
```

### Local Setup
```bash
# Install required tools
pip install azure-cli-ml mlflow azureml-sdk

# Verify vLLM compatibility
pip install vllm vllm[all]  # For all backends
```

### Model Preparation
```bash
# Download models locally or prepare for direct Azure ML import
# Models are typically fetched from HuggingFace during container startup

# For faster initialization, pre-download to Azure Storage:
az storage account create \
  --name ccpmlmodels \
  --resource-group ccp-ml-rg \
  --location eastus
```

---

## Step 1: Create Azure ML Workspace

```bash
# Define variables
WORKSPACE_NAME="ccp-ml-workspace"
RESOURCE_GROUP="ccp-ml-rg"
LOCATION="eastus"

# Create workspace (with required storage/keyvault)
az ml workspace create \
  --name $WORKSPACE_NAME \
  --resource-group $RESOURCE_GROUP \
  --location $LOCATION

# Show workspace details
az ml workspace show \
  --name $WORKSPACE_NAME \
  --resource-group $RESOURCE_GROUP
```

---

## Step 2: Create Compute Clusters

### 2.1 Small Model Compute (Phi-4, 7-8B)

```bash
# Create compute instance for small models
az ml compute create \
  --type amlcompute \
  --name phi-4-compute \
  --resource-group $RESOURCE_GROUP \
  --workspace-name $WORKSPACE_NAME \
  --vm-size Standard_A100_2s_v4 \
  --min-instances 1 \
  --max-instances 3 \
  --idle-time-before-scale-down 3600
```

### 2.2 Big Model Compute (Qwen2.5-72B)

```bash
# Create compute cluster for mid-size models
az ml compute create \
  --type amlcompute \
  --name qwen-72b-compute \
  --resource-group $RESOURCE_GROUP \
  --workspace-name $WORKSPACE_NAME \
  --vm-size Standard_A100_4s_v4 \
  --min-instances 1 \
  --max-instances 2 \
  --idle-time-before-scale-down 3600
```

### 2.3 Frontier Model Compute (Deepseek-V3, 671B)

```bash
# Create compute cluster for frontier models (expensive!)
# WARNING: This is for demonstration; ensure quota availability
az ml compute create \
  --type amlcompute \
  --name deepseek-v3-compute \
  --resource-group $RESOURCE_GROUP \
  --workspace-name $WORKSPACE_NAME \
  --vm-size Standard_H100_80s_v2 \
  --min-instances 1 \
  --max-instances 1 \
  --idle-time-before-scale-down 7200  # Don't auto-scale, it's expensive!
```

---

## Step 3: Create Online Endpoints

### 3.1 Register Models

```bash
# Register Qwen2.5-72B model
az ml model create \
  --name qwen2.5-72b \
  --path https://huggingface.co/Qwen/Qwen2.5-72B-Instruct \
  --resource-group $RESOURCE_GROUP \
  --workspace-name $WORKSPACE_NAME

# Register Phi-4
az ml model create \
  --name phi-4 \
  --path https://huggingface.co/microsoft/phi-4 \
  --resource-group $RESOURCE_GROUP \
  --workspace-name $WORKSPACE_NAME

# Register Deepseek-V3
az ml model create \
  --name deepseek-v3 \
  --path https://huggingface.co/deepseek-ai/DeepSeek-V3 \
  --resource-group $RESOURCE_GROUP \
  --workspace-name $WORKSPACE_NAME
```

### 3.2 Create Endpoints

Create `endpoints.yml`:

```yaml
$schema: https://azuremlschemas.azureedge.net/latest/managedOnlineEndpoint.schema.json
name: ccp-small-endpoints
auth_mode: key
properties:
  description: Claude Code Proxy - Small Models (Phi-4, 7-8B)
---
$schema: https://azuremlschemas.azureedge.net/latest/managedOnlineDeployment.schema.json
endpoint_name: ccp-small-endpoints
name: phi-4-deployment
model: azureml:phi-4:1
environment:
  image: mcr.microsoft.com/azureml/inference-cpu-cuda:latest
code_configuration:
  code: ./code
  scoring_script: score.py
instance_type: Standard_A100_2s_v4
instance_count: 1
liveness_probe:
  failure_threshold: 30
  initial_delay: 10
  period: 10
readiness_probe:
  success_threshold: 1
  failure_threshold: 10
  period: 10
request_settings:
  request_timeout_ms: 60000
  max_concurrent_requests_per_instance: 2
environment_variables:
  MODEL_NAME: phi-4
  TENSOR_PARALLEL_SIZE: "1"
  MAX_MODEL_LEN: "32768"
```

Deploy:

```bash
# Deploy small model endpoint
az ml online-endpoint create \
  --resource-group $RESOURCE_GROUP \
  --workspace-name $WORKSPACE_NAME \
  --file endpoints.yml

# Create big model endpoint
# (Create similar YAML for qwen-72b)

az ml online-endpoint create \
  --resource-group $RESOURCE_GROUP \
  --workspace-name $WORKSPACE_NAME \
  --file endpoints-big.yml

# Create frontier model endpoint (on-demand)
az ml online-endpoint create \
  --resource-group $RESOURCE_GROUP \
  --workspace-name $WORKSPACE_NAME \
  --file endpoints-frontier.yml
```

---

## Step 4: Test Endpoints

```bash
# Get endpoint details
az ml online-endpoint show \
  --name ccp-small-endpoints \
  --resource-group $RESOURCE_GROUP \
  --workspace-name $WORKSPACE_NAME

# Invoke endpoint
az ml online-endpoint invoke \
  --name ccp-small-endpoints \
  --deployment-name phi-4-deployment \
  --resource-group $RESOURCE_GROUP \
  --workspace-name $WORKSPACE_NAME \
  --request-file test-payload.json
```

### Test Payload (`test-payload.json`):

```json
{
  "messages": [
    {
      "role": "user",
      "content": "What is Claude Code Proxy?"
    }
  ],
  "temperature": 0.7,
  "max_tokens": 256
}
```

---

## Step 5: Update Claude Code Proxy

### 5.1 Update `.env`

```bash
# Switch to Azure provider with open-source models
PREFERRED_PROVIDER="azure"

# Model mapping
BIG_MODEL="qwen2.5-72b"
SMALL_MODEL="phi-4"
FRONTIER_MODEL="deepseek-v3"

# Azure configuration
AZURE_API_BASE="https://ccp-ml-workspace.eastus.inference.ml.azure.com"
AZURE_API_VERSION="2024-10-01-preview"

# Azure Deployment Map (endpoint names)
AZURE_DEPLOYMENT_MAP='{
  "phi-4": "ccp-small-endpoints",
  "qwen2.5-7b": "ccp-small-endpoints",
  "qwen2.5-72b": "ccp-big-endpoints",
  "deepseek-v3": "ccp-frontier-endpoints"
}'
```

### 5.2 Model Mapping in `server.py`

The proxy already supports the three-tier model mapping:

```python
# In server.py, the validation logic automatically handles:
# - haiku → SMALL_MODEL (e.g., phi-4)
# - sonnet → BIG_MODEL (e.g., qwen2.5-72b)
# - opus → FRONTIER_MODEL (e.g., deepseek-v3)

# When Claude Code requests:
# - claude-3-haiku-latest → mapped to phi-4
# - claude-3-sonnet-latest → mapped to qwen2.5-72b
# - claude-3-opus-latest → mapped to deepseek-v3
```

---

## Step 6: Cost Management

### Monitor Costs

```bash
# Create cost alert
az monitor metrics alert create \
  --name ccp-ml-cost-alert \
  --resource-group $RESOURCE_GROUP \
  --scopes /subscriptions/{subscription}/resourceGroups/$RESOURCE_GROUP \
  --condition "avg Percentage >= 80" \
  --description "Alert when usage hits 80%"
```

### Auto-Shutdown Strategy

```bash
# Set idle timeout for compute clusters to save costs
az ml compute update \
  --type amlcompute \
  --name qwen-72b-compute \
  --resource-group $RESOURCE_GROUP \
  --workspace-name $WORKSPACE_NAME \
  --idle-time-before-scale-down 1800  # 30 minutes
```

---

## Step 7: Monitoring & Logging

### Enable Application Insights

```bash
# Check if workspace has AI enabled
az ml workspace show \
  --name $WORKSPACE_NAME \
  --resource-group $RESOURCE_GROUP

# View logs
az ml online-endpoint get-logs \
  --name ccp-small-endpoints \
  --deployment-name phi-4-deployment \
  --lines 100 \
  --resource-group $RESOURCE_GROUP \
  --workspace-name $WORKSPACE_NAME
```

---

## Troubleshooting

### Issue: GPU Not Available

```bash
# Check quota
az vm list-usage --location eastus

# Request quota increase via Azure Portal
# Azure → Subscriptions → Usage + quotas → Change quota
```

### Issue: High Costs

```bash
# Identify expensive compute:
az ml compute list \
  --resource-group $RESOURCE_GROUP \
  --workspace-name $WORKSPACE_NAME \
  --query "[].{name:name, vm_size:properties.properties.vm_size}"

# Stop idle compute
az ml compute delete \
  --name deepseek-v3-compute \
  --resource-group $RESOURCE_GROUP \
  --workspace-name $WORKSPACE_NAME
```

### Issue: Model Download Timeout

```bash
# Pre-download to Azure Storage for faster initialization
az storage blob upload-batch \
  --account-name ccpmlmodels \
  --destination models \
  --source ./qwen-2.5-72b
```

---

## Scaling Strategy

### Scale Down to Save Costs

```bash
# Reduce to single small model endpoint during dev
az ml online-deployment update \
  --name phi-4-deployment \
  --endpoint-name ccp-small-endpoints \
  --instance-count 1 \
  --resource-group $RESOURCE_GROUP \
  --workspace-name $WORKSPACE_NAME
```

### Enable Auto-Scaling

```yaml
# In endpoint YAML
scale_settings:
  type: Auto
  min_instances: 1
  max_instances: 3
  target_utilization_percentage: 70
  polling_interval_in_seconds: 30
```

---

## Final Validation Checklist

- [ ] Azure ML workspace created
- [ ] Three compute clusters provisioned (small, big, frontier)
- [ ] Models registered in model registry
- [ ] Endpoints deployed and responding
- [ ] Test payloads successful
- [ ] `.env` updated with Azure deployment details
- [ ] AZURE_DEPLOYMENT_MAP configured correctly
- [ ] Cost alerts configured
- [ ] Monitoring dashboards set up
- [ ] Documentation updated in `.claude/CLAUDE.md`

---

## Next Steps

1. **Phase 1**: Deploy Phi-4 + Qwen2.5-72B (moderate cost)
2. **Phase 2**: Benchmark against Claude models
3. **Phase 3**: Add Deepseek-V3 (only if cost acceptable)
4. **Phase 4**: Set up auto-scaling and cost optimization

---

## Resources

- [Azure ML Documentation](https://learn.microsoft.com/en-us/azure/machine-learning/)
- [vLLM Deployment](https://docs.vllm.ai/en/latest/serving/deploying_with_azure_machine_learning.html)
- [Azure CLI ML Reference](https://learn.microsoft.com/en-us/cli/azure/ml?view=azure-cli-latest)
- [Qwen Model Card](https://huggingface.co/Qwen/Qwen2.5-72B-Instruct)
- [Deepseek-V3 Guide](https://github.com/deepseek-ai/DeepSeek-V3/blob/main/README.md)

---

**Status**: Ready for deployment
**Last Updated**: 2026-03-30
