# ---------------------------------------------------------------------------
# argparse CLI: ctmdeploy {deploy,whatif,failover,image,render,secrets}.
# Pure-python commands (render) work without the Azure SDKs; the others
# import them lazily inside the handlers. Entry point: ctmdeploy.cli:main.
# ---------------------------------------------------------------------------
"""Command line interface for the Control-M deployment toolchain.

Examples:
    python -m ctmdeploy deploy --env prod --phase all --yes
    python -m ctmdeploy whatif --env nonprod            # prints has_changes=...
    python -m ctmdeploy failover --env prod --test
    python -m ctmdeploy image build --env prod --version 1.0.0
    python -m ctmdeploy render --env nonprod --role server --stage qa
    python -m ctmdeploy secrets --env prod

``whatif`` prints every change summary and ends stdout with a single
machine-readable line ``has_changes=true|false`` for pipelines to parse.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from ctmdeploy.config import ENVIRONMENTS, ConfigError, EnvironmentConfig, find_repo_root, load_config

#: Environment variable overriding the packer binary location.
PACKER_BIN_ENV_VAR = "CTM_PACKER_BIN"
#: Default object id granted Key Vault Secrets Officer at foundation time.
DEPLOYER_OBJECT_ID_ENV_VAR = "CTM_DEPLOYER_OBJECT_ID"


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--env", required=True, choices=ENVIRONMENTS, help="target environment (nonprod|prod)"
    )
    parser.add_argument(
        "--repo-root",
        default=None,
        help="repository root (default: auto-detected / CTM_REPO_ROOT)",
    )
    parser.add_argument(
        "--allow-placeholders",
        action="store_true",
        help="accept the placeholder subscription id (offline work only)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ctmdeploy",
        description="Deploy and operate the BMC Control-M platform on Azure (Bicep + Packer).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    deploy_p = sub.add_parser("deploy", help="run the deployment phases")
    _add_common_args(deploy_p)
    deploy_p.add_argument(
        "--phase",
        default="all",
        choices=("all", "rg", "foundation", "secrets", "compute", "dr", "replication"),
        help="phase to run (default: all)",
    )
    deploy_p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    deploy_p.add_argument(
        "--wait", action="store_true", help="block on long-running replication jobs"
    )
    deploy_p.add_argument(
        "--deployer-object-id",
        default=os.environ.get(DEPLOYER_OBJECT_ID_ENV_VAR, ""),
        help="AAD object id granted Key Vault Secrets Officer (or set "
        f"{DEPLOYER_OBJECT_ID_ENV_VAR})",
    )

    whatif_p = sub.add_parser(
        "whatif", help="run the same deployments in what-if mode (the pre-apply change preview)"
    )
    _add_common_args(whatif_p)
    whatif_p.add_argument(
        "--phase",
        default="all",
        choices=("all", "rg", "foundation", "compute", "dr"),
        help="phase to analyze (default: all ARM phases)",
    )
    whatif_p.add_argument(
        "--output", default=None, help="also write a JSON summary to this file"
    )
    whatif_p.add_argument(
        "--deployer-object-id",
        default=os.environ.get(DEPLOYER_OBJECT_ID_ENV_VAR, ""),
        help="AAD object id used in the foundation parameters",
    )

    failover_p = sub.add_parser("failover", help="DR failover (see docs/DR-RUNBOOK.md)")
    _add_common_args(failover_p)
    failover_p.add_argument(
        "--test", action="store_true", help="Site Recovery test failover only"
    )
    failover_p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")

    image_p = sub.add_parser("image", help="golden image operations (Packer / Compute Gallery)")
    image_sub = image_p.add_subparsers(dest="image_command", required=True)

    image_build_p = image_sub.add_parser(
        "build", help="build the ctm-rhel9 golden image with Packer (requires packer on PATH)"
    )
    _add_common_args(image_build_p)
    image_build_p.add_argument("--version", required=True, help="image version, e.g. 1.0.0")
    image_build_p.add_argument(
        "--var",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="extra -var passed through to packer (repeatable)",
    )

    image_gallery_p = image_sub.add_parser(
        "gallery", help="deploy bicep/images.bicep (Compute Gallery + image definition)"
    )
    _add_common_args(image_gallery_p)

    render_p = sub.add_parser("render", help="debug: print a rendered cloud-init script")
    _add_common_args(render_p)
    render_p.add_argument("--role", required=True, choices=("em", "server", "agent"))
    render_p.add_argument("--stage", default=None, help="server/agent stage (e.g. dev, uat)")
    render_p.add_argument(
        "--node-role", default="primary", choices=("primary", "secondary"), dest="node_role"
    )
    render_p.add_argument(
        "--dr", action="store_true", help="render with the DR region's derived names"
    )

    secrets_p = sub.add_parser("secrets", help="seed the Key Vault secrets only")
    _add_common_args(secrets_p)

    return parser


def _load(args: argparse.Namespace, *, allow_placeholders: bool | None = None) -> tuple[EnvironmentConfig, Path]:
    root = Path(args.repo_root) if args.repo_root else find_repo_root()
    allow = args.allow_placeholders if allow_placeholders is None else allow_placeholders
    cfg = load_config(args.env, root, allow_placeholders=allow)
    return cfg, root


def _confirm(prompt: str) -> bool:
    return input(prompt).strip() == "yes"


# ---------------------------------------------------------------------------
# Command handlers (lazy imports keep `render` usable without Azure SDKs)
# ---------------------------------------------------------------------------


def cmd_deploy(args: argparse.Namespace) -> int:
    from ctmdeploy.deploy import Engine

    cfg, root = _load(args)
    if not args.yes:
        prompt = (
            f"Deploy {cfg.name} (phase: {args.phase}) to subscription "
            f"{cfg.subscription_id}? [yes/NO] "
        )
        if not _confirm(prompt):
            print("aborted")
            return 1
    engine = Engine(cfg, root, deployer_object_id=args.deployer_object_id)
    engine.run(args.phase, what_if=False, wait=args.wait)
    print(f"deploy {cfg.name} (phase: {args.phase}): done")
    return 0


def cmd_whatif(args: argparse.Namespace) -> int:
    from ctmdeploy.deploy import Engine

    cfg, root = _load(args)
    engine = Engine(cfg, root, deployer_object_id=args.deployer_object_id)
    results, has_changes = engine.run(args.phase, what_if=True)
    summaries = [r.what_if for r in results if r.what_if is not None]
    if args.output:
        payload = {
            "environment": cfg.environment,
            "hasChanges": has_changes,
            "deployments": [
                {"name": s.deployment, "hasChanges": s.has_changes, "counts": s.counts}
                for s in summaries
            ],
        }
        Path(args.output).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        print(f"what-if summary written to {args.output}")
    # Final machine-readable line parsed by the pipelines (planHasChanges).
    print(f"has_changes={str(has_changes).lower()}")
    return 0


def cmd_failover(args: argparse.Namespace) -> int:
    from ctmdeploy import failover

    cfg, _ = _load(args)
    return failover.run(cfg, test=args.test, yes=args.yes)


def cmd_secrets(args: argparse.Namespace) -> int:
    from ctmdeploy import secrets as secrets_mod
    from ctmdeploy.azure import get_credential

    cfg, _ = _load(args)
    credential = get_credential()
    values, overridden = secrets_mod.resolve(cfg, credential)
    actions = secrets_mod.seed(cfg, values, overridden, credential=credential)
    for vault, name, action in actions:
        print(f"secret {name} @ {vault}: {action}")
    return 0


def _resolve_packer_bin() -> str:
    candidate = os.environ.get(PACKER_BIN_ENV_VAR) or shutil.which("packer")
    if not candidate:
        raise ConfigError(
            "packer not found on PATH; install it (https://developer.hashicorp.com/packer) "
            f"or set {PACKER_BIN_ENV_VAR}"
        )
    return candidate


def cmd_image_build(args: argparse.Namespace) -> int:
    cfg, root = _load(args, allow_placeholders=True)
    packer = _resolve_packer_bin()
    packer_dir = root / "packer"
    template = packer_dir / "ctm-rhel9.pkr.hcl"
    if not template.is_file():
        raise ConfigError(f"packer template not found: {template}")

    extra_vars: list[str] = []
    for item in args.var:
        if "=" not in item:
            raise ConfigError(f"--var expects KEY=VALUE, got {item!r}")
        extra_vars += ["-var", item]

    print(f"==> packer init {packer_dir}")
    subprocess.run([packer, "init", "."], cwd=packer_dir, check=True)
    build_cmd = [
        packer,
        "build",
        "-var",
        f"image_version={args.version}",
        *extra_vars,
        str(template.name),
    ]
    print(f"==> {' '.join(build_cmd)}")
    subprocess.run(build_cmd, cwd=packer_dir, check=True)
    print(
        f"image {cfg.image.image_definition} version {args.version} published to gallery "
        f"{cfg.image.gallery_name} ({cfg.image.gallery_resource_group})"
    )
    return 0


def cmd_image_gallery(args: argparse.Namespace) -> int:
    from ctmdeploy.azure import Deployer, ResourceGroupScope

    cfg, root = _load(args)
    deployer = Deployer(cfg.subscription_id)
    print(f"==> ensuring resource group {cfg.image.gallery_resource_group} ({cfg.location})")
    deployer.ensure_resource_group(cfg.image.gallery_resource_group, cfg.location, cfg.tags)
    result = deployer.deploy(
        ResourceGroupScope(cfg.image.gallery_resource_group),
        "ctm-images",
        root / "bicep" / "images.bicep",
        {
            "location": cfg.location,
            "galleryName": cfg.image.gallery_name,
            "imageDefinition": cfg.image.image_definition,
            "tags": dict(cfg.tags),
        },
    )
    print(f"gallery ready: {result.outputs.get('galleryId', cfg.image.gallery_name)}")
    return 0


def cmd_render(args: argparse.Namespace) -> int:
    # Rendering is offline by design: placeholder subscription ids are fine.
    from ctmdeploy.config import agent_pools, node_plan
    from ctmdeploy.deploy import (
        agent_custom_data,
        node_custom_data,
        placeholder_foundation_view,
    )

    cfg, root = _load(args, allow_placeholders=True)
    region_role = "dr" if args.dr else "primary"
    view = placeholder_foundation_view(cfg, region_role)
    template_dir = root / "cloud-init"

    if args.role == "agent":
        if not args.stage or args.stage not in cfg.servers:
            raise ConfigError(f"--stage must be one of {sorted(cfg.servers)} for --role agent")
        pool = next((p for p in agent_pools(cfg, region_role) if p.stage == args.stage), None)
        if pool is None:  # --dr with a server that is not in the DR opt-in set
            raise ConfigError(
                f"server {args.stage!r} has no agent pool in the {region_role} region "
                "(dr.server_networks is the DR opt-in set)"
            )
        print(agent_custom_data(cfg, view, pool, template_dir))
        return 0

    plan = node_plan(cfg, "primary")
    if args.role == "em":
        matches = [i for i in plan if i.component == "em" and i.role == args.node_role]
        if not matches:
            raise ConfigError(
                f"no EM node with role {args.node_role!r} (em.enable_secondary is "
                f"{cfg.em.enable_secondary})"
            )
    else:
        if not args.stage or args.stage not in cfg.servers:
            raise ConfigError(f"--stage must be one of {sorted(cfg.servers)} for --role server")
        matches = [
            i
            for i in plan
            if i.component == "server" and i.server_key == args.stage and i.role == args.node_role
        ]
        if not matches:
            raise ConfigError(
                f"no {args.stage} server node with role {args.node_role!r} "
                "(enable_secondary is false?)"
            )
    print(node_custom_data(cfg, view, matches[0], template_dir))
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; returns a process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    handlers = {
        "deploy": cmd_deploy,
        "whatif": cmd_whatif,
        "failover": cmd_failover,
        "secrets": cmd_secrets,
        "render": cmd_render,
    }
    try:
        if args.command == "image":
            if args.image_command == "build":
                return cmd_image_build(args)
            return cmd_image_gallery(args)
        return handlers[args.command](args)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except subprocess.CalledProcessError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.returncode or 1
    except Exception as exc:  # ConfigError, DeployError, AzureToolingError, ...
        from ctmdeploy.render import RenderError

        known: tuple[type[BaseException], ...] = (ConfigError, RenderError, RuntimeError)
        if isinstance(exc, known):
            print(f"error: {exc}", file=sys.stderr)
            return 2
        raise


if __name__ == "__main__":
    raise SystemExit(main())
