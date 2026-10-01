"""契约交互：上行 OutputContract 校验 + 约束显式携带（planner/synthesizer）+ 输出自检。"""



from app.agent.runtime.planner import LLMPlanner
from app.agent.toolmeta import OutputContract, apply_output_contract
from tests.fakes import (
    ScriptedLLM,
    gateway_run,
    make_gateway,
)

# --- OutputContract 上行契约关（纯函数） ---


def test_contract_no_contract_passthrough() -> None:
    assert apply_output_contract("search", "hello", None) == "hello"


def test_contract_max_chars_clips() -> None:
    contract = OutputContract(max_chars=5)
    assert apply_output_contract("search", "hello world", contract) == "hello…"


def test_contract_required_missing_rejects() -> None:
    contract = OutputContract(required={"output": str})
    out = apply_output_contract("search", '{"state": "x"}', contract)
    assert "缺必填字段" in out and "output" in out


def test_contract_type_mismatch_rejects() -> None:
    contract = OutputContract(required={"output": str})
    out = apply_output_contract("search", '{"output": 1}', contract)
    assert "类型不符" in out


def test_contract_whitelist_strips_undeclared() -> None:
    contract = OutputContract(required={"output": str}, optional={"state": str})
    out = apply_output_contract("search", '{"output":"o","state":"s","reasoning":"秘密"}', contract)
    assert "output" in out and "state" in out
    assert "reasoning" not in out


def test_contract_rejects_non_json() -> None:
    contract = OutputContract(required={"output": str})
    out = apply_output_contract("search", "不是 JSON", contract)
    assert "不是合法 JSON" in out


# --- 约束显式携带（planner 下行任务包） ---


async def test_planner_generate_carries_constraints() -> None:
    llm = ScriptedLLM(['{"steps":[]}'])
    planner = LLMPlanner(make_gateway(llm=llm))
    async with gateway_run():
        await planner.generate("总结文档", ["search"], constraints="[MEMORY]\n- 禁止使用 ORM")
    assert len(llm.calls) == 1
    user_content = llm.calls[0][1].content
    assert "禁止使用 ORM" in user_content
