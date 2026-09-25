targetScope = 'resourceGroup'

param location string

@minLength(1)
@maxLength(32)
param sandboxGroupName string

param principalId string

@allowed([
  'User'
  'Group'
  'ServicePrincipal'
])
param principalType string = 'User'

param labId string

var dataOwnerRoleId = 'c24cf47c-5077-412d-a19c-45202126392c'

resource sandboxGroup 'Microsoft.App/sandboxGroups@2026-07-01' = {
  name: sandboxGroupName
  location: location
  tags: {
    managedBy: 'aca-sandboxes-lab'
    labId: labId
  }
  properties: {}
}

resource dataOwner 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(sandboxGroup.id, principalId, dataOwnerRoleId)
  scope: sandboxGroup
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', dataOwnerRoleId)
    principalId: principalId
    principalType: principalType
  }
}

output sandboxGroupId string = sandboxGroup.id
