"""Load one read-only MCP config for Claude Code and OpenCode runtimes."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

_SERVER_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_ENV_REF_RE = re.compile(
    r"^(?:\{env:([A-Za-z_][A-Za-z0-9_]*)\}|\$\{([A-Za-z_][A-Za-z0-9_]*)\})$"
)


@dataclass(frozen=True)
class LoadedMcpConfig:
    path: Path
    claude_servers: dict[str, dict[str, Any]]
    opencode_servers: dict[str, dict[str, Any]]
    revision: str

    @property
    def server_names(self) -> tuple[str, ...]:
        return tuple(sorted(self.claude_servers))


def _resolve_env(value: object, *, field: str, environment: dict[str, str]) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} 必须是字符串")
    match = _ENV_REF_RE.fullmatch(value.strip())
    if not match:
        return value
    name = match.group(1) or match.group(2)
    resolved = environment.get(name)
    if resolved is None:
        raise ValueError(f"{field} 引用的环境变量 {name} 未设置")
    return resolved


def _string_map(
    value: object, *, field: str, environment: dict[str, str]
) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{field} 必须是对象")
    return {
        str(key): _resolve_env(item, field=f"{field}.{key}", environment=environment)
        for key, item in value.items()
    }


def load_mcp_config(
    path_value: str | Path | None, *, environment: dict[str, str] | None = None
) -> LoadedMcpConfig:
    """Read and validate a Claude-style ``mcpServers`` JSON document.

    A missing/empty path intentionally means that MCP is disabled. A configured
    path that does not exist is an error so broken container mounts are visible.
    """
    if path_value is None or not str(path_value).strip():
        return LoadedMcpConfig(Path(), {}, {}, "")
    resolved_environment = {**os.environ, **(environment or {})}

    path = Path(path_value).expanduser().resolve()
    if not path.exists():
        raise ValueError(f"MCP 配置文件不存在: {path}")
    if not path.is_file():
        raise ValueError(f"MCP 配置路径不是文件: {path}")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"MCP 配置不是有效 JSON: {path}:{exc.lineno}:{exc.colno}"
        ) from exc
    if not isinstance(document, dict):
        raise ValueError("MCP 配置根节点必须是对象")
    raw_servers = document.get("mcpServers")
    if not isinstance(raw_servers, dict):
        raise ValueError("MCP 配置根节点必须包含 mcpServers 对象")
    if len(raw_servers) > 64:
        raise ValueError("MCP Server 不能超过 64 个")

    claude_servers: dict[str, dict[str, Any]] = {}
    opencode_servers: dict[str, dict[str, Any]] = {}
    for name, raw in raw_servers.items():
        if not isinstance(name, str) or not _SERVER_NAME_RE.fullmatch(name):
            raise ValueError(
                f"MCP 名称 {name!r} 无效：仅支持 1-64 位字母、数字、下划线和连字符"
            )
        if not isinstance(raw, dict):
            raise ValueError(f"MCP Server {name!r} 配置必须是对象")
        if raw.get("disabled") is True or raw.get("enabled") is False:
            continue

        transport = str(raw.get("type") or ("stdio" if raw.get("command") else "http"))
        if transport == "stdio":
            command = _resolve_env(
                raw.get("command"),
                field=f"{name}.command",
                environment=resolved_environment,
            ).strip()
            if not command:
                raise ValueError(f"stdio MCP {name!r} 缺少 command")
            raw_args = raw.get("args", [])
            if not isinstance(raw_args, list):
                raise ValueError(f"{name}.args 必须是数组")
            args = [
                _resolve_env(
                    item,
                    field=f"{name}.args[{index}]",
                    environment=resolved_environment,
                )
                for index, item in enumerate(raw_args)
            ]
            env = _string_map(
                raw.get("env"),
                field=f"{name}.env",
                environment=resolved_environment,
            )
            claude = {"type": "stdio", "command": command, "args": args}
            if env:
                claude["env"] = env
            opencode: dict[str, Any] = {
                "type": "local",
                "command": [command, *args],
                "enabled": True,
            }
            if env:
                opencode["environment"] = env
            if raw.get("cwd") is not None:
                opencode["cwd"] = _resolve_env(
                    raw["cwd"],
                    field=f"{name}.cwd",
                    environment=resolved_environment,
                )
        elif transport in {"http", "sse", "streamable-http"}:
            url = _resolve_env(
                raw.get("url"),
                field=f"{name}.url",
                environment=resolved_environment,
            ).strip()
            parsed = urlsplit(url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError(f"远程 MCP {name!r} 必须配置有效的 http(s) URL")
            headers = _string_map(
                raw.get("headers"),
                field=f"{name}.headers",
                environment=resolved_environment,
            )
            claude_type = "sse" if transport == "sse" else "http"
            claude = {"type": claude_type, "url": url}
            if headers:
                claude["headers"] = headers
            opencode = {"type": "remote", "url": url, "enabled": True}
            if headers:
                opencode["headers"] = headers
            if "oauth" in raw:
                opencode["oauth"] = raw["oauth"]
        else:
            raise ValueError(
                f"MCP Server {name!r} type={transport!r} 不受支持；"
                "可选 stdio/http/sse/streamable-http"
            )

        claude_servers[name] = claude
        opencode_servers[name] = opencode

    fingerprint = json.dumps(
        {"claude": claude_servers, "opencode": opencode_servers},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    revision = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()
    return LoadedMcpConfig(path, claude_servers, opencode_servers, revision)


def merge_opencode_mcp_config(
    inline_content: str, servers: dict[str, dict[str, Any]]
) -> str:
    """Merge canonical MCP servers into OpenCode's inline JSON config."""
    if inline_content.strip():
        try:
            config = json.loads(inline_content)
        except json.JSONDecodeError as exc:
            raise ValueError(
                "OPENCODE_CONFIG_CONTENT 必须是有效 JSON，才能与 MCP_CONFIG_PATH 合并"
            ) from exc
        if not isinstance(config, dict):
            raise ValueError("OPENCODE_CONFIG_CONTENT 根节点必须是对象")
    else:
        config = {}
    if servers:
        existing = config.get("mcp", {})
        if existing is None:
            existing = {}
        if not isinstance(existing, dict):
            raise ValueError("OpenCode 配置中的 mcp 必须是对象")
        config["mcp"] = {**existing, **servers}
    return (
        json.dumps(config, ensure_ascii=False, separators=(",", ":")) if config else ""
    )
