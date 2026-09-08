"""Behavior tests for the platform-portable signing-key sources."""

import os
from pathlib import Path

import pytest
from eth_account import Account

from aero_bot.keychain import KeychainKeySource
from aero_bot.signing_key import (
    DEFAULT_KEY_FILE_PATH,
    KEY_SOURCE_CHOICES,
    KEY_SOURCE_ENV,
    SIGNING_KEY_FILE_ENV,
    SIGNING_KEY_HEX_ENV,
    EnvSigningKeySource,
    FileSigningKeySource,
    SigningKeyFormatError,
    SigningKeySource,
    SigningKeyUnavailableError,
    load_signing_key_source,
    parse_signing_key_secret,
    public_address_from_key,
)


def ephemeral_key_hex() -> str:
    """Create one ephemeral test key and return it as bare hex text."""
    return bytes(Account.create().key).hex()


def write_key_file(path: Path, content: str, mode: int = 0o600) -> Path:
    """Write one key file with an explicit permission mode."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="ascii")
    os.chmod(path, mode)
    return path


class TestParseSigningKeySecret:
    """The shared textual-secret validation mirrors the Keychain rules."""

    def test_valid_bare_hex_round_trips(self) -> None:
        """A bare 64-character hex key returns the exact raw bytes."""
        key_hex = ephemeral_key_hex()
        assert parse_signing_key_secret(key_hex) == bytes.fromhex(key_hex)

    def test_0x_prefix_and_surrounding_whitespace_are_tolerated(self) -> None:
        """A 0x-prefixed secret with file-style padding still parses."""
        key_hex = ephemeral_key_hex()
        assert parse_signing_key_secret(f"  0x{key_hex}\n") == bytes.fromhex(key_hex)

    def test_wrong_length_reports_only_the_length(self) -> None:
        """A short secret fails with the stored length, never the content."""
        with pytest.raises(SigningKeyFormatError, match="64-character") as error:
            parse_signing_key_secret("abcd")
        assert "abcd" not in str(error.value)

    def test_non_hex_characters_refuse(self) -> None:
        """Hex-length non-hex text fails closed."""
        with pytest.raises(SigningKeyFormatError, match="64-character"):
            parse_signing_key_secret("z" * 64)

    def test_all_zero_placeholder_refuses(self) -> None:
        """The all-zero key is never a usable signing key."""
        with pytest.raises(SigningKeyFormatError, match="all-zero"):
            parse_signing_key_secret("0" * 64)


class TestEnvSigningKeySource:
    """The sealed environment-variable source never echoes its value."""

    def test_valid_value_round_trips(self) -> None:
        """A valid sealed value returns the exact raw key bytes."""
        key_hex = ephemeral_key_hex()
        source = EnvSigningKeySource.from_environment(environ={SIGNING_KEY_HEX_ENV: key_hex})
        assert source.load_signing_key() == bytes.fromhex(key_hex)

    def test_public_address_matches_the_account(self) -> None:
        """The reported address equals the key's true public address."""
        account = Account.create()
        source = EnvSigningKeySource.from_environment(
            environ={SIGNING_KEY_HEX_ENV: bytes(account.key).hex()}
        )
        assert source.public_address() == account.address.lower()

    def test_unset_variable_names_the_variable_not_a_value(self) -> None:
        """A missing variable fails closed naming the expected variable."""
        with pytest.raises(SigningKeyUnavailableError, match=SIGNING_KEY_HEX_ENV) as error:
            EnvSigningKeySource.from_environment(environ={}).load_signing_key()
        message = str(error.value)
        assert KEY_SOURCE_ENV in message

    def test_invalid_value_error_never_contains_the_value(self) -> None:
        """A malformed value's error quotes its length, never the value."""
        source = EnvSigningKeySource.from_environment(environ={SIGNING_KEY_HEX_ENV: "not-hex"})
        with pytest.raises(SigningKeyFormatError) as error:
            source.load_signing_key()
        assert "not-hex" not in str(error.value)

    def test_satisfies_the_source_protocol(self) -> None:
        """The source implements the shared protocol boundary."""
        source = EnvSigningKeySource.from_environment(
            environ={SIGNING_KEY_HEX_ENV: ephemeral_key_hex()}
        )
        assert isinstance(source, SigningKeySource)


