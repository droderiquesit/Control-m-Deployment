# ---------------------------------------------------------------------------
# Platform secrets: generation, external overrides (CTM_PLATFORM_SECRETS) and
# idempotent Key Vault seeding into every region's vault. Secrets are only
# created when absent or explicitly overridden — never rotated silently.
# Azure SDK imports are guarded inside the functions that need them.
# ---------------------------------------------------------------------------
"""Generate and seed the Control-M platform secrets.

Secret names (unchanged from the previous toolchain's per-environment setup):
``db-admin-password`` (32), ``em-db-password`` (32), ``em-admin-password``
(24), ``ctm-db-password-<stage>`` (32) per Control-M/Server and
``ssh-private-key`` (RSA-4096 PEM). Special characters are limited to ones
safe in XML and sed, exactly like the original password generation rules.
"""

from __future__ import annotations

import json
import os
import secrets as _stdlib_secrets
import string
from typing import TYPE_CHECKING, Any, Iterable, Mapping

from ctmdeploy.config import EnvironmentConfig

if TYPE_CHECKING:  # pragma: no cover - typing only, no runtime Azure import
    from azure.core.credentials import TokenCredential

#: Environment variable holding a JSON map of external overrides (DSV wiring).
#: Empty values are ignored so a pipeline can always export every key.
OVERRIDES_ENV_VAR = "CTM_PLATFORM_SECRETS"

#: Same charset as the original password generation (letters, digits, XML/sed-safe symbols).
PASSWORD_CHARS = string.ascii_letters + string.digits + "!#%^*-_=+"

SSH_PRIVATE_KEY = "ssh-private-key"


class SecretsError(RuntimeError):
    """Secret resolution or Key Vault seeding failed."""


# ---------------------------------------------------------------------------
# Pure helpers (no Azure)
# ---------------------------------------------------------------------------


def generate_password(length: int) -> str:
    """Cryptographically random password from the XML/sed-safe charset."""
    if length < 8:
        raise SecretsError(f"refusing to generate a password shorter than 8 ({length})")
    return "".join(_stdlib_secrets.choice(PASSWORD_CHARS) for _ in range(length))


