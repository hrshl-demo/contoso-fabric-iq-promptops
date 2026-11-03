// The data source and the Fabric capacity for the Contoso PromptOps platform. Resource-group scope: the resource group is created, empty, by
// `python -m tools.bootstrap apply`, because a pipeline that may create resource groups would need rights on the whole subscription.
//
// The storage account is optional (deployStorage, set from CD_DATA_SOURCE): many subscriptions have a policy that turns off its public network access,
// and then GitHub's runners cannot upload to it. Without it the seed data in the repository is loaded straight into the lakehouse.
//
// With a storage account the deployment runs in two phases (tools/platform.py): first with deployCapacity=false (only the storage account, which costs
// cents), then with deployCapacity=true. Nothing bills by the hour until the capacity phase, and a failure while the seed data is uploaded and built costs nothing.
targetScope = 'resourceGroup'

@description('Storage account name: 3-24 lowercase letters and digits, globally unique. tools/platform.py derives one from the subscription and the resource group. Unused when deployStorage is false.')
@minLength(3)
@maxLength(24)
param storageAccountName string

@description('Name of the container that holds the seed data, the data source of the lakehouse.')
param sourceContainerName string = 'source'

@description('Capacity name: 3-63 lowercase letters and digits, starting with a letter.')
@minLength(3)
@maxLength(63)
param capacityName string

@description('Azure region that supports Fabric capacities.')
param location string = resourceGroup().location

@description('Fabric capacity SKU. F4 is the smallest size that makes sense for the ontology demo.')
@allowed([
  'F2'
  'F4'
  'F8'
])
param capacitySku string = 'F4'

@description('Capacity administrators: object ids of the people and of the pipeline identities that use the capacity.')
@minLength(1)
param capacityAdmins array

@description('Create the capacity (it bills by the hour while it is not paused). Phase one of the deployment leaves it false.')
param deployCapacity bool = true

@description('Create the storage account and its container. False loads the seed data from the repository straight into the lakehouse.')
param deployStorage bool = true

param tags object = {}

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = if (deployStorage) {
  name: storageAccountName
  location: location
  tags: tags
  kind: 'StorageV2'
  sku: {
    name: 'Standard_LRS'
  }
  properties: {
    isHnsEnabled: true
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    defaultToOAuthAuthentication: true
    accessTier: 'Hot'
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = if (deployStorage) {
  parent: storage
  name: 'default'
}

resource sourceContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = if (deployStorage) {
  parent: blobService
  name: sourceContainerName
  properties: {
    publicAccess: 'None'
  }
}

resource capacity 'Microsoft.Fabric/capacities@2023-11-01' = if (deployCapacity) {
  name: capacityName
  location: location
  tags: tags
  sku: {
    name: capacitySku
    tier: 'Fabric'
  }
  properties: {
    administration: {
      members: capacityAdmins
    }
  }
}

output storageAccountName string = deployStorage ? storageAccountName : ''
output sourceContainerName string = deployStorage ? sourceContainerName : ''
output sourceUrl string = deployStorage ? 'https://${storageAccountName}.dfs.${environment().suffixes.storage}/${sourceContainerName}' : ''
output capacityName string = deployCapacity ? capacityName : ''
