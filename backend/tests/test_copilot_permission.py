"""Agent 权限系统（防线②③④）测试：参数契约 / 出站脱敏 / 安全熔断 / 工具调用量上限。

对应「RBAC 挡不住 Agent」四条防线里单用户项目可落地的三条：
- 防线② 参数级校验：``ParamContract`` + 图内工具节点统一拦截（穷举爆破源头遏制）。
- 防线③ DLP：``redact_sensitive`` 补中国场景 PII + LLM 网关出站脱敏。
- 防线④ 运行时熔断：``SecurityBreaker``（手动恢复，区别于基础设施熔断）+ 工具调用量第五轴。
"""

import uuid

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool

from app.agent.gateway import LLMGateway
from app.agent.guardrail.sensitive import redact_sensitive
from app.agent.resilience.security_breaker import SecurityBreaker, SecurityBreakerTripped
from app.agent.runtime.budget import BudgetExceeded, BudgetTracker, HardBudget
from app.agent.runtime.config import RuntimeConfig
from app.agent.runtime.reactive import build_reactive_graph
from app.agent.toolmeta import (
    ParamContract,
    SideEffectLevel,
    ToolMeta,
    validate_param_contract,
)
from app.integrations.llm import ChatMessage
from tests.fakes import ScriptedLLM

# --- 防线③：敏感信息脱敏（中国场景 PII） ---


def test_redact_chinese_id_card() -> None:
    assert redact_sensitive("身份证号 11010119900307777X") == "身份证号 [已脱敏]"
    # CJK 与数字相邻时（无空格）也能命中（数字边界断言替代 \b）
    assert redact_sensitive("身份证号11010119900307777X") == "身份证号[已脱敏]"


def test_redact_id_card_takes_precedence_over_phone() -> None:
    """18 位身份证若以 13-19 开头，须整段脱敏而非被手机号正则截走前 11 位。"""
    assert redact_sensitive("13050319900307777X") == "[已脱敏]"


def test_redact_bank_card() -> None:
    assert redact_sensitive("卡号 6222020202020202") == "卡号 [已脱敏]"


def test_redact_passport() -> None:
    assert redact_sensitive("护照 E12345678") == "护照 [已脱敏]"


def test_redact_does_not_touch_short_numbers() -> None:
    """不该误伤普通短数字（时间戳/页码等）。"""
    assert redact_sensitive("第 3 页，共 202 条") == "第 3 页，共 202 条"


# --- 防线③：LLM 网关出站脱敏 ---


async def test_gateway_complete_redacts_pii_before_send() -> None:
    llm = ScriptedLLM(["ok"])
    gateway = LLMGateway(llm=llm)
    await gateway.complete(
        "review", [ChatMessage("user", "手机号 13800138000 身份证 11010119900307777X")]
    )
    sent = llm.calls[-1][0].content
    assert "13800138000" not in sent
    assert "11010119900307777X" not in sent
    assert "[已脱敏]" in sent


async def test_gateway_complete_dlp_off_passes_through() -> None:
    llm = ScriptedLLM(["ok"])
    gateway = LLMGateway(llm=llm, dlp_redact=False)
    await gateway.complete("review", [ChatMessage("user", "手机号 13800138000")])
    sent = llm.calls[-1][0].content
    assert "13800138000" in sent


# --- 防线②：参数契约（纯函数） ---


def test_param_contract_min_max() -> None:
    contract = ParamContract(min={"limit": 1, "offset": 0}, max={"limit": 100})
    assert validate_param_contract("list_notes", {"limit": 1000}, contract) is not None
    assert validate_param_contract("list_notes", {"limit": 0}, contract) is not None
    assert validate_param_contract("list_notes", {"limit": 50, "offset": 0}, contract) is None


def test_param_contract_enum() -> None:
    contract = ParamContract(enum={"kind": frozenset({"soul", "user"})})
    assert validate_param_contract("update_profile", {"kind": "bogus"}, contract) is not None
    assert validate_param_contract("update_profile", {"kind": "soul"}, contract) is None


def test_param_contract_pattern() -> None:
    contract = ParamContract(
        pattern={
            "note_id": r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
            r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
        }
    )
    assert validate_param_contract("read_note", {"note_id": "not-a-uuid"}, contract) is not None
    assert validate_param_contract("read_note", {"note_id": str(uuid.uuid4())}, contract) is None


def test_param_contract_none_passthrough() -> None:
    assert validate_param_contract("x", {"a": 1}, None) is None


# --- 防线②：工具调用量第五轴 ---


