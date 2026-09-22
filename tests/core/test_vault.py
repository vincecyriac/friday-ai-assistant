import base64

import pytest

from friday.core.vault import MasterKeyError, Vault, VaultError


def test_generate_master_key_is_32_bytes_urlsafe():
    key = Vault.generate_master_key()
    assert len(base64.urlsafe_b64decode(key)) == 32
    assert Vault.generate_master_key() != key


@pytest.mark.parametrize("bad", ["", "not-base64!!", base64.b64encode(b"short").decode()])
def test_short_or_malformed_master_key_rejected(bad):
    with pytest.raises(MasterKeyError) as excinfo:
        Vault.from_master_key(bad)
    assert "FRIDAY_MASTER_KEY" in str(excinfo.value)


def test_standard_and_urlsafe_base64_both_accepted():
    raw = bytes(range(32))
    a = Vault.from_master_key(base64.b64encode(raw).decode())
    b = Vault.from_master_key(base64.urlsafe_b64encode(raw).decode())
    assert a.fingerprint() == b.fingerprint()
    assert len(a.fingerprint()) == 8


def test_roundtrip_and_token_format():
    v = Vault.from_master_key(Vault.generate_master_key())
    token = v.encrypt("llm.gemini_api_key", "s3cret")
    assert token.startswith("v1:") and token.count(":") == 2
    assert v.decrypt("llm.gemini_api_key", token) == "s3cret"
    assert v.encrypt("llm.gemini_api_key", "s3cret") != token      # fresh nonce every time


def test_wrong_master_key_fails():
    a = Vault.from_master_key(Vault.generate_master_key())
    b = Vault.from_master_key(Vault.generate_master_key())
    with pytest.raises(VaultError):
        b.decrypt("k", a.encrypt("k", "x"))


def test_setting_key_is_bound_as_aad():
    v = Vault.from_master_key(Vault.generate_master_key())
    token = v.encrypt("llm.tripo_api_key", "x")
    with pytest.raises(VaultError):
        v.decrypt("llm.gemini_api_key", token)


def test_tampered_ciphertext_fails():
    v = Vault.from_master_key(Vault.generate_master_key())
    version, nonce, ct = v.encrypt("k", "hello").split(":")
    raw = bytearray(base64.b64decode(ct))
    raw[0] ^= 0x01
    with pytest.raises(VaultError):
        v.decrypt("k", f"{version}:{nonce}:{base64.b64encode(bytes(raw)).decode()}")


@pytest.mark.parametrize("bad", ["", "v1:", "v1:abc", "v2:AAAA:BBBB", "plain text", "v1:!!:!!"])
def test_malformed_tokens_fail_cleanly(bad):
    v = Vault.from_master_key(Vault.generate_master_key())
    with pytest.raises(VaultError):
        v.decrypt("k", bad)
