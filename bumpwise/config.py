from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


# 项目根目录
PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class ProviderProfile:
    """一家模型服务商的接入方式"""

    api_key_env: str                # 密钥存放在哪个环境变量里
    base_url_env: str | None        # 请求地址存放在哪个环境变量里, None 表示使用 SDK 的默认地址
    default_model: str              # 没有设置 LLM_MODEL 时使用的模型

    api_format: str = "anthropic"  


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
    max_turns: int = 40                         # 一次任务最多调用模型多少次,防止无限循环
    command_timeout: int = 60                   # 执行一个命令的超时时间,单位秒
    max_output_chars: int = 20_000              # 工具返回模型的内容最多多少字符,超出就截断
    runs_dir: Path = PROJECT_ROOT / "runs"  

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
            max_turns=int(os.getenv("BUMPWISE_MAX_TURNS", "40")),
        )











