# ---------------------------------------------------------------------------
# Pure-python tests for the v3 additions: the optional network_infrastructure
# block (dataclasses, placement cross-check, peer_to resolution, address-
# space overlap detection) and the installer-media mapping (media block
# parsing + the pure filename -> blob-prefix function in ctmdeploy.media).
# No Azure SDK required: pyyaml only.
# Runnable via `python3 -m pytest` or plain `python3 deploy/tests/test_network_media.py`.
# ---------------------------------------------------------------------------
"""network_infrastructure validation + media mapping tests (v3 addendum)."""

from __future__ import annotations

import sys
import tempfile
import traceback
import unittest
from pathlib import Path
from typing import Any

import yaml

DEPLOY_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = DEPLOY_DIR.parent
if str(DEPLOY_DIR) not in sys.path:
    sys.path.insert(0, str(DEPLOY_DIR))

from ctmdeploy import config as cfgmod  # noqa: E402
from ctmdeploy import media  # noqa: E402  (azure-storage-blob is lazy)


def _skip(message: str) -> None:
    raise unittest.SkipTest(message)


def _load_dict(data: dict[str, Any]) -> cfgmod.EnvironmentConfig:
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as handle:
        yaml.safe_dump(data, handle, sort_keys=False)
        path = handle.name
    try:
        return cfgmod.load_config_file(path, allow_placeholders=True)
    finally:
        Path(path).unlink(missing_ok=True)


def _expect_config_error(data: dict[str, Any], *needles: str) -> None:
    try:
        _load_dict(data)
    except cfgmod.ConfigError as exc:
        message = str(exc)
        for needle in needles:
            assert needle in message, f"{needle!r} not in error: {message}"
    else:
        raise AssertionError(f"config was accepted; expected an error about {needles}")


# ---------------------------------------------------------------------------
# Synthetic base config (v2 placement) + a matching network_infrastructure
# ---------------------------------------------------------------------------


def _base_config(environment: str = "nonprod") -> dict[str, Any]:
    return {
        "subscription_id": cfgmod.PLACEHOLDER_SUBSCRIPTION_ID,
        "environment": environment,
        "location": "eastus2",
        "unique_suffix": "n01" if environment == "nonprod" else "p01",
        "tags": {},
        "network": {
            "utility": {
                "vnet_name": "vnet-util",
                "vnet_resource_group_name": "rg-net",
                "em_subnet_name": "snet-em",
                "data_subnet_name": "snet-data",
                "pe_subnet_name": "snet-pe",
            },
            "existing_private_dns_zone_ids": {},
        },
        "em": {"vm_size": "Standard_D4s_v5"},
        "servers": {
            "dev": {
                "vm_size": "Standard_D4s_v5",
                "agents": {
                    "vm_size": "Standard_D2s_v5",
                    "min": 1,
                    "max": 2,
                    "default": 1,
                },
                "network": {
                    "vnet_name": "vnet-dev",
                    "vnet_resource_group_name": "rg-net",
                    "server_subnet_name": "snet-server",
                    "agents_subnet_name": "snet-agents",
                },
            },
        },
        "database": {"sku_name": "GP_Standard_D2ds_v5"},
        "dr": {"enabled": False},
    }


def _infrastructure_block(create: bool = True) -> dict[str, Any]:
    """A network_infrastructure block matching _base_config's placement."""
    return {
        "create": create,
        "user_access_cidrs": ["198.51.100.0/24"],
        "bastion_cidrs": ["203.0.113.0/28"],
        "vnets": [
            {
                "name": "vnet-util",
                "resource_group_name": "rg-net",
                "location": "eastus2",
                "role": "utility",
                "address_space": "10.60.0.0/22",
                "subnets": [
                    {"name": "snet-em", "prefix": "10.60.0.0/24", "role": "em"},
                    {"name": "snet-data", "prefix": "10.60.1.0/27", "role": "data"},
                    {"name": "snet-pe", "prefix": "10.60.1.64/26", "role": "pe"},
                ],
            },
            {
                "name": "vnet-dev",
                "resource_group_name": "rg-net",
                "location": "eastus2",
                "role": "spoke",
                "peer_to": "vnet-util",
                "address_space": "10.60.8.0/24",
                "subnets": [
                    {"name": "snet-server", "prefix": "10.60.8.0/26", "role": "server"},
                    {"name": "snet-agents", "prefix": "10.60.8.64/26", "role": "agents"},
                ],
            },
        ],
    }


def _config_with_infrastructure(create: bool = True) -> dict[str, Any]:
    data = _base_config()
    data["network_infrastructure"] = _infrastructure_block(create)
    return data


# ---------------------------------------------------------------------------
# network_infrastructure: parsing + resolution
# ---------------------------------------------------------------------------


def test_network_infrastructure_defaults_when_absent() -> None:
    cfg = _load_dict(_base_config())
    infra = cfg.network_infrastructure
    assert infra.create is False
    assert infra.vnets == ()
    assert infra.user_access_cidrs == () and infra.bastion_cidrs == ()
    assert infra.vnets_param() == []
    assert cfg.media.azure_artifacts is None