class TestFileSigningKeySource:
    """The owner-only key file refuses loose permissions before reading."""

    def test_mode_600_file_with_newline_round_trips(self, tmp_path: Path) -> None:
        """A 0600 file holding bare hex plus a trailing newline loads exactly."""
        key_hex = ephemeral_key_hex()
        path = write_key_file(tmp_path / "signing_key.hex", key_hex + "\n", mode=0o600)
        source = FileSigningKeySource(path)
        assert source.load_signing_key() == bytes.fromhex(key_hex)
        assert source.path == path

    def test_mode_400_file_is_stricter_and_allowed(self, tmp_path: Path) -> None:
        """Owner-read-only storage is stricter than 0600 and still loads."""
        key_hex = ephemeral_key_hex()
        path = write_key_file(tmp_path / "signing_key.hex", "0x" + key_hex, mode=0o400)
        source = FileSigningKeySource(path)
        assert source.load_signing_key() == bytes.fromhex(key_hex)

    def test_group_readable_file_refuses_with_chmod_guidance(self, tmp_path: Path) -> None:
        """A 0644 file refuses before any key byte is read."""
        key_hex = ephemeral_key_hex()
        path = write_key_file(tmp_path / "signing_key.hex", key_hex, mode=0o644)
        source = FileSigningKeySource(path)
        with pytest.raises(SigningKeyUnavailableError, match="chmod 600") as error:
            source.load_signing_key()
        assert key_hex not in str(error.value)

    def test_other_writable_file_refuses(self, tmp_path: Path) -> None:
        """World-writable storage refuses like group-readable storage."""
        path = write_key_file(tmp_path / "signing_key.hex", ephemeral_key_hex(), mode=0o602)
        with pytest.raises(SigningKeyUnavailableError, match="group or others"):
            FileSigningKeySource(path).load_signing_key()

    def test_missing_file_names_the_path(self, tmp_path: Path) -> None:
        """A missing file fails closed quoting the expected location."""
        source = FileSigningKeySource(tmp_path / "absent.hex")
        with pytest.raises(SigningKeyUnavailableError, match="does not exist") as error:
            source.load_signing_key()
        assert str(tmp_path / "absent.hex") in str(error.value)

    def test_directory_path_refuses_as_not_a_regular_file(self, tmp_path: Path) -> None:
        """A directory at the key path refuses as not a regular file."""
        with pytest.raises(SigningKeyUnavailableError, match="not a regular file"):
            FileSigningKeySource(tmp_path).load_signing_key()

    def test_invalid_content_fails_as_a_format_error(self, tmp_path: Path) -> None:
        """An owner-only file holding garbage fails with the format error."""
        path = write_key_file(tmp_path / "signing_key.hex", "garbage!", mode=0o600)
        with pytest.raises(SigningKeyFormatError, match="64-character"):
            FileSigningKeySource(path).load_signing_key()

    def test_environment_override_selects_the_path(self, tmp_path: Path) -> None:
        """``AERO_BOT_SIGNING_KEY_FILE`` overrides the default location."""
        path = write_key_file(tmp_path / "sealed.hex", ephemeral_key_hex(), mode=0o600)
        source = FileSigningKeySource.from_environment(environ={SIGNING_KEY_FILE_ENV: str(path)})
        assert source.path == path
        assert source.load_signing_key() == bytes.fromhex(path.read_text(encoding="ascii"))

    def test_environment_default_is_the_documented_config_path(self) -> None:
        """Without an override the documented default path is selected."""
        source = FileSigningKeySource.from_environment(environ={})
        assert source.path == DEFAULT_KEY_FILE_PATH

    def test_satisfies_the_source_protocol(self, tmp_path: Path) -> None:
        """The source implements the shared protocol boundary."""
        path = write_key_file(tmp_path / "signing_key.hex", ephemeral_key_hex(), mode=0o600)
        assert isinstance(FileSigningKeySource(path), SigningKeySource)


class TestLoadSigningKeySource:
    """The factory selects the source through the environment mechanism."""

    def test_explicit_keychain_selects_the_keychain(self) -> None:
        """``AERO_BOT_KEY_SOURCE=keychain`` builds the Keychain source."""
        source = load_signing_key_source(environ={KEY_SOURCE_ENV: "keychain"}, platform="darwin")
        assert isinstance(source, KeychainKeySource)

    def test_explicit_env_selects_the_sealed_variable(self) -> None:
        """``AERO_BOT_KEY_SOURCE=env`` builds the sealed-variable source."""
        source = load_signing_key_source(
            environ={KEY_SOURCE_ENV: "env", SIGNING_KEY_HEX_ENV: ephemeral_key_hex()},
            platform="linux",
        )
        assert isinstance(source, EnvSigningKeySource)

    def test_explicit_file_selects_the_key_file(self) -> None:
        """``AERO_BOT_KEY_SOURCE=file`` builds the key-file source."""
        source = load_signing_key_source(environ={KEY_SOURCE_ENV: "file"}, platform="linux")
        assert isinstance(source, FileSigningKeySource)

    def test_explicit_unknown_value_lists_the_choices(self) -> None:
        """An unknown selector fails closed enumerating every legal value."""
        with pytest.raises(ValueError, match="keychain") as error:
            load_signing_key_source(environ={KEY_SOURCE_ENV: "usb"}, platform="linux")
        message = str(error.value)
        assert KEY_SOURCE_ENV in message
        assert all(choice in message for choice in KEY_SOURCE_CHOICES)

    def test_blank_selector_behaves_as_unset(self) -> None:
        """A whitespace-only selector falls through to platform defaulting."""
        source = load_signing_key_source(environ={KEY_SOURCE_ENV: "  "}, platform="darwin")
        assert isinstance(source, KeychainKeySource)

    def test_darwin_defaults_to_the_keychain(self) -> None:
        """MacOS keeps its Keychain default even when other vars are set."""
        source = load_signing_key_source(
            environ={SIGNING_KEY_HEX_ENV: ephemeral_key_hex()}, platform="darwin"
        )
        assert isinstance(source, KeychainKeySource)

    def test_linux_prefers_the_sealed_variable_when_present(self) -> None:
        """Non-darwin platforms prefer the sealed variable over the file."""
        source = load_signing_key_source(
            environ={SIGNING_KEY_HEX_ENV: ephemeral_key_hex()}, platform="linux"
        )
        assert isinstance(source, EnvSigningKeySource)

    def test_linux_falls_back_to_the_key_file(self) -> None:
        """Without the sealed variable the owner-only file is selected."""
        source = load_signing_key_source(environ={}, platform="linux")
        assert isinstance(source, FileSigningKeySource)


class TestPublicAddressHelper:
    """The address helper reports addresses and nothing else."""

    def test_address_is_lowercase_and_matches_the_account(self) -> None:
        """The helper returns the account's normalized address."""
        account = Account.create()
        assert public_address_from_key(bytes(account.key)) == account.address.lower()
