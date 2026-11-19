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
from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from heimdall.database.db import db_manager
from heimdall.auth.vault import (
    crypto_vault,
    mask_secret,
    generate_siwe_message,
    verify_wallet_signature,
    test_key_connectivity,
)

logger = logging.getLogger(__name__)

auth_router = APIRouter(prefix="/api/v1", tags=["Authentication & Key Vault"])


# ==============================================================================
# 请求与响应模型 (Pydantic Schemas)
# ==============================================================================

class VerifySignatureRequest(BaseModel):
    """钱包验签请求体"""
    address: str = Field(..., description="钱包公钥地址 (0x...)", example="0x71C...49b2")
    signature: str = Field(..., description="EIP-191 personal_sign 签名 hex 串", example="0x...")
    message: str = Field(..., description="签名所对应的完整明文挑战文本")


class CreateKeyRequest(BaseModel):
    """新增凭证请求体"""
    wallet_address: str = Field(..., description="归属钱包地址")
    category: str = Field(..., description="分类: 'exchange' 或 'llm'", example="exchange")
    provider: str = Field(..., description="服务商: 'hyperliquid', 'binance', 'okx', 'anthropic', 'openai', 'deepseek', 'gemini'", example="hyperliquid")
    label: str = Field(..., description="自定义备注名称", example="Hyperliquid 主账户 API Agent")
    payload: Dict[str, Any] = Field(..., description="明文凭证内容 (后端加密后立即销毁明文)")


class DirectTestKeyRequest(BaseModel):
    """直接测试连通性请求体 (未保存前预检)"""
    provider: str = Field(..., description="服务商标识")
    payload: Dict[str, Any] = Field(..., description="待测试的凭证内容")


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
    wallet_address: Optional[str] = Query(default=None, description="过滤指定钱包绑定的密钥")
) -> List[Dict[str, Any]]:
    """
    查询已保存的凭证列表（绝对禁止返回明文 Secret，只返回打码脱敏的 key_mask）
    """
    if wallet_address:
        sql = "SELECT key_id, wallet_address, category, provider, label, key_mask, is_active, last_tested_at, last_test_status, last_test_latency_ms, created_at, updated_at FROM user_api_keys WHERE wallet_address = ? ORDER BY created_at DESC"
        rows = await db_manager.execute_query(sql, (wallet_address.lower(),))
    else:
        sql = "SELECT key_id, wallet_address, category, provider, label, key_mask, is_active, last_tested_at, last_test_status, last_test_latency_ms, created_at, updated_at FROM user_api_keys ORDER BY created_at DESC"
        rows = await db_manager.execute_query(sql)

    return rows


@auth_router.post("/vault/keys")
async def create_vault_key(req: CreateKeyRequest) -> Dict[str, Any]:
    """
    新增或保存加密凭证 (采用 AES-256-GCM 硬件认证加密入库，绝不明文裸奔)
    """
    key_id = f"key_{uuid.uuid4().hex[:10]}"
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    # 智能提取关键识别信息并打码
    raw_key = req.payload.get("api_key") or req.payload.get("private_key") or req.payload.get("agent_wallet") or ""
    mask = mask_secret(str(raw_key))

    # 执行 AES-256-GCM 加密
    encrypted_str = crypto_vault.encrypt_payload(req.payload)

    sql = """
    INSERT INTO user_api_keys (
        key_id, wallet_address, category, provider, label, key_mask, encrypted_data, is_active, created_at, updated_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
    """
    await db_manager.execute_commit(
        sql,
        (
            key_id,
            req.wallet_address.lower(),
            req.category,
            req.provider.lower(),
            req.label,
            mask,
            encrypted_str,
            now_iso,
            now_iso,
        )
    )

    return {
        "success": True,
        "key_id": key_id,
        "label": req.label,
        "key_mask": mask,
        "category": req.category,
        "provider": req.provider.lower(),
        "created_at": now_iso,
    }


@auth_router.delete("/vault/keys/{key_id}")
async def delete_vault_key(key_id: str) -> Dict[str, Any]:
    """撤销并彻底物理销毁指定的凭证"""
    affected = await db_manager.execute_commit(
        "DELETE FROM user_api_keys WHERE key_id = ?",
        (key_id,)
    )
    if affected == 0:
        raise HTTPException(status_code=404, detail="未找到对应的凭证或已被销毁")
    return {"success": True, "message": f"凭证 {key_id} 已安全销毁"}


@auth_router.post("/vault/keys/{key_id}/test")
async def test_saved_vault_key(key_id: str) -> Dict[str, Any]:
    """
    测试已保存凭证的实时连通性与接口延时
    """
    rows = await db_manager.execute_query(
        "SELECT provider, encrypted_data FROM user_api_keys WHERE key_id = ?",
        (key_id,)
    )
    if not rows:
        raise HTTPException(status_code=404, detail="凭证不存在")

    row = rows[0]
    provider = row["provider"]
    encrypted_data = row["encrypted_data"]

    # 解密载荷
    try:
        credentials = crypto_vault.decrypt_payload(encrypted_data)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"凭证解密失败: {e}")

    # 运行连通性测试
    is_ok, msg, latency = await test_key_connectivity(provider, credentials)
    status_str = "OK" if is_ok else "FAILED"
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

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


@auth_router.post("/vault/keys/test-direct")
async def test_direct_key(req: DirectTestKeyRequest) -> Dict[str, Any]:
    """
    保存前的明文直连测试（不会将任何密钥存入数据库）
    """
    is_ok, msg, latency = await test_key_connectivity(req.provider.lower(), req.payload)
    return {
        "success": is_ok,
        "status": "OK" if is_ok else "FAILED",
        "message": msg,
        "latency_ms": latency,
    }
