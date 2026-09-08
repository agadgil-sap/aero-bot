"""Platform-portable signing-key sourcing: Keychain, sealed env var, or key file.

The macOS Keychain source (``aero_bot.keychain``) remains the canonical
option on macOS. This module adds the two sealed sources that carry the same
discipline on Linux deployments and selects one through the same
environment-driven mechanism every command already uses:

- ``EnvSigningKeySource`` reads the 64-character hexadecimal key from the
  single environment variable ``AERO_BOT_SIGNING_KEY_HEX``. The variable is
  expected to be sealed by the process supervisor (a 0600
  ``EnvironmentFile=`` or a systemd credential), never exported by a shell
  profile, never committed, and never logged.
- ``FileSigningKeySource`` reads the key from an owner-only regular file
  (default ``~/.config/aero-bot/signing-key.hex``, overridable with
  ``AERO_BOT_SIGNING_KEY_FILE``). The file must carry mode 0600 or stricter
  (no group or other permission bits); anything looser refuses before a
  single byte of key material is read.

Selection: ``AERO_BOT_KEY_SOURCE`` selects ``keychain``, ``env``, or ``file``
explicitly; unset, macOS defaults to the Keychain and every other platform
prefers the sealed environment variable when present and falls back to the
key file.

The module holds the same four invariants the Keychain source does:

- Key bytes exist only in the return value of ``load_signing_key`` and must
  flow directly into exactly one signing call by the caller; no module,
  class, or global state ever stores, caches, or echoes them.
- Every validation error names at most the environment variable, the file
  path, or the secret's length - never any part of the secret itself.
- The file source refuses world- or group-readable storage before reading,
  so loose permissions can never become a leak through this module.
- Everything the module reports about the key it holds - to callers, audits,
  and operators - is the derived public address alone.
"""

import os
import stat
import sys
from collections.abc import Mapping
from pathlib import Path
from string import hexdigits
from typing import Protocol, runtime_checkable

from eth_account import Account

from aero_bot.domain import normalize_evm_address
from aero_bot.keychain import KeychainKeySource

# Environment variable selecting the key source: keychain, env, or file.
KEY_SOURCE_ENV = "AERO_BOT_KEY_SOURCE"
# Environment variable carrying the sealed hexadecimal signing key.
SIGNING_KEY_HEX_ENV = "AERO_BOT_SIGNING_KEY_HEX"
# Environment variable carrying an explicit key-file path.
SIGNING_KEY_FILE_ENV = "AERO_BOT_SIGNING_KEY_FILE"
# The explicit selector's only legal values.
KEY_SOURCE_CHOICES = frozenset({"keychain", "env", "file"})
# The default owner-only key file lives in the user's private config tree.
DEFAULT_KEY_FILE_PATH = Path.home() / ".config" / "aero-bot" / "signing-key.hex"
# A signing key is exactly 32 raw bytes, written as 64 hexadecimal characters.
KEY_HEX_LENGTH = 64
# The all-zero key is a placeholder, never a usable secp256k1 secret.
ZERO_KEY = b"\x00" * 32
# Owner-only permission mask: any group or other bit set on the key file refuses.
OWNER_ONLY_PERMISSION_MASK = 0o077


class SigningKeyUnavailableError(RuntimeError):
    """Signal that the configured key source could not yield a secret."""


class SigningKeyFormatError(ValueError):
    """Signal that the stored secret is not a usable 32-byte signing key."""


@runtime_checkable
class SigningKeySource(Protocol):
    """Define the boundary every signing-key source implements."""

    def load_signing_key(self) -> bytes:
        """Return exactly 32 raw key bytes for exactly one signing call."""
        ...

    def public_address(self) -> str:
        """Return only the derived public address of the held key."""
        ...


def parse_signing_key_secret(text: str) -> bytes:
    """Validate one textual secret and return exactly 32 raw key bytes.

    Args:
        text: The stripped secret text; a leading ``0x`` is optional.

    Returns:
        The 32 raw private-key bytes.

    Raises:
        SigningKeyFormatError: If the text is not exactly a 0x-optional
            64-character hexadecimal key, or is the all-zero placeholder.
    """
    stripped = text.strip()
    if stripped.startswith(("0x", "0X")):
        stripped = stripped[2:]
    if len(stripped) != KEY_HEX_LENGTH or any(character not in hexdigits for character in stripped):
        raise SigningKeyFormatError(
            "the signing-key secret is not a 64-character hexadecimal key; "
            f"the stored length was {len(stripped)} characters"
        )
    key = bytes.fromhex(stripped)
    if key == ZERO_KEY:
        raise SigningKeyFormatError(
            "the signing-key secret is the all-zero key and cannot be a signing key"
        )
    return key


def public_address_from_key(key: bytes) -> str:
    """Derive and return only the public address of raw key bytes.

    Args:
        key: Exactly 32 raw private-key bytes.

    Returns:
        The lowercase public address; no key material survives this call.
    """
    return normalize_evm_address(Account.from_key(key).address)


