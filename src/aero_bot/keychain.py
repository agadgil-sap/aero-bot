"""macOS Keychain sourcing for the canary execution signing key.

This module is the only component in the application that reads the bot
owner's private key. It shells out to the macOS ``security`` tool with
``find-generic-password -s SERVICE -a ACCOUNT -w`` and returns exactly 32 raw
key bytes; the service and account names come from the environment
(``AERO_BOT_KEYCHAIN_SERVICE`` defaulting to ``aero-bot`` and
``AERO_BOT_KEYCHAIN_ACCOUNT`` defaulting to ``bot-key``).

The module holds four invariants that contain the key material:

- Key bytes exist only in the return value of ``load_signing_key`` and must
  flow directly into exactly one signing call by the caller; no module,
  class, or global state ever stores, caches, or echoes them.
- The subprocess is invoked with the absolute path ``/usr/bin/security`` so a
  hijacked ``PATH`` cannot substitute a different binary.
- Every exception and diagnostic quotes at most a bounded tail of the
  command's ``stderr`` and never any part of its ``stdout``, which is where
  the secret arrives.
- Everything the module reports about the key it holds - to callers, audits,
  and operators - is the derived public address alone.
"""

import os
import subprocess
from collections.abc import Callable, Mapping
from string import hexdigits

from eth_account import Account

from aero_bot.domain import normalize_evm_address

# The macOS security tool has a fixed installation path; invoking it absolutely
# prevents PATH substitution from redirecting the key read.
SECURITY_TOOL_PATH = "/usr/bin/security"
# Environment variable carrying the Keychain service name.
KEYCHAIN_SERVICE_ENV = "AERO_BOT_KEYCHAIN_SERVICE"
# Environment variable carrying the Keychain account name.
KEYCHAIN_ACCOUNT_ENV = "AERO_BOT_KEYCHAIN_ACCOUNT"
# Default Keychain service used when the environment does not override it.
DEFAULT_KEYCHAIN_SERVICE = "aero-bot"
# Default Keychain account used when the environment does not override it.
DEFAULT_KEYCHAIN_ACCOUNT = "bot-key"
# Thirty seconds leaves room for the one-time Keychain access prompt a user
# must approve before the secret is released to this process.
KEYCHAIN_TIMEOUT_SECONDS = 30.0
# A signing key is exactly 32 raw bytes, written as 64 hexadecimal characters.
KEY_HEX_LENGTH = 64
# The all-zero key is a placeholder, never a usable secp256k1 secret.
ZERO_KEY = b"\x00" * 32
# Error diagnostics quote at most this many trailing stderr characters.
STDERR_DIAGNOSTIC_LIMIT = 200


class KeychainUnavailableError(RuntimeError):
    """Signal that the Keychain read could not complete or found no secret."""


class KeychainSecretFormatError(ValueError):
    """Signal that the stored secret is not a usable 32-byte signing key."""


# The subprocess boundary: a callable with subprocess.run's semantics.
SecurityCommandRunner = Callable[..., subprocess.CompletedProcess[bytes]]


def _clean_name(value: str, description: str) -> str:
    """Validate one Keychain name from the environment.

    Args:
        value: Raw environment value for the service or account name.
        description: Human label used in the validation error.

    Returns:
        The name when it is a non-empty string without padding.

    Raises:
        ValueError: If the name is empty, padded, or contains control bytes.
    """
    if value == "" or value != value.strip():
        raise ValueError(f"Keychain {description} name must be non-empty and unpadded")
    if any(
        character.isspace() or ord(character) < 32 or ord(character) == 127 for character in value
    ):
        raise ValueError(
            f"Keychain {description} name must not contain whitespace or control characters"
        )
    return value


