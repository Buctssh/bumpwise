from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


# 项目根目录
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 一次任务默认的 token 预算 (预算怎样控制运行, 见 harness/budget.py; 默认值是多少, 属于配置).
# 暂定 (ADR-018): 003, 004 测试通过时分别用了 122 万和 109 万, 但那时还没有做完验证. 只有 2 次运行, V6 用"预算和通过率"的曲线校准
DEFAULT_TOKEN_BUDGET = 2_000_000


@dataclass(frozen=True)
class ProviderProfile:
    """一家模型服务商的接入方式"""

    api_key_env: str                # 密钥存放在哪个环境变量里
    base_url_env: str | None        # 请求地址存放在哪个环境变量里, None 表示使用 SDK 的默认地址
    default_model: str              # 没有设置 LLM_MODEL 时使用的模型

    api_format: str = "anthropic"  # 接口格式: anthropic 或 openai, 决定使用哪个客户端


PROVIDERS: dict[str, ProviderProfile] = {
    "deepseek-anthropic": ProviderProfile(
        api_key_env="DEEPSEEK_API_KEY",
        base_url_env="DEEPSEEK_ANTHROPIC_BASE_URL",
        default_model="deepseek-flash"
    ),
    "deepseek-openai": ProviderProfile(
        api_key_env="DEEPSEEK_API_KEY",
        base_url_env="DEEPSEEK_OPENAI_BASE_URL",
        default_model="deepseek-flash",
        api_format="openai"
    )
}


@dataclass(frozen=True)
class Settings:
    """运行一次 agent 所需的全部配置"""

    provider: str
    api_format: str
    api_key: str
    base_url: str | None
    model: str
    max_tokens: int = 8192                      # 模型单次回复最多生成多少 token
    # 正常的限制是 token 预算 (见 harness/budget.py), 轮数只是断路器, 正常情况下不应该触发.
    # 80: 按 003, 004 的增长速度推算, 默认预算大约在第 45 到 53 轮用完, 留出余量 (ADR-018)
    max_turns: int = 80                         # 一次任务最多调用模型多少次
    token_budget: int = DEFAULT_TOKEN_BUDGET     # 一次任务最多处理多少 token. 题目里可以单独设置 ([budget])
    command_timeout: int = 60                   # 执行一个命令的超时时间,单位秒
    max_output_chars: int = 20_000              # 工具返回模型的内容最多多少字符,超出就截断
    llm_max_retries: int = 3                    # 调用模型失败时，SDK 自动重试的次数
    llm_timeout: float = 300.0                  # 调用模型一次的超时时间，单位秒
    runs_dir: Path = PROJECT_ROOT / "runs"     # 每次运行的记录保存在这里
    workspace_dir: Path = PROJECT_ROOT / "workspace"   # 本地镜像和每次运行的工作区 (见 task/workspace.py)
    evals_dir: Path = PROJECT_ROOT / "evals"            # 评测的输出: 每次实验的清单和报告 (见 evaluation/batch.py)
    memories_dir: Path = PROJECT_ROOT / "memories"      # 经验卡片 (见 memory/, ADR-024)

    @classmethod
    def from_env(cls) -> Settings:
        """从项目根目录的 .env 和环境变量中读取配置"""
        load_dotenv(PROJECT_ROOT / ".env")

        provider = os.getenv("LLM_PROVIDER", "").strip().lower()
        if provider not in PROVIDERS:
            supported = ", ".join(PROVIDERS)
            raise RuntimeError(
                f"LLM_PROVIDER 设置为 {provider!r}，这不是支持的服务商。"
                f"请在 .env 中设置为以下之一：{supported}"
            )

        profile = PROVIDERS[provider]

        api_key = os.getenv(profile.api_key_env)
        if not api_key:
            raise RuntimeError(f"缺少 {profile.api_key_env}，请在 .env 中设置")

        base_url = None
        if profile.base_url_env:
            base_url = os.getenv(profile.base_url_env)
            if not base_url:
                raise RuntimeError(f"缺少 {profile.base_url_env}，请在 .env 中设置")

        return cls(
            provider=provider,
            api_format=profile.api_format,
            api_key=api_key,
            base_url=base_url,
            model=os.getenv("LLM_MODEL") or profile.default_model,
            # 默认值直接取字段的默认值, 不在这里重复写 40
            max_turns=int(os.getenv("BUMPWISE_MAX_TURNS") or cls.max_turns),
            token_budget=int(os.getenv("BUMPWISE_TOKEN_BUDGET") or cls.token_budget),
        )