def generate_private_key_pem() -> str:
    """RSA-4096 private key, PKCS#1 PEM (same shape as the TF tls provider)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=4096)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return pem.decode("ascii")


def ssh_public_key_from_pem(pem: str) -> str:
    """OpenSSH public key derived from a stored PEM private key."""
    from cryptography.hazmat.primitives import serialization

    try:
        key = serialization.load_pem_private_key(pem.encode("ascii"), password=None)
    except (ValueError, TypeError) as exc:
        raise SecretsError(f"ssh-private-key is not a valid unencrypted PEM key: {exc}") from exc
    public = key.public_key().public_bytes(
        encoding=serialization.Encoding.OpenSSH,
        format=serialization.PublicFormat.OpenSSH,
    )
    return public.decode("ascii")


def secret_specs(cfg: EnvironmentConfig) -> dict[str, int]:
    """Required secret names mapped to their generated length (0 = SSH key)."""
    specs: dict[str, int] = {
        "db-admin-password": 32,
        "em-db-password": 32,
        "em-admin-password": 24,
    }
    for server in cfg.servers.values():
        specs[server.secret_name] = 32
    specs[SSH_PRIVATE_KEY] = 0
    return specs


def secret_names(cfg: EnvironmentConfig) -> tuple[str, ...]:
    """All secret names the platform requires, in a stable order."""
    return tuple(secret_specs(cfg))


def vault_uris(cfg: EnvironmentConfig) -> list[str]:
    """Key Vault URIs to seed: primary, plus DR when dr.enabled."""
    uris = [cfg.key_vault_uri]
    if cfg.dr.enabled:
        uris.append(cfg.dr_key_vault_uri)
    return uris


def load_overrides(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """External secret overrides from CTM_PLATFORM_SECRETS (JSON object).

    Values that are empty strings are dropped, so CI can export every key
    unconditionally and only the populated ones win.
    """
    env = os.environ if environ is None else environ
    raw = env.get(OVERRIDES_ENV_VAR, "")
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SecretsError(f"{OVERRIDES_ENV_VAR} is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise SecretsError(f"{OVERRIDES_ENV_VAR} must be a JSON object of name -> value")
    overrides: dict[str, str] = {}
    for key, value in parsed.items():
        if value is None or value == "":
            continue
        if not isinstance(value, str):
            raise SecretsError(f"{OVERRIDES_ENV_VAR}[{key!r}] must be a string")
        overrides[str(key)] = value
    return overrides


def _generate(name: str, length: int) -> str:
    return generate_private_key_pem() if name == SSH_PRIVATE_KEY else generate_password(length)


# ---------------------------------------------------------------------------
# Key Vault access (lazy Azure imports)
# ---------------------------------------------------------------------------


def _secret_client(vault_uri: str, credential: "TokenCredential") -> Any:
    from azure.keyvault.secrets import SecretClient

    return SecretClient(vault_url=vault_uri, credential=credential)


def get_secret(vault_uri: str, name: str, credential: "TokenCredential") -> str | None:
    """Current value of one secret, or None when the secret does not exist.

    An unreachable vault (not deployed yet, DNS failure) also reads as None;
    authentication/authorization problems are raised so a permissions issue
    never silently produces a fresh secret.
    """
    from azure.core.exceptions import (
        ClientAuthenticationError,
        HttpResponseError,
        ResourceNotFoundError,
        ServiceRequestError,
    )

    client = _secret_client(vault_uri, credential)
    try:
        return client.get_secret(name).value
    except ResourceNotFoundError:
        return None
    except ServiceRequestError:
        # Vault endpoint does not resolve yet (foundation not deployed).
        return None
    except ClientAuthenticationError:
        raise
    except HttpResponseError as exc:
        if exc.status_code == 403:
            raise SecretsError(
                f"access to {vault_uri} secret {name!r} is forbidden; grant the deployer "
                "'Key Vault Secrets Officer' (or pass --deployer-object-id at deploy time)"
            ) from exc
        raise


def resolve(
    cfg: EnvironmentConfig,
    credential: "TokenCredential | None" = None,
    *,
    environ: Mapping[str, str] | None = None,
    read_vaults: bool = True,
) -> tuple[dict[str, str], set[str]]:
    """Resolve the full secret map for one environment.

    Precedence per name: explicit override from CTM_PLATFORM_SECRETS, then
    the value already stored in a region vault (primary first, so both
    regions stay identical), then a freshly generated value. Returns the
    values and the set of names that came from explicit overrides.
    """
    overrides = load_overrides(environ)
    specs = secret_specs(cfg)
    unknown = set(overrides) - set(specs)
    if unknown:
        raise SecretsError(
            f"{OVERRIDES_ENV_VAR} contains unknown secret names: {sorted(unknown)}; "
            f"expected a subset of {sorted(specs)}"
        )

    values: dict[str, str] = {}
    cred = credential
    for name, length in specs.items():
        if name in overrides:
            values[name] = overrides[name]
            continue
        existing: str | None = None
        if read_vaults:
            if cred is None:
                from ctmdeploy.azure import get_credential

                cred = get_credential()
            for uri in vault_uris(cfg):
                existing = get_secret(uri, name, cred)
                if existing is not None:
                    break
        values[name] = existing if existing is not None else _generate(name, length)
    return values, set(overrides)


def seed(
    cfg: EnvironmentConfig,
    values: Mapping[str, str],
    overridden: Iterable[str] = (),
    credential: "TokenCredential | None" = None,
) -> list[tuple[str, str, str]]:
    """Idempotently write the resolved secrets into every region vault.

    A secret is written only when it is absent from the vault, or when an
    explicit override differs from the stored value. Returns (vault, name,
    action) tuples with action in {'created', 'updated', 'unchanged'}.
    Secret values are never logged.
    """
    if credential is None:
        from ctmdeploy.azure import get_credential

        credential = get_credential()

    overridden_set = set(overridden)
    actions: list[tuple[str, str, str]] = []
    for uri in vault_uris(cfg):
        client = _secret_client(uri, credential)
        for name, value in values.items():
            current = get_secret(uri, name, credential)
            if current is None:
                client.set_secret(name, value)
                actions.append((uri, name, "created"))
            elif name in overridden_set and current != value:
                client.set_secret(name, value)
                actions.append((uri, name, "updated"))
            else:
                actions.append((uri, name, "unchanged"))
    return actions
