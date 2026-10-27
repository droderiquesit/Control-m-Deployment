// Azure Compute Gallery for the Control-M golden images: gallery 'ctmgallery'
// plus TWO image definitions (v4.1 two-image model):
//   * 'ctm-rhel9' — OS only (EM/Server VMs; never contains BMC software);
//   * 'ctm-agent' — the same OS prep PLUS the Control-M/Agent silent-installed
//     but UNCONFIGURED (no server registration at build time; generalized).
//     The agent VM scale sets boot from this one.
// Image VERSIONS are published by Packer (packer/ctm-rhel9.pkr.hcl and
// packer/ctm-agent.pkr.hcl), never by Bicep.
// Deploy into the shared image resource group (ctm-images-rg).

@description('Azure region for the gallery (image versions replicate to all deployment regions via Packer).')
param location string

@description('Compute Gallery name.')
param galleryName string = 'ctmgallery'

@description('Base (OS-only) image definition name; also used as the identifier SKU.')
param imageDefinition string = 'ctm-rhel9'

@description('Agent image definition name (OS prep + Control-M/Agent baked in, unconfigured); also used as the identifier SKU.')
param agentImageDefinition string = 'ctm-agent'

@description('Tags applied to every resource.')
param tags object = {}

resource gallery 'Microsoft.Compute/galleries@2023-07-03' = {
  name: galleryName
  location: location
  tags: tags
  properties: {
    description: 'Control-M golden images (built by Packer, consumed by the ctmdeploy Bicep deployments).'
  }
}

resource image 'Microsoft.Compute/galleries/images@2023-07-03' = {
  parent: gallery
  name: imageDefinition
  location: location
  tags: tags
  properties: {
    osType: 'Linux'
    osState: 'Generalized'
    hyperVGeneration: 'V2'
    architecture: 'x64'
    identifier: {
      publisher: 'ctm'
      offer: 'rhel9'
      sku: imageDefinition
    }
    recommended: {
      vCPUs: {
        min: 2
        max: 64
      }
      memory: {
        min: 4
        max: 256
      }
    }
  }
}

// Same Gen2/Linux shape as the base image; only the identifier SKU differs.
resource agentImage 'Microsoft.Compute/galleries/images@2023-07-03' = {
  parent: gallery
  name: agentImageDefinition
  location: location
  tags: tags
  properties: {
    osType: 'Linux'
    osState: 'Generalized'
    hyperVGeneration: 'V2'
    architecture: 'x64'
    identifier: {
      publisher: 'ctm'
      offer: 'rhel9'
      sku: agentImageDefinition
    }
    recommended: {
      vCPUs: {
        min: 2
        max: 64
      }
      memory: {
        min: 4
        max: 256
      }
    }
  }
}

output galleryId string = gallery.id
output imageDefinitionId string = image.id
output agentImageDefinitionId string = agentImage.id
