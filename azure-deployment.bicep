// Azure ML Deployment template for open-source models
// Deploys Qwen, Phi, Deepseek models to Azure Container Instance endpoints

param location string = resourceGroup().location
param environmentName string = 'prod'
param workspaceName string = 'ccp-ml-workspace'

// Model configuration
param smallModelName string = 'phi-4-mini'  // 7-8B
param bigModelName string = 'qwen2.5-72b'   // 72B
param frontierModelName string = 'deepseek-v3'  // 671B

// Compute SKUs
param smallComputeSku string = 'Standard_A100_2s_v4'  // 1x A100 (24GB)
param bigComputeSku string = 'Standard_A100_4s_v4'    // 2x A100 (80GB)
param frontierComputeSku string = 'Standard_H100_80s_v2'  // 8x H100

// Tags
param tags object = {
  environment: environmentName
  project: 'claude-code-proxy'
  modelType: 'open-source-llm'
  deploymentDate: utcNow('u')
}

// Create ML Workspace if needed
resource mlWorkspace 'Microsoft.MachineLearningServices/workspaces@2024-10-01' = {
  name: workspaceName
  location: location
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    friendlyName: 'Claude Code Proxy ML Deployment'
    keyVault: subscriptionResourceId('Microsoft.KeyVault/vaults', '${workspaceName}-kv')
    storageAccount: subscriptionResourceId('Microsoft.Storage/storageAccounts', '${workspaceName}sa')
    containerRegistry: subscriptionResourceId('Microsoft.ContainerRegistry/registries', '${replace(workspaceName, '-', '')}cr')
  }
  tags: tags
}

// ============================================
// TIER 1: SMALL_MODEL (7-8B) Deployment
// ============================================

// Create compute instance for small model
resource smallModelCompute 'Microsoft.MachineLearningServices/workspaces/computes@2024-10-01' = {
  parent: mlWorkspace
  name: '${smallModelName}-compute'
  location: location
  properties: {
    computeType: 'AmlCompute'
    properties: {
      vmSize: smallComputeSku
      minNodeCount: 1
      maxNodeCount: 3
      idleTimeBeforeScaleDown: 3600
      remoteLoginPortPublicAccess: 'NotSpecified'
    }
  }
  tags: union(tags, {
    tier: 'small'
    modelSize: '7-8B'
  })
}

// Create online deployment for small model (vLLM backend)
resource smallModelDeployment 'Microsoft.MachineLearningServices/workspaces/onlineEndpoints/deployments@2024-10-01' = {
  name: '${mlWorkspace.name}/${smallModelName}-endpoint/${smallModelName}-deployment'
  location: location
  properties: {
    endpointComputeType: 'Managed'
    modelMountPath: '/var/azureml-app/model'
    codeConfiguration: {
      codePath: './code'
      scoringScript: 'score.py'
    }
    environmentVariables: {
      MODEL_NAME: smallModelName
      VLLM_WORKER_MULTIPROC_METHOD: 'spawn'
      MAX_BATCH_SIZE: '64'
      TENSOR_PARALLEL_SIZE: '1'
    }
    properties: {
      mleClientVersion: 'unknown'
    }
  }
  tags: union(tags, {
    tier: 'small'
  })
  dependsOn: [smallModelCompute]
}

// ============================================
// TIER 2: BIG_MODEL (70-72B) Deployment
// ============================================

resource bigModelCompute 'Microsoft.MachineLearningServices/workspaces/computes@2024-10-01' = {
  parent: mlWorkspace
  name: '${bigModelName}-compute'
  location: location
  properties: {
    computeType: 'AmlCompute'
    properties: {
      vmSize: bigComputeSku
      minNodeCount: 1
      maxNodeCount: 2
      idleTimeBeforeScaleDown: 3600
    }
  }
  tags: union(tags, {
    tier: 'big'
    modelSize: '70-72B'
    gpuCount: 2
  })
}

resource bigModelDeployment 'Microsoft.MachineLearningServices/workspaces/onlineEndpoints/deployments@2024-10-01' = {
  name: '${mlWorkspace.name}/${bigModelName}-endpoint/${bigModelName}-deployment'
  location: location
  properties: {
    endpointComputeType: 'Managed'
    modelMountPath: '/var/azureml-app/model'
    codeConfiguration: {
      codePath: './code'
      scoringScript: 'score.py'
    }
    environmentVariables: {
      MODEL_NAME: bigModelName
      VLLM_WORKER_MULTIPROC_METHOD: 'spawn'
      MAX_BATCH_SIZE: '32'
      TENSOR_PARALLEL_SIZE: '2'
    }
  }
  tags: union(tags, {
    tier: 'big'
  })
  dependsOn: [bigModelCompute]
}

// ============================================
// TIER 3: FRONTIER_MODEL (671B) Deployment
// ============================================
// Note: This requires significant infrastructure (8x H100)
// Consider deploying separately or on-demand

resource frontierModelCompute 'Microsoft.MachineLearningServices/workspaces/computes@2024-10-01' = {
  parent: mlWorkspace
  name: '${frontierModelName}-compute'
  location: location
  properties: {
    computeType: 'AmlCompute'
    properties: {
      vmSize: frontierComputeSku
      minNodeCount: 1
      maxNodeCount: 1  // Expensive, don't auto-scale
      idleTimeBeforeScaleDown: 7200  // 2 hours before auto-shutdown
    }
  }
  tags: union(tags, {
    tier: 'frontier'
    modelSize: '671B'
    gpuCount: 8
    costPerHour: 'HIGH'
  })
}

resource frontierModelDeployment 'Microsoft.MachineLearningServices/workspaces/onlineEndpoints/deployments@2024-10-01' = {
  name: '${mlWorkspace.name}/${frontierModelName}-endpoint/${frontierModelName}-deployment'
  location: location
  properties: {
    endpointComputeType: 'Managed'
    modelMountPath: '/var/azureml-app/model'
    codeConfiguration: {
      codePath: './code'
      scoringScript: 'score.py'
    }
    environmentVariables: {
      MODEL_NAME: frontierModelName
      VLLM_WORKER_MULTIPROC_METHOD: 'spawn'
      MAX_BATCH_SIZE: '16'
      TENSOR_PARALLEL_SIZE: '8'
      PIPELINE_PARALLEL_SIZE: '2'
    }
  }
  tags: union(tags, {
    tier: 'frontier'
  })
  dependsOn: [frontierModelCompute]
}

// ============================================
// Outputs
// ============================================

output workspaceId string = mlWorkspace.id
output workspaceName string = mlWorkspace.name

output smallModelEndpoint string = smallModelDeployment.properties.scoringUri ?? 'Pending'
output bigModelEndpoint string = bigModelDeployment.properties.scoringUri ?? 'Pending'
output frontierModelEndpoint string = frontierModelDeployment.properties.scoringUri ?? 'Pending'

output deploymentConfig object = {
  small: {
    name: smallModelName
    modelSize: '7-8B'
    computeType: smallComputeSku
    endpoint: smallModelDeployment.properties.scoringUri ?? 'Pending'
  }
  big: {
    name: bigModelName
    modelSize: '70-72B'
    computeType: bigComputeSku
    endpoint: bigModelDeployment.properties.scoringUri ?? 'Pending'
  }
  frontier: {
    name: frontierModelName
    modelSize: '671B'
    computeType: frontierComputeSku
    endpoint: frontierModelDeployment.properties.scoringUri ?? 'Pending'
  }
}
