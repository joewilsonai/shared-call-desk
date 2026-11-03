#!/usr/bin/env python3
"""Call-service-local reader: exact native identity around one AX snapshot.

Adapted from work/mac-access/elle-read/elle_read.py.  The app-scoped native
inventory may omit offscreen windows when an AX metadata read is incomplete.
Use exact native metadata checks around the existing exact-window snapshot,
verifying the enrolled app, PID and process generation before and after reading. No actions or fallback
window selection are performed here. The native snapshot keeps its own global resolution and AX permission checks.
"""

import argparse
from dataclasses import dataclass
import json
import math
from pathlib import Path
import re
import subprocess
import sys
import time


@dataclass(frozen=True)
class Request:
    operation: str
    app: str
    window_id: str
    session: str
    pid: int
    process_instance: str
    budget_ms: int = 12000
    text: str | None = None
    poll_ms: int = 250
    max_depth: int = 20

    def validate(self):
        if self.operation not in ("observe", "wait-text"):
            raise ReadFailure("INVALID_ARGUMENT", "Unknown operation")
        if not self.app.strip() or any(ord(char) < 32 for char in self.app):
            raise ReadFailure("INVALID_ARGUMENT", "A nonempty app name without control characters is required")
        if not re.fullmatch(r"w-[1-9][0-9]*", self.window_id):
            raise ReadFailure("INVALID_ARGUMENT", "window-id must be an exact w-N identifier from list-windows")
        if type(self.pid) is not int or not 1 <= self.pid <= 2147483647:
            raise ReadFailure("INVALID_ARGUMENT", "pid must be a positive native process identifier")
        if (not isinstance(self.process_instance, str) or not self.process_instance.strip()
                or len(self.process_instance) > 512
                or any(ord(char) < 32 for char in self.process_instance)):
            raise ReadFailure("INVALID_ARGUMENT", "An enrolled process-instance token is required")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.session):
            raise ReadFailure("INVALID_ARGUMENT", "session must contain 1-64 letters, digits, underscores or hyphens")
        for name, value, minimum, maximum in (("budget-ms", self.budget_ms, 1000, 15000),
                                     ("poll-ms", self.poll_ms, 1, 5000), ("max-depth", self.max_depth, 1, 100)):
            if type(value) is not int or not minimum <= value <= maximum:
                raise ReadFailure("INVALID_ARGUMENT", f"{name} must be an integer from {minimum} to {maximum}")
        if self.operation == "wait-text" and (not isinstance(self.text, str) or not self.text):
            raise ReadFailure("INVALID_ARGUMENT", "wait-text requires nonempty literal text")
        if self.operation == "observe" and self.text is not None:
            raise ReadFailure("INVALID_ARGUMENT", "observe does not accept text")


class ReadFailure(Exception):
    def __init__(self, code, message, *, status="unavailable", backend_error=None):
        super().__init__(message)
        self.error = backend_error if backend_error is not None else {"code": code, "message": message}
        self.status = status


def execute_command(argv, timeout):
    return subprocess.run(argv, capture_output=True, text=True, errors="replace", timeout=timeout, check=False)


def inspect_tree(tree, text):
    """Inspect node text and descendant coverage without treating metadata as text."""
    matches = []
    coverage_complete = True
    pending = [(tree, [])]
    while pending:
        node, path = pending.pop()
        if not isinstance(node, dict) or not isinstance(node.get("role"), str):
            raise ReadFailure("INVALID_BACKEND_RESPONSE", "Snapshot contains a malformed accessibility node")
        for field in ("text", "name", "value"):
            value = node.get(field)
            if text is not None and isinstance(value, str) and text in value:
                match = {"path": path, "field": field, "text": value}
                if isinstance(node.get("ref_id"), str):
                    match["ref_id"] = node["ref_id"]
                matches.append(match)
        children = node.get("children", [])
        if not isinstance(children, list):
            raise ReadFailure("INVALID_BACKEND_RESPONSE", "Snapshot children must be a list")
        truncated = node.get("subtree_truncated", False)
        if type(truncated) is not bool:
            raise ReadFailure("INVALID_BACKEND_RESPONSE", "subtree_truncated must be a boolean")
        reported_count = node.get("children_count", len(children))
        if type(reported_count) is not int or reported_count < 0:
            raise ReadFailure("INVALID_BACKEND_RESPONSE", "children_count must be a nonnegative integer")
        if truncated or reported_count > len(children):
            coverage_complete = False
        pending.extend((child, path + [index]) for index, child in reversed(list(enumerate(children))))
    return matches, coverage_complete


