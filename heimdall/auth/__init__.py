"""
Heimdall 认证与安全密钥金库模块 (Authentication & Key Vault)
提供 Web3 钱包去中心化登录 (SIWE)、AES-256-GCM 凭证加密存储与连通性自检。
"""

from heimdall.auth.vault import crypto_vault, mask_secret, verify_wallet_signature

__all__ = ["crypto_vault", "mask_secret", "verify_wallet_signature"]
