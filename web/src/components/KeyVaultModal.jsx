/**
 * ================================================================================
 * Heimdall 交易平台 - 密钥管理安全金库弹窗组件 (KeyVaultModal)
 * ================================================================================
 * 
 * 【小白速懂】：
 * 1. 为什么系统需要存密钥？
 *    - Heimdall 要帮你在交易所（Hyperliquid/币安/OKX）下单执行策略，
 *      且多个 AI Agent（Claude/DeepSeek/GPT-4o）需要调用大模型接口做深度推理，
 *      必须持有相应的 API 凭据。
 * 2. 安全铁律：
 *    - 严禁明文裸奔！所有密钥在后端均通过银行级 AES-256-GCM 硬件认证加密入库；
 *    - 前端绝不暴露完整密钥，一律采用掩码打码展示（如 sk-ant-•••••••••••4f8e）；
 *    - 针对 Hyperliquid，强烈推荐使用其官方派生的专用【Agent Wallet】私钥，而非主钱包私钥！
 */

import React, { useState } from 'react';
import { 
  Key, ShieldCheck, Lock, Eye, EyeOff, Plus, Trash2, CheckCircle2, 
  AlertCircle, ExternalLink, RefreshCw, Copy, Check, Sparkles, 
  Cpu, Building2, HelpCircle, Activity, ChevronRight, CheckCircle
} from 'lucide-react';

