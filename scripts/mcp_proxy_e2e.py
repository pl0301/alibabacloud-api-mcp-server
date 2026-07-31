#!/usr/bin/env python3
"""Run local Proxy -> remote CloudSpec MCP dual-era acceptance checks."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import anyio
from mcp import Client, types
from mcp.client.stdio import StdioServerParameters, stdio_client

RUNSCRIPT_TOOL = "AlibabaCloud___RunScript"
GET_TASK_TOOL = "AlibabaCloud___GetTask"

RUNSCRIPT_SOURCE = (
    "result = await call_cli(product='Ecs', version='2014-05-26', "
    "action='DescribeRegions', params={})"
)

NON_TERMINAL_STATUSES = {
    "Received",
    "ApprovalPending",
    "Queued",
    "Allocating",
    "Running",
}

TERMINAL_STATUSES = {
    "ValidationFailed",
    "Succeeded",
    "Failed",
    "ApprovalRejected",
    "ApprovalExpired",
    "Expired",
}

_SECRET_KEY_FRAGMENTS = {
    "authorization",
    "accesskeyid",
    "accesskeysecret",
    "secretaccesskey",
    "securitytoken",
    "bearertoken",
    "temporaryaccesskey",
    "tmpak",
}

_CREDENTIAL_ENV_NAMES = (
    "ALIBABA_CLOUD_ACCESS_KEY_ID",
    "ALIBABA_CLOUD_ACCESS_KEY_SECRET",
    "ALIBABA_CLOUD_SECURITY_TOKEN",
    "ALIBABACLOUD_MCP_BEARER_TOKEN",
)


def _normalized_key(key: object) -> str:
    return "".join(character for character in str(key).lower() if character.isalnum())


def sanitize_output(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized: dict[object, Any] = {}
        for key, item in value.items():
            normalized = _normalized_key(key)
            if any(fragment in normalized for fragment in _SECRET_KEY_FRAGMENTS):
                sanitized[key] = "<redacted>"
            else:
                sanitized[key] = sanitize_output(item)
        return sanitized
    if isinstance(value, list):
        return [sanitize_output(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_output(item) for item in value]
    return value


def _redact_error_message(message: str) -> str:
    redacted = message
    for env_name in _CREDENTIAL_ENV_NAMES:
        secret = os.environ.get(env_name)
        if secret:
            redacted = redacted.replace(secret, "<redacted>")
    return redacted


def _parse_json_object(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    candidates = [stripped]
    if stripped.startswith("```") and stripped.endswith("```"):
        body = stripped[3:-3].strip()
        if body.startswith("json"):
            body = body[4:].lstrip()
        candidates.append(body)
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start >= 0 and end > start:
        candidates.append(stripped[start : end + 1])

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def extract_result_payload(result: types.CallToolResult) -> dict[str, Any]:
    if isinstance(result.structured_content, dict):
        return result.structured_content

    for content in result.content:
        if isinstance(content, types.TextContent):
            parsed = _parse_json_object(content.text)
            if parsed is not None:
                return parsed

    raise RuntimeError("Tool response did not contain a JSON object payload.")


def extract_process_id(task: dict[str, Any]) -> str:
    process_id = task.get("processID")
    if not isinstance(process_id, str) or not process_id.strip():
        raise RuntimeError("RunScript response did not contain a processID.")
    return process_id


def validate_successful_task(task: dict[str, Any]) -> None:
    process_id = extract_process_id(task)
    status = task.get("status")
    if status != "Succeeded":
        raise RuntimeError(
            f"RunScript failed for processID={process_id}: status={status}"
        )
    if task.get("nextAction") not in (None, "None"):
        raise RuntimeError(
            f"Succeeded task unexpectedly requires a next action: "
            f"{task.get('nextAction')}"
        )
    if task.get("waitTimedOut") is True:
        raise RuntimeError("Succeeded task unexpectedly reports waitTimedOut=true.")
    if not task.get("result"):
        raise RuntimeError(
            "Succeeded task did not contain real call_cli output."
        )


def build_server_parameters(args: argparse.Namespace) -> StdioServerParameters:
    repository_root = Path(__file__).resolve().parents[1]
    child_env = os.environ.copy()
    bearer_token_env = args.bearer_token_env
    if bearer_token_env:
        bearer_token = os.environ.get(bearer_token_env)
        if not bearer_token:
            raise RuntimeError(
                f"Bearer token environment variable is not set: "
                f"{bearer_token_env}"
            )
        child_env["ALIBABACLOUD_MCP_BEARER_TOKEN"] = bearer_token

    proxy_args = [
        "-m",
        "alibabacloud.mcp_proxy",
        "--server-url",
        args.server_url,
        "--read-timeout",
        str(args.proxy_read_timeout_seconds),
        "--debug",
        "--log-file",
        args.log_file,
    ]
    return StdioServerParameters(
        command=sys.executable,
        args=proxy_args,
        env=child_env,
        cwd=repository_root,
    )


def print_json(value: Any) -> None:
    print(
        json.dumps(
            sanitize_output(value),
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
        flush=True,
    )


def build_runscript_arguments() -> dict[str, str]:
    """Return only fields accepted by CloudSpec's strict RunScript schema."""
    return {
        "script": RUNSCRIPT_SOURCE,
    }