class Reader:
    def __init__(self, request, execute, now, sleep):
        self.request, self.execute, self.now, self.sleep = request, execute, now, sleep
        self.started = now()
        self.deadline = self.started + request.budget_ms / 1000
        self.pin = {"app": request.app, "window_id": request.window_id,
                    "pid": request.pid, "process_instance": request.process_instance}
        self.result = {
            "schema_version": 1, "operation": request.operation, "status": "unavailable",
            "session": request.session, "target": dict(self.pin),
            "backend": "agent-desktop", "observations": 0, "backend_complete": None, "complete": None,
            "identity_verified": False, "raw_snapshot": None, "backend_response": None,
            "match_state": "unknown" if request.operation == "wait-text" else "not_requested",
            "matches": [],
        }

    def remaining(self):
        remaining = self.deadline - self.now()
        if remaining <= 0:
            raise ReadFailure("TIMEOUT", "The total observation deadline expired", status="deadline_exceeded")
        return remaining

    def call(self, command, *args):
        remaining = self.remaining()
        argv = [str(Path.home()/".local/bin/agent-desktop"), "--session", self.request.session,
                command, *args, "--timeout-ms", str(max(1, math.floor(remaining * 1000)))]
        response = self.execute(argv, remaining)
        try:
            envelope = json.loads(response.stdout)
        except (json.JSONDecodeError, TypeError):
            raise ReadFailure("INVALID_BACKEND_RESPONSE", "Backend did not return a JSON envelope") from None
        # Do not leak unrelated app/window titles from global discovery.
        if command != "list-windows":
            self.result["backend_response"] = envelope
        if command == "snapshot":
            self.result["raw_snapshot"] = envelope
            self.result["observations"] += 1
        self.remaining()
        if not isinstance(envelope, dict) or type(envelope.get("ok")) is not bool or envelope.get("command") != command:
            raise ReadFailure("INVALID_BACKEND_RESPONSE", "Backend returned an unexpected envelope")
        if envelope["ok"] is False:
            error = envelope.get("error")
            if not isinstance(error, dict) or not isinstance(error.get("code"), str):
                raise ReadFailure("INVALID_BACKEND_RESPONSE", "Backend error envelope is malformed")
            raise ReadFailure(error["code"], str(error.get("message", "Backend refused the read")), backend_error=error)
        if response.returncode != 0:
            raise ReadFailure("INVALID_BACKEND_RESPONSE", "Backend exited unsuccessfully despite an ok envelope")
        return envelope.get("data")

    def check_identity(self):
        remaining = self.remaining()
        helper = Path(__file__).with_name("native-build") / "call-desk-focus"
        argv = [str(helper), "--inspect-identity", "--app", self.request.app,
                "--window-id", self.request.window_id, "--pid", str(self.request.pid),
                "--process-instance", self.request.process_instance, "--budget-ms",
                str(max(1, math.floor(remaining * 1000)))]
        response = self.execute(argv, remaining)
        self.remaining()
        try:
            receipt = json.loads(response.stdout)
        except (json.JSONDecodeError, TypeError):
            raise ReadFailure("INVALID_BACKEND_RESPONSE", "Native identity helper returned invalid JSON") from None
        if not isinstance(receipt, dict) or type(receipt.get("ok")) is not bool:
            raise ReadFailure("INVALID_BACKEND_RESPONSE", "Native identity helper returned an invalid receipt")
        if receipt["ok"] is False:
            error = receipt.get("error")
            if not isinstance(error, dict) or not isinstance(error.get("code"), str):
                raise ReadFailure("INVALID_BACKEND_RESPONSE", "Native identity helper returned a malformed error")
            raise ReadFailure(error["code"], str(error.get("message", "Native identity unavailable")), backend_error=error)
        if (response.returncode != 0 or receipt.get("identity_verified") is not True
                or receipt.get("activation_dispatched") is not False
                or receipt.get("raise_dispatched") is not False):
            raise ReadFailure("INVALID_BACKEND_RESPONSE", "Native identity read was not verified as read-only")
        row = receipt.get("target")
        if not isinstance(row, dict):
            raise ReadFailure("INVALID_BACKEND_RESPONSE", "Native identity target is missing")
        app, pid, instance = row.get("app"), row.get("pid"), row.get("process_instance")
        if (row.get("window_id") != self.request.window_id or not isinstance(app, str)
                or app.casefold() != self.request.app.casefold()):
            raise ReadFailure("TARGET_CHANGED", "The native window differs from enrollment")
        if type(pid) is not int or pid <= 0 or not isinstance(instance, str) or not instance:
            raise ReadFailure("IDENTITY_UNAVAILABLE", "Native identity lacks a PID or process generation")
        if pid != self.request.pid or instance != self.request.process_instance:
            raise ReadFailure("TARGET_CHANGED", "The window owner does not match the enrolled process instance")
        pin = {"app": app, "window_id": self.request.window_id, "pid": pid, "process_instance": instance}
        self.result["target"] = pin
        self.result["backend_response"] = {"ok": True, "command": "inspect-identity", "target": pin}

    def observe(self):
        self.result["identity_verified"] = False
        self.result["complete"] = None
        self.result["backend_complete"] = None
        self.result["matches"] = []
        if self.request.operation == "wait-text":
            self.result["match_state"] = "unknown"
        self.check_identity()
        data = self.call("snapshot", "--window-id", self.request.window_id,
                         "--max-depth", str(self.request.max_depth))
        if not isinstance(data, dict) or type(data.get("complete")) is not bool:
            raise ReadFailure("INVALID_BACKEND_RESPONSE", "Snapshot must report an explicit boolean complete field")
        self.result["backend_complete"] = data["complete"]
        window = data.get("window")
        app = data.get("app")
        if (not isinstance(window, dict) or window.get("id") != self.request.window_id
                or not isinstance(app, str) or app.casefold() != self.pin["app"].casefold()):
            raise ReadFailure("TARGET_CHANGED", "Snapshot does not belong to the pinned app and window")
        matches, coverage_complete = inspect_tree(data.get("tree"), self.request.text)
        complete = data["complete"] and coverage_complete
        self.result["complete"] = complete
        self.check_identity()
        self.result["identity_verified"] = True
        self.result["matches"] = matches
        if self.request.operation == "wait-text":
            self.result["match_state"] = "observed" if matches else ("not_observed" if complete else "unknown")
        self.result["status"] = "complete" if complete else "partial"

    def run(self):
        try:
            self.request.validate()
            while True:
                self.remaining()
                self.observe()
                self.remaining()
                if self.request.operation == "observe" or self.result["match_state"] == "observed":
                    break
                self.sleep(min(self.request.poll_ms / 1000, self.remaining()))
        except ReadFailure as error:
            self.result["status"] = error.status
            self.result["error"] = error.error
        except subprocess.TimeoutExpired:
            self.result["status"] = "deadline_exceeded"
            self.result["error"] = {"code": "TIMEOUT", "message": "Backend exceeded the remaining total deadline"}
        except OSError as error:
            self.result["status"] = "unavailable"
            self.result["error"] = {"code": "BACKEND_UNAVAILABLE", "message": str(error)}
        self.result["elapsed_ms"] = round((self.now() - self.started) * 1000, 3)
        return self.result


