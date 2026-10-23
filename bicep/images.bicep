// Azure Compute Gallery for the Control-M golden image: gallery 'ctmgallery'
// plus the 'ctm-rhel9' image definition (RHEL 9, Gen2). Image VERSIONS are
// published by Packer (packer/ctm-rhel9.pkr.hcl), never by Bicep.
// Deploy into the shared image resource group (ctm-images-rg).

@description('Azure region for the gallery (image versions replicate to all deployment regions via Packer).')
param location string

@description('Compute Gallery name.')
param galleryName string = 'ctmgallery'

@description('Image definition name; also used as the identifier SKU.')
param imageDefinition string = 'ctm-rhel9'

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

output galleryId string = gallery.id
output imageDefinitionId string = image.id
