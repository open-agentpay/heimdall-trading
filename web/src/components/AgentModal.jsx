/**
 * ================================================================================
 * Heimdall 交易平台 - 智能体深度决策与原因解析抽屉 (Agent Decision Modal)
 * ================================================================================
 * 
 * 【小白速懂】：
 * 当你在中间的“博弈牌桌”上点击任意一个智能体（如多头研究员、空头研究员或硬风控守卫）时，
 * 这个弹窗就会浮现出来，为你原汁原味地解剖这个智能体：
 * 1. 它到底做出了什么决策（做多 LONG、做空 SHORT、还是弃权 NEUTRAL）？
 * 2. 它的置信度有多高（例如 85%）？
 * 3. 它的核心立论是什么？背后拿出了哪些量化指标或链上巨鲸数据做证据？
 * 4. 如果市场走势不及预期，在什么价格或条件下它的立论会被推翻（失效止损点）？
 */

import React from 'react';

export default function AgentModal({ agent, onClose }) {
  if (!agent) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/35 backdrop-blur-xs p-4 animate-fade-in">
      <div 
        className="w-full max-w-lg bg-[#FAF7F2] border-2 border-[#D8CEBE] rounded-3xl shadow-2xl overflow-hidden flex flex-col transform transition-all"
        onClick={(e) => e.stopPropagation()}
      >
        
        {/* 弹窗顶栏：角色形象与关闭按键 */}
        <div className="p-4 bg-white border-b border-[#EBE3D7] flex items-center justify-between">
          <div className="flex items-center space-x-3">
            <div 
              className="w-12 h-12 rounded-2xl flex items-center justify-center text-2xl shadow-xs"
              style={{ backgroundColor: `${agent.color}15`, border: `2px solid ${agent.color}` }}
            >
              {agent.avatar}
            </div>
            <div>
              <div className="flex items-center space-x-2">
                <h3 className="font-bold text-base text-[#2E2823]">{agent.name}</h3>
                <span 
                  className="text-[10px] px-2 py-0.5 rounded-full font-mono font-bold text-white shadow-xs"
                  style={{ backgroundColor: agent.color }}
                >
                  {agent.badge}
                </span>
              </div>
              <p className="text-xs text-[#7E7469] font-medium">{agent.title}</p>
            </div>
          </div>

          <button 
            onClick={onClose}
            className="w-8 h-8 rounded-full bg-[#EFE9DF] hover:bg-[#E5DACB] text-[#5E554B] flex items-center justify-center text-sm font-bold transition"
          >
            ✕
          </button>
        </div>

        {/* 弹窗主体内容滚动区 */}
        <div className="p-5 overflow-y-auto max-h-[70vh] space-y-4 text-xs">
          
          {/* 1. 角色白话速懂介绍 */}
          <div className="bg-[#FFFDFB] p-3 rounded-xl border border-[#E8DEC0] leading-relaxed text-[#5E554B]">
            {agent.roleDescription}
          </div>

          {/* 2. 决策结论与置信度量化仪表 */}
          <div className="bg-white p-3.5 rounded-xl border border-[#E6DFD5] space-y-2 shadow-xs">
            <div className="flex items-center justify-between">
              <span className="font-semibold text-[#8C8276] uppercase tracking-wide text-[11px]">
                当前决策观点 (Decision Stance)
              </span>
              <span 
                className="font-mono font-bold text-xs px-2.5 py-0.5 rounded-md"
                style={{
                  backgroundColor: agent.stance === 'LONG' ? '#EBF5EE' : agent.stance === 'SHORT' ? '#FCECE8' : '#EFE9DF',
                  color: agent.stance === 'LONG' ? '#4D835C' : agent.stance === 'SHORT' ? '#C24D4D' : '#5E554B',
                }}
              >
                {agent.stance} · {agent.tag}
              </span>
            </div>

            {/* 置信度进度槽 */}
            <div className="space-y-1 pt-1">
              <div className="flex justify-between text-[11px] text-[#7E7469] font-mono">
                <span>模型确定性置信度 (Confidence Score):</span>
                <span className="font-bold text-[#2E2823]">{Math.round(agent.confidence * 100)}%</span>
              </div>
              <div className="w-full h-2 bg-[#EFE9DF] rounded-full overflow-hidden">
                <div 
                  className="h-full rounded-full transition-all duration-500"
                  style={{ 
                    width: `${agent.confidence * 100}%`,
                    backgroundColor: agent.color 
                  }}
                />
              </div>
            </div>
          </div>

          {/* 3. 核心立论主张 (Key Thesis) */}
          <div className="bg-white p-3.5 rounded-xl border border-[#E6DFD5] space-y-1.5 shadow-xs">
            <div className="font-bold text-xs text-[#2E2823] flex items-center space-x-1.5">
              <span>🎯</span>
              <span>核心决策论据 (Key Thesis)</span>
            </div>
            <p className="text-[#4A4237] leading-relaxed text-[11px] bg-[#FAF7F2] p-2.5 rounded-lg border border-[#EDE5DA]">
              {agent.keyThesis}
            </p>
          </div>

          {/* 4. 量化指标与证据清单 (Supporting Evidence) */}
          <div className="bg-white p-3.5 rounded-xl border border-[#E6DFD5] space-y-2 shadow-xs">
            <div className="font-bold text-xs text-[#2E2823] flex items-center space-x-1.5">
              <span>🔍</span>
              <span>数据证据与指标支撑 (Evidence Checklist)</span>
            </div>
            <div className="space-y-1.5">
              {agent.evidence.map((item, idx) => (
                <div key={idx} className="flex items-start space-x-2 text-[11px] text-[#4A4237]">
                  <span className="text-[#4D835C] font-bold">✓</span>
                  <span className="leading-tight">{item}</span>
                </div>
              ))}
            </div>
          </div>

          {/* 5. 观点失效与止损条件 (Invalidation Stop Condition) */}
          <div className="bg-[#FCF7F5] p-3 rounded-xl border border-[#F2D6CA] space-y-1">
            <div className="font-bold text-[11px] text-[#C24D4D] flex items-center space-x-1">
              <span>🛡️</span>
              <span>立论失效与防守点位 (Invalidation Rule)</span>
            </div>
            <p className="text-[#8C3A3A] text-[11px] leading-relaxed">
              {agent.invalidation}
            </p>
          </div>

          {/* 6. 大语言模型完整思考独白 (Chain-of-Thought) */}
          <div className="bg-[#241F1C] p-3 rounded-xl border border-[#3D352E] space-y-1 text-[#EAE2D8] font-mono text-[10px]">
            <div className="flex items-center justify-between text-[#8E8276] border-b border-[#3D352E] pb-1">
              <span>🧠 LLM Chain-of-Thought Stream</span>
              <span>Latency: 45ms</span>
            </div>
            <p className="leading-relaxed pt-1 text-[#D2C8BC]">
              {agent.thinkingMonologue}
            </p>
          </div>

        </div>

        {/* 弹窗底栏操作 */}
        <div className="p-3 bg-white border-t border-[#EBE3D7] flex items-center justify-between">
          <span className="text-[10px] text-[#9E917F]">
            审计追踪 ID: <code className="bg-[#EFE9DF] px-1 rounded">cot_agent_{agent.id}_v1</code>
          </span>
          <button 
            onClick={onClose}
            className="px-4 py-1.5 rounded-xl bg-[#DE6438] hover:bg-[#C8542A] text-white text-xs font-semibold shadow-xs transition"
          >
            已阅知 (Dismiss)
          </button>
        </div>

      </div>
    </div>
  );
}
