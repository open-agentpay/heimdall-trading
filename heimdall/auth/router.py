"""
================================================================================
Heimdall 交易平台 - 钱包认证与安全金库路由 (Auth & Vault API Router)
================================================================================

提供：
1. Web3 钱包挑战码生成与 SIWE 签名核验 (/api/v1/auth/nonce, /api/v1/auth/verify)
2. 敏感凭证 (API Keys / 私钥) 密文托管与脱敏列表 (/api/v1/vault/keys)
3. 凭证一键测通连通性探针 (/api/v1/vault/keys/{id}/test)
"""

import time
import uuid
import secrets
import logging
from typing import Dict, Any, List, Optional
from fastapi import APIRouter, HTTPException, Query, Header, Depends, status
from pydantic import BaseModel, Field

from heimdall.database.db import db_manager
from heimdall.auth.vault import (
    crypto_vault,
    mask_secret,
    generate_siwe_message,
    verify_wallet_signature,
    test_key_connectivity,
    extract_credential_scope,
    normalize_exchange_scope,
)
from heimdall.auth.dependencies import require_authenticated_wallet

logger = logging.getLogger(__name__)

auth_router = APIRouter(prefix="/api/v1", tags=["Authentication & Key Vault"])


# ==============================================================================
# 凭证热重载（2026-11-22 修复）
# ==============================================================================
# 新建 / 编辑 / 激活 / 删除密钥后，让运行中的机器人立即改用新的激活密钥。
# 实现在 heimdall/engine/fleet/credentials.py（CredentialReloader），这里只负责判断何时触发。
# - 数据库写入已经成功，热重载失败只记 WARN，不影响接口返回；
# - bot_manager 在函数内导入：auth 路由被 heimdall.api.server 导入时，api.runtime 可能尚未加载。

#: 被视为 LLM 密钥的服务商（与本文件激活互斥 SQL、LLMService 检索条件保持一致）
LLM_PROVIDERS = ("deepseek", "anthropic", "openai", "gemini", "custom", "openai_compatible")


def _is_llm_key(category: Optional[str], provider: Optional[str]) -> bool:
    """category=llms，或服务商属于 LLM_PROVIDERS"""
    return (category or "").strip().lower() == "llms" or (provider or "").strip().lower() in LLM_PROVIDERS


async def _hot_reload_llm(wallet: str, reason: str) -> None:
    """强制重载该钱包名下运行中机器人的全部 LLM 服务（失败只告警）"""
    try:
        from heimdall.api.runtime import bot_manager

        await bot_manager.reload_llm_credentials(wallet, reason=reason)
    except Exception as exc:
        logger.warning("[凭证热重载] 刷新钱包 %s 的 LLM 凭证失败（原因=%s）: %s", wallet, reason, exc)


async def _hot_reload_exchange(wallet: str, provider: str, reason: str) -> None:
    """重新注入该钱包名下对应交易所机器人的凭证（失败只告警）"""
    try:
        from heimdall.api.runtime import bot_manager

        await bot_manager.reload_exchange_credentials(wallet, provider, reason=reason)
    except Exception as exc:
        logger.warning("[凭证热重载] 刷新钱包 %s 的 %s 交易所凭证失败（原因=%s）: %s", wallet, provider, reason, exc)


# ==============================================================================
# 请求与响应模型 (Pydantic Schemas)
# ==============================================================================

class VerifySignatureRequest(BaseModel):
    """钱包验签请求体"""
    address: str = Field(..., description="钱包公钥地址 (0x...)", example="0x71C...49b2")
    signature: str = Field(..., description="EIP-191 personal_sign 签名 hex 串", example="0x...")
    message: str = Field(..., description="签名所对应的完整明文挑战文本")


class CreateKeyRequest(BaseModel):
    """新增凭证请求体（归属钱包由 Bearer 会话强制绑定，防越权伪造）"""
    wallet_address: Optional[str] = Field(default=None, description="已废弃，后端强制以 Bearer 鉴权钱包地址为准")
    category: str = Field(..., description="分类: 'exchange' 或 'llm'", example="exchange")
    provider: str = Field(..., description="服务商: 'hyperliquid', 'binance', 'okx', 'anthropic', 'openai', 'deepseek', 'gemini'", example="hyperliquid")
    label: str = Field(..., description="自定义备注名称", example="Hyperliquid 主账户 API Agent")
    payload: Dict[str, Any] = Field(..., description="明文凭证内容 (后端加密后立即销毁明文)")


