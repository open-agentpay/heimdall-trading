"""
================================================================================
Heimdall 交易平台 - 安全加密金库与 Web3 钱包认证 (Security & Key Vault)
================================================================================

【小白速懂】：
1. 为什么要搞“去中心化钱包登录”？
   - 传统网站需要注册账号、记复杂密码，容易被脱裤泄漏；
   - Web3 钱包登录（EIP-4361 SIWE 标准）使用区块链非对称加密签名，不消耗任何 Gas 费；
   - 钱包私钥永远保留在用户本地小狐狸或 OKX 钱包中，服务器只验证签名合法性，极其安全！

2. 为什么要搞“本地安全金库 (Vault)”？
   - 量化交易需要配置交易所的 API Key（币安/OKX/Hyperliquid）和大模型 Key（Claude/OpenAI/DeepSeek）；
   - 如果明文存数据库，数据库文件一旦被盗，资金直接面临洗劫！
   - 我们使用银行级 AES-256-GCM 硬件认证加密，落盘数据库全部为密文乱码，只有配合本地主密钥才能解密！
"""

import os
import time
import json
import base64
import secrets
import logging
from typing import Dict, Any, Optional, Tuple
from pathlib import Path
from Crypto.Cipher import AES
from eth_account import Account
from eth_account.messages import encode_defunct

from heimdall.config import settings

logger = logging.getLogger(__name__)


def mask_secret(secret: str, prefix_len: int = 4, suffix_len: int = 4) -> str:
    """
    敏感密钥脱敏打码展示
    例如：'sk-ant-api03-abcdef1234567890' -> 'sk-ant-••••••••••••7890'
    """
    if not secret:
        return ""
    length = len(secret)
    if length <= (prefix_len + suffix_len):
        return "••••••••"
    
    # 针对 sk-ant- / sk- 等常见前缀进行智能保留
    prefix = secret[:prefix_len]
    suffix = secret[-suffix_len:]
    masked_middle = "•" * min(12, max(6, length - prefix_len - suffix_len))
    return f"{prefix}{masked_middle}{suffix}"


def generate_siwe_message(address: str, nonce: str, chain_id: int = 1) -> str:
    """
    生成符合 EIP-4361 (Sign-In with Ethereum) 规范的标准签名挑战文本
    """
    issued_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    msg = (
        f"heimdall.trade wants you to sign in with your Ethereum account:\n"
        f"{address}\n\n"
        f"Sign in to Heimdall AI Quant Platform. This request will not trigger any blockchain transaction or cost any gas fees.\n\n"
        f"URI: https://heimdall.trade\n"
        f"Version: 1\n"
        f"Chain ID: {chain_id}\n"
        f"Nonce: {nonce}\n"
        f"Issued At: {issued_at}"
    )
    return msg


def verify_wallet_signature(address: str, signature: str, message: str) -> bool:
    """
    校验以太坊 EIP-191 消息签名

    :param address: 用户的钱包公钥地址 (0x...)
    :param signature: 钱包客户端返回的 16 进制签名字符串 (0x...)
    :param message: 用户签名的原始明文挑战文本
    :return: 签名是否由该地址对应的私钥签署
    """
    try:
        signable_msg = encode_defunct(text=message)
        recovered_address = Account.recover_message(signable_msg, signature=signature)
        is_valid = recovered_address.lower() == address.lower()
        if not is_valid:
            logger.warning(
                "[Web3Auth] 签名验签失败！恢复地址 %s 与提交地址 %s 不匹配",
                recovered_address, address
            )
        return is_valid
    except Exception as e:
        logger.error("[Web3Auth] 验签异常报错: %s", str(e))
        return False


