from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# backend/ 目录（相对本文件向上三级：core -> app -> backend）
BACKEND_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BACKEND_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # App
    app_name: str = "kima"
    environment: str = "development"
    debug: bool = True
    api_prefix: str = "/api"

    # Database
    database_url: str = "postgresql+asyncpg://kima:kima@localhost:5432/kima"

    # LLM（DeepSeek，OpenAI 兼容）
    llm_provider: str = "fake"
    llm_api_key: str = ""
    llm_base_url: str = "https://api.deepseek.com"
    llm_model: str = "deepseek-flash"

    # Embedding（SiliconFlow）
    embedding_provider: str = "fake"
    embedding_api_key: str = ""
    embedding_base_url: str = "https://api.siliconflow.cn/v1"
    embedding_model: str = "BAAI/bge-m3"
    embedding_dim: int = 1024

    # Rerank（SiliconFlow）
    rerank_provider: str = "fake"
    rerank_api_key: str = ""
    rerank_base_url: str = "https://api.siliconflow.cn/v1"
    rerank_model: str = "BAAI/bge-reranker-v2-m3"
    rerank_min_score: float = 0.3

    # Web Search（博查）
    web_search_provider: str = "fake"
    web_search_api_key: str = ""
    web_search_base_url: str = "https://api.bochaai.com/v1"

    # MinerU
    mineru_api_base_url: str = "https://mineru.net"
    mineru_api_token: str = ""

    # RAG 上下文组装
    context_max_tokens: int = 6000
    history_recent_turns: int = 3

    # 笔记向量化（idle 触发）
    note_revectorize_idle_seconds: int = 120

    # Copilot 记忆（四型：约束/事实/偏好/情节）
    memory_episodic_ttl_days: int = 30  # 情节记忆 TTL（激活衰减用）
    memory_recency_window_days: int = 7  # recency 窗：last_access 在此窗口内 +0.3
    memory_conflict_top_k: int = 10  # 写记忆冲突判定的 cosine 预筛候选数
    memory_superseded_window_days: int = 7  # 软删除窗口：被 superseded 后 N 天内可召回复活
    memory_revival_similarity: float = 0.5  # 复活阈值：superseded 记忆与 query 余弦相似度 ≥ 此值
    memory_block_max_tokens: int = 2000  # 记忆注入块 token 预算（约束子预算 ≤40%）

    # Copilot 工具
    copilot_max_result_chars: int = 4000  # read/search 工具返回截断阈值
    copilot_idempotency_ttl_seconds: int = 86400  # 写工具幂等记录 TTL（processing 残留回收，秒）

    # Copilot 输出审查（自检节点，必选闸门）：每次 agent 产出后对账「声称 vs 工具轨迹」
    copilot_review_max_attempts: int = 2  # 审查发现不一致时的自动修复重试上限

    # Copilot 四轴预算（硬终止边界，见 agent/runtime/budget.py）
    copilot_budget_max_turns: int = 20
    copilot_budget_max_seconds: float = 120.0
    copilot_budget_max_tokens: int = 100_000
    copilot_budget_max_cost_cny: float = 1.0
    copilot_budget_max_tool_calls: int = 50  # 单 run 工具调用次数上限（穷举爆破第五轴）

    # 大文档警告：文档嵌入预估 token 超过此阈值 → 置 needs_approval，用户确认后再嵌入
    copilot_document_warn_tokens: int = 100_000

    # Copilot 全局日预算（跨 run 硬上限，见 agent/runtime/budget.py 的 DailyBudget）
    copilot_daily_max_cost_cny: float = 10.0
    copilot_daily_max_tokens: int = 1_000_000

    # 实时计费（价格时变主数据 + 调用级成本审计，见 docs/pricing.md）
    copilot_pricing_check_enabled: bool = True  # 启动「未来 3 天覆盖」校验开关
    copilot_pricing_cache_ttl_seconds: int = 60  # resolve 内存缓存 TTL

    # LLM 网关（所有 LLM 出口的统一门禁/记账/快照，见 agent/gateway.py 与 docs/llm-gateway.md）
    copilot_llm_gateway_soft_threshold: float = 0.80  # 任一轴占用 ≥ 此值软提示收尾
    copilot_llm_gateway_retry_attempts: int = 3  # 瞬时异常退避重试（含首次）
    copilot_llm_gateway_timeout_seconds: float = 60.0  # 单次 LLM 调用秒轴硬熔断
    copilot_llm_snapshot_ttl_days: int = 7  # 快照保留天数（防无限膨胀）
    copilot_llm_dlp_redact: bool = True  # 出站 DLP：LLM 上下文发往模型服务商前脱敏
    copilot_cache_hit_rate_warn: float = 0.3  # 前缀缓存命中率告警阈值（低且输入量够大 → warning）

    # Copilot run 内上下文窗口预算（六层 L0-L5，见 agent/runtime/context.py）
    copilot_context_max_tokens: int = 1048576  # window：DeepSeek V4 官方 1M
    copilot_context_l0_ratio: float = 0.08  # L0 system（超仅告警）
    copilot_context_l1_ratio: float = 0.15  # L1 state（超仅告警）
    copilot_context_l2_ratio: float = 0.35  # L2 memory + RAG（超裁剪）
    copilot_context_skills_budget: int = 25000  # L3 skills 全文（超截断）
    copilot_context_safety_margin: int = 500  # L5 计算预留的安全余量

    # Copilot 防循环（死循环/幽灵循环，见 agent/runtime/loop_guard.py）
    copilot_loop_repeat_threshold: int = 5
    copilot_loop_window_size: int = 5
    copilot_loop_stall_threshold: int = 4

    # Copilot HITL：写工具需人工确认（interrupt 挂起 + resume）。
    # 分级审批（Policy-as-Code，见 agent/approval.py）：graded = 仅 HIGH（覆盖人设档案）审批、
    # MEDIUM（建笔记/写记忆）自动放行 + 事后审计；strict = 所有写操作审批（旧布尔语义）。
    copilot_require_write_approval: bool = False
    copilot_approval_mode: str = "graded"  # graded | strict
    copilot_approval_timeout_seconds: float = 900.0  # 审批超时 fail-close（默认 15 分钟）

    # Copilot 安全熔断（防线④，手动恢复，见 agent/resilience/security_breaker.py）：
    # 连续 N 次参数契约违规（越界/穷举尝试）→ 冻结 run，需运维介入 reset（不自动恢复）
    copilot_security_breaker_enabled: bool = False
    copilot_security_breaker_threshold: int = 5

    # Worker 偷懒（事件驱动 idle 兜底，秒）
    worker_idle_fallback_seconds: int = 300

    # LangFuse 可观测（无 key 默认关）
    langfuse_provider: str = ""  # 空 | cloud
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"

    # CORS
    # NoDecode：env 里是逗号分隔字符串，跳过 pydantic-settings 的 JSON 解码，交给下方 _split_cors
    cors_origins: Annotated[list[str], NoDecode] = ["http://localhost:5173"]

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_cors(cls, value: object) -> object:
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @property
    def is_production(self) -> bool:
        return self.environment.lower() == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
