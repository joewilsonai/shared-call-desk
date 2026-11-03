"""Offline contract checks; these never invoke Chrome or the native CLI."""
import json
import subprocess
from dataclasses import replace
from types import SimpleNamespace

import pytest

from pinned_reader import Request, main, run


def request(**changes):
    return replace(Request("observe", "Google Chrome", "w-121", "reader-test",
                           669, "process-fixture", budget_ms=4000, max_depth=45), **changes)


def row(**changes):
    return dict(dict(id="w-121", app_name="Google Chrome", pid=669,
                     process_instance="process-fixture", title="Voice",
                     state={"visible": False, "accessible": False}), **changes)


def snapshot(**changes):
    return dict(dict(app="Google Chrome", window={"id": "w-121", "title": "Voice"},
                     complete=True, snapshot_id="snapshot-fixture", tree={
                         "role": "window", "children": [
                             {"role": "button", "name": "Answer call",
                              "ref_id": "snapshot-fixture:answer"}]}), **changes)


class Backend:
    def __init__(self, before=None, data=None, after=None):
        self.before = before if before is not None else [row()]
        self.after = after if after is not None else self.before
        self.data = data if data is not None else snapshot()
        self.commands = []
        self.inventories = 0

    def __call__(self, argv, timeout):
        self.commands.append(argv)
        assert timeout <= 4
        if argv[1] == "--inspect-identity":
            assert argv[argv.index("--app")+1] == "Google Chrome"
            assert argv[argv.index("--window-id")+1] == "w-121"
            assert argv[argv.index("--pid")+1] == "669"
            assert argv[argv.index("--process-instance")+1] == "process-fixture"
            data = self.before if self.inventories == 0 else self.after
            self.inventories += 1
            matches = [item for item in data if item.get("id") == "w-121"]
            if len(matches) != 1:
                code = "WINDOW_NOT_FOUND" if not matches else "AMBIGUOUS_TARGET"
                body = {"ok": False, "error": {"code": code, "message": "Fixture identity mismatch"}}
                return SimpleNamespace(stdout=json.dumps(body), returncode=1)
            item = matches[0]
            body = {"ok": True, "identity_verified": True,
                    "activation_dispatched": False, "raise_dispatched": False,
                    "target": {"app": item.get("app_name"), "window_id": item.get("id"),
                               "pid": item.get("pid"), "process_instance": item.get("process_instance")}}
        else:
            assert argv[3] == "snapshot" and "--app" not in argv
            assert argv[argv.index("--session")+1] == "reader-test"
            assert argv[argv.index("--window-id")+1] == "w-121"
            body = {"ok": True, "command": "snapshot", "data": self.data}
        return SimpleNamespace(stdout=json.dumps(body), returncode=0)


def observe(backend, req=None, **kwargs):
    return run(req or request(), execute=backend, now=lambda: 100.0,
               sleep=lambda _: pytest.fail("observe must not sleep"), **kwargs)


def test_global_discovery_retains_offscreen_window_and_exact_session_refs():
    backend = Backend(before=[row(id="w-777", pid=777, title="Unrelated private title"), row()])
    result = observe(backend)
    assert result["status"] == "complete" and result["identity_verified"]
    assert result["target"]["pid"] == 669
    assert result["raw_snapshot"]["data"]["tree"]["children"][0]["ref_id"] == "snapshot-fixture:answer"
    assert [a[1] if a[1] == "--inspect-identity" else a[3] for a in backend.commands] == ["--inspect-identity", "snapshot", "--inspect-identity"]
    assert "Unrelated private title" not in json.dumps(result)
    assert "w-777" not in json.dumps(result)


@pytest.mark.parametrize("changed", [dict(pid=777), dict(process_instance="recycled-pid"),
                                     dict(app_name="Other Browser")])
def test_wrong_initial_enrollment_never_reads_window(changed):
    backend = Backend(before=[row(**changed)])
    result = observe(backend)
    assert result["status"] == "unavailable"
    assert result["error"]["code"] == "TARGET_CHANGED"
    assert not result["identity_verified"]
    assert len(backend.commands) == 1


@pytest.mark.parametrize("after", [[row(pid=777)], [row(process_instance="recycled-pid")], []])
def test_post_read_identity_change_cannot_return_verified_snapshot(after):
    result = observe(Backend(after=after))
    assert result["status"] == "unavailable"
    assert not result["identity_verified"]


def test_missing_target_never_selects_other_chrome_window():
    backend = Backend(before=[row(id="w-222")])
    result = observe(backend)
    assert result["error"]["code"] == "WINDOW_NOT_FOUND"
    assert len(backend.commands) == 1


def test_duplicate_exact_window_fails_closed():
    result = observe(Backend(before=[row(), row()]))
    assert result["error"]["code"] == "AMBIGUOUS_TARGET"


