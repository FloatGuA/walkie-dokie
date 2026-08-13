"""Replaceable provider boundaries for document parsing."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from .domain import ParseResult


class UnsupportedSourceError(ValueError):
    pass


@runtime_checkable
class ParserProvider(Protocol):
    name: str
    version: str

    def supports(self, path: Path) -> bool: ...

    def parse(self, path: Path) -> ParseResult: ...


class ParserRegistry:
    """Explicit parser routing; no filename-based fallback hidden inside an Agent."""

    def __init__(self, providers: tuple[ParserProvider, ...] | list[ParserProvider]):
        self._providers = {provider.name: provider for provider in providers}
        if not self._providers:
            raise ValueError("至少需要一个 ParserProvider")

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._providers)

    def resolve(self, path: Path, *, provider_name: str | None = None) -> ParserProvider:
        if provider_name is not None:
            try:
                provider = self._providers[provider_name]
            except KeyError as exc:
                raise UnsupportedSourceError(
                    f"未知 ParserProvider：{provider_name!r}"
                ) from exc
            if not provider.supports(path):
                raise UnsupportedSourceError(
                    f"ParserProvider {provider_name!r} 不支持 {path.suffix!r}"
                )
            return provider

        matches = [provider for provider in self._providers.values() if provider.supports(path)]
        if len(matches) != 1:
            raise UnsupportedSourceError(
                f"文件 {path.name!r} 匹配到 {len(matches)} 个 ParserProvider，需要显式选择"
            )
        return matches[0]
