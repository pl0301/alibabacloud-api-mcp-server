from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

import pytest
from mcp import types

if sys.version_info < (3, 11):
    from exceptiongroup import ExceptionGroup

_SCRIPT_PATH = (
    Path(__file__).resolve().parents[1] / "scripts" / "mcp_proxy_e2e.py"
)
_SPEC = importlib.util.spec_from_file_location("mcp_proxy_e2e", _SCRIPT_PATH)
assert _SPEC is not None
assert _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)

NON_TERMINAL_STATUSES = _MODULE.NON_TERMINAL_STATUSES
RUNSCRIPT_SOURCE = _MODULE.RUNSCRIPT_SOURCE
TERMINAL_STATUSES = _MODULE.TERMINAL_STATUSES
build_server_parameters = _MODULE.build_server_parameters
build_runscript_arguments = _MODULE.build_runscript_arguments
extract_result_payload = _MODULE.extract_result_payload
sanitize_output = _MODULE.sanitize_output
validate_successful_task = _MODULE.validate_successful_task


def test_readonly_tool_matrix_uses_strict_nondestructive_arguments() -> None:
    assert _MODULE.build_readonly_tool_cases() == (
        (
            "AlibabaCloud___ListProducts",
            {"filter": "Ecs"},
        ),
        (
            "AlibabaCloud___ListApis",
            {
                "product": "Ecs",
                "apiVersion": "2014-05-26",
                "filter": "DescribeRegions",
                "includeApiDefinition": False,
            },
        ),
        (
            "AlibabaCloud___ListProductRegions",
            {"product": "Ecs"},
        ),
        (
            "AlibabaCloud___GetApiDefinition",
            {
                "product": "Ecs",
                "apiVersion": "2014-05-26",
                "apiName": "DescribeRegions",
            },
        ),
    )


def test_readonly_result_summary_proves_nonempty_response_without_dumping_body() -> None:
    result = types.CallToolResult(
        content=[
            types.TextContent(
                type="text",
                text='{"items":[{"name":"DescribeRegions"}]}',
            )
        ],
        structuredContent={"items": [{"name": "DescribeRegions"}]},
        isError=False,
    )

    assert _MODULE.summarize_readonly_tool_result(
        "AlibabaCloud___GetApiDefinition",
        result,
    ) == {
        "phase": "readonly-tool",
        "tool": "AlibabaCloud___GetApiDefinition",
        "isError": False,
        "contentCount": 1,
        "structuredContentType": "dict",
        "responseNonempty": True,
    }


def test_readonly_result_summary_rejects_empty_response() -> None:
    result = types.CallToolResult(content=[], isError=False)

    with pytest.raises(RuntimeError, match="empty response"):
        _MODULE.summarize_readonly_tool_result(
            "AlibabaCloud___ListProducts",
            result,
        )


@pytest.mark.asyncio
async def test_readonly_smoke_calls_every_case_once(capsys) -> None:
    class RecordingClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        async def call_tool(
            self,
            name: str,
            arguments: dict[str, object],
        ) -> types.CallToolResult:
            self.calls.append((name, arguments))
            return types.CallToolResult(
                content=[
                    types.TextContent(
                        type="text",
                        text='{"ok":true}',
                    )
                ],
                isError=False,
            )

    client = RecordingClient()

    await _MODULE._run_readonly_tool_smoke(client)

    assert tuple(client.calls) == _MODULE.build_readonly_tool_cases()
    assert capsys.readouterr().out.count('"phase": "readonly-tool"') == 4


def test_parser_accepts_fixed_modern_mode_and_repeated_list_calls() -> None:
    args = _MODULE.build_parser().parse_args(
        [
            "--server-url",
            "https://example.com/mcp",
            "--mode",
            "2026-07-28",
            "--log-file",
            "/tmp/proxy.log",
            "--list-repeat-count",
            "3",
            "--parallel-list-count",
            "10",
            "--run-readonly-tool-smoke",
        ]
    )

    assert args.mode == "2026-07-28"
    assert args.list_repeat_count == 3
    assert args.parallel_list_count == 10
    assert args.run_readonly_tool_smoke is True


@pytest.mark.asyncio
async def test_parallel_list_smoke_collects_every_response(capsys) -> None:
    class RecordingClient:
        def __init__(self) -> None:
            self.calls = 0

        async def list_tools(self) -> types.ListToolsResult:
            self.calls += 1
            return types.ListToolsResult(
                tools=[
                    types.Tool(
                        name="ExampleTool",
                        inputSchema={"type": "object"},
                    )
                ],
                resultType="complete",
            )

    client = RecordingClient()

    await _MODULE._run_parallel_list_smoke(
        client,
        count=10,
        require_complete=True,
    )

    output = capsys.readouterr().out
    assert client.calls == 10
    assert '"phase": "tools/list-parallel"' in output
    assert '"requestCount": 10' in output
    assert '"toolCounts": [\n    1' in output


