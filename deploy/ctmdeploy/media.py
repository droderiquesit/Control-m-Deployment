# ---------------------------------------------------------------------------
# Installer media mover: `ctmdeploy media upload` pushes the BMC EPD files +
# silent-install XMLs (downloaded from the Azure Artifacts universal
# packages by the pipeline, or staged manually) into the installers blob
# container of each platform (primary and, when dr.enabled, DR). The
# filename -> blob-prefix mapping is a pure function; azure-storage-blob is
# imported lazily inside upload() so the module stays importable offline.
# ---------------------------------------------------------------------------
"""Upload BMC installer media to the platform's installers blob containers.

``python -m ctmdeploy media upload --env <env> [--role primary|dr|both]
--path <downloaded-dir>`` walks the directory, maps every file to its blob
name and uploads it with overwrite enabled (nodes pull the media at first
boot via managed identity, so the VMs never change).

Mapping rule (the Azure Artifacts package layout is FLAT; see
docs/AZURE-DEVOPS.md):

* a path already prefixed ``em/...`` or ``server/...`` is uploaded verbatim
  (the pipeline may download each package into such a directory);
* otherwise the file maps by its name: ``DREM*`` and ``silent-em-*`` go to
  the ``em/`` prefix, ``DRSRV*`` and ``silent-server-*`` to ``server/``;
* anything else is skipped with a warning (never uploaded).

After the uploads a manifest is printed and every ``installer_blobs`` name
still missing from the container is reported as a WARNING, so a partial
media set is visible before the first VM boots against it.
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path, PureWindowsPath
from typing import TYPE_CHECKING, Any

from ctmdeploy.config import INSTALLERS_CONTAINER, EnvironmentConfig

if TYPE_CHECKING:  # pragma: no cover - typing only, no runtime Azure import
    from azure.core.credentials import TokenCredential

#: --role values accepted by `ctmdeploy media upload`.
ROLES = ("primary", "dr", "both")

#: Filename prefixes mapped to the `em/` blob prefix (EPD tarball + XMLs).
_EM_FILE_PREFIXES = ("DREM", "silent-em-")
#: Filename prefixes mapped to the `server/` blob prefix.
_SERVER_FILE_PREFIXES = ("DRSRV", "silent-server-")


class MediaError(RuntimeError):
    """The media upload cannot run (bad path, role, or ambiguous mapping)."""


# ---------------------------------------------------------------------------
# Pure helpers (no Azure, no filesystem)
# ---------------------------------------------------------------------------


def blob_name_for(relative_path: str) -> str | None:
    """Blob name (inside the installers container) for one media file.

    ``relative_path`` is the file's path relative to the upload root (either
    separator). Returns None for files the mapping does not cover (they are
    skipped, never uploaded). Pure function — unit-testable offline.
    """
    # PureWindowsPath splits on both separators, normalizing either input.
    parts = PureWindowsPath(relative_path).parts
    if not parts:
        return None
    # An already-prefixed tree (em/... or server/...) is kept verbatim.
    if parts[0] in ("em", "server") and len(parts) > 1:
        return "/".join(parts)
    # Flat files (or files under any other directory, e.g. silent_xml/) map
    # by filename to their component prefix.
    name = parts[-1]
    if name.startswith(_EM_FILE_PREFIXES):
        return f"em/{name}"
    if name.startswith(_SERVER_FILE_PREFIXES):
        return f"server/{name}"
    return None


def resolve_targets(cfg: EnvironmentConfig, role: str | None) -> list[tuple[str, str]]:
    """``(region_role, blob_account_name)`` targets for one upload run.

    role None/'' defaults to 'both' when dr.enabled, else 'primary' (the
    addendum rule). An explicit 'dr'/'both' without a DR region is an error.
    """
    resolved = role or ("both" if cfg.dr.enabled else "primary")
    if resolved not in ROLES:
        raise MediaError(f"--role must be one of {ROLES}, got {resolved!r}")
    targets: list[tuple[str, str]] = []
    if resolved in ("primary", "both"):
        targets.append(("primary", cfg.blob_account_name))
    if resolved in ("dr", "both"):
        if not cfg.dr.enabled:
            raise MediaError(
                f"--role {resolved} requires dr.enabled, but environment "
                f"{cfg.environment!r} has no DR region (use --role primary)"
            )
        targets.append(("dr", cfg.dr_blob_account_name))
    return targets


# ---------------------------------------------------------------------------
# Upload planning (filesystem, still no Azure)
# ---------------------------------------------------------------------------


def plan_uploads(root: Path) -> tuple[list[tuple[Path, str]], list[Path]]:
    """Walk ``root`` and map every file to its blob name.

    Returns ``(uploads, skipped)``: uploads is a sorted list of
    ``(local_file, blob_name)``; skipped lists the files the mapping does not
    cover. Two files mapping to the same blob name is an error (ambiguous
    media set) rather than a silent last-write-wins.
    """
    uploads: list[tuple[Path, str]] = []
    skipped: list[Path] = []
    seen: dict[str, Path] = {}
    for file in sorted(path for path in root.rglob("*") if path.is_file()):
        blob = blob_name_for(file.relative_to(root).as_posix())
        if blob is None:
            skipped.append(file)
            continue
        if blob in seen:
            raise MediaError(
                f"both {seen[blob]} and {file} map to blob {blob!r}; "
                "remove one of them (the media set is ambiguous)"
            )
        seen[blob] = file
        uploads.append((file, blob))
    return uploads, skipped


def _human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{int(size)} B"  # pragma: no cover - unreachable


# ---------------------------------------------------------------------------
# Upload (lazy azure-storage-blob import)
# ---------------------------------------------------------------------------


def upload(
    cfg: EnvironmentConfig,
    path: str | Path,
    *,
    role: str | None = None,
    credential: "TokenCredential | None" = None,
) -> int:
    """Upload the media under ``path`` to each target installers container.

    Uses managed identity / az login (DefaultAzureCredential) and always
    overwrites, so re-publishing a fixed media set converges. Prints the
    manifest and warns about every ``installer_blobs`` name still missing
    from a container after the run. Returns a process exit code (0).
    """
    root = Path(path)
    if not root.is_dir():
        raise MediaError(f"--path {root} is not a directory")
    uploads, skipped = plan_uploads(root)
    if not uploads:
        raise MediaError(
            f"no uploadable media found under {root} (expected EPD files/silent XMLs "
            "named DREM*/DRSRV*/silent-em-*/silent-server-*, or an em/ + server/ tree)"
        )
    targets = resolve_targets(cfg, role)

    print(f"Upload manifest ({len(uploads)} file(s) from {root}):")
    for local, blob in uploads:
        print(f"    {blob}  <-  {local.relative_to(root)}  ({_human_size(local.stat().st_size)})")
    for local in skipped:
        print(f"    WARNING: skipped {local.relative_to(root)} (no em/server prefix mapping)")

    if credential is None:
        from ctmdeploy.azure import get_credential

        credential = get_credential()
    from azure.storage.blob import BlobServiceClient

    expected: dict[str, str] = asdict(cfg.installer_blobs)
    for region_role, account in targets:
        container: Any = BlobServiceClient(
            account_url=f"https://{account}.blob.core.windows.net", credential=credential
        ).get_container_client(INSTALLERS_CONTAINER)
        print(f"==> uploading to {account}/{INSTALLERS_CONTAINER} ({region_role})")
        for local, blob in uploads:
            with local.open("rb") as handle:
                container.upload_blob(name=blob, data=handle, overwrite=True)
            print(f"    uploaded {blob}")
        # Completeness check: every blob name the cloud-init bootstrap will
        # request (config installer_blobs) must now exist in the container.
        uploaded = {blob for _, blob in uploads}
        for key, blob in sorted(expected.items()):
            if blob in uploaded or container.get_blob_client(blob).exists():
                continue
            print(
                f"    WARNING: installer_blobs.{key} = {blob!r} is still missing from "
                f"{account}/{INSTALLERS_CONTAINER}; nodes will fail to bootstrap until "
                "it is uploaded"
            )
    print(f"media upload ({', '.join(role_ for role_, _ in targets)}): done")
    return 0