class EnvSigningKeySource:
    """Read the signing key from one sealed environment variable on demand."""

    def __init__(self, environ: Mapping[str, str]) -> None:
        """Hold the environment mapping; the key is read only on demand.

        Args:
            environ: The process environment mapping carrying the sealed
                hexadecimal key under ``AERO_BOT_SIGNING_KEY_HEX``.
        """
        self._environ = environ

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] = os.environ) -> "EnvSigningKeySource":
        """Build the sealed-variable source from one environment mapping.

        Args:
            environ: Environment mapping supplying the sealed key.

        Returns:
            A key source reading ``AERO_BOT_SIGNING_KEY_HEX`` on demand.
        """
        return cls(environ)

    def load_signing_key(self) -> bytes:
        """Read the sealed variable and return exactly 32 raw key bytes.

        Returns:
            The 32 raw private-key bytes.

        Raises:
            SigningKeyUnavailableError: If the variable is not set at all.
            SigningKeyFormatError: If the stored value is not a usable
                32-byte hexadecimal key.
        """
        if SIGNING_KEY_HEX_ENV not in self._environ:
            raise SigningKeyUnavailableError(
                f"the signing-key environment variable {SIGNING_KEY_HEX_ENV} is not set; "
                "seal the 64-character hexadecimal key there (0600 EnvironmentFile or "
                "systemd credential) or select another source via "
                f"{KEY_SOURCE_ENV}={{{','.join(sorted(KEY_SOURCE_CHOICES))}}}"
            )
        return parse_signing_key_secret(self._environ[SIGNING_KEY_HEX_ENV])

    def public_address(self) -> str:
        """Derive and return only the public address of the sealed key.

        Returns:
            The lowercase public address of the variable-held signing key.

        Raises:
            SigningKeyUnavailableError: If the variable is not set.
            SigningKeyFormatError: If the stored value is unusable.
        """
        return public_address_from_key(self.load_signing_key())


class FileSigningKeySource:
    """Read the signing key from one owner-only key file on demand."""

    def __init__(self, path: Path) -> None:
        """Point the source at one key-file path.

        Args:
            path: Absolute or home-relative path of the owner-only key file.
        """
        self._path = path.expanduser()

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] = os.environ) -> "FileSigningKeySource":
        """Build the key-file source from one environment mapping.

        Args:
            environ: Environment mapping optionally carrying an explicit
                path under ``AERO_BOT_SIGNING_KEY_FILE``.

        Returns:
            A key source reading the override path, or the documented
            default ``~/.config/aero-bot/signing-key.hex``.
        """
        override = environ.get(SIGNING_KEY_FILE_ENV, "").strip()
        return cls(Path(override) if override else DEFAULT_KEY_FILE_PATH)

    @property
    def path(self) -> Path:
        """Return the key-file path being read; paths are not secrets."""
        return self._path

    def load_signing_key(self) -> bytes:
        """Read the owner-only file and return exactly 32 raw key bytes.

        Returns:
            The 32 raw private-key bytes.

        Raises:
            SigningKeyUnavailableError: If the file is missing or is not a
                regular file.
            SigningKeyFormatError: If the file's content is not a usable
                32-byte hexadecimal key.
        """
        try:
            file_stat = self._path.stat()
        except FileNotFoundError as error:
            raise SigningKeyUnavailableError(
                f"the signing-key file {self._path} does not exist; create it "
                "owner-only with mode 0600 (see docs/execution.md for the "
                "setup procedure)"
            ) from error
        except OSError as error:
            raise SigningKeyUnavailableError(
                f"the signing-key file {self._path} could not be inspected: "
                f"{error.strerror or error}"
            ) from error
        if not stat.S_ISREG(file_stat.st_mode):
            raise SigningKeyUnavailableError(
                f"the signing-key path {self._path} is not a regular file"
            )
        if file_stat.st_mode & OWNER_ONLY_PERMISSION_MASK:
            raise SigningKeyUnavailableError(
                f"the signing-key file {self._path} is readable by group or "
                f"others (mode {stat.filemode(file_stat.st_mode)}); refusing to "
                "read it - run `chmod 600` on the file before use"
            )
        try:
            raw = self._path.read_bytes()
        except OSError as error:
            raise SigningKeyUnavailableError(
                f"the signing-key file {self._path} could not be read: {error.strerror or error}"
            ) from error
        return parse_signing_key_secret(raw.decode("ascii", "replace"))

    def public_address(self) -> str:
        """Derive and return only the public address of the file-held key.

        Returns:
            The lowercase public address of the file-held signing key.

        Raises:
            SigningKeyUnavailableError: If the file cannot be read.
            SigningKeyFormatError: If the file's content is unusable.
        """
        return public_address_from_key(self.load_signing_key())


def load_signing_key_source(
    environ: Mapping[str, str] = os.environ,
    platform: str = sys.platform,
) -> KeychainKeySource | EnvSigningKeySource | FileSigningKeySource:
    """Select and return the signing-key source for this environment.

    ``AERO_BOT_KEY_SOURCE`` selects ``keychain``, ``env``, or ``file``
    explicitly. Unset, macOS defaults to the Keychain; every other platform
    prefers the sealed environment variable when present and otherwise the
    owner-only key file.

    Args:
        environ: The process environment mapping driving selection.
        platform: The platform identifier; ``sys.platform`` by default.

    Returns:
        The selected key source; nothing has been read yet.

    Raises:
        ValueError: If ``AERO_BOT_KEY_SOURCE`` holds an unknown value.
    """
    explicit = environ.get(KEY_SOURCE_ENV, "").strip()
    if explicit:
        if explicit not in KEY_SOURCE_CHOICES:
            raise ValueError(
                f"{KEY_SOURCE_ENV} must be one of "
                f"{{{','.join(sorted(KEY_SOURCE_CHOICES))}}}, not {explicit!r}"
            )
        if explicit == "keychain":
            return KeychainKeySource.from_environment(environ)
        if explicit == "env":
            return EnvSigningKeySource.from_environment(environ)
        return FileSigningKeySource.from_environment(environ)
    if platform == "darwin":
        return KeychainKeySource.from_environment(environ)
    if SIGNING_KEY_HEX_ENV in environ:
        return EnvSigningKeySource.from_environment(environ)
    return FileSigningKeySource.from_environment(environ)