def test_tracker_tool_call_limit() -> None:
    tracker = BudgetTracker(HardBudget(max_tool_calls=2))
    tracker.record_tool_calls(1)  # count=1，未超
    tracker.record_tool_calls(1)  # count=2，恰好到上限（仍放行）
    assert tracker.tool_call_count == 2
    with pytest.raises(BudgetExceeded):
        tracker.record_tool_calls(1)  # count=3 > 2 → 超限


def test_tracker_tool_call_unlimited_by_default() -> None:
    tracker = BudgetTracker(HardBudget())
    tracker.record_tool_calls(1000)  # 默认 max_tool_calls=None，不限
    assert tracker.tool_call_count == 1000


# --- 防线④：安全熔断（手动恢复） ---


def test_security_breaker_trips_and_manual_reset() -> None:
    breaker = SecurityBreaker(threshold=2)
    assert breaker.record_violation() is False
    assert breaker.record_violation() is True  # 达阈值 → 熔断
    assert breaker.is_tripped() is True
    assert breaker.record_violation() is False  # 已熔断后不再累计
    breaker.reset()
    assert breaker.is_tripped() is False


# --- 图内集成：参数契约拒收 + 安全熔断冻结 + 工具调用量上限 ---


class _ScriptedModel(FakeMessagesListChatModel):
    def bind_tools(self, tools: object, **kwargs: object) -> "_ScriptedModel":
        return self


def _registry_for_limit() -> dict[str, ToolMeta]:
    return {
        "list_notes": ToolMeta(
            "list_notes",
            SideEffectLevel.LOW,
            "tool_result",
            100,
            param_contract=ParamContract(min={"limit": 1}, max={"limit": 100}),
        )
    }


async def test_graph_rejects_invalid_param_and_runs_valid() -> None:
    calls: list[int] = []

    @tool
    async def list_notes(limit: int = 50) -> str:
        """列出笔记。"""
        calls.append(limit)
        return "ok"

    model = _ScriptedModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[{"name": "list_notes", "args": {"limit": 1000}, "id": "c1"}],
            ),
            AIMessage(
                content="",
                tool_calls=[{"name": "list_notes", "args": {"limit": 10}, "id": "c2"}],
            ),
            AIMessage(content="done"),
        ]
    )
    graph = build_reactive_graph(
        model, [list_notes], registry=_registry_for_limit(), runtime=RuntimeConfig()
    )
    initial = {
        "messages": [HumanMessage(content="hi")],
        "attempts": 0,
        "review_verdict": "",
        "review_issues": [],
        "correction": "",
    }
    async for _ in graph.astream(initial, stream_mode="updates"):
        pass
    # 非法 limit=1000 被拒收，只执行了合法 limit=10
    assert calls == [10]


async def test_graph_security_breaker_freezes_on_repeated_violations() -> None:
    calls: list[int] = []

    @tool
    async def list_notes(limit: int = 50) -> str:
        """列出笔记。"""
        calls.append(limit)
        return "ok"

    model = _ScriptedModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[{"name": "list_notes", "args": {"limit": 1000}, "id": f"c{i}"}],
            )
            for i in range(5)
        ]
    )
    graph = build_reactive_graph(
        model,
        [list_notes],
        registry=_registry_for_limit(),
        security_breaker=SecurityBreaker(threshold=2),
        runtime=RuntimeConfig(),
    )
    initial = {
        "messages": [HumanMessage(content="hi")],
        "attempts": 0,
        "review_verdict": "",
        "review_issues": [],
        "correction": "",
    }
    with pytest.raises(SecurityBreakerTripped):
        async for _ in graph.astream(initial, stream_mode="updates"):
            pass
    assert calls == []  # 熔断前所有违规调用都被拒收，工具从未真正执行


async def test_graph_tool_call_budget_terminates() -> None:
    calls: list[int] = []

    @tool
    async def list_notes(limit: int = 50) -> str:
        """列出笔记。"""
        calls.append(limit)
        return "ok"

    model = _ScriptedModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[{"name": "list_notes", "args": {"limit": 10}, "id": "c1"}],
            ),
            AIMessage(
                content="",
                tool_calls=[{"name": "list_notes", "args": {"limit": 20}, "id": "c2"}],
            ),
        ]
    )
    graph = build_reactive_graph(
        model,
        [list_notes],
        registry=_registry_for_limit(),
        runtime=RuntimeConfig(budget=HardBudget(max_tool_calls=1)),
    )
    initial = {
        "messages": [HumanMessage(content="hi")],
        "attempts": 0,
        "review_verdict": "",
        "review_issues": [],
        "correction": "",
    }
    with pytest.raises(BudgetExceeded):
        async for _ in graph.astream(initial, stream_mode="updates"):
            pass
    assert calls == [10]  # 只执行了第一次，第二次被第五轴预算拦截
