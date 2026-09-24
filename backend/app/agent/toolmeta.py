"""工具元数据（拦截契约）：把「工具是什么性质」下沉为代码级强类型声明。

工具不能只是一段代码，它必须是一个自描述实体，向 Agent Loop 暴露它的「安全指纹」。
Loop 要做并发裁决、熔断、权限拦截、幂等键注入、超时控制，靠的是这份元数据，而不是
去解析 Description（模型会看错，代码不能看错）。

此前这些性质散落在多份硬编码字典里（``WRITE_TOOL_NAMES`` / ``_RETRIEVAL_SOURCE`` /
``_TOOL_SOURCE``），加一个工具要改三四处、漏一处就漂移。现在统一收敛到 ``ToolMeta``：
一次声明，Loop / 零信任打分 / 权限门禁 / 超时 / 幂等键注入都从它派生。

对应「六大契约」里的拦截契约 + 执行契约 + 反馈契约的元数据承载层。
"""

import hashlib
import json
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class SideEffectLevel(StrEnum):
    """副作用等级：LOW 安全只读 / MEDIUM 一般写（审计）/ HIGH 高危写（审批）。"""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(frozen=True)
class OutputContract:
    """工具结果的交货标准（上行契约）：只验形状、不看内容（对应「契约关」）。

    与下行「任务包=白名单」相反，工具结果是代码/检索确定性产生的、也可能夹带超长正文，
    故在流向 synthesizer（父 Agent）前过一道契约关：

    - ``max_chars``：结果跨过上行的上限，超出按头尾截断——对抗「N 个步骤各回一段长正文，
      父 Agent 照单全收、Prompt Cache 全毁」（结构裁剪）。
    - ``required`` / ``optional``：若工具返回结构化 JSON，声明可放行的字段（白名单剥离
      + 类型校验）；字符串型工具不声明这两个，仅受 ``max_chars`` 约束。

    未声明 ``output_contract`` 的工具，结果原样放行（向后兼容）。形状归框架管、好坏归
    业务管：本契约不判断内容对错，只判断「长成什么样才准过关」。
    """

    max_chars: int | None = None
    required: dict[str, type] = field(default_factory=dict)
    optional: dict[str, type] = field(default_factory=dict)


# 检索类工具结果流向 synthesizer 的默认上限（上行契约的结构裁剪），与事件日志截断
# ``helpers.TOOL_RESULT_CHARS`` 对齐：合成回答只需要结论级上下文，长正文走 spill/read_tool_result。
SYNTHESIS_RESULT_CHARS = 2000


def _clip(text: str, max_chars: int) -> str:
    return text if len(text) <= max_chars else text[:max_chars] + "…"


def apply_output_contract(name: str, result: str, contract: OutputContract | None) -> str:
    """上行契约关：对工具结果做「形状校验 + 白名单剥离 + 结构裁剪」。

    无契约 → 原样放行；有 ``required``/``optional`` → 期望结果为 JSON 对象，逐字段验
    存在性/类型、剥掉未声明字段；有 ``max_chars`` → 截断。契约不符 → 返回拒收占位符
    （不抛异常：工具已执行、副作用已发生，只拒收「流向父 Agent」的载荷，把不符结果留在
    审计日志里，父 Agent 收到「契约不符」短消息，而非被脏数据带偏）。
    """
    if contract is None:
        return result
    if contract.required or contract.optional:
        try:
            data = json.loads(result)
        except (ValueError, TypeError):
            return f"（契约校验失败：{name} 结果不是合法 JSON）"
        if not isinstance(data, dict):
            return f"（契约校验失败：{name} 结果不是对象）"
        declared = set(contract.required) | set(contract.optional)
        for field_name, typ in contract.required.items():
            if field_name not in data:
                return f"（契约校验失败：{name} 缺必填字段 {field_name!r}）"
            if not isinstance(data[field_name], typ):
                return f"（契约校验失败：{name} 字段 {field_name!r} 类型不符）"
        result = json.dumps(
            {k: v for k, v in data.items() if k in declared}, ensure_ascii=False
        )
    if contract.max_chars is not None:
        result = _clip(result, contract.max_chars)
    return result


