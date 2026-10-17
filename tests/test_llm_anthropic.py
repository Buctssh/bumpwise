from __future__ import annotations

from types import SimpleNamespace

from bumpwise.config import Settings
from bumpwise.llm.anthropic_client import AnthropicClient


class FakeBlock:
    def __init__(self, data: dict) -> None:
        self._data = data

    def model_dump(self, exclude_none: bool = False) -> dict:
        return dict(self._data)


def make_client(usage: SimpleNamespace) -> AnthropicClient:
    """创建客户端, 再把 SDK 换成假的: 不联网, 只检查返回值是怎么转换成 LLMResponse 的"""
    settings = Settings(provider="fake", api_format="anthropic", api_key="x", base_url=None, model="m")
    client = AnthropicClient(settings)
    response = SimpleNamespace(
        content=[FakeBlock({"type": "text", "text": "hi"})], stop_reason="end_turn", usage=usage
    )
    client._client = SimpleNamespace(messages=SimpleNamespace(create=lambda **kwargs: response))
    return client


def test_reads_cache_tokens():
    # 运行 001: 只读取 input_tokens, 命中缓存的部分被漏掉了, 统计到的输入严重偏低
    usage = SimpleNamespace(input_tokens=144, output_tokens=10, cache_read_input_tokens=30000, cache_creation_input_tokens=500)
    resp = make_client(usage).create("system", [], [{"role": "user", "content": "x"}])
    assert (resp.input_tokens, resp.cache_read_tokens, resp.cache_write_tokens) == (144, 30000, 500)
    assert resp.total_input_tokens == 30644
    assert resp.text == "hi"


def test_missing_cache_fields_are_zero():
    # 有些兼容接口不返回缓存字段, 或者返回 None
    usage = SimpleNamespace(input_tokens=5, output_tokens=1, cache_read_input_tokens=None)
    resp = make_client(usage).create("system", [], [])
    assert (resp.cache_read_tokens, resp.cache_write_tokens) == (0, 0)