@pytest.mark.parametrize("data", [snapshot(app="Other Browser"),
                                  snapshot(window={"id": "w-222"})])
def test_snapshot_must_match_exact_application_and_window(data):
    result = observe(Backend(data=data))
    assert result["error"]["code"] == "TARGET_CHANGED"
    assert not result["identity_verified"]


@pytest.mark.parametrize("data", [snapshot(complete=False), snapshot(tree={
    "role": "window", "children": [], "children_count": 2}), snapshot(tree={
        "role": "window", "subtree_truncated": True})])
def test_partial_tree_is_not_upgraded_to_complete(data):
    result = observe(Backend(data=data))
    assert result["status"] == "partial" and not result["complete"]
    assert result["identity_verified"]


def test_permission_failure_is_not_retried_or_retargeted():
    calls = []
    def denied(argv, timeout):
        calls.append(argv)
        return SimpleNamespace(stdout=json.dumps({"ok": False, "command": "list-windows",
                               "error": {"code": "PERM_DENIED", "message": "Denied"}}), returncode=1)
    result = observe(denied)
    assert result["error"]["code"] == "PERM_DENIED"
    assert len(calls) == 1 and not result["identity_verified"]


def test_subprocess_timeout_is_bounded_unavailable_evidence():
    def timeout(argv, budget):
        raise subprocess.TimeoutExpired(argv, budget)
    result = observe(timeout)
    assert result["status"] == "deadline_exceeded"
    assert not result["identity_verified"]


def test_total_deadline_applies_across_inventory_and_snapshot():
    backend = Backend()
    clock = iter([100, 100, 100, 104.01, 104.01])
    result = run(request(), execute=backend, now=lambda: next(clock), sleep=lambda _: None)
    assert result["status"] == "deadline_exceeded"
    assert len(backend.commands) == 1


@pytest.mark.parametrize("changes", [dict(pid=0), dict(pid=True), dict(process_instance=""),
                                     dict(process_instance="bad\ntoken")])
def test_expected_process_identity_is_mandatory(changes):
    backend = Backend()
    result = observe(backend, request(**changes))
    assert result["error"]["code"] == "INVALID_ARGUMENT"
    assert backend.commands == []


def test_cli_requires_enrolled_pid_and_generation(capsys):
    assert main(["observe", "--app", "Google Chrome", "--window-id", "w-121",
                 "--session", "reader-test"]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "INVALID_ARGUMENT"


def test_wait_text_uses_verified_same_session_reference():
    result = observe(Backend(), request(operation="wait-text", text="Answer call"))
    assert result["match_state"] == "observed"
    assert result["matches"][0]["ref_id"] == "snapshot-fixture:answer"


@pytest.mark.parametrize('invalid',[0,999,15001,True,12000.0])
def test_reader_budget_validation_precedes_any_native_call(invalid):
    backend=Backend()
    result=observe(backend,request(budget_ms=invalid))
    assert result['error']['code']=='INVALID_ARGUMENT' and backend.commands==[]


def test_cli_native_budget_defaults_to_twelve_seconds(monkeypatch,capsys):
    seen=[]
    monkeypatch.setattr('pinned_reader.run',lambda req:(seen.append(req) or {'status':'complete'}))
    assert main(['observe','--app','Google Chrome','--window-id','w-121','--pid','669',
                 '--process-instance','fixture','--session','reader-test'])==0
    assert seen[0].budget_ms==12000
    assert json.loads(capsys.readouterr().out)['status']=='complete'



@pytest.mark.parametrize("change", [
    {"identity_verified": False}, {"identity_verified": 1},
    {"activation_dispatched": True}, {"raise_dispatched": True},
    {"activation_dispatched": None}, {"ok": 1},
])
def test_identity_helper_must_prove_strict_read_only_success(change):
    backend = Backend()
    def corrupt(argv, timeout):
        response = backend(argv, timeout)
        body = json.loads(response.stdout)
        body.update(change)
        return SimpleNamespace(stdout=json.dumps(body), returncode=0)
    result = observe(corrupt)
    assert result["status"] == "unavailable"
    assert result["error"]["code"] == "INVALID_BACKEND_RESPONSE"
    assert not result["identity_verified"]
    assert len(backend.commands) == 1


def test_native_identity_calls_share_remaining_snapshot_budget():
    backend = Backend()
    clock = [100.0]
    budgets = []
    def timed(argv, timeout):
        budgets.append((timeout, int(argv[argv.index("--budget-ms" if argv[1] == "--inspect-identity" else "--timeout-ms")+1])))
        result = backend(argv, timeout)
        clock[0] += 0.75
        return result
    result = run(request(), execute=timed, now=lambda: clock[0], sleep=lambda _: None)
    assert result["status"] == "complete"
    assert budgets == [(4.0, 4000), (3.25, 3250), (2.5, 2500)]