class KeychainKeySource:
    """Read the canary signing key from the macOS Keychain on demand."""

    def __init__(
        self,
        service: str = DEFAULT_KEYCHAIN_SERVICE,
        account: str = DEFAULT_KEYCHAIN_ACCOUNT,
        timeout_seconds: float = KEYCHAIN_TIMEOUT_SECONDS,
        runner: SecurityCommandRunner = subprocess.run,
    ) -> None:
        """Configure one Keychain read boundary.

        Args:
            service: Keychain generic-password service name.
            account: Keychain generic-password account name.
            timeout_seconds: Bound on one Keychain subprocess read.
            runner: Subprocess runner, injectable for deterministic tests.

        Raises:
            ValueError: If any name is empty or the timeout is not positive.
        """
        self._service = _clean_name(service, "service")
        self._account = _clean_name(account, "account")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._timeout_seconds = timeout_seconds
        self._runner = runner

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] = os.environ,
        timeout_seconds: float = KEYCHAIN_TIMEOUT_SECONDS,
        runner: SecurityCommandRunner = subprocess.run,
    ) -> "KeychainKeySource":
        """Build the key source from the process environment.

        Args:
            environ: Environment mapping supplying the optional overrides.
            timeout_seconds: Bound on one Keychain subprocess read.
            runner: Subprocess runner, injectable for deterministic tests.

        Returns:
            A key source configured with the environment's names or defaults.
        """
        return cls(
            service=environ.get(KEYCHAIN_SERVICE_ENV, DEFAULT_KEYCHAIN_SERVICE),
            account=environ.get(KEYCHAIN_ACCOUNT_ENV, DEFAULT_KEYCHAIN_ACCOUNT),
            timeout_seconds=timeout_seconds,
            runner=runner,
        )

    @property
    def service(self) -> str:
        """Return the Keychain service name being read."""
        return self._service

    @property
    def account(self) -> str:
        """Return the Keychain account name being read."""
        return self._account

    def load_signing_key(self) -> bytes:
        """Read the stored secret and return exactly 32 raw key bytes.

        The returned bytes are the only form in which key material leaves
        this module, and the caller must pass them into exactly one signing
        call without storing them anywhere else.

        Returns:
            The 32 raw private-key bytes.

        Raises:
            KeychainUnavailableError: If the security tool is missing, times
                out, exits nonzero, or returns no secret at all.
            KeychainSecretFormatError: If the stored secret is not a 32-byte
                hexadecimal key or is the all-zero placeholder.
        """
        argv = [
            SECURITY_TOOL_PATH,
            "find-generic-password",
            "-s",
            self._service,
            "-a",
            self._account,
            "-w",
        ]
        try:
            completed = self._runner(
                argv,
                capture_output=True,
                timeout=self._timeout_seconds,
                check=False,
            )
        except FileNotFoundError as error:
            raise KeychainUnavailableError(
                "the macOS security tool was not found at "
                f"{SECURITY_TOOL_PATH}; the Keychain key source requires macOS"
            ) from error
        except subprocess.TimeoutExpired as error:
            raise KeychainUnavailableError(
                f"Keychain lookup for service {self._service!r} account "
                f"{self._account!r} timed out after {self._timeout_seconds} seconds"
            ) from error
        if completed.returncode != 0:
            raise KeychainUnavailableError(
                f"Keychain lookup for service {self._service!r} account "
                f"{self._account!r} failed with exit status {completed.returncode}: "
                f"{self._stderr_tail(completed.stderr)}. Store the 32-byte signing "
                "key as a hexadecimal secret under this service and account "
                "(see docs/execution.md for the setup procedure)"
            )
        secret = completed.stdout.strip()
        if secret == b"":
            raise KeychainUnavailableError(
                f"Keychain lookup for service {self._service!r} account "
                f"{self._account!r} returned an empty secret"
            )
        return self._parse_key_secret(secret)

    def public_address(self) -> str:
        """Derive and return only the public address of the stored key.

        The key bytes are read, converted to an address, and discarded; no
        key material survives this call.

        Returns:
            The lowercase public address of the Keychain-held signing key.

        Raises:
            KeychainUnavailableError: If the underlying read cannot complete.
            KeychainSecretFormatError: If the stored secret is unusable.
        """
        key = self.load_signing_key()
        address = Account.from_key(key).address
        return normalize_evm_address(address)

    def _parse_key_secret(self, secret: bytes) -> bytes:
        """Decode the validated Keychain output into raw key bytes.

        Args:
            secret: Stripped stdout bytes holding the stored hexadecimal key.

        Returns:
            The 32 raw private-key bytes.

        Raises:
            KeychainSecretFormatError: If the secret is not exactly a
                0x-optional 64-character hexadecimal key, or is all zeros.
        """
        text = secret.decode("ascii", "replace")
        if text.startswith(("0x", "0X")):
            text = text[2:]
        if len(text) != KEY_HEX_LENGTH or any(character not in hexdigits for character in text):
            raise KeychainSecretFormatError(
                "the Keychain secret is not a 64-character hexadecimal signing key; "
                "the stored length was "
                f"{len(text)} characters"
            )
        key = bytes.fromhex(text)
        if key == ZERO_KEY:
            raise KeychainSecretFormatError(
                "the Keychain secret is the all-zero key and cannot be a signing key"
            )
        return key

    @staticmethod
    def _stderr_tail(stderr: bytes) -> str:
        """Bound the diagnostic excerpt of the security tool's stderr.

        Args:
            stderr: Raw captured stderr bytes.

        Returns:
            A compact, secret-free tail suitable for an error message.
        """
        text = stderr.decode("utf-8", "replace").strip()
        if len(text) > STDERR_DIAGNOSTIC_LIMIT:
            text = "..." + text[-STDERR_DIAGNOSTIC_LIMIT:]
        return text if text else "no diagnostic output"