def test_network_infrastructure_valid_config_loads() -> None:
    cfg = _load_dict(_config_with_infrastructure())
    infra = cfg.network_infrastructure
    assert infra.create is True
    assert [v.name for v in infra.vnets] == ["vnet-util", "vnet-dev"]
    utility = infra.vnet("vnet-util")
    assert utility is not None and utility.role == "utility"
    assert utility.subnet("snet-data").role == "data"
    spoke = infra.vnet("vnet-dev")
    assert spoke.peer_to == ("vnet-util",)  # v4: peer_to is a tuple (string or list in YAML)
    assert infra.spoke_address_spaces() == ["10.60.8.0/24"]


def test_vnets_param_is_the_raw_config_shape() -> None:
    """v4: network/main.bicep resolves peer address spaces ITSELF, so the
    python-side param is the raw config shape with peer_to as a list."""
    cfg = _load_dict(_config_with_infrastructure())
    items = {item["name"]: item for item in cfg.network_infrastructure.vnets_param()}
    utility, spoke = items["vnet-util"], items["vnet-dev"]
    assert set(utility) == {
        "name",
        "resource_group_name",
        "location",
        "role",
        "peer_to",
        "address_space",
        "subnets",
    }
    assert spoke["peer_to"] == ["vnet-util"] and utility["peer_to"] == []
    assert utility["subnets"][0] == {"name": "snet-em", "prefix": "10.60.0.0/24", "role": "em"}
    assert spoke["address_space"] == "10.60.8.0/24"


def test_peer_to_accepts_string_or_list() -> None:
    data = _config_with_infrastructure()
    data["network_infrastructure"]["vnets"][1]["peer_to"] = ["vnet-util"]
    cfg = _load_dict(data)
    assert cfg.network_infrastructure.vnet("vnet-dev").peer_to == ("vnet-util",)

    data = _config_with_infrastructure()
    data["network_infrastructure"]["vnets"][1]["peer_to"] = {"bad": "shape"}
    _expect_config_error(data, "peer_to", "string or a list")

    data = _config_with_infrastructure()
    data["network_infrastructure"]["vnets"][1]["peer_to"] = ["vnet-util", "vnet-util"]
    _expect_config_error(data, "peer_to", "duplicate")


def test_create_false_keeps_block_inert() -> None:
    """With create false (BYO default) the block parses but is NOT validated:
    a stale peer_to or placement mismatch must not block a BYO deployment."""
    data = _config_with_infrastructure(create=False)
    data["network_infrastructure"]["vnets"][1]["peer_to"] = "vnet-gone"
    data["network_infrastructure"]["vnets"][0]["address_space"] = "10.60.0.0/22"
    cfg = _load_dict(data)
    assert cfg.network_infrastructure.create is False
    assert cfg.network_infrastructure.vnet("vnet-dev").peer_to == ("vnet-gone",)


def test_vnet_role_and_subnet_role_are_validated_structurally() -> None:
    data = _config_with_infrastructure(create=False)  # structural: even when inert
    data["network_infrastructure"]["vnets"][0]["role"] = "hub"
    _expect_config_error(data, "network_infrastructure.vnets[0].role", "hub")

    data = _config_with_infrastructure(create=False)
    data["network_infrastructure"]["vnets"][0]["subnets"][0]["role"] = "web"
    _expect_config_error(data, "subnets[0].role", "web")

    data = _config_with_infrastructure(create=False)
    data["network_infrastructure"]["vnets"].append(
        dict(data["network_infrastructure"]["vnets"][0])
    )
    _expect_config_error(data, "duplicate VNet name", "vnet-util")


# ---------------------------------------------------------------------------
# network_infrastructure: validation failures (create: true)
# ---------------------------------------------------------------------------


def test_missing_placement_vnet_is_a_clear_error() -> None:
    data = _config_with_infrastructure()
    # The dev server's VNet disappears from the created set.
    data["network_infrastructure"]["vnets"][1]["name"] = "vnet-other"
    _expect_config_error(
        data, "config.servers.dev.network.server_subnet_name", "vnet-dev", "missing from"
    )


def test_missing_placement_subnet_is_a_clear_error() -> None:
    data = _config_with_infrastructure()
    data["network_infrastructure"]["vnets"][0]["subnets"][1]["name"] = "snet-other"
    _expect_config_error(
        data, "config.network.utility.data_subnet_name", "snet-data", "missing from VNet"
    )


def test_placement_resource_group_mismatch_is_a_clear_error() -> None:
    data = _config_with_infrastructure()
    data["network_infrastructure"]["vnets"][1]["resource_group_name"] = "rg-elsewhere"
    _expect_config_error(data, "vnet-dev", "rg-elsewhere", "rg-net", "must match")


def test_placement_subnet_role_mismatch_is_a_clear_error() -> None:
    data = _config_with_infrastructure()
    # Point the EM placement at the data subnet: wrong NSG rule set.
    data["network"]["utility"]["em_subnet_name"] = "snet-data"
    _expect_config_error(data, "config.network.utility.em_subnet_name", "role", "em")


def test_agents_sharing_the_server_subnet_is_allowed() -> None:
    """agents_subnet_name defaults to the server subnet; the cross-check must
    accept a role-'server' subnet for the agents placement."""
    data = _config_with_infrastructure()
    del data["servers"]["dev"]["network"]["agents_subnet_name"]
    cfg = _load_dict(data)
    assert cfg.servers["dev"].network.agents_subnet == "snet-server"


