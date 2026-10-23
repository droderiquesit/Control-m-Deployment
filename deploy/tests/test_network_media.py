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
    assert spoke.peer_to == "vnet-util"
    assert infra.spoke_address_spaces() == ["10.60.8.0/24"]


def test_vnets_param_resolves_peer_and_spoke_address_spaces() -> None:
    cfg = _load_dict(_config_with_infrastructure())
    items = {item["name"]: item for item in cfg.network_infrastructure.vnets_param()}
    utility, spoke = items["vnet-util"], items["vnet-dev"]
    # Config shape passed verbatim + the resolved address-space arrays.
    assert set(utility) == {
        "name",
        "resource_group_name",
        "location",
        "role",
        "peer_to",
        "address_space",
        "subnets",
        "peerAddressSpaces",
        "spokeAddressSpaces",
    }
    assert utility["peerAddressSpaces"] == ["10.60.8.0/24"]  # its peering spokes
    assert utility["spokeAddressSpaces"] == ["10.60.8.0/24"]  # ALL spokes
    assert spoke["peerAddressSpaces"] == ["10.60.0.0/22"]  # its utility VNet
    assert spoke["spokeAddressSpaces"] == []
    assert spoke["peer_to"] == "vnet-util" and utility["peer_to"] == ""
    assert utility["subnets"][0] == {"name": "snet-em", "prefix": "10.60.0.0/24", "role": "em"}


def test_create_false_keeps_block_inert() -> None:
    """With create false (BYO default) the block parses but is NOT validated:
    a stale peer_to or placement mismatch must not block a BYO deployment."""
    data = _config_with_infrastructure(create=False)
    data["network_infrastructure"]["vnets"][1]["peer_to"] = "vnet-gone"
    data["network_infrastructure"]["vnets"][0]["address_space"] = "10.60.0.0/22"
    cfg = _load_dict(data)
    assert cfg.network_infrastructure.create is False
    assert cfg.network_infrastructure.vnet("vnet-dev").peer_to == "vnet-gone"


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
    # Unknown target.
    data = _config_with_infrastructure()
    data["network_infrastructure"]["vnets"][1]["peer_to"] = "vnet-nope"
    _expect_config_error(data, "peer_to", "vnet-nope", "does not name a VNet")

    # Spoke without any peer_to.
    data = _config_with_infrastructure()
    del data["network_infrastructure"]["vnets"][1]["peer_to"]
    _expect_config_error(data, "vnets[vnet-dev]", "peer_to", "required")

    # peer_to on a utility VNet.
    data = _config_with_infrastructure()
    data["network_infrastructure"]["vnets"][0]["peer_to"] = "vnet-dev"
    _expect_config_error(data, "vnets[vnet-util]", "peer_to", "only valid on 'spoke'")

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
    assert items["vnet-dev-dr"]["peerAddressSpaces"] == ["10.80.0.0/22"]
    assert items["vnet-util-dr"]["peerAddressSpaces"] == ["10.80.9.0/24"]
    # Utility items carry ALL spoke address spaces (both regions).
    assert items["vnet-util-dr"]["spokeAddressSpaces"] == ["10.60.8.0/24", "10.80.9.0/24"]
    assert items["vnet-util"]["spokeAddressSpaces"] == ["10.60.8.0/24", "10.80.9.0/24"]


# ---------------------------------------------------------------------------
# The committed PoC plan (environments/<env>/config.yaml)
# ---------------------------------------------------------------------------

#: Binding v3 PoC address plan (non-overlapping across platforms).
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
    assert {v.name: v.address_space for v in infra.vnets} == _POC_PLAN["nonprod"]
    assert {v.name for v in infra.vnets if v.role == "utility"} == {"vnet-dev-utility"}
    for name in ("vnet-ctm-dev", "vnet-ctm-qa"):
        assert infra.vnet(name).peer_to == "vnet-dev-utility"
    items = {item["name"]: item for item in infra.vnets_param()}
    assert items["vnet-dev-utility"]["spokeAddressSpaces"] == ["10.60.8.0/24", "10.60.9.0/24"]
    assert items["vnet-ctm-qa"]["peerAddressSpaces"] == ["10.60.0.0/22"]


def test_poc_network_plan_validates_prod() -> None:
    cfg = _poc_config("prod")
    infra = cfg.network_infrastructure
    assert {v.name: v.address_space for v in infra.vnets} == _POC_PLAN["prod"]
    assert infra.vnet("vnet-ctm-prod-dr").peer_to == "vnet-dr-utility"
    assert infra.vnet("vnet-ctm-uat").peer_to == "vnet-prod-utility"
    # The utility subnets follow the PoC split (em /24, postgres /27, pe /26).
    utility = infra.vnet("vnet-prod-utility")
    assert {s.role: s.prefix for s in utility.subnets} == {
        "em": "10.70.0.0/24",
        "data": "10.70.1.0/27",
        "pe": "10.70.1.64/26",
    }


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


def test_media_block_requires_all_three_packages() -> None:
    data = _base_config()
    data["media"] = _media_block()
    del data["media"]["azure_artifacts"]["packages"]["silent_xml"]
    _expect_config_error(data, "media.azure_artifacts.packages", "silent_xml")


def test_media_block_rejects_unknown_packages() -> None:
    data = _base_config()
    data["media"] = _media_block()
    data["media"]["azure_artifacts"]["packages"]["agent"] = {"name": "x", "version": "1"}
    _expect_config_error(data, "media.azure_artifacts.packages", "unknown keys", "agent")


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
    """The exact parameter names phase_network hands bicep/network.bicep."""
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
    assert captured["template"] == "network.bicep"
    assert captured["scope"] == SubscriptionScope("eastus2")
    params = captured["params"]
    assert set(params) == {"vnets", "userAccessCidrs", "bastionCidrs", "tags"}
    assert params["userAccessCidrs"] == ["198.51.100.0/24"]
    assert params["bastionCidrs"] == ["203.0.113.0/28"]
    assert params["tags"]["managedBy"] == "bicep"
    assert [item["name"] for item in params["vnets"]] == ["vnet-util", "vnet-dev"]


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
