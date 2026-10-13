from __future__ import annotations

from ..config import Settings
from . import create_llm_client

ECHO_TOOL = {
    "name": "echo",
    "description": "原样返回传入的文本",
    "input_schema": {
        "type": "object",
        "properties": {"text": {"type": "string", "description": "要返回的文本"}},
        "required": ["text"],
    },
}

if __name__ == "__main__":
    settings = Settings.from_env()
    print(f"provider: {settings.provider}  format: {settings.api_format}  model: {settings.model}")

    client = create_llm_client(settings)
    resp = client.create(
        system="你是一个测试助手。",
        tools=[ECHO_TOOL],
        messages=[{"role": "user", "content": "请调用 echo 工具，参数 text 设为 hello"}],
    )
    print("stop_reason:", resp.stop_reason)
    print("content:", resp.content)
    print("tokens:", resp.input_tokens, "/", resp.output_tokens)