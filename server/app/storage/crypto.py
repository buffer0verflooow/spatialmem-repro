"""敏感字段加解密：基于 Fernet 对称加密。

用于对数据库中的敏感字段（如 reply_content、ocr_text）做加密存储。
加密密钥通过 app/config.py 的 field_encryption_key 配置。
"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

from app.observability import get_logger

log = get_logger(__name__)


class FieldEncryptor:
    """字段级加解密器。

    使用 Fernet（AES-128-CBC + HMAC-SHA256）对字符串做加解密。
    """

    def __init__(self, key: str | bytes) -> None:
        """初始化加密器。

        Args:
            key: Fernet 密钥（base64 编码的 32 字节）
        """
        if isinstance(key, str):
            key = key.encode()
        self._f = Fernet(key)

    def encrypt(self, plaintext: str) -> bytes:
        """加密字符串。

        Args:
            plaintext: 待加密的明文字符串

        Returns:
            加密后的字节
        """
        return self._f.encrypt(plaintext.encode("utf-8"))

    def decrypt(self, ciphertext: bytes) -> str:
        """解密字节。

        Args:
            ciphertext: 加密后的字节

        Returns:
            解密后的明文字符串

        Raises:
            InvalidToken: 密钥错误或数据被篡改
        """
        try:
            return self._f.decrypt(ciphertext).decode("utf-8")
        except InvalidToken:
            log.error("field_decrypt_failed", hint="密钥错误或数据已篡改")
            raise

    @staticmethod
    def generate_key() -> str:
        """生成新的 Fernet 密钥（base64 编码）。

        Returns:
            适合存入 .env 的密钥字符串
        """
        return Fernet.generate_key().decode()


def create_encryptor(key: str) -> FieldEncryptor | None:
    """工厂函数：根据配置创建加密器，key 为空时返回 None。

    Args:
        key: Fernet 密钥字符串，空字符串表示不启用

    Returns:
        FieldEncryptor 实例或 None
    """
    if not key:
        return None
    try:
        return FieldEncryptor(key)
    except Exception:
        log.error("field_encryptor_init_failed", hint="field_encryption_key 格式错误")
        return None