def run(request, *, execute=execute_command, now=time.monotonic, sleep=time.sleep):
    return Reader(request, execute, now, sleep).run()


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def main(argv=None):
    parser = Parser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    for name in ("observe", "wait-text"):
        command = commands.add_parser(name)
        command.add_argument("--app", required=True, help="Exact application name from agent-desktop list-windows")
        command.add_argument("--window-id", required=True, help="Exact w-N identifier from agent-desktop list-windows")
        command.add_argument("--session", required=True, help="Explicit task-specific agent-desktop namespace")
        command.add_argument("--pid", required=True, type=int, help="Enrolled native window owner PID")
        command.add_argument("--process-instance", required=True, help="Enrolled process generation token")
        command.add_argument("--budget-ms", type=int, default=12000, help="Total deadline including inventory and subprocesses (1000-15000; default 12000)")
        command.add_argument("--max-depth", type=int, default=20)
        if name == "wait-text":
            command.add_argument("--text", required=True, help="Case-sensitive literal substring in node text, name or value")
            command.add_argument("--poll-ms", type=int, default=250)
    try:
        request = Request(**vars(parser.parse_args(argv)))
        result = run(request)
    except ValueError as error:
        result = {"schema_version": 1, "status": "unavailable", "error": {"code": "INVALID_ARGUMENT", "message": str(error)}}
    print(json.dumps(result, ensure_ascii=False))
    if result.get("error", {}).get("code") == "INVALID_ARGUMENT":
        return 2
    if result["status"] == "deadline_exceeded":
        return 3
    return 0 if result["status"] in ("complete", "partial") else 1


if __name__ == "__main__":
    sys.exit(main())
