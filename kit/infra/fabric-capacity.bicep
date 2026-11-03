// Microsoft Fabric capacity for the Contoso bank ontology demo. F4 is the smallest size that makes sense for the ontology
// demo; pause it when idle (az fabric capacity suspend) and scale to F8 only if ontology-agent generation needs headroom.
targetScope = 'resourceGroup'

@description('Capacity name: 3-63 lowercase letters and digits, starting with a letter.')
@minLength(3)
@maxLength(63)
param capacityName string

@description('Azure region that supports Fabric capacities (southindia, centralindia, ...).')
param location string = resourceGroup().location

@description('Fabric capacity SKU.')
@allowed([
  'F2'
  'F4'
  'F8'
  'F16'
  'F32'
  'F64'
])
param sku string = 'F4'

@description('Capacity administrators: user principal names or object ids.')
@minLength(1)
param adminMembers array

param tags object = {}

resource capacity 'Microsoft.Fabric/capacities@2023-11-01' = {
  name: capacityName
  location: location
  tags: tags
  sku: {
    name: sku
    tier: 'Fabric'
  }
  properties: {
    administration: {
      members: adminMembers
    }
  }
}

output capacityId string = capacity.id
output capacityName string = capacity.name
output capacitySku string = capacity.sku.name