def test_overlapping_address_spaces_are_rejected() -> None:
    data = _config_with_infrastructure()
    data["network_infrastructure"]["vnets"][1]["address_space"] = "10.60.2.0/24"  # inside /22
    # Keep the subnets inside the new space so only the overlap can fail.
    data["network_infrastructure"]["vnets"][1]["subnets"] = [
        {"name": "snet-server", "prefix": "10.60.2.0/26", "role": "server"},
        {"name": "snet-agents", "prefix": "10.60.2.64/26", "role": "agents"},
    ]
    _expect_config_error(data, "address spaces overlap", "vnet-util", "vnet-dev")


def test_unparseable_address_space_is_rejected() -> None:
    data = _config_with_infrastructure()
    data["network_infrastructure"]["vnets"][0]["address_space"] = "10.60.0.1/22"  # host bits
    _expect_config_error(data, "vnet-util", "10.60.0.1/22", "not a valid CIDR")


def test_subnet_outside_vnet_space_is_rejected() -> None:
    data = _config_with_infrastructure()
    data["network_infrastructure"]["vnets"][1]["subnets"][0]["prefix"] = "10.99.0.0/26"
    _expect_config_error(data, "snet-server", "outside the VNet address space", "10.60.8.0/24")


def test_overlapping_subnets_within_a_vnet_are_rejected() -> None:
    data = _config_with_infrastructure()
    data["network_infrastructure"]["vnets"][1]["subnets"][1]["prefix"] = "10.60.8.32/27"
    _expect_config_error(data, "snet-agents", "overlaps subnet", "snet-server")


def test_bad_peer_to_is_rejected() -> None:
    # Unknown target (spokes must resolve locally; only 'build' may cross envs).
    data = _config_with_infrastructure()
    data["network_infrastructure"]["vnets"][1]["peer_to"] = "vnet-nope"
    _expect_config_error(data, "peer_to", "vnet-nope", "does not name a VNet")

    # Spoke without any peer_to.
    data = _config_with_infrastructure()
    del data["network_infrastructure"]["vnets"][1]["peer_to"]
    _expect_config_error(data, "vnets[vnet-dev]", "peer_to", "exactly ONE")

    # A spoke with two targets is also rejected (exactly one utility).
    data = _config_with_infrastructure()
    data["network_infrastructure"]["vnets"][1]["peer_to"] = ["vnet-util", "vnet-other"]
    _expect_config_error(data, "vnets[vnet-dev]", "peer_to", "exactly ONE")

    # utility peer_to must name ANOTHER utility (spoke target rejected).
    data = _config_with_infrastructure()
    data["network_infrastructure"]["vnets"][0]["peer_to"] = "vnet-dev"
    _expect_config_error(data, "vnets[vnet-util]", "peer_to", "utility VNets")

    # utility peering to itself is rejected.
    data = _config_with_infrastructure()
    data["network_infrastructure"]["vnets"][0]["peer_to"] = "vnet-util"
    _expect_config_error(data, "vnets[vnet-util]", "peer_to", "OTHER")

    # peer_to pointing at another spoke.
    data = _config_with_infrastructure()
    second = {
        "name": "vnet-qa",
        "resource_group_name": "rg-net",
        "location": "eastus2",
        "role": "spoke",
        "peer_to": "vnet-dev",
        "address_space": "10.60.9.0/24",
        "subnets": [{"name": "snet-server", "prefix": "10.60.9.0/26", "role": "server"}],
    }
    data["network_infrastructure"]["vnets"].append(second)
    _expect_config_error(data, "vnets[vnet-qa]", "peer_to", "utility")


def _build_vnet(peer_to) -> dict[str, Any]:
    return {
        "name": "vnet-build",
        "resource_group_name": "rg-net",
        "location": "eastus2",
        "role": "build",
        "peer_to": peer_to,
        "address_space": "10.90.0.0/24",
        "subnets": [{"name": "snet-build", "prefix": "10.90.0.0/26", "role": "build"}],
    }


def test_utility_to_utility_global_peering_is_allowed() -> None:
    data = _config_with_infrastructure()
    data["network_infrastructure"]["vnets"].append(
        {
            "name": "vnet-util-dr",
            "resource_group_name": "rg-net-dr",
            "location": "centralus",
            "role": "utility",
            "address_space": "10.80.0.0/22",
            "subnets": [{"name": "snet-em", "prefix": "10.80.0.0/24", "role": "em"}],
        }
    )
    data["network_infrastructure"]["vnets"][0]["peer_to"] = ["vnet-util-dr"]
    cfg = _load_dict(data)
    assert cfg.network_infrastructure.vnet("vnet-util").peer_to == ("vnet-util-dr",)


