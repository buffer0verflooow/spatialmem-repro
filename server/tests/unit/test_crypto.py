"""敏感字段加解密单元测试。"""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from app.storage.crypto import FieldEncryptor, create_encryptor


class TestFieldEncryptor:
    """加解密正确性。"""

    @pytest.fixture
    def encryptor(self) -> FieldEncryptor:
        key = Fernet.generate_key()
        return FieldEncryptor(key)

    def test_roundtrip(self, encryptor: FieldEncryptor):
        """加密后解密应恢复原文。"""
        plaintext = "这是一条敏感的回复内容"
        ciphertext = encryptor.encrypt(plaintext)
        assert encryptor.decrypt(ciphertext) == plaintext

    def test_roundtrip_unicode(self, encryptor: FieldEncryptor):
        """Unicode 字符正确加解密。"""
        plaintext = "Hello 你好 🌍 こんにちは"
        ciphertext = encryptor.encrypt(plaintext)
        assert encryptor.decrypt(ciphertext) == plaintext

    def test_roundtrip_empty_string(self, encryptor: FieldEncryptor):
        """空字符串也能正确加解密。"""
        ciphertext = encryptor.encrypt("")
        assert encryptor.decrypt(ciphertext) == ""

    def test_ciphertext_differs_from_plaintext(self, encryptor: FieldEncryptor):
        """密文与明文不同。"""
        plaintext = "敏感数据"
        ciphertext = encryptor.encrypt(plaintext)
        assert ciphertext != plaintext.encode("utf-8")

    def test_ciphertext_is_bytes(self, encryptor: FieldEncryptor):
        """加密输出为 bytes 类型。"""
        ciphertext = encryptor.encrypt("test")
        assert isinstance(ciphertext, bytes)

    def test_different_plaintexts_different_ciphertexts(self, encryptor: FieldEncryptor):
        """不同明文产生不同密文。"""
        ct1 = encryptor.encrypt("text1")
        ct2 = encryptor.encrypt("text2")
        assert ct1 != ct2

    def test_wrong_key_decrypt_fails(self):
        """用错误的密钥解密应抛出异常。"""
        key1 = Fernet.generate_key()
        key2 = Fernet.generate_key()
        enc1 = FieldEncryptor(key1)
        enc2 = FieldEncryptor(key2)

        ciphertext = enc1.encrypt("secret data")
        with pytest.raises(Exception):
            enc2.decrypt(ciphertext)

    def test_tampered_ciphertext_fails(self, encryptor: FieldEncryptor):
        """篡改密文后解密应失败。"""
        ciphertext = encryptor.encrypt("test")
        tampered = ciphertext[:-1] + bytes([ciphertext[-1] ^ 0xFF])
        with pytest.raises(Exception):
            encryptor.decrypt(tampered)

    def test_string_key_accepted(self):
        """构造函数接受字符串密钥。"""
        key = Fernet.generate_key().decode()
        enc = FieldEncryptor(key)
        assert enc.decrypt(enc.encrypt("test")) == "test"

    def test_bytes_key_accepted(self):
        """构造函数接受 bytes 密钥。"""
        key = Fernet.generate_key()
        enc = FieldEncryptor(key)
        assert enc.decrypt(enc.encrypt("test")) == "test"


class TestGenerateKey:
    """密钥生成。"""

    def test_generate_returns_string(self):
        key = FieldEncryptor.generate_key()
        assert isinstance(key, str)

    def test_generated_key_works(self):
        key = FieldEncryptor.generate_key()
        enc = FieldEncryptor(key)
        assert enc.decrypt(enc.encrypt("test")) == "test"


class TestCreateEncryptor:
    """工厂函数。"""

    def test_empty_key_returns_none(self):
        assert create_encryptor("") is None

    def test_valid_key_returns_encryptor(self):
        key = Fernet.generate_key().decode()
        enc = create_encryptor(key)
        assert enc is not None
        assert isinstance(enc, FieldEncryptor)

    def test_invalid_key_returns_none(self):
        assert create_encryptor("not-a-valid-fernet-key") is None