@dataclass(frozen=True)
class ParamContract:
    """下行参数契约（对应「参数级校验」防线②）：工具入参的声明式边界。

    传统做法把参数校验散落在每个工具体内（``tools.py`` 里各写各的 ``min(max(...))`` /
    ``_parse_uuid`` / ``_parse_kind``），加一个工具要记得手写校验、漏一处就漂移。本契约
    把边界下沉为 ``ToolMeta`` 上的强类型声明，Loop 在执行前统一校验（``validate_param_contract``），
    违规即 fail-closed 拒收——对抗「Agent 自主拼接参数穷举爆破」的源头拦截（决策防线②）。

    - ``min`` / ``max``：int/float 参数的闭区间边界（如 ``limit ∈ [1,100]``）。
    - ``enum``：str 参数的允许取值集合（如 ``kind ∈ {constraint, ...}``）。
    - ``pattern``：str 参数的正则（如 UUID 格式）。
    - ``max_length``：str 参数的最大长度。
    - ``required``：必填参数名集合。

    未声明的字段不校验（向后兼容）；声明了才在校验时逐项检查。
    """

    min: dict[str, float] = field(default_factory=dict)
    max: dict[str, float] = field(default_factory=dict)
    enum: dict[str, frozenset[str]] = field(default_factory=dict)
    pattern: dict[str, str] = field(default_factory=dict)
    max_length: dict[str, int] = field(default_factory=dict)
    required: frozenset[str] = frozenset()


def validate_param_contract(
    name: str, args: dict[str, Any] | None, contract: ParamContract | None
) -> str | None:
    """校验工具入参是否满足参数契约；返回违规描述（供 fail-closed 拒收），合规返回 None。

    文档防线②的落地：参数校验应在「请求抵达后端之前」统一拦截，而非散落工具体内。
    本函数是纯函数（无副作用），供 reactive 的 tool 节点与 planner 的 run_tool 在执行前
    共用同一套契约。
    """
    if contract is None:
        return None
    args = args or {}
    for field_name in contract.required:
        value = args.get(field_name)
        if value is None or value == "":
            return f"{name} 参数校验失败：缺少必填参数 {field_name!r}"
    for field_name, lo in contract.min.items():
        value = args.get(field_name)
        if value is not None and isinstance(value, (int, float)) and value < lo:
            return f"{name} 参数校验失败：{field_name}={value} 低于最小值 {lo}"
    for field_name, hi in contract.max.items():
        value = args.get(field_name)
        if value is not None and isinstance(value, (int, float)) and value > hi:
            return f"{name} 参数校验失败：{field_name}={value} 超过最大值 {hi}"
    for field_name, allowed in contract.enum.items():
        value = args.get(field_name)
        if value is not None and value not in allowed:
            return (
                f"{name} 参数校验失败：{field_name}={value!r} 不是合法取值"
                f"（{'/'.join(sorted(allowed))}）"
            )
    for field_name, pattern in contract.pattern.items():
        value = args.get(field_name)
        if value is not None and not re.fullmatch(pattern, str(value)):
            return f"{name} 参数校验失败：{field_name} 不符合格式要求"
    for field_name, max_len in contract.max_length.items():
        value = args.get(field_name)
        if value is not None and isinstance(value, str) and len(value) > max_len:
            return f"{name} 参数校验失败：{field_name} 长度 {len(value)} 超过上限 {max_len}"
    return None