def test_build_vnet_peering_rules() -> None:
    # A build VNet must peer somewhere.
    data = _config_with_infrastructure()
    data["network_infrastructure"]["vnets"].append(_build_vnet([]))
    _expect_config_error(data, "vnets[vnet-build]", "peer_to", "required")

    # Locally-known targets must be utilities (or resolution-only externals).
    data = _config_with_infrastructure()
    data["network_infrastructure"]["vnets"].append(_build_vnet(["vnet-dev"]))
    _expect_config_error(data, "vnets[vnet-build]", "peer_to", "utility")

    # CROSS-ENVIRONMENT names (not declared in this config) are tolerated:
    # the prod build VNet peers to the NONPROD utility VNet by name.
    data = _config_with_infrastructure()
    data["network_infrastructure"]["vnets"].append(
        _build_vnet(["vnet-util", "vnet-other-env-utility"])
    )
    cfg = _load_dict(data)
    build = cfg.network_infrastructure.vnet("vnet-build")
    assert build.peer_to == ("vnet-util", "vnet-other-env-utility")
    # Raw param shape keeps the full list for network/main.bicep to resolve.
    items = {item["name"]: item for item in cfg.network_infrastructure.vnets_param()}
    assert items["vnet-build"]["peer_to"] == ["vnet-util", "vnet-other-env-utility"]


def _external_vnet(**overrides) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "name": "vnet-other-env-utility",
        "resource_group_name": "rg-net-other",
        "location": "eastus2",
        "role": "external",
        "address_space": "10.50.0.0/22",
    }
    entry.update(overrides)
    return entry


def test_external_vnet_entries_are_resolution_only() -> None:
    """v4.1: role 'external' declares name+rg+address_space of a VNet another
    plan deploys — no subnets, no peer_to; build peer_to resolves against it."""
    # Accepted without subnets, and the build peering resolves to it.
    data = _config_with_infrastructure()
    data["network_infrastructure"]["vnets"].append(_external_vnet())
    data["network_infrastructure"]["vnets"].append(
        _build_vnet(["vnet-util", "vnet-other-env-utility"])
    )
    cfg = _load_dict(data)
    external = cfg.network_infrastructure.vnet("vnet-other-env-utility")
    assert external is not None and external.role == "external"
    assert external.subnets == () and external.peer_to == ()
    # The raw param shape carries the entry for network/main.bicep (which
    # creates nothing for role 'external' but resolves peerings through it).
    items = {item["name"]: item for item in cfg.network_infrastructure.vnets_param()}
    assert items["vnet-other-env-utility"]["role"] == "external"
    assert items["vnet-other-env-utility"]["subnets"] == []

    # Subnets on an external entry are rejected (nothing is deployed for it).
    data = _config_with_infrastructure()
    data["network_infrastructure"]["vnets"].append(
        _external_vnet(subnets=[{"name": "snet-x", "prefix": "10.50.0.0/26", "role": "em"}])
    )
    _expect_config_error(data, "external", "resolution-only")

    # ...and so is a peer_to of its own (the owning plan declares those).
    data = _config_with_infrastructure()
    data["network_infrastructure"]["vnets"].append(_external_vnet(peer_to="vnet-util"))
    _expect_config_error(data, "external", "peer_to")

    # External address spaces still participate in the overlap check.
    data = _config_with_infrastructure()
    data["network_infrastructure"]["vnets"].append(
        _external_vnet(address_space="10.60.0.0/16")  # overlaps vnet-util /22
    )
    _expect_config_error(data, "address spaces overlap", "vnet-other-env-utility")


def test_bastion_subnet_naming_rule() -> None:
    # role 'bastion' must be named exactly AzureBastionSubnet (structural).
    data = _config_with_infrastructure(create=False)
    data["network_infrastructure"]["vnets"][0]["subnets"].append(
        {"name": "snet-bastion", "prefix": "10.60.2.0/26", "role": "bastion"}
    )
    _expect_config_error(data, "AzureBastionSubnet", "bastion")

    # ...and the name AzureBastionSubnet may only carry role 'bastion'.
    data = _config_with_infrastructure(create=False)
    data["network_infrastructure"]["vnets"][0]["subnets"].append(
        {"name": "AzureBastionSubnet", "prefix": "10.60.2.0/26", "role": "pe"}
    )
    _expect_config_error(data, "AzureBastionSubnet", "role 'bastion'")

    # The correct pairing loads (plus appgw/func/build roles).
    data = _config_with_infrastructure()
    data["network_infrastructure"]["vnets"][0]["subnets"] += [
        {"name": "AzureBastionSubnet", "prefix": "10.60.2.0/26", "role": "bastion"},
        {"name": "snet-appgw", "prefix": "10.60.2.64/26", "role": "appgw"},
        {"name": "snet-func", "prefix": "10.60.2.128/26", "role": "func"},
    ]
    cfg = _load_dict(data)
    utility = cfg.network_infrastructure.vnet("vnet-util")
    assert utility.subnet_by_role("bastion").name == "AzureBastionSubnet"
    assert utility.subnet_by_role("appgw").name == "snet-appgw"
    assert utility.subnet_by_role("func").name == "snet-func"


def test_bad_access_cidrs_are_rejected() -> None:
    data = _config_with_infrastructure()
    data["network_infrastructure"]["user_access_cidrs"] = ["not-a-cidr"]
    _expect_config_error(data, "user_access_cidrs", "not-a-cidr")

    data = _config_with_infrastructure()
    data["network_infrastructure"]["bastion_cidrs"] = ["10.0.0.5/8"]
    _expect_config_error(data, "bastion_cidrs", "10.0.0.5/8")


