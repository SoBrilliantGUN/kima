"""Copilot 知识 Agent 编排包（LangGraph）。

runtime/          —— 执行运行时（state/budget/context/loop_guard/reactive/planner/executor/...）
gateway.py        —— LLM 网关（门禁/快照/重试/记账）+ gateway_context + gateway_codec
tools.py          —— build_tools（11 工具闭包，@copilot_tool 自动注册 ToolMeta）+ tools_helpers
memory.py         —— assemble_system_prompt（L0）+ format_memory_block（L2）+ CONSTRAINT_REMINDER
compose.py        —— CopilotRuntime（聚合依赖 + 装配成品）+ build_runtime（装配收敛）
orchestrate.py    —— 共用编排纯函数（stream_graph / commit_assistant / assemble_context / ...）
run.py            —— 对话运行时主入口 run()
resume.py         —— HITL 恢复 / 崩溃恢复入口 resume() / resume_after_crash()
plan_runner.py    —— planner 执行模式（DAG 规划 → 拓扑执行 → 合成 + 输出自检）
events.py         —— Copilot 流式事件类型（SSE 推给前端的结构化事件联合类型）
side_effect.py    —— DbSideEffectVerifier（写工具副作用确定性对账）
helpers.py        —— 共享小工具（预算包裹/文本裁剪/历史截断/来源评级）
（qa 已并入 reactive 主循环，限只读检索工具集 tools.QA_TOOL_NAMES）
"""