@dataclass(frozen=True)
class ToolMeta:
    """工具安全指纹：注册时声明，Loop 据此裁决。

    - ``side_effect_level``：副作用等级（写工具 ≥ MEDIUM，删/冻结类高危 = HIGH）
    - ``source``：结果来源评级（kb/web/tool_result），供零信任打分
    - ``estimated_latency_ms``：预计延迟，超时上限 = 此值 × :data:`LATENCY_TIMEOUT_FACTOR`
    - ``enforced_idempotent``：写工具是否由 Loop 强制注入「业务意图」幂等键（执行契约）
    - ``idempotency_key_fields``：构成「业务身份」的参数字段（幂等键哈希这些字段）。空则
      不注入；``create_note`` 用 ``("content",)`` 对齐 content_hash 唯一索引，``write_memory``
      用 ``("kind","content","entity_id")``。键 = scope + tool_name + 这些字段的哈希，同一
      业务意图跨重试/崩溃拿到同一键，而不是「第几次调用」的位置序号。
    - ``tier``：加载层级（l1 常驻 / l2 角色注入 / l3 冷检索），供未来 L1/L2/L3 懒加载路由
    - ``output_contract``：结果的交货标准（上行契约），None 则结果原样流向 synthesizer
    - ``param_contract``：入参的声明式边界（下行参数契约，防线②），None 则入参不校验
    - ``resource``：工具所属共享依赖（级联熔断，如 ``db``/``web``/``file``）；None 不参与级联。
      任一工具失败累加 ``resource:<name>`` 的共享计数，共享依赖挂 → 同资源工具一起熔断。
    """

    name: str
    side_effect_level: SideEffectLevel
    source: str
    estimated_latency_ms: int
    enforced_idempotent: bool = False
    idempotency_key_fields: tuple[str, ...] = ()  # 「业务身份」字段（幂等键哈希源，见类 docstring）
    tier: str = "l1"  # 加载层级（l1 常驻 / l2 角色注入 / l3 冷检索），当前全 l1，为未来懒加载埋点
    output_contract: OutputContract | None = None
    param_contract: ParamContract | None = None
    resource: str | None = None  # 级联熔断的共享依赖名（None = 不参与级联）

    @property
    def is_readonly(self) -> bool:
        return self.side_effect_level is SideEffectLevel.LOW

    @property
    def has_side_effect(self) -> bool:
        return not self.is_readonly


# 超时上限 = estimated_latency_ms × 此系数
LATENCY_TIMEOUT_FACTOR = 3

# 单次推理模型可见工具上限（铁律 #1：超过 20 会注意力稀释 + token 吞噬 +
# 位置衰减三重叠加，准确率断崖）。在 build_tools 构建工具集时断言，让「第 21 个工具」在
# 启动/测试阶段即失败，而不是静默降智。
MAX_VISIBLE_TOOLS = 20


# 工具名 → ToolMeta 的注册表（build_tools 产出，Loop 消费）。
ToolRegistry = dict[str, ToolMeta]


def _canonical_digest(payload: dict[str, Any]) -> str:
    """规范化参数字典的 sha256（键排序 + default=str，确保跨调用稳定、参数顺序无关）。"""
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def idempotency_key_for(
    tool_name: str, args: dict[str, Any], key_fields: tuple[str, ...], scope: str
) -> str:
    """业务意图幂等键：``scope:tool_name:sha256(规范化 key_fields)``（执行契约）。

    键标识「一次业务意图」（``key_fields`` 声明的业务唯一键），而非位置序号——同一意图
    跨重试/崩溃/回环重发拿到同一个键；不同参数则键自然不同（去重）。同键不同参数的
    冲突检测由 :func:`request_hash_for` 配合幂等表（``repositories/idempotency.py``）完成。
    """
    payload = {field: args.get(field) for field in key_fields}
    return f"{scope}:{tool_name}:{_canonical_digest(payload)}"


def request_hash_for(args: dict[str, Any]) -> str:
    """完整业务参数指纹（同键不同参数冲突检测）：传入全部业务参数（不含框架注入键）。"""
    return _canonical_digest(args)