def test_create_true_requires_vnets() -> None:
    data = _base_config()
    data["network_infrastructure"] = {"create": True, "vnets": []}
    _expect_config_error(data, "network_infrastructure.vnets", "at least one")


def test_dr_placement_is_cross_checked_too() -> None:
    """With dr.enabled, the DR utility + DR server VNets must be declared."""
    data = _config_with_infrastructure()
    data["environment"] = "prod"
    data["unique_suffix"] = "p01"
    data["dr"] = {
        "enabled": True,
        "location": "centralus",
        "network": {
            "utility": {
                "vnet_name": "vnet-util-dr",
                "vnet_resource_group_name": "rg-net-dr",
                "em_subnet_name": "snet-em",
                "data_subnet_name": "snet-data",
                "pe_subnet_name": "snet-pe",
            },
        },
        "server_networks": {
            "dev": {
                "vnet_name": "vnet-dev-dr",
                "vnet_resource_group_name": "rg-net-dr",
                "server_subnet_name": "snet-server",
                "agents_subnet_name": "snet-agents",
            },
        },
    }
    # Without the DR VNets in the created set: a clear error names the ref.
    _expect_config_error(data, "config.dr.network.utility", "vnet-util-dr", "missing from")

    # Adding both DR VNets (non-overlapping spaces) makes the config load.
    data["network_infrastructure"]["vnets"] += [
        {
            "name": "vnet-util-dr",
            "resource_group_name": "rg-net-dr",
            "location": "centralus",
            "role": "utility",
            "address_space": "10.80.0.0/22",
            "subnets": [
                {"name": "snet-em", "prefix": "10.80.0.0/24", "role": "em"},
                {"name": "snet-data", "prefix": "10.80.1.0/27", "role": "data"},
                {"name": "snet-pe", "prefix": "10.80.1.64/26", "role": "pe"},
            ],
        },
        {
            "name": "vnet-dev-dr",
            "resource_group_name": "rg-net-dr",
            "location": "centralus",
            "role": "spoke",
            "peer_to": "vnet-util-dr",
            "address_space": "10.80.9.0/24",
            "subnets": [
                {"name": "snet-server", "prefix": "10.80.9.0/26", "role": "server"},
                {"name": "snet-agents", "prefix": "10.80.9.64/26", "role": "agents"},
            ],
        },
    ]
    cfg = _load_dict(data)
    items = {item["name"]: item for item in cfg.network_infrastructure.vnets_param()}
    # Raw shape only — network/main.bicep resolves peer address spaces itself.
    assert items["vnet-dev-dr"]["peer_to"] == ["vnet-util-dr"]
    assert items["vnet-util-dr"]["peer_to"] == []
    assert items["vnet-util-dr"]["address_space"] == "10.80.0.0/22"
    assert cfg.network_infrastructure.spoke_address_spaces() == [
        "10.60.8.0/24",
        "10.80.9.0/24",
    ]


# ---------------------------------------------------------------------------
# The committed PoC plan (environments/<env>/config.yaml)
# ---------------------------------------------------------------------------

#: Binding PoC address plan (non-overlapping across platforms; v4 adds the
#: build VNet to prod). Asserted as a SUBSET so the network-plan owner may
#: extend the block without breaking this suite.
_POC_PLAN = {
    "nonprod": {
        "vnet-dev-utility": "10.60.0.0/22",
        "vnet-ctm-dev": "10.60.8.0/24",
        "vnet-ctm-qa": "10.60.9.0/24",
    },
    "prod": {
        "vnet-prod-utility": "10.70.0.0/22",
        "vnet-ctm-uat": "10.70.8.0/24",
        "vnet-ctm-prod": "10.70.9.0/24",
        "vnet-dr-utility": "10.80.0.0/22",
        "vnet-ctm-prod-dr": "10.80.9.0/24",
        "vnet-ctm-build": "10.90.0.0/24",
        # v4.1: resolution-only external entry (created by the NONPROD plan)
        # so the build <-> dev-utility peering resolves on the ctmdeploy path.
        "vnet-dev-utility": "10.60.0.0/22",
    },
}


def _raw_env_config(env: str) -> dict[str, Any]:
    path = REPO_ROOT / "environments" / env / "config.yaml"
    if not path.is_file():
        _skip(f"{path} does not exist yet")
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _poc_config(env: str) -> cfgmod.EnvironmentConfig:
    """The committed config with network_infrastructure.create flipped on."""
    raw = _raw_env_config(env)
    if "network_infrastructure" not in raw:
        _skip(
            f"environments/{env}/config.yaml has no network_infrastructure block yet "
            "(appended by the config agent)"
        )
    raw["network_infrastructure"]["create"] = True
    return _load_dict(raw)


def test_poc_network_plan_ships_disabled() -> None:
    for env in ("nonprod", "prod"):
        raw = _raw_env_config(env)
        if "network_infrastructure" not in raw:
            _skip(f"environments/{env}/config.yaml has no network_infrastructure block yet")
        assert raw["network_infrastructure"]["create"] is False, (
            f"{env}: network_infrastructure.create must ship false (BYO default)"
        )
        # The shipped file must also load as-is.
        cfgmod.load_config(env, REPO_ROOT, allow_placeholders=True)


