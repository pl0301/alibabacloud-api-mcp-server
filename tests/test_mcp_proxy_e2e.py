from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

import pytest
from mcp import types

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