class DirectTestKeyRequest(BaseModel):
    """直接测试连通性请求体 (未保存前预检)"""
    provider: str = Field(..., description="服务商标识")
    payload: Dict[str, Any] = Field(..., description="待测试的凭证内容")


class UpdateKeyNetworkRequest(BaseModel):
    """切换交易所密钥所属网络环境（测试网/模拟盘 vs 正式网）"""
    is_testnet: bool = Field(..., description="True 表示测试网/模拟交易密钥，False 表示正式网密钥")


class UpdateKeyRequest(BaseModel):
    """更新凭据配置请求体 (支持修改 model, base_url, label 以及密钥内容)"""
    label: Optional[str] = Field(default=None, description="自定义别名备注")
    model: Optional[str] = Field(default=None, description="LLM 模型代码 (如 deepseek-chat, gpt-4o)")
    base_url: Optional[str] = Field(default=None, description="自定义 API 端点 Base URL")
    api_key: Optional[str] = Field(default=None, description="更新明文 API Key (留空或空串则保持原密钥)")
    secret_key: Optional[str] = Field(default=None, description="更新明文 Secret Key (留空或空串则保持原密钥)")


# ==============================================================================
# 钱包登录鉴权路由 (Web3 SIWE Auth)
# ==============================================================================

@auth_router.get("/auth/nonce")
async def get_wallet_nonce(
    address: str = Query(..., description="用户钱包地址", example="0x71C6707375d742e33f001A83e4cf6b8f399E49b2"),
    chain_id: int = Query(default=1, description="EVM 链 ID，默认 1 (以太坊主网/Arbitrum)"),
) -> Dict[str, Any]:
    """
    【步骤 1】：获取钱包登录挑战文本与防重放 Nonce
    """
    clean_address = address.strip()
    if not clean_address.startswith("0x") or len(clean_address) != 42:
        raise HTTPException(status_code=400, detail="钱包地址格式不合法，须为 42 位 0x 开头 EVM 地址")

    # 生成 16 字节安全随机 Nonce
    nonce = secrets.token_hex(8)
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    
    # 持久化或更新至数据库
    upsert_sql = """
    INSERT INTO user_wallets (address, nonce, chain_id, created_at)
    VALUES (?, ?, ?, ?)
    ON CONFLICT(address) DO UPDATE SET
        nonce = excluded.nonce,
        chain_id = excluded.chain_id;
    """
    await db_manager.execute_commit(upsert_sql, (clean_address.lower(), nonce, chain_id, now_iso))

    siwe_message = generate_siwe_message(clean_address, nonce, chain_id=chain_id)
    return {
        "address": clean_address,
        "nonce": nonce,
        "chain_id": chain_id,
        "message": siwe_message,
    }


@auth_router.post("/auth/verify")
async def verify_wallet_signature_endpoint(req: VerifySignatureRequest) -> Dict[str, Any]:
    """
    【步骤 2】：提交钱包签名进行密码学校验，核验通过后颁发 Session Token
    """
    clean_address = req.address.strip().lower()
    
    # 1. 查询数据库中预留的 Nonce
    rows = await db_manager.execute_query(
        "SELECT nonce FROM user_wallets WHERE address = ?",
        (clean_address,)
    )
    if not rows:
        raise HTTPException(status_code=404, detail="未找到该钱包地址的登录挑战，请重新获取 Nonce")

    expected_nonce = rows[0]["nonce"]
    if expected_nonce not in req.message:
        raise HTTPException(status_code=400, detail="签名内容中的 Nonce 已失效或被篡改，请刷新重试")

    # 2. 校验签名
    is_valid = verify_wallet_signature(clean_address, req.signature, req.message)
    if not is_valid:
        raise HTTPException(status_code=401, detail="钱包签名验证不通过，请核对是否使用当前钱包私钥签署")

    # 3. 登录成功，生成 Session Token 并记录时间戳
    session_token = f"hmd_sess_{uuid.uuid4().hex}"
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    
    # 更新登录状态并轮换 Nonce (一次性防重放)
    new_nonce = secrets.token_hex(8)
    await db_manager.execute_commit(
        """
        UPDATE user_wallets
        SET session_token = ?, last_login_at = ?, nonce = ?
        WHERE address = ?
        """,
        (session_token, now_iso, new_nonce, clean_address)
    )

    short_addr = f"{clean_address[:6]}...{clean_address[-4:]}"
    return {
        "success": True,
        "session_token": session_token,
        "wallet": {
            "address": clean_address,
            "short_address": short_addr,
            "last_login_at": now_iso,
        },
        "message": "钱包签名验证成功，已建立安全会话",
    }