def test_main_collapses_sdk_exception_group_to_safe_leaf_message(
    monkeypatch,
    capsys,
) -> None:
    def raise_group(*args, **kwargs) -> None:
        raise ExceptionGroup(
            "stdio transport failed",
            [
                ExceptionGroup(
                    "client session failed",
                    [RuntimeError("upstream authentication failed")],
                )
            ],
        )

    monkeypatch.setattr(_MODULE.anyio, "run", raise_group)

    exit_code = _MODULE.main(
        [
            "--server-url",
            "https://example.com/mcp",
            "--mode",
            "2026-07-28",
            "--log-file",
            "/tmp/proxy.log",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.err == "Error: upstream authentication failed\n"
    assert "Traceback" not in captured.err


def test_extract_result_payload_from_structured_content() -> None:
    result = types.CallToolResult(
        content=[],
        structuredContent={
            "processID": "cli-structured",
            "status": "Queued",
        },
    )

    assert extract_result_payload(result) == {
        "processID": "cli-structured",
        "status": "Queued",
    }


def test_extract_result_payload_from_json_text_content() -> None:
    result = types.CallToolResult(
        content=[
            types.TextContent(
                type="text",
                text='{"processID":"cli-text","status":"Running"}',
            )
        ]
    )

    assert extract_result_payload(result) == {
        "processID": "cli-text",
        "status": "Running",
    }


def test_status_sets_cover_known_non_terminal_and_terminal_states() -> None:
    assert NON_TERMINAL_STATUSES == {
        "Received",
        "ApprovalPending",
        "Queued",
        "Allocating",
        "Running",
    }
    assert TERMINAL_STATUSES == {
        "ValidationFailed",
        "Succeeded",
        "Failed",
        "ApprovalRejected",
        "ApprovalExpired",
        "Expired",
    }
    assert NON_TERMINAL_STATUSES.isdisjoint(TERMINAL_STATUSES)


def test_successful_get_task_requires_real_result() -> None:
    task = {
        "processID": "cli-success",
        "status": "Succeeded",
        "nextAction": None,
        "waitTimedOut": False,
        "result": {"Regions": {"Region": [{"RegionId": "cn-hangzhou"}]}},
    }

    validate_successful_task(task)


@pytest.mark.parametrize(
    "task, message",
    [
        (
            {
                "processID": "cli-failed",
                "status": "Failed",
                "nextAction": "InspectError",
                "waitTimedOut": False,
                "error": {"code": "OpenApiCallFailed"},
            },
            "status=Failed",
        ),
        (
            {
                "processID": "cli-empty",
                "status": "Succeeded",
                "nextAction": None,
                "waitTimedOut": False,
                "result": None,
            },
            "real call_cli output",
        ),
        (
            {
                "processID": "cli-next",
                "status": "Succeeded",
                "nextAction": "CallGetTaskAgain",
                "waitTimedOut": False,
                "result": {"ok": True},
            },
            "next action",
        ),
    ],
)
def test_invalid_terminal_task_is_rejected(
    task: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(RuntimeError, match=message):
        validate_successful_task(task)


def test_driver_uses_current_python_and_local_source() -> None:
    args = argparse.Namespace(
        server_url="https://example.com/mcp",
        log_file="/tmp/mcp-proxy-e2e.log",
        proxy_read_timeout_seconds=120.0,
        bearer_token_env=None,
    )

    parameters = build_server_parameters(args)

    assert parameters.command == sys.executable
    assert parameters.args[:2] == ["-m", "alibabacloud.mcp_proxy"]
    assert "--server-url" in parameters.args
    assert "https://example.com/mcp" in parameters.args
    assert "--debug" in parameters.args
    assert parameters.cwd is not None


def test_sanitize_output_removes_credential_values_recursively() -> None:
    value = {
        "Authorization": "Bearer secret",
        "nested": {
            "AccessKeyId": "ak-secret",
            "AccessKeySecret": "sk-secret",
            "SecurityToken": "sts-secret",
            "safe": "visible",
        },
        "items": [{"tmpAK": "temporary-secret"}],
    }

    sanitized = sanitize_output(value)

    assert sanitized == {
        "Authorization": "<redacted>",
        "nested": {
            "AccessKeyId": "<redacted>",
            "AccessKeySecret": "<redacted>",
            "SecurityToken": "<redacted>",
            "safe": "visible",
        },
        "items": [{"tmpAK": "<redacted>"}],
    }


def test_runscript_source_calls_read_only_describe_regions() -> None:
    assert "await call_cli" in RUNSCRIPT_SOURCE
    assert "product='Ecs'" in RUNSCRIPT_SOURCE
    assert "version='2014-05-26'" in RUNSCRIPT_SOURCE
    assert "action='DescribeRegions'" in RUNSCRIPT_SOURCE
    assert "params={}" in RUNSCRIPT_SOURCE


def test_runscript_arguments_match_strict_tool_schema() -> None:
    assert build_runscript_arguments() == {
        "script": RUNSCRIPT_SOURCE,
    }