async def _run_runscript_smoke(
    client: Client,
    args: argparse.Namespace,
) -> dict[str, Any]:
    run_result = await client.call_tool(
        RUNSCRIPT_TOOL,
        build_runscript_arguments(),
    )
    if run_result.is_error:
        raise RuntimeError("AlibabaCloud___RunScript returned isError=true.")

    start_task = extract_result_payload(run_result)
    process_id = extract_process_id(start_task)
    print_json(
        {
            "phase": "runscript",
            "processID": process_id,
            "status": start_task.get("status"),
            "nextAction": start_task.get("nextAction"),
            "waitTimedOut": start_task.get("waitTimedOut"),
        }
    )

    deadline = anyio.current_time() + args.timeout_seconds
    while True:
        get_task_result = await client.call_tool(
            GET_TASK_TOOL,
            {
                "processID": process_id,
                "waitTimeoutSeconds": 20,
                "pollIntervalSeconds": 2,
            },
        )
        if get_task_result.is_error:
            raise RuntimeError("AlibabaCloud___GetTask returned isError=true.")
        task = extract_result_payload(get_task_result)
        status = task.get("status")
        print_json(
            {
                "phase": "get-task",
                "processID": process_id,
                "status": status,
                "nextAction": task.get("nextAction"),
                "waitTimedOut": task.get("waitTimedOut"),
            }
        )

        if status in TERMINAL_STATUSES:
            validate_successful_task(task)
            print_json(
                {
                    "phase": "runscript-complete",
                    "processID": process_id,
                    "task": task,
                }
            )
            return task
        if status not in NON_TERMINAL_STATUSES:
            raise RuntimeError(
                f"Unknown GetTask status for processID={process_id}: {status}"
            )
        if anyio.current_time() >= deadline:
            raise TimeoutError(
                f"GetTask did not reach terminal state for processID={process_id}"
            )
        await anyio.sleep(args.poll_interval_seconds)


async def run_e2e(args: argparse.Namespace) -> None:
    parameters = build_server_parameters(args)
    transport = stdio_client(parameters)
    async with Client(
        transport,
        mode=args.mode,
        cache=None,
    ) as client:
        if args.mode == "auto" and client.protocol_version != "2026-07-28":
            raise RuntimeError(
                "Modern auto negotiation did not select MCP 2026-07-28: "
                f"{client.protocol_version}"
            )

        capabilities = client.server_capabilities
        print_json(
            {
                "phase": "connected",
                "requestedMode": args.mode,
                "protocolVersion": client.protocol_version,
                "capabilities": capabilities.model_dump(
                    by_alias=True,
                    exclude_none=True,
                ),
            }
        )

        tools_result = await client.list_tools()
        tool_names = [tool.name for tool in tools_result.tools]
        print_json(
            {
                "phase": "tools/list",
                "resultType": tools_result.result_type,
                "toolCount": len(tool_names),
                "requiredToolsPresent": {
                    RUNSCRIPT_TOOL: RUNSCRIPT_TOOL in tool_names,
                    GET_TASK_TOOL: GET_TASK_TOOL in tool_names,
                },
            }
        )
        if args.mode == "auto" and tools_result.result_type != "complete":
            raise RuntimeError(
                f"Modern tools/list returned resultType={tools_result.result_type!r}"
            )
        if args.run_runscript_smoke:
            missing = {
                RUNSCRIPT_TOOL,
                GET_TASK_TOOL,
            }.difference(tool_names)
            if missing:
                raise RuntimeError(
                    f"Required RunScript tools are missing: {sorted(missing)}"
                )
            await _run_runscript_smoke(client, args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Launch the current local Proxy source and test it against a "
            "remote CloudSpec MCP endpoint."
        )
    )
    parser.add_argument("--server-url", required=True)
    parser.add_argument("--mode", choices=("auto", "legacy"), required=True)
    parser.add_argument("--log-file", required=True)
    parser.add_argument(
        "--proxy-read-timeout-seconds",
        type=float,
        default=120.0,
    )
    parser.add_argument(
        "--poll-interval-seconds",
        type=float,
        default=1.0,
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=600.0,
    )
    parser.add_argument(
        "--bearer-token-env",
        help=(
            "Optional environment variable containing a bearer token. The "
            "value is forwarded to the child only through "
            "ALIBABACLOUD_MCP_BEARER_TOKEN."
        ),
    )
    parser.add_argument(
        "--run-runscript-smoke",
        action="store_true",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        anyio.run(run_e2e, args)
    except (RuntimeError, TimeoutError, OSError) as exc:
        print(f"Error: {_redact_error_message(str(exc))}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