# ==============================================================================
# 安全金库凭证管理路由 (API Key Vault)
# ==============================================================================

@auth_router.get("/vault/keys")
async def list_vault_keys(
    caller_wallet: str = Depends(require_authenticated_wallet),
) -> List[Dict[str, Any]]:
    """
    查询当前已鉴权钱包保存的加密凭证脱敏列表（强制走 Bearer Token 校验，禁止凭 URL 越权读取）。
    仅提取非敏感的 is_testnet 环境标记，绝不返回 encrypted_data 或明文密钥。
    """
    sql = "SELECT key_id, wallet_address, category, provider, label, key_mask, encrypted_data, is_active, last_tested_at, last_test_status, last_test_latency_ms, created_at, updated_at FROM user_api_keys WHERE LOWER(wallet_address) = ? ORDER BY created_at DESC"
    rows = await db_manager.execute_query(sql, (caller_wallet,))
    sanitized: List[Dict[str, Any]] = []
    for r in rows:
        item = dict(r)
        enc = item.pop("encrypted_data", None)
        prov = str(item.get("provider") or "").lower()
        is_testnet = prov == "binance_demo"
        cred_mt, cred_em = None, None
        if enc:
            try:
                creds = crypto_vault.decrypt_payload(enc)
                cred_mt, cred_em = extract_credential_scope(prov, creds)
                if cred_em:
                    is_testnet = cred_em == "TESTNET"
                elif prov in ("binance", "binance_demo"):
                    if "is_testnet" in creds:
                        is_testnet = bool(creds.get("is_testnet"))
                    elif str(creds.get("mode", "")).lower() in ("demo", "testnet"):
                        is_testnet = True
                if item.get("category") == "llms" or prov in ("custom", "openai_compatible", "deepseek", "openai", "anthropic", "gemini"):
                    item["model"] = creds.get("model") or ""
                    item["base_url"] = creds.get("base_url") or ""
            except Exception:
                pass
        item["market_type"] = cred_mt
        item["execution_mode"] = cred_em
        item["is_testnet"] = is_testnet
        item["is_active"] = bool(item.get("is_active", 1))
        sanitized.append(item)
    logger.info("[密钥金库查询] 钱包=%s | 返回脱敏凭证条数=%d", caller_wallet, len(sanitized))
    return sanitized


def _identity_mask(payload: Dict[str, Any]) -> str:
    """
    生成用于列表展示的脱敏标识。
    优先使用可公开的标识（API Key 公钥、钱包地址）；只提供了私钥时（如 Hyperliquid 未填地址），
    由私钥推导出钱包地址再打码，绝不截取私钥本身的字符展示。
    """
    public_id = payload.get("api_key") or payload.get("agent_wallet") or payload.get("wallet_address")
    if public_id:
        return mask_secret(str(public_id))
    private_key = payload.get("private_key") or payload.get("secret_key")
    if private_key:
        try:
            from eth_account import Account
            return mask_secret(Account.from_key(str(private_key).strip()).address)
        except Exception:
            return "••••••••"
    return ""