def test_poc_network_plan_validates_nonprod() -> None:
    cfg = _poc_config("nonprod")
    infra = cfg.network_infrastructure
    declared = {v.name: v.address_space for v in infra.vnets}
    for name, space in _POC_PLAN["nonprod"].items():
        assert declared.get(name) == space, (name, declared)
    assert {v.name for v in infra.vnets if v.role == "utility"} == {"vnet-dev-utility"}
    for name in ("vnet-ctm-dev", "vnet-ctm-qa"):
        assert infra.vnet(name).peer_to == ("vnet-dev-utility",)
    assert cfg.network_infrastructure.spoke_address_spaces() == [
        "10.60.8.0/24",
        "10.60.9.0/24",
    ]
    # v4 access-layer subnets in the utility VNet (bastion + func).
    utility = infra.vnet("vnet-dev-utility")
    bastion = utility.subnet_by_role("bastion")
    assert bastion is not None and bastion.name == "AzureBastionSubnet"
    assert bastion.prefix == "10.60.2.0/26"
    func = utility.subnet_by_role("func")
    assert func is not None and func.prefix == "10.60.2.64/26"
    # ...and the placement helpers resolve subnet ids from the plan.
    assert cfg.bastion_subnet_id().endswith(
        "/virtualNetworks/vnet-dev-utility/subnets/AzureBastionSubnet"
    )
    assert cfg.func_subnet_id().endswith("/subnets/snet-ctm-func")


def test_poc_network_plan_validates_prod() -> None:
    cfg = _poc_config("prod")
    infra = cfg.network_infrastructure
    declared = {v.name: v.address_space for v in infra.vnets}
    for name, space in _POC_PLAN["prod"].items():
        assert declared.get(name) == space, (name, declared)
    assert infra.vnet("vnet-ctm-prod-dr").peer_to == ("vnet-dr-utility",)
    assert infra.vnet("vnet-ctm-uat").peer_to == ("vnet-prod-utility",)
    # The utility subnets follow the PoC split (em /24, postgres /27, pe /26)
    # plus the v4 access-layer additions (bastion /26, appgw /26, func /26).
    utility = infra.vnet("vnet-prod-utility")
    roles = {s.role: s.prefix for s in utility.subnets}
    assert roles["em"] == "10.70.0.0/24"
    assert roles["data"] == "10.70.1.0/27"
    assert roles["pe"] == "10.70.1.64/26"
    assert roles["bastion"] == "10.70.2.0/26"
    assert roles["appgw"] == "10.70.2.64/26"
    assert roles["func"] == "10.70.2.128/26"
    assert utility.subnet_by_role("bastion").name == "AzureBastionSubnet"
    # Utility <-> utility GLOBAL peering (appgw -> DR LB, bastion -> DR).
    assert "vnet-dr-utility" in infra.vnet("vnet-prod-utility").peer_to
    # The build VNet peers to ALL THREE utility VNets; vnet-dev-utility is
    # CREATED by the nonprod plan but declared here as a resolution-only
    # role-'external' entry, so the build <-> dev-utility pair resolves (and
    # is emitted) on the ctmdeploy path too — not just via the bicepparams.
    build = infra.vnet("vnet-ctm-build")
    assert build is not None and build.role == "build"
    assert set(build.peer_to) == {
        "vnet-dev-utility",
        "vnet-prod-utility",
        "vnet-dr-utility",
    }
    assert build.subnet_by_role("build") is not None
    external = infra.vnet("vnet-dev-utility")
    assert external is not None and external.role == "external"
    assert external.resource_group_name == "rg-network-nonprod"
    assert external.address_space == "10.60.0.0/22"
    assert external.subnets == () and external.peer_to == ()
    # Placement helpers used by the access phase resolve against this plan.
    assert cfg.appgw_subnet_id().endswith(
        "/virtualNetworks/vnet-prod-utility/subnets/snet-ctm-appgw"
    )
    assert cfg.build_agents_subnet_id().endswith(
        "/virtualNetworks/vnet-ctm-build/subnets/snet-ctm-build"
    )


def test_poc_network_plan_dr_utility_has_the_appgw_subnet() -> None:
    """The DR gateway twin's subnet ships in the prod plan (both inputs are
    kept in sync with network/params/prod.bicepparam by hand)."""
    cfg = _poc_config("prod")
    dr_utility = cfg.network_infrastructure.vnet("vnet-dr-utility")
    assert dr_utility is not None
    appgw = dr_utility.subnet_by_role("appgw")
    assert appgw is not None and appgw.name == "snet-ctm-appgw"
    assert appgw.prefix == "10.80.2.0/26"
    # The access phase resolves the twin's subnet id from this entry.
    assert cfg.appgw_subnet_id("dr").endswith(
        "/virtualNetworks/vnet-dr-utility/subnets/snet-ctm-appgw"
    )
    # Each gateway's backend connections ride the em-subnet user-access rule:
    # BOTH appgw subnet prefixes must stay in user_access_cidrs.
    cidrs = set(cfg.network_infrastructure.user_access_cidrs)
    assert {"10.70.2.64/26", "10.80.2.0/26"} <= cidrs