class CryptoVault:
    """
    AES-256-GCM 工业级安全密钥金库
    使用 GCM 带认证加密模式，防篡改、防伪造、防暴力破解。
    """

    def __init__(self, master_key_hex: Optional[str] = None):
        """
        初始化安全金库。优先读取配置中的 master_key；若无则自动在本地安全生成持久化主密钥。
        """
        self.master_key = self._resolve_master_key(master_key_hex)

    def _resolve_master_key(self, custom_hex: Optional[str]) -> bytes:
        """解析或自动派生 256 位 (32 字节) 主加密密钥"""
        if custom_hex and len(custom_hex) >= 64:
            try:
                return bytes.fromhex(custom_hex[:64])
            except ValueError:
                pass
        
        # 检查 settings 中是否存在
        if settings.master_key and len(settings.master_key) >= 64:
            try:
                return bytes.fromhex(settings.master_key[:64])
            except ValueError:
                pass

        # 本地自动持久化生成并存储在 data/.vault.key (权限建议只读)
        project_root = Path(__file__).resolve().parent.parent.parent
        key_file = project_root / "data" / ".vault.key"
        
        if key_file.exists():
            try:
                with open(key_file, "rb") as f:
                    raw_key = f.read().strip()
                if len(raw_key) == 32:
                    return raw_key
                elif len(raw_key) == 64:
                    return bytes.fromhex(raw_key.decode("utf-8"))
            except Exception as e:
                logger.warning("[CryptoVault] 读取本地密钥文件失败，将重新派生: %s", e)

        # 全新派生 32 字节随机熵
        new_key = secrets.token_bytes(32)
        try:
            key_file.parent.mkdir(parents=True, exist_ok=True)
            with open(key_file, "wb") as f:
                f.write(new_key)
            logger.info("[CryptoVault] 已为本机生成新的安全金库主密钥: %s", key_file)
        except Exception as e:
            logger.warning("[CryptoVault] 保存本地密钥文件失败 (内存保持可用): %s", e)

        return new_key

    def encrypt_payload(self, data_dict: Dict[str, Any]) -> str:
        """
        对任意 JSON 字典数据执行 AES-256-GCM 加密，返回安全 Base64 编码封包
        封包格式: base64( 12字节Nonce + 16字节Tag + 密文字节 )
        """
        json_bytes = json.dumps(data_dict, ensure_ascii=False).encode("utf-8")
        # 遵循 NIST SP 800-38D 标准：显式生成 12 字节独一无二 IV/Nonce
        nonce = secrets.token_bytes(12)
        cipher = AES.new(self.master_key, AES.MODE_GCM, nonce=nonce)
        ciphertext, tag = cipher.encrypt_and_digest(json_bytes)
        
        # 打包: Nonce (12B) + Tag (16B) + Ciphertext
        packed = nonce + tag + ciphertext
        return base64.b64encode(packed).decode("utf-8")

    def decrypt_payload(self, encrypted_b64: str) -> Dict[str, Any]:
        """
        解密 Base64 封包并校验 Tag 防篡改认证，恢复原始 JSON 字典
        """
        try:
            raw = base64.b64decode(encrypted_b64.encode("utf-8"))
            if len(raw) < 28:
                raise ValueError("加密封包长度异常，已遭截断")
            
            nonce = raw[:12]
            tag = raw[12:28]
            ciphertext = raw[28:]

            cipher = AES.new(self.master_key, AES.MODE_GCM, nonce=nonce)
            decrypted_bytes = cipher.decrypt_and_verify(ciphertext, tag)
            return json.loads(decrypted_bytes.decode("utf-8"))
        except Exception as e:
            logger.error("[CryptoVault] 解密校验失败 (可能密钥不匹配或数据遭篡改): %s", e)
            raise ValueError(f"凭证解密失败: {e}")


async def test_key_connectivity(provider: str, credentials: Dict[str, Any]) -> Tuple[bool, str, float]:
    """
    轻量级凭证连通性测试探针

    :param provider: 'binance', 'okx', 'hyperliquid', 'anthropic', 'openai', 'deepseek', 'gemini'
    :param credentials: 解密后的明文凭证
    :return: (is_success, message, latency_ms)
    """
    import aiohttp
    start_time = time.time()
    
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=4.0)) as session:
            # 1. Binance
            if provider == "binance":
                api_key = credentials.get("api_key", "")
                headers = {"X-MBX-APIKEY": api_key} if api_key else {}
                async with session.get("https://api.binance.com/api/v3/ping", headers=headers) as resp:
                    latency = round((time.time() - start_time) * 1000, 1)
                    if resp.status == 200:
                        return True, f"币安网关连通正常 (HTTP 200)", latency
                    return False, f"币安返回异常: {resp.status}", latency

            # 2. Hyperliquid
            elif provider == "hyperliquid":
                payload = {"type": "meta"}
                async with session.post("https://api.hyperliquid.xyz/info", json=payload) as resp:
                    latency = round((time.time() - start_time) * 1000, 1)
                    if resp.status == 200:
                        return True, f"Hyperliquid L1 节点响应正常", latency
                    return False, f"Hyperliquid 节点异常: {resp.status}", latency

            # 3. DeepSeek
            elif provider == "deepseek":
                api_key = credentials.get("api_key", "")
                headers = {"Authorization": f"Bearer {api_key}"}
                async with session.get("https://api.deepseek.com/models", headers=headers) as resp:
                    latency = round((time.time() - start_time) * 1000, 1)
                    if resp.status == 200:
                        return True, f"DeepSeek API 验证成功", latency
                    elif resp.status in (401, 403):
                        return False, f"API Key 无效或未授权 ({resp.status})", latency
                    return True, f"节点连通 (HTTP {resp.status})", latency

            # 4. Anthropic Claude
            elif provider == "anthropic":
                api_key = credentials.get("api_key", "")
                latency = round((time.time() - start_time) * 1000, 1)
                if api_key.startswith("sk-ant-"):
                    return True, "Anthropic 凭证格式有效，待命接入", latency
                return False, "Key 格式应以 sk-ant- 开头", latency

            # 5. OpenAI
            elif provider == "openai":
                api_key = credentials.get("api_key", "")
                latency = round((time.time() - start_time) * 1000, 1)
                if api_key.startswith("sk-"):
                    return True, "OpenAI 凭证格式有效", latency
                return False, "Key 格式应以 sk- 开头", latency

            # 兜底
            else:
                latency = round((time.time() - start_time) * 1000, 1)
                return True, f"{provider} 凭证已登记就绪", latency

    except Exception as e:
        latency = round((time.time() - start_time) * 1000, 1)
        return False, f"网络连接超时或故障: {str(e)[:50]}", latency


# 全局单例金库实例
crypto_vault = CryptoVault()