@auth_router.post("/vault/keys")
async def create_vault_key(
    req: CreateKeyRequest,
    caller_wallet: str = Depends(require_authenticated_wallet),
) -> Dict[str, Any]:
    """
    新增或保存加密凭证 (AES-256-GCM 认证加密入库，强制绑定至 Bearer 鉴权钱包地址)。
    """
    key_id = f"key_{uuid.uuid4().hex[:10]}"
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    # 智能提取关键识别信息并打码
    mask = _identity_mask(req.payload)

    # 交易所凭证显式写入 market_type / execution_mode（否则建机器人时精确匹配会失败）
    payload_to_store = normalize_exchange_scope(req.provider, req.payload)

    # 执行 AES-256-GCM 加密
    encrypted_str = crypto_vault.encrypt_payload(payload_to_store)

    is_active = 1
    if req.category == "llms":
        set_active = req.payload.get("set_active", True)
        if set_active:
            # 设为默认激活密钥：将该钱包下其他 LLM 密钥先置为备用 (is_active=0)
            await db_manager.execute_commit(
                "UPDATE user_api_keys SET is_active = 0, updated_at = ? WHERE wallet_address = ? AND (category = 'llms' OR provider IN ('deepseek', 'anthropic', 'openai', 'gemini', 'custom'))",
                (now_iso, caller_wallet),
            )
            is_active = 1
        else:
            is_active = 0

    sql = """
    INSERT INTO user_api_keys (
        key_id, wallet_address, category, provider, label, key_mask, encrypted_data, is_active, created_at, updated_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    await db_manager.execute_commit(
        sql,
        (
            key_id,
            caller_wallet,
            req.category,
            req.provider.lower(),
            req.label,
            mask,
            encrypted_str,
            is_active,
            now_iso,
            now_iso,
        )
    )
    logger.info(
        "[密钥金库入库] 钱包=%s | key_id=%s | 分类=%s | 服务商=%s | 别名=%s",
        caller_wallet, key_id, req.category, req.provider.lower(), req.label,
    )

    # 新建并设为激活的 LLM 密钥：运行中的机器人立即改用它
    if is_active and _is_llm_key(req.category, req.provider):
        await _hot_reload_llm(caller_wallet, reason=f"新建并激活 LLM 密钥 {key_id}")

    return {
        "success": True,
        "key_id": key_id,
        "label": req.label,
        "key_mask": mask,
        "category": req.category,
        "provider": req.provider.lower(),
        "created_at": now_iso,
    }


# 预置演示与内置接入点元数据 (用于新用户引导与免配置探针)
PRESET_DEMO_KEYS = {
    "key_hl_01": {"provider": "hyperliquid", "label": "Hyperliquid L1 (推荐底座)"},
    "key_binance_01": {"provider": "binance", "label": "Binance Futures (币安合约)"},
    "key_claude_01": {"provider": "anthropic", "label": "Anthropic Claude 3.5 Sonnet"},
    "key_deepseek_01": {"provider": "deepseek", "label": "DeepSeek V3 / R1 (深度求索)"},
}


@auth_router.patch("/vault/keys/{key_id}/network")
async def update_vault_key_network(
    key_id: str,
    req: UpdateKeyNetworkRequest,
    caller_wallet: str = Depends(require_authenticated_wallet),
) -> Dict[str, Any]:
    """
    切换已有金库凭证的网络环境（测试网/模拟盘 <-> 正式网）：
    严格校验所有权（防 BOLA 越权），更新密文元数据后重新 AES-256-GCM 加密落盘。
    """
    rows = await db_manager.execute_query(
        "SELECT wallet_address, provider, encrypted_data FROM user_api_keys WHERE key_id = ?",
        (key_id,),
    )
    if not rows:
        raise HTTPException(status_code=404, detail=f"未找到 ID 为 '{key_id}' 的密钥凭证")

    row = rows[0]
    owner = (row.get("wallet_address") or "").strip().lower()
    if owner and owner != caller_wallet:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="越权操作被拒绝：该安全凭证属于其他钱包用户，禁止修改其网络环境",
        )

    try:
        creds = crypto_vault.decrypt_payload(row["encrypted_data"])
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"凭证解密失败: {exc}")

    prov = str(row.get("provider") or "").lower()
    if prov == "binance_demo" and not req.is_testnet:
        raise HTTPException(status_code=400, detail="Binance Spot Demo 只有测试环境，不能切换为正式网")
    creds["is_testnet"] = bool(req.is_testnet)
    creds["mode"] = "demo" if req.is_testnet else "mainnet"
    # 凭证里显式写着 execution_mode 时它优先于 is_testnet，必须一起更新，否则切换不生效
    creds["execution_mode"] = "TESTNET" if req.is_testnet else "MAINNET"
    creds = normalize_exchange_scope(prov, creds)
    new_encrypted = crypto_vault.encrypt_payload(creds)
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    await db_manager.execute_commit(
        "UPDATE user_api_keys SET encrypted_data = ?, updated_at = ? WHERE key_id = ?",
        (new_encrypted, now_iso, key_id),
    )
    mode_label = "测试网/模拟盘" if req.is_testnet else "正式网"
    logger.info(
        "[密钥环境切换] 钱包=%s | key_id=%s | provider=%s | 环境=%s",
        caller_wallet, key_id, row.get("provider"), mode_label,
    )
    return {
        "success": True,
        "key_id": key_id,
        "provider": row.get("provider"),
        "is_testnet": bool(req.is_testnet),
        "mode": creds["mode"],
        "message": f"已将该密钥切换为「{mode_label}」环境",
    }


@auth_router.patch("/vault/keys/{key_id}")
async def update_vault_key(
    key_id: str,
    req: UpdateKeyRequest,
    caller_wallet: str = Depends(require_authenticated_wallet),
) -> Dict[str, Any]:
    """
    更新加密凭据的配置信息 (model, base_url, label 及可选密钥内容)。
    【防越权加固】：严格核验所有权，非凭据所属钱包禁止跨租户修改！
    """
    rows = await db_manager.execute_query(
        "SELECT wallet_address, category, provider, label, key_mask, encrypted_data, is_active FROM user_api_keys WHERE key_id = ?",
        (key_id,),
    )
    if not rows:
        raise HTTPException(status_code=404, detail="目标凭据不存在或已被销毁")

    row = dict(rows[0])
    owner = (row.get("wallet_address") or "").strip().lower()
    if owner and owner != caller_wallet:
        raise HTTPException(status_code=403, detail="越权拦截：禁止修改非本人钱包所属的凭据")

    creds = crypto_vault.decrypt_payload(row["encrypted_data"]) if row.get("encrypted_data") else {}

    # 1. 更新 model
    if req.model is not None:
        creds["model"] = req.model.strip()

    # 2. 更新 base_url
    if req.base_url is not None:
        raw_url = req.base_url.strip()
        for suffix in ("/chat/completions", "/chat/completions/", "/completions", "/completions/"):
            if raw_url.endswith(suffix):
                raw_url = raw_url[:-len(suffix)]
                break
        creds["base_url"] = raw_url.rstrip("/")

    # 3. 更新 label
    new_label = req.label.strip() if req.label is not None else row.get("label", "")

    # 4. 可选更新 key 内容
    new_mask = row.get("key_mask", "••••••••••••••••")
    if req.api_key and req.api_key.strip():
        ak = req.api_key.strip()
        creds["api_key"] = ak
        new_mask = ak[:4] + "••••" + ak[-4:] if len(ak) > 8 else "••••••••"
    if req.secret_key and req.secret_key.strip():
        sk = req.secret_key.strip()
        creds["secret_key"] = sk
        creds["private_key"] = sk

    new_encrypted = crypto_vault.encrypt_payload(creds)
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    await db_manager.execute_commit(
        "UPDATE user_api_keys SET label = ?, key_mask = ?, encrypted_data = ?, updated_at = ? WHERE key_id = ?",
        (new_label, new_mask, new_encrypted, now_iso, key_id),
    )

    # 若当前密钥处于激活生效状态，触发热重载：
    # - LLM 密钥：刷新名下机器人的全部 LLM 服务（含按角色路由的分析师 / 辩论 / 裁判模型）；
    # - 交易所密钥：通过 configure_credentials 重建对应交易所机器人的 SDK 客户端（同一账户换新 API 密钥）
    if bool(row.get("is_active")):
        if _is_llm_key(row.get("category"), row.get("provider")):
            await _hot_reload_llm(caller_wallet, reason=f"编辑激活中的 LLM 密钥 {key_id}")
        else:
            await _hot_reload_exchange(
                caller_wallet, str(row.get("provider") or ""), reason=f"编辑激活中的交易所密钥 {key_id}",
            )

    logger.info(
        "[密钥配置更新] 钱包=%s | key_id=%s | model=%s | base_url=%s",
        caller_wallet, key_id, creds.get("model"), creds.get("base_url"),
    )

    return {
        "success": True,
        "key_id": key_id,
        "label": new_label,
        "model": creds.get("model", ""),
        "base_url": creds.get("base_url", ""),
        "key_mask": new_mask,
        "message": "凭据配置已成功更新",
    }


@auth_router.post("/vault/keys/{key_id}/activate")
async def activate_vault_key(
    key_id: str,
    caller_wallet: str = Depends(require_authenticated_wallet),
) -> Dict[str, Any]:
    """
    将指定密钥设为当前激活使用的凭证（在多个大模型密钥之间无缝切换当前活跃模型）
    """
    rows = await db_manager.execute_query(
        "SELECT key_id, wallet_address, category, provider, label FROM user_api_keys WHERE key_id = ?",
        (key_id,),
    )
    if not rows:
        raise HTTPException(status_code=404, detail=f"未找到 ID 为 '{key_id}' 的密钥凭证")

    row = rows[0]
    owner = (row.get("wallet_address") or "").strip().lower()
    if owner and owner != caller_wallet:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="越权操作被拒绝：该安全凭证属于其他钱包用户，禁止切换其激活状态",
        )

    category = row.get("category")
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    # 若为 LLM 密钥，单选激活互斥（同一钱包下同一时间只有一个 LLM 激活生效）
    if category == "llms":
        await db_manager.execute_commit(
            "UPDATE user_api_keys SET is_active = 0, updated_at = ? WHERE wallet_address = ? AND (category = 'llms' OR provider IN ('deepseek', 'anthropic', 'openai', 'gemini', 'custom'))",
            (now_iso, caller_wallet),
        )

    await db_manager.execute_commit(
        "UPDATE user_api_keys SET is_active = 1, updated_at = ? WHERE key_id = ?",
        (now_iso, key_id),
    )

    # 切换激活的 LLM 密钥：运行中的机器人立即改用新密钥。
    # （修复前这里导入了不存在的 heimdall.engine.bot_manager.bot_manager，热重载从未执行）
    if _is_llm_key(category, row.get("provider")):
        await _hot_reload_llm(caller_wallet, reason=f"切换激活 LLM 密钥 {key_id}")
    else:
        # 交易所密钥决定机器人在哪个交易所账户交易，运行中切换会让独立账本与账户对不上，因此不热切换
        logger.info(
            "[密钥激活] 交易所密钥 %s 已设为激活；运行中的机器人继续使用当前已注入的凭证，重启机器人后生效", key_id,
        )

    logger.info("[密钥激活] 钱包=%s | 成功激活凭据: key_id=%s, label=%s, provider=%s", caller_wallet, key_id, row.get("label"), row.get("provider"))
    return {
        "success": True,
        "key_id": key_id,
        "is_active": True,
        "message": f"凭证【{row.get('label')}】已成功设为当前默认生效密钥",
    }


@auth_router.delete("/vault/keys/{key_id}")
async def delete_vault_key(
    key_id: str,
    caller_wallet: str = Depends(require_authenticated_wallet),
) -> Dict[str, Any]:
    """
    撤销并彻底物理销毁指定的凭证
    【防越权加固】：严格核验所有权，非凭据所属钱包禁止跨租户物理删除！
    """
    rows = await db_manager.execute_query(
        "SELECT wallet_address, category, provider, is_active FROM user_api_keys WHERE key_id = ?",
        (key_id,)
    )
    if rows:
        owner = (rows[0].get("wallet_address") or "").strip().lower()
        if owner and owner != caller_wallet:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="越权操作被拒绝：该安全凭证属于其他钱包用户，禁止跨租户销毁",
            )
        await db_manager.execute_commit(
            "DELETE FROM user_api_keys WHERE key_id = ?",
            (key_id,)
        )
        # 删除的是激活中的 LLM 密钥：运行中的机器人立即停止使用它（改用其他激活密钥或本地算法兜底）
        if bool(rows[0].get("is_active")) and _is_llm_key(rows[0].get("category"), rows[0].get("provider")):
            await _hot_reload_llm(caller_wallet, reason=f"删除激活中的 LLM 密钥 {key_id}")
        return {"success": True, "message": f"凭证 {key_id} 已安全销毁"}

    if key_id in PRESET_DEMO_KEYS or key_id.startswith("key_"):
        return {"success": True, "message": f"预置凭证 {key_id} 已移除"}
    raise HTTPException(status_code=404, detail="未找到对应的凭证或已被销毁")


@auth_router.post("/vault/keys/{key_id}/test")
async def test_saved_vault_key(
    key_id: str,
    caller_wallet: str = Depends(require_authenticated_wallet),
) -> Dict[str, Any]:
    """
    测试凭证的实时连通性与接口延时：
    【防越权加固】：严格校验归属权，若为其他用户私有凭据直接 403 阻断，杜绝借用他人 Key 盗刷探测；
    1. 优先查库核验所有权后解密真实 API Key 发起测试；
    2. 若为预置演示节点或未持久化的凭证，自适应执行服务商连通性探针，杜绝 404 报错。
    """
    rows = await db_manager.execute_query(
        "SELECT wallet_address, provider, encrypted_data FROM user_api_keys WHERE key_id = ?",
        (key_id,)
    )

    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    # 1. 真实入库凭证：解密并执行测试 (严格核验所有权)
    if rows:
        row = rows[0]
        owner = (row.get("wallet_address") or "").strip().lower()
        if owner and owner != caller_wallet:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="越权访问被拒绝：该安全凭证属于其他钱包用户，禁止未经授权进行接口连通性测试",
            )

        provider = row["provider"]
        encrypted_data = row["encrypted_data"]

        try:
            credentials = crypto_vault.decrypt_payload(encrypted_data)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"凭证解密失败: {e}")

        is_ok, msg, latency = await test_key_connectivity(provider, credentials)
        status_str = "OK" if is_ok else "FAILED"

        # 更新测试结果入库
        await db_manager.execute_commit(
            """
            UPDATE user_api_keys
            SET last_test_status = ?, last_tested_at = ?, last_test_latency_ms = ?
            WHERE key_id = ?
            """,
            (status_str, now_iso, latency, key_id)
        )

        return {
            "success": is_ok,
            "status": status_str,
            "message": msg,
            "latency_ms": latency,
            "tested_at": now_iso,
        }

    # 2. 预置演示节点或临时 ID 探针自适应
    provider = None
    if key_id in PRESET_DEMO_KEYS:
        provider = PRESET_DEMO_KEYS[key_id]["provider"]
    else:
        lowered = key_id.lower()
        if "hl" in lowered or "hyperliquid" in lowered:
            provider = "hyperliquid"
        elif "binance" in lowered:
            provider = "binance"
        elif "claude" in lowered or "anthropic" in lowered:
            provider = "anthropic"
        elif "deepseek" in lowered:
            provider = "deepseek"

    if provider:
        is_ok, msg, latency = await test_key_connectivity(provider, {})
        status_str = "OK" if is_ok else "WARNING"
        return {
            "success": True,
            "status": status_str,
            "message": f"服务商 [{provider.upper()}] 节点探针响应: {msg}",
            # 探针没有测到耗时就如实返回 None，不再填 85ms
            "latency_ms": latency if latency else None,
            "tested_at": now_iso,
        }

    raise HTTPException(status_code=404, detail=f"未找到 ID 为 '{key_id}' 的密钥凭证")


@auth_router.post("/vault/keys/test-direct")
@auth_router.post("/vault/test-direct")
async def test_direct_key(
    req: DirectTestKeyRequest,
    caller_wallet: str = Depends(require_authenticated_wallet),
) -> Dict[str, Any]:
    """
    保存前的明文直连测试（强制校验 Web3 Bearer 会话，防匿名滥用外网探针；不会将密钥存入数据库）
    """
    logger.info("[密钥直连探针] 钱包=%s | 服务商=%s", caller_wallet, req.provider.lower())
    is_ok, msg, latency = await test_key_connectivity(req.provider.lower(), req.payload)
    return {
        "success": is_ok,
        "status": "OK" if is_ok else "FAILED",
        "message": msg,
        "latency_ms": latency,
    }