def test_poc_media_block_parses() -> None:
    for env in ("nonprod", "prod"):
        raw = _raw_env_config(env)
        if "media" not in raw:
            _skip(f"environments/{env}/config.yaml has no media block yet")
        cfg = cfgmod.load_config(env, REPO_ROOT, allow_placeholders=True)
        artifacts = cfg.media.azure_artifacts
        assert artifacts is not None, f"{env}: media.azure_artifacts did not parse"
        assert artifacts.feed == "ctm-media"
        assert set(artifacts.packages) == set(cfgmod.MEDIA_PACKAGE_KEYS)
        assert artifacts.packages["em"].name == "control-m-em"
        assert artifacts.packages["server"].name == "control-m-server"
        assert artifacts.packages["silent_xml"].name == "control-m-silent-xml"


# ---------------------------------------------------------------------------
# media: config block validation (synthetic)
# ---------------------------------------------------------------------------


def _media_block() -> dict[str, Any]:
    return {
        "azure_artifacts": {
            "organization": "https://dev.azure.com/example",
            "project": "platform",
            "feed": "ctm-media",
            "packages": {
                "em": {"name": "control-m-em", "version": "9.0.21.300"},
                "server": {"name": "control-m-server", "version": "9.0.21.300"},
                "agent": {"name": "control-m-agent", "version": "9.0.21.300"},
                "silent_xml": {"name": "control-m-silent-xml", "version": "1.0.0"},
            },
        }
    }


def test_media_block_parses_synthetic() -> None:
    data = _base_config()
    data["media"] = _media_block()
    cfg = _load_dict(data)
    artifacts = cfg.media.azure_artifacts
    assert artifacts.organization == "https://dev.azure.com/example"
    assert artifacts.project == "platform"
    assert artifacts.packages["silent_xml"].version == "1.0.0"
    assert artifacts.packages["agent"].name == "control-m-agent"  # v4


def test_media_block_requires_all_four_packages() -> None:
    assert cfgmod.MEDIA_PACKAGE_KEYS == ("em", "server", "agent", "silent_xml")
    for missing in cfgmod.MEDIA_PACKAGE_KEYS:
        data = _base_config()
        data["media"] = _media_block()
        del data["media"]["azure_artifacts"]["packages"][missing]
        _expect_config_error(data, "media.azure_artifacts.packages", missing)


def test_media_block_rejects_unknown_packages() -> None:
    data = _base_config()
    data["media"] = _media_block()
    data["media"]["azure_artifacts"]["packages"]["plugin"] = {"name": "x", "version": "1"}
    _expect_config_error(data, "media.azure_artifacts.packages", "unknown keys", "plugin")


# ---------------------------------------------------------------------------
# media: filename -> blob prefix mapping (pure function)
# ---------------------------------------------------------------------------


def test_blob_name_for_flat_epd_files() -> None:
    assert (
        media.blob_name_for("DREM.9.0.21.300_Linux-x86_64.tar.Z")
        == "em/DREM.9.0.21.300_Linux-x86_64.tar.Z"
    )
    assert (
        media.blob_name_for("DRSRV.9.0.21.300_Linux-x86_64.tar.Z")
        == "server/DRSRV.9.0.21.300_Linux-x86_64.tar.Z"
    )


def test_blob_name_for_silent_xmls() -> None:
    assert media.blob_name_for("silent-em-primary.xml") == "em/silent-em-primary.xml"
    assert media.blob_name_for("silent-em-secondary.xml") == "em/silent-em-secondary.xml"
    assert media.blob_name_for("silent-server-primary.xml") == "server/silent-server-primary.xml"
    assert (
        media.blob_name_for("silent-server-secondary.xml")
        == "server/silent-server-secondary.xml"
    )


def test_blob_name_for_already_prefixed_tree() -> None:
    assert media.blob_name_for("em/custom-media.tar.Z") == "em/custom-media.tar.Z"
    assert media.blob_name_for("server/nested/dir/file.bin") == "server/nested/dir/file.bin"
    # The verbatim rule wins over the filename rule inside em/ and server/.
    assert media.blob_name_for("em/silent-server-primary.xml") == "em/silent-server-primary.xml"


def test_blob_name_for_other_directories_map_by_filename() -> None:
    # The silent_xml package may be downloaded into its own directory.
    assert media.blob_name_for("silent_xml/silent-em-primary.xml") == "em/silent-em-primary.xml"
    assert (
        media.blob_name_for("media/drop/DRSRV.9.0.21.300_Linux-x86_64.tar.Z")
        == "server/DRSRV.9.0.21.300_Linux-x86_64.tar.Z"
    )


def test_blob_name_for_unmapped_files_is_none() -> None:
    assert media.blob_name_for("README.md") is None
    assert media.blob_name_for("notes/checksums.txt") is None
    assert media.blob_name_for("em") is None  # a bare file named 'em', not a tree
    assert media.blob_name_for("") is None


def test_blob_name_for_windows_separators() -> None:
    assert media.blob_name_for("em\\silent-em-primary.xml") == "em/silent-em-primary.xml"
    assert media.blob_name_for("silent_xml\\silent-server-primary.xml") == (
        "server/silent-server-primary.xml"
    )


