"""LLM 意图分类器测试：plan/qa/task 映射 + 解析失败回退 None。"""

from app.agent.runtime.intent_classifier import LLMIntentClassifier
from app.agent.runtime.router import Intent
from tests.fakes import ScriptedLLM, gateway_run, make_gateway


async def test_intent_classifier_maps_plan() -> None:
    llm = ScriptedLLM(['{"intent":"plan"}'])
    classifier = LLMIntentClassifier(make_gateway(llm=llm))
    async with gateway_run():
        result = await classifier.classify("遍历知识库所有文档总结成笔记")
    assert result is Intent.PLAN


async def test_intent_classifier_maps_qa() -> None:
    llm = ScriptedLLM(['{"intent":"qa"}'])
    classifier = LLMIntentClassifier(make_gateway(llm=llm))
    async with gateway_run():
        result = await classifier.classify("向量检索是什么")
    assert result is Intent.QA


async def test_intent_classifier_maps_task() -> None:
    llm = ScriptedLLM(['{"intent":"task"}'])
    classifier = LLMIntentClassifier(make_gateway(llm=llm))
    async with gateway_run():
        result = await classifier.classify("帮我写一条笔记")
    assert result is Intent.TASK


async def test_intent_classifier_fallback_none_on_parse_failure() -> None:
    """解析失败 → 回退 None（run.py 按 TASK 处理），不静默升级成 plan。"""
    llm = ScriptedLLM(["not json"])
    classifier = LLMIntentClassifier(make_gateway(llm=llm))
    async with gateway_run():
        result = await classifier.classify("随便")
    assert result is None