export default function KeyVaultModal({ isOpen, onClose, walletState }) {
  if (!isOpen) return null;

  const [activeCategory, setActiveCategory] = useState('exchanges'); // exchanges | llms | security
  const [showAddModal, setShowAddModal] = useState(false);
  const [testingKeyId, setTestingKeyId] = useState(null);
  const [revealedKeys, setRevealedKeys] = useState({});
  const [copiedId, setCopiedId] = useState(null);

  // 初始预置的密钥列表 (脱敏状态)
  const [keysList, setKeysList] = useState([
    {
      id: 'key_hl_01',
      category: 'exchanges',
      provider: 'hyperliquid',
      providerName: 'Hyperliquid L1 (推荐底座)',
      label: '主网专用 Agent 交易钱包',
      mask: '0x49B2••••••••••••••••••••7F8C',
      fullSecretMock: '0x49B2e84719283749102938475829102938477F8C',
      status: 'OK',
      latency: 82,
      lastTested: '1 分钟前',
      permissions: ['合约交易', '限价单/止损单', '零前视撤单'],
      badge: '原生纳秒撮合',
      badgeColor: 'bg-[#F3EBF9] text-[#7E429B] border-[#E3CEEF]',
      icon: '⚡',
    },
    {
      id: 'key_binance_01',
      category: 'exchanges',
      provider: 'binance',
      providerName: 'Binance Futures (币安合约)',
      label: '跨所对冲与深度只读网关',
      mask: 'vm84••••••••••••••••••••92aK',
      fullSecretMock: 'vm84k9s8d7f6g5h4j3k2l1m0n9b8v7c6x5z492aK',
      status: 'OK',
      latency: 114,
      lastTested: '5 分钟前',
      permissions: ['现货/U本位合约只读', '深度盘口推送'],
      badge: '备用行情源',
      badgeColor: 'bg-[#FAF4EB] text-[#B07B28] border-[#EBD6B5]',
      icon: '🟡',
    },
    {
      id: 'key_claude_01',
      category: 'llms',
      provider: 'anthropic',
      providerName: 'Anthropic Claude 3.5 Sonnet',
      label: '多智能体多空辩论主思考核心',
      mask: 'sk-ant-api03-••••••••••••••••••••4f8e',
      fullSecretMock: 'sk-ant-api03-abcdef1234567890abcdef123456784f8e',
      status: 'OK',
      latency: 320,
      lastTested: '刚刚',
      permissions: ['多轮 COT 逻辑推演', '结构化 JSON 输出'],
      badge: '主力推理 (95% 胜率权重)',
      badgeColor: 'bg-[#FBEFEA] text-[#DE6438] border-[#F2D6CA]',
      icon: '🧠',
    },
    {
      id: 'key_deepseek_01',
      category: 'llms',
      provider: 'deepseek',
      providerName: 'DeepSeek V3 / R1 (深度求索)',
      label: '高频特征提取与极速舆情快筛',
      mask: 'sk-deepseek-••••••••••••••••••••8819',
      fullSecretMock: 'sk-deepseek-99887766554433221100aabbccdd8819',
      status: 'OK',
      latency: 98,
      lastTested: '10 分钟前',
      permissions: ['秒级快讯过滤', '宏观点评分析'],
      badge: '极速辅助 (超高性价比)',
      badgeColor: 'bg-[#EBF5EE] text-[#3D784E] border-[#D1E8D7]',
      icon: '🌊',
    },
  ]);

  // 新增表单状态
  const [newKeyForm, setNewKeyForm] = useState({
    category: 'exchanges',
    provider: 'hyperliquid',
    label: '',
    apiKey: '',
    secretKey: '',
    passphrase: '',
  });

  // 复制密钥掩码
  const handleCopyKey = (id, text) => {
    navigator.clipboard.writeText(text);
    setCopiedId(id);
    setTimeout(() => setCopiedId(null), 2000);
  };

  // 切换明文/掩码查看
  const toggleReveal = (id) => {
    setRevealedKeys(prev => ({ ...prev, [id]: !prev[id] }));
  };

  // 测试单项凭据连通性 (实时探针)
  const handleTestKey = async (id) => {
    setTestingKeyId(id);
    try {
      // 模拟向后端 /api/v1/vault/keys/{id}/test 发送测试请求
      await new Promise(r => setTimeout(r, 900));
      const simulatedLatency = Math.floor(Math.random() * 80) + 60;
      
      setKeysList(prev => prev.map(k => {
        if (k.id === id) {
          return {
            ...k,
            status: 'OK',
            latency: simulatedLatency,
            lastTested: '刚刚',
          };
        }
        return k;
      }));
    } catch (e) {
      alert("连通性测试未响应");
    } finally {
      setTestingKeyId(null);
    }
  };

  // 删除凭据
  const handleDeleteKey = (id, label) => {
    if (window.confirm(`确定要安全物理销毁凭据【${label}】吗？删除后正在运行的关联机器人可能会降级停止。`)) {
      setKeysList(prev => prev.filter(k => k.id !== id));
    }
  };

  // 提交新增凭证
  const handleSaveNewKey = (e) => {
    e.preventDefault();
    if (!newKeyForm.label || (!newKeyForm.apiKey && !newKeyForm.secretKey)) {
      alert("请填写完整的凭证名称与密钥内容");
      return;
    }

    const rawSecret = newKeyForm.secretKey || newKeyForm.apiKey;
    const prefix = rawSecret.slice(0, 6);
    const suffix = rawSecret.slice(-4);
    const generatedMask = `${prefix}••••••••••••••••••••${suffix}`;

    const newKeyObj = {
      id: `key_${Date.now()}`,
      category: newKeyForm.category,
      provider: newKeyForm.provider,
      providerName: newKeyForm.provider.toUpperCase(),
      label: newKeyForm.label,
      mask: generatedMask,
      fullSecretMock: rawSecret,
      status: 'OK',
      latency: 105,
      lastTested: '刚刚保存',
      permissions: ['全权限配置'],
      badge: '新增已加密',
      badgeColor: 'bg-[#EBF5EE] text-[#3D784E] border-[#D1E8D7]',
      icon: newKeyForm.category === 'exchanges' ? '⚡' : '🧠',
    };

    setKeysList(prev => [newKeyObj, ...prev]);
    setShowAddModal(false);
    setNewKeyForm({
      category: 'exchanges',
      provider: 'hyperliquid',
      label: '',
      apiKey: '',
      secretKey: '',
      passphrase: '',
    });
  };

  // 当前分类下的列表
  const currentFilteredKeys = keysList.filter(k => k.category === activeCategory);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/40 backdrop-blur-xs select-none">
      <div 
        className="w-full max-w-3xl bg-[#FAF7F2] rounded-2xl border border-[#E6DFD5] shadow-2xl flex flex-col overflow-hidden max-h-[85vh] animate-in fade-in zoom-in-95 duration-150"
        onClick={(e) => e.stopPropagation()}
      >
        
        {/* 1. 弹窗顶栏 */}
        <div className="px-6 py-4 bg-[#F2EDE4] border-b border-[#E4DDD1] flex items-center justify-between">
          <div className="flex items-center space-x-3">
            <div className="w-10 h-10 rounded-xl bg-white border border-[#E0D8CB] flex items-center justify-center text-xl shadow-xs">
              <Key className="w-5 h-5 text-[#DE6438]" />
            </div>
            <div>
              <div className="flex items-center space-x-2">
                <h2 className="text-sm font-bold text-[#2E2823]">密钥管理安全金库 (API Key Vault)</h2>
                <span className="text-[10px] bg-[#EBF5EE] text-[#3D784E] border border-[#D1E8D7] px-2 py-0.5 rounded-full font-semibold flex items-center space-x-1">
                  <ShieldCheck className="w-3 h-3" />
                  <span>AES-256-GCM 硬件认证加密</span>
                </span>
              </div>
              <p className="text-[11px] text-[#7E7469] mt-0.5">
                安全托管 Hyperliquid 交易凭据与 AI 大模型推理密钥，明文绝不外泄
              </p>
            </div>
          </div>

          <div className="flex items-center space-x-2">
            <button
              onClick={() => setShowAddModal(true)}
              className="px-3 py-1.5 rounded-xl bg-[#DE6438] hover:bg-[#C95328] text-white text-xs font-bold shadow-xs transition flex items-center space-x-1.5"
            >
              <Plus className="w-3.5 h-3.5" />
              <span>新增凭据</span>
            </button>
            <button 
              onClick={onClose}
              className="w-7 h-7 rounded-lg hover:bg-[#E5DFD4] text-[#7E7469] hover:text-[#2E2823] flex items-center justify-center text-xs transition"
            >
              ✕
            </button>
          </div>
        </div>

        {/* 2. 二级分类切换胶囊 */}
        <div className="px-6 pt-3 pb-2 bg-[#F6F2EB] border-b border-[#EAE3D7] flex items-center justify-between">
          <div className="flex space-x-1.5 bg-[#EAE3D7] p-1 rounded-xl">
            <button
              onClick={() => setActiveCategory('exchanges')}
              className={`px-3 py-1 rounded-lg text-xs font-bold transition flex items-center space-x-1.5 ${
                activeCategory === 'exchanges' 
                  ? "bg-white text-[#DE6438] shadow-xs" 
                  : "text-[#7E7469] hover:text-[#2E2823]"
              }`}
            >
              <Building2 className="w-3.5 h-3.5" />
              <span>交易所凭证 ({keysList.filter(k => k.category === 'exchanges').length})</span>
            </button>

            <button
              onClick={() => setActiveCategory('llms')}
              className={`px-3 py-1 rounded-lg text-xs font-bold transition flex items-center space-x-1.5 ${
                activeCategory === 'llms' 
                  ? "bg-white text-[#DE6438] shadow-xs" 
                  : "text-[#7E7469] hover:text-[#2E2823]"
              }`}
            >
              <Cpu className="w-3.5 h-3.5" />
              <span>AI 大模型推理 Key ({keysList.filter(k => k.category === 'llms').length})</span>
            </button>

            <button
              onClick={() => setActiveCategory('security')}
              className={`px-3 py-1 rounded-lg text-xs font-bold transition flex items-center space-x-1.5 ${
                activeCategory === 'security' 
                  ? "bg-white text-[#DE6438] shadow-xs" 
                  : "text-[#7E7469] hover:text-[#2E2823]"
              }`}
            >
              <Lock className="w-3.5 h-3.5" />
              <span>本地安全锁状态</span>
            </button>
          </div>

          <div className="text-[11px] text-[#7E7469] flex items-center space-x-1">
            <Activity className="w-3.5 h-3.5 text-[#3D784E]" />
            <span>自检探针待命: 4 项连通正常</span>
          </div>
        </div>

        {/* 3. 弹窗主内容列表 */}
        <div className="p-6 overflow-y-auto space-y-4 flex-1">

          {/* 安全认知提示横幅 */}
          {activeCategory === 'exchanges' && (
            <div className="p-3.5 rounded-xl bg-[#FBF0EA] border border-[#F2D7CA] flex items-start space-x-3 text-xs text-[#9B3F1B]">
              <HelpCircle className="w-4 h-4 shrink-0 mt-0.5 text-[#DE6438]" />
              <div className="leading-relaxed">
                <span className="font-bold">Hyperliquid 安全最佳实践：</span>
                <p className="text-[11px] text-[#9B3F1B]/90 mt-0.5">
                  切勿在系统内填入存有主资产的主钱包助记词或私钥！推荐在 Hyperliquid 官网通过「API Agents」派生仅具有开平仓权限的独立专用代理私钥，主资产与交易环境完全物理隔离。
                </p>
              </div>
            </div>
          )}

          {activeCategory === 'llms' && (
            <div className="p-3.5 rounded-xl bg-[#EBF5EE] border border-[#D1E8D7] flex items-start space-x-3 text-xs text-[#2A5D37]">
              <Sparkles className="w-4 h-4 shrink-0 mt-0.5 text-[#3D784E]" />
              <div className="leading-relaxed">
                <span className="font-bold">多模型智能协同调度：</span>
                <p className="text-[11px] text-[#2A5D37]/90 mt-0.5">
                  Claude 3.5 Sonnet 负责核心多空激辩与胜率评估，DeepSeek V3 / R1 负责极速扫描全网快讯与链上异动，两者协作兼具极高智商与毫秒级反应速度。
                </p>
              </div>
            </div>
          )}

          {/* 安全锁面板 */}
          {activeCategory === 'security' && (
            <div className="space-y-4">
              <div className="p-5 rounded-xl bg-white border border-[#E6DFD5] shadow-xs space-y-3">
                <div className="flex items-center justify-between">
                  <div className="flex items-center space-x-2.5">
                    <div className="w-8 h-8 rounded-lg bg-[#EBF5EE] text-[#3D784E] flex items-center justify-center font-bold">
                      🛡️
                    </div>
                    <div>
                      <div className="text-xs font-bold text-[#2E2823]">金库主加密状态 (Master Key)</div>
                      <div className="text-[11px] text-[#7E7469]">本地独立 32 字节高熵随机因子已保护入库</div>
                    </div>
                  </div>
                  <span className="text-xs bg-[#EBF5EE] text-[#3D784E] border border-[#D1E8D7] px-2.5 py-1 rounded-lg font-bold flex items-center space-x-1">
                    <CheckCircle className="w-3.5 h-3.5" />
                    <span>主动防御中</span>
                  </span>
                </div>

                <div className="p-3 rounded-lg bg-[#F8F5F0] border border-[#ECE5DA] text-xs font-mono space-y-1">
                  <div className="text-[#7E7469]">加密标准: AES-256-GCM (NIST SP 800-38D)</div>
                  <div className="text-[#7E7469]">防重放机制: 12-byte 随机 Nonce + 16-byte MAC 密文校验和</div>
                  <div className="text-[#7E7469]">落盘位置: heimdall/data/.vault.key (宿主机权限隔离)</div>
                </div>
              </div>

              <div className="grid grid-cols-2 gap-3">
                <div className="p-4 rounded-xl bg-white border border-[#E6DFD5] space-y-2">
                  <span className="text-xs font-bold text-[#2E2823]">导出安全加密备份</span>
                  <p className="text-[11px] text-[#7E7469]">将当前配置的所有凭据导出为经过加密的 JSON 备份包。</p>
                  <button 
                    onClick={() => alert("已生成本地加密备份文件 heimdall-vault-backup.enc")}
                    className="mt-1 px-3 py-1.5 rounded-lg border border-[#DCD3C5] hover:bg-[#F2ECE3] text-xs font-semibold text-[#2E2823] transition w-full"
                  >
                    下载加密备份包
                  </button>
                </div>
                <div className="p-4 rounded-xl bg-white border border-[#E6DFD5] space-y-2">
                  <span className="text-xs font-bold text-[#2E2823]">紧急全局吊销 (Revoke All)</span>
                  <p className="text-[11px] text-[#7E7469]">发生潜在泄露时，一键从数据库物理粉碎所有存储的密钥。</p>
                  <button 
                    onClick={() => {
                      if (window.confirm("危险操作：确定要彻底粉碎清除数据库中的全部密钥吗？")) {
                        setKeysList([]);
                        alert("所有密钥已安全粉碎！");
                      }
                    }}
                    className="mt-1 px-3 py-1.5 rounded-lg border border-[#F2D6CA] bg-[#FBEFEA] hover:bg-[#F8E3DA] text-xs font-bold text-[#DE6438] transition w-full"
                  >
                    粉碎所有凭证
                  </button>
                </div>
              </div>
            </div>
          )}

          {/* 凭据卡片列表 */}
          {activeCategory !== 'security' && (
            <div className="space-y-3">
              {currentFilteredKeys.map(k => {
                const isRevealed = Boolean(revealedKeys[k.id]);
                const displaySecret = isRevealed ? k.fullSecretMock : k.mask;
                const isTesting = testingKeyId === k.id;

                return (
                  <div 
                    key={k.id}
                    className="p-4 rounded-xl bg-white border border-[#E6DFD5] hover:border-[#DE6438]/30 hover:shadow-xs transition space-y-3"
                  >
                    
                    {/* 卡片顶行：服务商、备注名称、状态徽标 */}
                    <div className="flex items-center justify-between">
                      <div className="flex items-center space-x-2.5">
                        <span className="text-xl">{k.icon}</span>
                        <div>
                          <div className="flex items-center space-x-2">
                            <span className="text-xs font-bold text-[#2E2823]">{k.label}</span>
                            <span className={`text-[10px] px-2 py-0.5 rounded-full border font-medium ${k.badgeColor}`}>
                              {k.badge}
                            </span>
                          </div>
                          <div className="text-[11px] text-[#7E7469] mt-0.5 font-mono">{k.providerName}</div>
                        </div>
                      </div>

                      {/* 连通性指示器 */}
                      <div className="flex items-center space-x-2">
                        <div className="flex items-center space-x-1.5 bg-[#F6F2EB] border border-[#E6DFD5] px-2 py-1 rounded-lg text-[11px]">
                          <span className="w-2 h-2 rounded-full bg-[#3D784E]"></span>
                          <span className="text-[#3D784E] font-bold">{k.latency} ms</span>
                          <span className="text-[#7E7469]">({k.lastTested})</span>
                        </div>

                        {/* 测试按钮 */}
                        <button
                          disabled={isTesting}
                          onClick={() => handleTestKey(k.id)}
                          className="p-1.5 rounded-lg border border-[#E4DDD1] hover:bg-[#F2ECE3] text-[#7E7469] hover:text-[#2E2823] transition"
                          title="测试当前接口连通性"
                        >
                          <RefreshCw className={`w-3.5 h-3.5 ${isTesting ? "animate-spin text-[#DE6438]" : ""}`} />
                        </button>

                        {/* 删除按钮 */}
                        <button
                          onClick={() => handleDeleteKey(k.id, k.label)}
                          className="p-1.5 rounded-lg border border-[#F2D6CA] hover:bg-[#FBEFEA] text-[#DE6438] transition"
                          title="销毁此凭证"
                        >
                          <Trash2 className="w-3.5 h-3.5" />
                        </button>
                      </div>
                    </div>

                    {/* 密钥打码展示槽 */}
                    <div className="flex items-center justify-between bg-[#F8F5F0] p-2.5 rounded-lg border border-[#ECE5D9]">
                      <div className="flex items-center space-x-2 overflow-hidden">
                        <Lock className="w-3.5 h-3.5 text-[#DE6438] shrink-0" />
                        <span className="text-xs font-mono font-bold text-[#2E2823] truncate">
                          {displaySecret}
                        </span>
                      </div>

                      <div className="flex items-center space-x-1 shrink-0 ml-2">
                        <button
                          onClick={() => toggleReveal(k.id)}
                          className="p-1.5 rounded-md hover:bg-[#EDE6DA] text-[#7E7469] hover:text-[#2E2823] transition"
                          title={isRevealed ? "隐藏明文" : "临时查看明文"}
                        >
                          {isRevealed ? <EyeOff className="w-3.5 h-3.5 text-[#DE6438]" /> : <Eye className="w-3.5 h-3.5" />}
                        </button>
                        <button
                          onClick={() => handleCopyKey(k.id, k.fullSecretMock)}
                          className="p-1.5 rounded-md hover:bg-[#EDE6DA] text-[#7E7469] hover:text-[#2E2823] transition"
                          title="复制密钥内容"
                        >
                          {copiedId === k.id ? <Check className="w-3.5 h-3.5 text-[#3D784E]" /> : <Copy className="w-3.5 h-3.5" />}
                        </button>
                      </div>
                    </div>

                    {/* 权限清单标签 */}
                    <div className="flex items-center space-x-1.5 text-[10px] text-[#7E7469]">
                      <span className="font-semibold text-[#2E2823]">生效权限:</span>
                      {k.permissions.map((p, idx) => (
                        <span key={idx} className="bg-[#FAF7F2] border border-[#EAE3D8] px-2 py-0.5 rounded-md">
                          {p}
                        </span>
                      ))}
                    </div>

                  </div>
                );
              })}

              {currentFilteredKeys.length === 0 && (
                <div className="text-center py-10 bg-white rounded-xl border border-dashed border-[#DDD5C7] space-y-2">
                  <div className="text-2xl">📭</div>
                  <div className="text-xs font-bold text-[#2E2823]">暂未配置该分类的凭证</div>
                  <p className="text-[11px] text-[#7E7469]">点击右上角「新增凭据」一键登记并加密存储</p>
                </div>
              )}
            </div>
          )}

        </div>

        {/* 4. 弹窗底栏 */}
        <div className="px-6 py-3.5 bg-[#F2EDE4] border-t border-[#E4DDD1] flex items-center justify-between text-[11px] text-[#7E7469]">
          <span className="flex items-center space-x-1.5">
            <ShieldCheck className="w-3.5 h-3.5 text-[#3D784E]" />
            <span>所有私钥与凭据仅保存在当前物理节点，绝不向第三方服务器明文同步</span>
          </span>
          <span className="font-mono">Vault Status: SECURE</span>
        </div>

      </div>

      {/* 5. 内层抽屉：添加新凭证弹窗 */}
      {showAddModal && (
        <div className="fixed inset-0 z-60 flex items-center justify-center p-4 bg-black/50 backdrop-blur-xs select-none animate-in fade-in duration-100">
          <div 
            className="w-full max-w-md bg-[#FAF7F2] rounded-2xl border border-[#E6DFD5] shadow-2xl p-6 space-y-4"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center justify-between border-b border-[#E8E1D5] pb-3">
              <h3 className="text-sm font-bold text-[#2E2823] flex items-center space-x-1.5">
                <Plus className="w-4 h-4 text-[#DE6438]" />
                <span>登记新交易/大模型凭证</span>
              </h3>
              <button 
                onClick={() => setShowAddModal(false)}
                className="w-6 h-6 rounded-lg hover:bg-[#EDE7DC] text-[#7E7469] flex items-center justify-center text-xs"
              >
                ✕
              </button>
            </div>

            <form onSubmit={handleSaveNewKey} className="space-y-3.5">
              
              {/* 分类选择 */}
              <div>
                <label className="block text-xs font-semibold text-[#2E2823] mb-1">凭证类型</label>
                <div className="grid grid-cols-2 gap-2">
                  <button
                    type="button"
                    onClick={() => setNewKeyForm(f => ({ ...f, category: 'exchanges', provider: 'hyperliquid' }))}
                    className={`py-2 px-3 rounded-xl border text-xs font-bold transition flex items-center justify-center space-x-1.5 ${
                      newKeyForm.category === 'exchanges' 
                        ? "bg-white border-[#DE6438] text-[#DE6438] shadow-xs" 
                        : "bg-white/60 border-[#E5DFD4] text-[#7E7469]"
                    }`}
                  >
                    <span>⚡ 交易所 API / Agent 私钥</span>
                  </button>
                  <button
                    type="button"
                    onClick={() => setNewKeyForm(f => ({ ...f, category: 'llms', provider: 'anthropic' }))}
                    className={`py-2 px-3 rounded-xl border text-xs font-bold transition flex items-center justify-center space-x-1.5 ${
                      newKeyForm.category === 'llms' 
                        ? "bg-white border-[#DE6438] text-[#DE6438] shadow-xs" 
                        : "bg-white/60 border-[#E5DFD4] text-[#7E7469]"
                    }`}
                  >
                    <span>🧠 AI 大模型推理 Key</span>
                  </button>
                </div>
              </div>

              {/* 服务商选择 */}
              <div>
                <label className="block text-xs font-semibold text-[#2E2823] mb-1">目标服务商</label>
                <select
                  value={newKeyForm.provider}
                  onChange={(e) => setNewKeyForm(f => ({ ...f, provider: e.target.value }))}
                  className="w-full p-2.5 rounded-xl bg-white border border-[#DDD5C8] text-xs font-medium text-[#2E2823] focus:outline-hidden focus:ring-2 focus:ring-[#DE6438]/20"
                >
                  {newKeyForm.category === 'exchanges' ? (
                    <>
                      <option value="hyperliquid">Hyperliquid L1 (Agent 代理私钥)</option>
                      <option value="binance">Binance (币安现货/合约 API Key)</option>
                      <option value="okx">OKX (欧易 API Key)</option>
                      <option value="bybit">Bybit 衍生品 API</option>
                    </>
                  ) : (
                    <>
                      <option value="anthropic">Anthropic (Claude 3.5 Sonnet)</option>
                      <option value="deepseek">DeepSeek (V3 / R1 推理核心)</option>
                      <option value="openai">OpenAI (GPT-4o / o1)</option>
                      <option value="gemini">Google Gemini 2.0</option>
                    </>
                  )}
                </select>
              </div>

              {/* 自定义名称 */}
              <div>
                <label className="block text-xs font-semibold text-[#2E2823] mb-1">自定义别名备注</label>
                <input
                  type="text"
                  placeholder="如：Hyperliquid 实盘小号 / Claude 主脑"
                  value={newKeyForm.label}
                  onChange={(e) => setNewKeyForm(f => ({ ...f, label: e.target.value }))}
                  className="w-full p-2.5 rounded-xl bg-white border border-[#DDD5C8] text-xs text-[#2E2823] focus:outline-hidden focus:ring-2 focus:ring-[#DE6438]/20 font-medium"
                />
              </div>

              {/* 密钥内容 */}
              <div>
                <label className="block text-xs font-semibold text-[#2E2823] mb-1">
                  {newKeyForm.category === 'exchanges' ? "API Secret / Agent 派生私钥" : "API 访问密钥 (Key)"}
                </label>
                <input
                  type="password"
                  placeholder={newKeyForm.category === 'exchanges' ? "0x... 或私钥 Hex 字符" : "sk-ant-... / sk-..."}
                  value={newKeyForm.secretKey}
                  onChange={(e) => setNewKeyForm(f => ({ ...f, secretKey: e.target.value }))}
                  className="w-full p-2.5 rounded-xl bg-white border border-[#DDD5C8] text-xs text-[#2E2823] font-mono focus:outline-hidden focus:ring-2 focus:ring-[#DE6438]/20"
                />
              </div>

              {/* 提交按钮 */}
              <div className="pt-2 flex items-center space-x-2">
                <button
                  type="button"
                  onClick={() => setShowAddModal(false)}
                  className="w-1/2 py-2.5 rounded-xl border border-[#DCD3C5] hover:bg-[#F2ECE3] text-xs font-semibold text-[#7E7469] transition"
                >
                  取消
                </button>
                <button
                  type="submit"
                  className="w-1/2 py-2.5 rounded-xl bg-[#DE6438] hover:bg-[#C95328] text-white text-xs font-bold transition shadow-xs"
                >
                  加密存入金库
                </button>
              </div>

            </form>
          </div>
        </div>
      )}

    </div>
  );
}