# ---------------------------------------------------------------------------
# media: upload planning + target resolution (no Azure)
# ---------------------------------------------------------------------------


def test_plan_uploads_maps_and_skips() -> None:
    import shutil

    root = Path(tempfile.mkdtemp(prefix="ctm-media-"))
    try:
        (root / "silent_xml").mkdir()
        (root / "DREM.9.0.21.300_Linux-x86_64.tar.Z").write_bytes(b"em-media")
        (root / "DRSRV.9.0.21.300_Linux-x86_64.tar.Z").write_bytes(b"srv-media")
        (root / "silent_xml" / "silent-em-primary.xml").write_text("<em/>")
        (root / "README.md").write_text("not media")
        uploads, skipped = media.plan_uploads(root)
        assert {blob for _, blob in uploads} == {
            "em/DREM.9.0.21.300_Linux-x86_64.tar.Z",
            "server/DRSRV.9.0.21.300_Linux-x86_64.tar.Z",
            "em/silent-em-primary.xml",
        }
        assert [path.name for path in skipped] == ["README.md"]

        # Two files mapping to one blob name is ambiguous -> error.
        (root / "em").mkdir()
        (root / "em" / "silent-em-primary.xml").write_text("<dup/>")
        try:
            media.plan_uploads(root)
        except media.MediaError as exc:
            assert "silent-em-primary.xml" in str(exc) and "ambiguous" in str(exc)
        else:
            raise AssertionError("duplicate blob mapping was accepted")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_resolve_targets_defaults_and_errors() -> None:
    cfg = _load_dict(_base_config())  # dr disabled
    assert media.resolve_targets(cfg, None) == [("primary", cfg.blob_account_name)]
    assert media.resolve_targets(cfg, "primary") == [("primary", cfg.blob_account_name)]
    for bad_role in ("dr", "both"):
        try:
            media.resolve_targets(cfg, bad_role)
        except media.MediaError as exc:
            assert "dr.enabled" in str(exc)
        else:
            raise AssertionError(f"--role {bad_role} without DR was accepted")
    try:
        media.resolve_targets(cfg, "everything")
    except media.MediaError as exc:
        assert "everything" in str(exc)
    else:
        raise AssertionError("an unknown role was accepted")


# ---------------------------------------------------------------------------
# deploy: the network phase ordering + skip behavior (no Azure calls)
# ---------------------------------------------------------------------------


def test_network_phase_is_first_and_skips_when_create_false() -> None:
    from ctmdeploy import deploy as deploymod

    assert deploymod.PHASES[0] == "network"
    cfg = _load_dict(_base_config())  # create defaults to false
    engine = deploymod.Engine(cfg, REPO_ROOT)
    results, has_changes = engine.run("network")
    assert results == [] and has_changes is False


def test_network_phase_params_match_bicep_contract() -> None:
    """The exact parameter names phase_network hands network/main.bicep (v4:
    the standalone network/ folder supersedes bicep/network.bicep and takes
    the RAW vnets shape)."""
    from ctmdeploy import deploy as deploymod

    cfg = _load_dict(_config_with_infrastructure())
    engine = deploymod.Engine(cfg, REPO_ROOT)
    captured: dict[str, Any] = {}

    def fake_deploy(scope, name, template, params, what_if):
        captured.update(scope=scope, name=name, template=template, params=params)
        return []

    engine._deploy = fake_deploy  # type: ignore[method-assign]
    engine.phase_network()
    from ctmdeploy.azure import SubscriptionScope

    assert captured["name"] == "ctm-nonprod-network"
    assert captured["template"] == "network/main.bicep"
    assert captured["scope"] == SubscriptionScope("eastus2")
    params = captured["params"]
    assert set(params) == {"vnets", "userAccessCidrs", "bastionCidrs", "tags"}
    assert params["userAccessCidrs"] == ["198.51.100.0/24"]
    assert params["bastionCidrs"] == ["203.0.113.0/28"]
    assert params["tags"]["managedBy"] == "bicep"
    assert [item["name"] for item in params["vnets"]] == ["vnet-util", "vnet-dev"]
    # Raw config shape: peer_to normalized to a list, no python-side resolution.
    assert params["vnets"][1]["peer_to"] == ["vnet-util"]
    assert "peerAddressSpaces" not in params["vnets"][0]
    # ...and the template path resolves from the repo root, not bicep/.
    assert engine._bicep("network/main.bicep") == REPO_ROOT / "network" / "main.bicep"
    assert engine._bicep("access.bicep") == REPO_ROOT / "bicep" / "access.bicep"


# ---------------------------------------------------------------------------
# Plain-python runner (pytest also discovers the test_* functions above)
# ---------------------------------------------------------------------------


def _run_all() -> int:
    passed = failed = skipped = 0
    for name, fn in sorted(globals().items()):
        if not (name.startswith("test_") and callable(fn)):
            continue
        try:
            fn()
        except unittest.SkipTest as skip:
            skipped += 1
            print(f"SKIP {name}: {skip}")
        except Exception:
            failed += 1
            print(f"FAIL {name}")
            traceback.print_exc()
        else:
            passed += 1
            print(f"PASS {name}")
    print(f"\n{Path(__file__).name}: {passed} passed, {failed} failed, {skipped} skipped")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
