# terraform init -backend-config=backend.hcl   (values from bootstrap outputs)
resource_group_name  = "ctm-tfstate-rg"
storage_account_name = "REPLACE_WITH_BOOTSTRAP_STORAGE_ACCOUNT"
container_name       = "tfstate"
key                  = "control-m/dev.tfstate"
use_azuread_auth     = true
