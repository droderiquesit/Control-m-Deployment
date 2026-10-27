# ---------------------------------------------------------------------------
# ctmdeploy package: the python orchestrator for the Control-M Azure platform.
# Submodules: config (YAML -> dataclasses), render (cloud-init), secrets
# (Key Vault seeding), azure (ARM/Bicep), deploy (phases), replication, failover.
# ---------------------------------------------------------------------------
"""Deployment orchestrator for the BMC Control-M platform on Azure.

Only :mod:`ctmdeploy.config` and :mod:`ctmdeploy.render` may be imported
without the Azure SDKs installed; every other module imports the SDKs
lazily inside the functions that need them.
"""

__version__ = "1.0.0"

__all__ = [
    "__version__",
]
