// Traffic Manager priority failover for a PUBLIC Control-M Web endpoint:
// primary region first, DR second. Deployed only when both load balancers
// expose a public IP.
@description('Profile name, e.g. ctm-prod-tm.')
param name string

@description('DNS relative name of the Traffic Manager profile.')
param relativeName string

@description('Public IP resource id of the primary load balancer.')
param primaryEndpointTargetId string

@description('Public IP resource id of the DR load balancer.')
param drEndpointTargetId string

@description('Tags applied to the profile.')
param tags object = {}

resource profile 'Microsoft.Network/trafficManagerProfiles@2022-04-01' = {
  name: name
  location: 'global'
  tags: tags
  properties: {
    profileStatus: 'Enabled'
    trafficRoutingMethod: 'Priority'
    dnsConfig: {
      relativeName: relativeName
      ttl: 30
    }
    monitorConfig: {
      protocol: 'HTTPS'
      port: 443
      path: '/ControlM/'
      intervalInSeconds: 30
      timeoutInSeconds: 10
      toleratedNumberOfFailures: 3
    }
  }
}

resource primaryEndpoint 'Microsoft.Network/trafficManagerProfiles/azureEndpoints@2022-04-01' = {
  parent: profile
  name: 'primary'
  properties: {
    targetResourceId: primaryEndpointTargetId
    endpointStatus: 'Enabled'
    priority: 1
    weight: 1
  }
}

resource drEndpoint 'Microsoft.Network/trafficManagerProfiles/azureEndpoints@2022-04-01' = {
  parent: profile
  name: 'dr'
  properties: {
    targetResourceId: drEndpointTargetId
    endpointStatus: 'Enabled'
    priority: 2
    weight: 1
  }
}

output profileId string = profile.id
output fqdn string = profile.properties.dnsConfig.fqdn
