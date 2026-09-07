"""Behavior tests for the macOS Keychain signing-key source."""

import subprocess
from typing import Any, cast

import pytest
from eth_account import Account

from aero_bot.keychain import (
    DEFAULT_KEYCHAIN_ACCOUNT,
    DEFAULT_KEYCHAIN_SERVICE,
    KEYCHAIN_ACCOUNT_ENV,
    KEYCHAIN_SERVICE_ENV,
    SECURITY_TOOL_PATH,
    KeychainKeySource,
    KeychainSecretFormatError,
    KeychainUnavailableError,
)


class FakeSecurityRunner:
    """Record subprocess invocations and serve canned security-tool output."""

    def __init__(
        self,
        stdout: bytes = b"",
        stderr: bytes = b"",
        returncode: int = 0,
        raises: Exception | None = None,
    ) -> None:
        """Configure one canned outcome for every find-generic-password call.

        Args:
            stdout: Byte output the security tool would print.
            stderr: Byte diagnostic output.
            returncode: Process exit status.
            raises: Exception raised instead of returning a completed process.
        """
        self._stdout = stdout
        self._stderr = stderr
        self._returncode = returncode
        self._raises = raises
        self.calls: list[dict[str, Any]] = []

    def __call__(self, *args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        """Serve the canned outcome while recording the exact invocation."""
        self.calls.append({"args": args, "kwargs": kwargs})
        if self._raises is not None:
            raise self._raises
        argv = cast("list[str]", args[0])
        return subprocess.CompletedProcess(
            args=argv,
            returncode=self._returncode,
            stdout=self._stdout,
            stderr=self._stderr,
        )


def ephemeral_key_hex() -> str:
    """Create one ephemeral test key and return it as bare hex text."""
    return bytes(Account.create().key).hex()


def make_source(runner: FakeSecurityRunner) -> KeychainKeySource:
    """Build a key source over one fake security subprocess."""
    return KeychainKeySource(runner=runner)


def test_environment_defaults_apply_when_unset() -> None:
    """An empty environment selects the documented default names."""
    source = KeychainKeySource.from_environment(environ={})
    assert source.service == DEFAULT_KEYCHAIN_SERVICE
    assert source.account == DEFAULT_KEYCHAIN_ACCOUNT


def test_environment_overrides_apply_when_set() -> None:
    """Explicit environment variables override both Keychain names."""
    source = KeychainKeySource.from_environment(
        environ={KEYCHAIN_SERVICE_ENV: "other-service", KEYCHAIN_ACCOUNT_ENV: "other-account"}
    )
    assert source.service == "other-service"
    assert source.account == "other-account"


def test_environment_rejects_empty_or_padded_names() -> None:
    """Empty, padded, and control-character names fail before any subprocess."""
    with pytest.raises(ValueError, match="non-empty and unpadded"):
        KeychainKeySource.from_environment(environ={KEYCHAIN_SERVICE_ENV: ""})
    with pytest.raises(ValueError, match="non-empty and unpadded"):
        KeychainKeySource.from_environment(environ={KEYCHAIN_ACCOUNT_ENV: " padded "})
    with pytest.raises(ValueError, match="whitespace or control"):
        KeychainKeySource.from_environment(environ={KEYCHAIN_SERVICE_ENV: "inner space"})


def test_constructor_validates_names_and_timeout() -> None:
    """The constructor applies the same name rules and bounds the timeout."""
    with pytest.raises(ValueError, match="service"):
        KeychainKeySource(service="")
    with pytest.raises(ValueError, match="account"):
        KeychainKeySource(account="\tname")
    with pytest.raises(ValueError, match="timeout_seconds"):
        KeychainKeySource(timeout_seconds=0)


def test_load_signing_key_invokes_security_exactly() -> None:
    """The read uses the absolute tool path with documented flags only."""
    key_hex = ephemeral_key_hex()
    runner = FakeSecurityRunner(stdout=("0x" + key_hex + "\n").encode("ascii"))
    source = make_source(runner)
    assert source.load_signing_key() == bytes.fromhex(key_hex)
    assert runner.calls[0]["args"][0] == [
        SECURITY_TOOL_PATH,
        "find-generic-password",
        "-s",
        DEFAULT_KEYCHAIN_SERVICE,
        "-a",
        DEFAULT_KEYCHAIN_ACCOUNT,
        "-w",
    ]
    kwargs = runner.calls[0]["kwargs"]
    assert kwargs == {"capture_output": True, "timeout": 30.0, "check": False}


def test_load_signing_key_accepts_unprefixed_and_uppercase_hex() -> None:
    """Both bare and 0x-prefixed keys decode to the same raw bytes."""
    key_hex = ephemeral_key_hex().upper()
    runner = FakeSecurityRunner(stdout=(key_hex + "\r\n").encode("ascii"))
    assert make_source(runner).load_signing_key() == bytes.fromhex(key_hex.lower())


def test_load_signing_key_rejects_wrong_length_and_non_hex() -> None:
    """Anything but exactly 64 hexadecimal characters is refused."""
    runner = FakeSecurityRunner(stdout=b"0x" + b"a" * 63)
    with pytest.raises(KeychainSecretFormatError, match="64-character hexadecimal"):
        make_source(runner).load_signing_key()
    runner = FakeSecurityRunner(stdout=b"0x" + b"z" * 64)
    with pytest.raises(KeychainSecretFormatError, match="64-character hexadecimal"):
        make_source(runner).load_signing_key()


def test_load_signing_key_rejects_zero_placeholder() -> None:
    """The all-zero key is never treated as a usable signing key."""
    runner = FakeSecurityRunner(stdout=b"0x" + b"0" * 64)
    with pytest.raises(KeychainSecretFormatError, match="all-zero"):
        make_source(runner).load_signing_key()


def test_missing_secret_fails_with_actionable_secret_free_error() -> None:
    """A nonzero exit surfaces the bounded stderr without any secret bytes."""
    key_hex = ephemeral_key_hex()
    runner = FakeSecurityRunner(
        stdout=("0x" + key_hex).encode("ascii"),
        stderr=(
            b"security: SecKeychainSearchCopyNext: The specified item could not be "
            b"found in the keychain."
        ),
        returncode=44,
    )
    with pytest.raises(KeychainUnavailableError) as excinfo:
        make_source(runner).load_signing_key()
    message = str(excinfo.value)
    assert DEFAULT_KEYCHAIN_SERVICE in message
    assert DEFAULT_KEYCHAIN_ACCOUNT in message
    assert "exit status 44" in message
    assert "could not be found" in message
    assert "docs/execution.md" in message
    # The secret that stdout carried must never appear in the failure.
    assert key_hex not in message


def test_long_stderr_is_truncated_to_a_bounded_tail() -> None:
    """Diagnostics quote at most the trailing stderr characters."""
    runner = FakeSecurityRunner(
        stderr=b"HEAD-MARKER" + b"x" * 400 + b"meaningful tail", returncode=1
    )
    with pytest.raises(KeychainUnavailableError) as excinfo:
        make_source(runner).load_signing_key()
    message = str(excinfo.value)
    assert "meaningful tail" in message
    # The head of an oversized diagnostic is dropped entirely.
    assert "HEAD-MARKER" not in message
    assert "..." in message


def test_empty_stderr_reports_no_diagnostic() -> None:
    """A silent failure still produces a complete sentence."""
    runner = FakeSecurityRunner(returncode=1)
    with pytest.raises(KeychainUnavailableError, match="no diagnostic output"):
        make_source(runner).load_signing_key()


def test_empty_stdout_is_an_unavailable_failure() -> None:
    """An empty secret is a missing secret, not a format problem."""
    runner = FakeSecurityRunner(stdout=b"   \n")
    with pytest.raises(KeychainUnavailableError, match="empty secret"):
        make_source(runner).load_signing_key()


def test_missing_security_tool_fails_cleanly() -> None:
    """A missing security binary reports the macOS requirement."""
    runner = FakeSecurityRunner(raises=FileNotFoundError("security"))
    with pytest.raises(KeychainUnavailableError, match="requires macOS"):
        make_source(runner).load_signing_key()


def test_keychain_timeout_fails_cleanly() -> None:
    """A stalled Keychain prompt is bounded by the configured timeout."""
    runner = FakeSecurityRunner(raises=subprocess.TimeoutExpired(cmd="security", timeout=30.0))
    with pytest.raises(KeychainUnavailableError, match="timed out"):
        make_source(runner).load_signing_key()


def test_public_address_derives_and_discards_the_key() -> None:
    """Only the lowercase public address leaves the derivation call."""
    account = Account.create()
    runner = FakeSecurityRunner(stdout=("0x" + bytes(account.key).hex()).encode("ascii"))
    source = make_source(runner)
    assert source.public_address() == account.address.lower()
    # The read happens exactly once per derivation with no caching.
    assert len(runner.calls) == 1
    assert source.public_address() == account.address.lower()
    assert len(runner.calls) == 2
