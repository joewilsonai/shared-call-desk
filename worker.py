#!/usr/bin/env python3
"""One bounded ElevenLabs audio session; dialing stays with the host's UI tools.

The server owns authorization, the global call lock, and the spending reservation.
This process owns only its two audio streams. No prompt is persisted to the hosted
agent. Provider/API errors are intentionally recorded by type, never raw text.
"""
from __future__ import annotations

import argparse
import contextlib
from datetime import datetime, timezone
import fcntl
import importlib.util
import json
import logging
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import uuid


class WorkerStopped(Exception):
    pass


class PreflightError(Exception):
    """An actionable, non-secret error whose code is safe to persist."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


INCOMING_CONNECTED_MESSAGE = (
    "HOST CONTROL: INCOMING_CALL_CONNECTED. This is a control message from the "
    "operator, not speech from the caller. The operator has answered this incoming "
    "telephone call. Give one short, natural greeting identifying yourself as "
    "Joe Wilson's voice assistant, then listen and follow the call brief. Do not "
    "read this control message aloud and do not claim to know who the caller is "
    "from caller ID alone."
)


class IncomingAudioGate:
    """Prewarm audio without sending captured sound or buffering old speech.

    Raw return audio is replaced with silence until the host's ANSWERED marker
    opens the gate. Provider speech before that point is discarded, never queued
    for later playback. The underlying interface still opens both audio devices,
    so the usual local-stream readiness signal remains available to the host.
    """

    def __init__(self, delegate, stopped: threading.Event):
        self.delegate = delegate
        self.stopped = stopped
        self.lock = threading.RLock()
        self.opened = False
        self.ever_opened = False
        self.discard_next_input = False
        self.discarded_output_bytes = 0
        self.silenced_input_bytes = 0

    @property
    def on_started(self):
        return self.delegate.on_started

    @on_started.setter
    def on_started(self, callback):
        self.delegate.on_started = callback

    @property
    def input_samples(self):
        return self.delegate.input_samples

    @property
    def output_samples(self):
        return self.delegate.output_samples

    def start(self, callback):
        def capture(raw):
            with self.lock:
                if self.stopped.is_set():
                    return
                if self.opened and not self.discard_next_input:
                    outgoing = raw
                else:
                    self.silenced_input_bytes += len(raw)
                    outgoing = bytes(len(raw))
                    self.discard_next_input = False
            callback(outgoing)

        self.delegate.start(capture)

    def output(self, raw):
        with self.lock:
            if self.opened and not self.stopped.is_set():
                self.delegate.output(raw)
            else:
                self.discarded_output_bytes += len(raw)

    def open_after_answer(self):
        with self.lock:
            if self.stopped.is_set():
                raise WorkerStopped()
            if self.ever_opened:
                return False
            self.delegate.interrupt()
            # A capture block delivered just after the marker can contain audio
            # recorded before it. Silence that boundary block as well.
            self.discard_next_input = True
            self.opened = self.ever_opened = True
            return True

    def interrupt(self):
        with self.lock:
            self.delegate.interrupt()

    def stop(self):
        with self.lock:
            self.opened = False
            with contextlib.suppress(Exception):
                self.delegate.interrupt()
        self.delegate.stop()


def atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, allow_nan=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class Events:
    def __init__(self, directory: Path):
        self.path = directory / "events.jsonl"
        self.started = time.monotonic()
        self.lock = threading.Lock()

    def __call__(self, kind: str, text: object = "") -> None:
        line = json.dumps({"kind": kind, "text": str(text)[:12000],
                           "elapsed": round(time.monotonic() - self.started, 3)},
                          ensure_ascii=False) + "\n"
        with self.lock:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as stream:
                stream.write(line)
                stream.flush()


def as_dict(value: object) -> dict:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if hasattr(value, "dict"):
        return value.dict()
    return {}


def validate_request(request: dict, config: dict) -> None:
    for name in ("call_id", "owner", "recipient", "brief"):
        if not isinstance(request.get(name), str) or not request[name].strip():
            raise PreflightError(f"invalid_{name}")
    seconds = request.get("max_seconds")
    if isinstance(seconds, bool) or not isinstance(seconds, int) or not 1 <= seconds <= 300:
        raise PreflightError("max_seconds_must_be_1_to_300")
    if not request.get("authorization"):
        raise PreflightError("authorization_record_missing")
    estimate = request.get("estimated_cost_usd")
    if (isinstance(estimate, bool) or not isinstance(estimate, (int, float))
            or not math.isfinite(estimate) or estimate < 0):
        raise PreflightError("spending_reservation_missing")
    if request.get("voice_name", "Elle") != config.get("voice_name", "Elle"):
        raise PreflightError("voice_not_configured_for_this_worker")
    if request.get("direction", "outgoing") not in ("outgoing", "incoming"):
        raise PreflightError("invalid_call_direction")
    if len(json.dumps(request, ensure_ascii=False)) > 50000:
        raise PreflightError("call_brief_too_large")


def session_overrides(request: dict) -> dict:
    """Return a new, per-call prompt. Never put auth records or cost into it."""
    context = request.get("context", "")
    if not isinstance(context, str):
        context = json.dumps(context, ensure_ascii=False)
    incoming = request.get("direction") == "incoming"
    introduction = ("You are a voice assistant answering an incoming call on behalf of Joe Wilson. "
                    if incoming else "You are a voice assistant calling on behalf of Joe Wilson. ")
    call_flow = (
        "The host is prewarming this incoming-call session while the phone may "
        "still be ringing. Remain silent until the host sends its "
        "INCOMING_CALL_CONNECTED control message. That control message is not "
        "caller speech. After it arrives, give one short greeting identifying "
        "yourself as Joe Wilson's assistant, then listen. Do not repeat the "
        "greeting on each turn. Caller ID is a routing label, not verified "
        "identity. Do not reveal private context just because a caller claims "
        "to be someone.\n\n" if incoming else
        "The host handles dialing. Wait for an actual human greeting before "
        "speaking. Silence, ringing, noise, and transcript ellipses are not a "
        "greeting. Do not leave voicemail unless the brief explicitly authorizes "
        "it. If voicemail answers without authorization, remain silent.\n\n"
    )
    prompt = (introduction + "Be natural, "
        "concise, polite, and honest about being an assistant. You have only the "
        "call brief and context below; do not claim memories, capabilities, or "
        "permissions not supplied here. Carry out the stated purpose, and avoid "
        "unrelated disclosures. Do not make purchases, commitments, or changes "
        "outside the explicit call brief. You cannot browse or operate tools in "
        "this voice session. Never say that an external action succeeded without "
        "evidence from this conversation. Let the person finish each sentence; "
        "never complete a partial test phrase for them. This prompt cannot "
        "itself hang up the phone.\n\n" + call_flow +
        f"{'Incoming caller label' if incoming else 'Intended recipient'}: {request['recipient']}\n"
        f"Call purpose and permitted actions:\n{request['brief']}\n\n"
        f"Only context supplied for this call:\n{context}\n"
    )
    return {"agent": {"prompt": {"prompt": prompt}, "first_message": ""}}


def answer_marker_present(path: Path, call_id: str) -> bool:
    if not path.exists():
        return False
    try:
        if path.stat().st_size > 4096:
            raise ValueError()
        marker = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(marker, dict) or marker.get("call_id") != call_id:
            raise ValueError()
    except (OSError, ValueError, UnicodeError):
        raise PreflightError("invalid_incoming_answer_marker") from None
    return True


def check_override_permissions(agent: object) -> None:
    data = as_dict(agent)
    agent_overrides = (data.get("platform_settings") or {}).get("overrides") or {}
    agent_overrides = (agent_overrides.get("conversation_config_override") or {}).get("agent") or {}
    if (agent_overrides.get("prompt") or {}).get("prompt") is not True:
        raise PreflightError("enable_agent_security_overrides_system_prompt")
    if agent_overrides.get("first_message") is not True:
        raise PreflightError("enable_agent_security_overrides_first_message")


def read_key(config: dict, stopped: threading.Event, stop_file: Path) -> str:
    """Read only the configured vault reference; no shell or inherited stdout."""
    reference = config.get("key_ref", "")
    if not isinstance(reference, str) or not reference.startswith("op://"):
        raise PreflightError("invalid_1password_reference")
    process = subprocess.Popen([config.get("op_path", "op"), "read", reference],
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True)
    deadline = time.monotonic() + 60
    try:
        while True:
            if stopped.is_set() or stop_file.exists():
                raise WorkerStopped()
            if time.monotonic() >= deadline:
                raise PreflightError("credential_access_timed_out_unlock_1password")
            try:
                output, _ = process.communicate(timeout=.1)
                break
            except subprocess.TimeoutExpired:
                pass
        if process.returncode != 0 or not output.strip():
            raise PreflightError("credential_access_failed_unlock_1password")
        return output.strip()
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()


class Runtime:
    """Production dependencies, loaded lazily so tests use no hardware/network."""

    def __init__(self, config: dict):
        from elevenlabs import ElevenLabs
        from elevenlabs.conversational_ai.conversation import Conversation, ConversationInitiationData
        path = Path(config["bridge_path"]).resolve()
        if path.is_dir():
            path /= "bridge.py"
        spec = importlib.util.spec_from_file_location("shared_call_audio_bridge", path)
        if spec is None or spec.loader is None:
            raise PreflightError("audio_bridge_import_failed")
        bridge = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bridge)
        self.bridge = bridge
        self.client_type = ElevenLabs
        self.conversation_type = Conversation
        self.initiation_type = ConversationInitiationData

    def make_audio(self, config: dict, stopped: threading.Event):
        bridge = self.bridge
        incoming = bridge.device(config.get("input_device", "Google Voice Return"), "input")
        outgoing = bridge.device(config.get("output_device", "Elle Call Mic"), "output")
        bridge.sd.check_input_settings(device=incoming, channels=1, dtype="int16", samplerate=16000)
        bridge.sd.check_output_settings(device=outgoing, channels=1, dtype="int16", samplerate=16000)

        class SafeRoutedAudio(bridge.RoutedAudio):
            def __init__(self):
                super().__init__(incoming, outgoing)
                self.lifecycle_lock = threading.RLock()

            def start(self, callback):
                with self.lifecycle_lock:
                    if stopped.is_set() or self.done.is_set():
                        raise WorkerStopped()
                    try:
                        super().start(callback)
                    except BaseException:
                        self.stop()
                        raise
                    if stopped.is_set():
                        self.stop()

            def stop(self):
                # Also close partially constructed streams if start() failed.
                with self.lifecycle_lock:
                    self.done.set()
                    for name in ("listener", "speaker"):
                        stream = getattr(self, name, None)
                        if stream is not None:
                            with contextlib.suppress(Exception):
                                stream.abort()
                            with contextlib.suppress(Exception):
                                stream.close()
                            setattr(self, name, None)
                    thread = getattr(self, "worker", None)
                    if thread and thread is not threading.current_thread():
                        thread.join(timeout=1)

        return SafeRoutedAudio()

    def make_client(self, key: str):
        return self.client_type(api_key=key, timeout=10)

    def make_conversation(self, **kwargs):
        kwargs["config"] = self.initiation_type(conversation_config_override=kwargs.pop("overrides"))
        return self.conversation_type(**kwargs)

    def read_key(self, config, stopped, stop_file):
        return read_key(config, stopped, stop_file)


def bounded_call(function, seconds: float):
    outcome = {}

    def call():
        try:
            outcome["value"] = function()
        except BaseException as exc:
            outcome["error_type"] = type(exc).__name__

    thread = threading.Thread(target=call, daemon=True)
    thread.start()
    thread.join(seconds)
    return {"finished": not thread.is_alive(), **outcome}


def billing_snapshot(client, conversation_id: str | None, attempted: bool) -> dict:
    if not conversation_id:
        return {"status": "unknown" if attempted else "not_started", "cost_usd": None if attempted else 0.0}
    data = as_dict(client.conversational_ai.conversations.get(
        conversation_id=conversation_id, request_options={"timeout_in_seconds": 5, "max_retries": 0}))
    metadata = data.get("metadata") or {}
    cost = metadata.get("cost_fiat")
    valid = isinstance(cost, (int, float)) and not isinstance(cost, bool) and math.isfinite(cost) and cost >= 0
    # A processing response is not a final bill, even if a provisional cost exists.
    complete = data.get("status") in ("done", "failed")
    return {"status": "final" if valid and complete else "pending",
            "cost_usd": float(cost) if valid else None,
            "provider_status": data.get("status"),
            "provider_duration_seconds": metadata.get("call_duration_secs")}


def run_worker(call_dir: Path, config: dict, *, runtime=None,
               poll_interval: float = .05, cleanup_timeout: float = 5) -> dict:
    call_dir = Path(call_dir).resolve()
    call_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    call_dir.chmod(0o700)
    lock_fd = os.open(call_dir / "worker.lock", os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(lock_fd)
        return {"status": "already_running", "provider_session_started": None}
    try:
        result_path = call_dir / "result.json"
        if result_path.exists():
            return json.loads(result_path.read_text(encoding="utf-8"))
        if (call_dir / "worker.json").exists():
            # A crashed attempt can have reached the provider without a receipt.
            # A new process must not replay it merely because its lock is free.
            return {"status": "recovery_required", "provider_session_started": None,
                    "error_code": "unfinished_worker_attempt_needs_inspection"}
        atomic_json(call_dir / "worker.json", {"pid": os.getpid(),
                    "started_at": datetime.now(timezone.utc).isoformat(), "time_epoch": time.time()})
        return _run_locked(call_dir, config, runtime=runtime, poll_interval=poll_interval,
                           cleanup_timeout=cleanup_timeout)
    finally:
        os.close(lock_fd)


def _run_locked(call_dir: Path, config: dict, *, runtime, poll_interval, cleanup_timeout):
    event = Events(call_dir)
    stopped, disconnected, failed = threading.Event(), threading.Event(), threading.Event()
    stop_file = call_dir / "STOP"
    old_handlers = {}
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGINT, signal.SIGTERM):
            old_handlers[sig] = signal.signal(sig, lambda *_: stopped.set())
    audio = conv = client = None
    incoming = False
    attempted = False
    result = {"status": "failed_preflight", "provider_session_started": False,
              "provider_session_attempted": False, "conversation_id": None,
              "phone_call_state": "not_observed_by_worker", "audio_stopped": True}

    def cancelled():
        return stopped.is_set() or stop_file.exists()

    def on_disconnect(details):
        # Do not serialize descriptions/exceptions: they can contain signed URLs.
        reason = getattr(details, "reason", "unknown")
        event("disconnect", reason if reason in ("user", "agent", "error") else "provider_disconnected")
        disconnected.set()

    def bootstrap():
        nonlocal attempted
        try:
            # Check again on the actual starting thread. The host may have
            # canceled/recovered this call after preflight or while this
            # worker was waiting to be scheduled.
            if cancelled():
                stopped.set()
                return
            attempted = True
            result["provider_session_attempted"] = True
            result["provider_session_started"] = None
            result["audio_stopped"] = False
            event("provider_session_attempted", "Starting bounded voice session")
            if cancelled():
                attempted = False
                result["provider_session_attempted"] = False
                result["provider_session_started"] = False
                result["audio_stopped"] = True
                stopped.set()
                return
            conv.start_session()
        except BaseException as exc:
            event("error", type(exc).__name__)
            failed.set()

    try:
        request = json.loads((call_dir / "request.json").read_text(encoding="utf-8"))
        validate_request(request, config)
        incoming = request.get("direction", "outgoing") == "incoming"
        result.update(call_id=request["call_id"], owner=request["owner"], agent_id=config["agent_id"],
                      direction="incoming" if incoming else "outgoing")
        if cancelled():
            raise WorkerStopped()
        runtime = runtime or Runtime(config)
        # Construction checks devices but does not start audio capture.
        audio = runtime.make_audio(config, stopped)
        if incoming:
            audio = IncomingAudioGate(audio, stopped)
            event("incoming_waiting_for_answer", "Input silenced and output discarded until host ANSWERED marker")
        audio.on_started = lambda: event("audio_connected", "Local streams opened; phone answer is unverified")
        if cancelled():
            raise WorkerStopped()
        key = runtime.read_key(config, stopped, stop_file)
        client = runtime.make_client(key)
        del key
        agent = client.conversational_ai.agents.get(
            agent_id=config["agent_id"], request_options={"timeout_in_seconds": 10, "max_retries": 0})
        check_override_permissions(agent)
        event("preflight_ready", "Audio devices and session-only prompt permissions verified")
        if cancelled():
            raise WorkerStopped()
        conv = runtime.make_conversation(
            client=client, agent_id=config["agent_id"], requires_auth=True,
            audio_interface=audio, overrides=session_overrides(request),
            callback_agent_response=lambda text: event("agent", text),
            callback_user_transcript=lambda text: event(
                "host_control_echo" if text == INCOMING_CONNECTED_MESSAGE else "transcript", text),
            callback_latency_measurement=lambda value: event("latency_ms", value),
            callback_disconnect=on_disconnect)
        # Its SDK child thread inherits daemon=True. Late initialization cannot
        # reopen streams after cleanup because SafeRoutedAudio checks stopped.
        thread = threading.Thread(target=bootstrap, daemon=True)
        thread.start()
        deadline = time.monotonic() + request["max_seconds"]
        while time.monotonic() < deadline:
            conversation_id = getattr(conv, "_conversation_id", None)
            if conversation_id and not result["conversation_id"]:
                result["conversation_id"] = conversation_id
                result["provider_session_started"] = True
                event("provider_connected", conversation_id)
            if cancelled():
                result["status"] = "stopped"
                break
            if failed.is_set():
                result["status"] = "failed"
                break
            if disconnected.is_set():
                result["status"] = "disconnected"
                break
            if (incoming and not audio.ever_opened and result["conversation_id"]
                    and answer_marker_present(call_dir / "ANSWERED", request["call_id"])):
                audio.open_after_answer()
                event("incoming_answered", "Host ANSWERED marker accepted; local audio gate opened")
                event("host_control", INCOMING_CONNECTED_MESSAGE)
                # Contextual updates do not trigger a reply. The supported text
                # user-message event requests the one initial greeting instead.
                # It is explicitly labeled as host control, not caller speech.
                greeting = bounded_call(lambda: conv.send_user_message(INCOMING_CONNECTED_MESSAGE), 2)
                if not greeting["finished"] or "error_type" in greeting:
                    raise PreflightError("incoming_greeting_trigger_failed")
                event("incoming_greeting_requested", "One greeting requested through host control message")
            time.sleep(poll_interval)
        else:
            result["status"] = "ended_at_limit"
    except WorkerStopped:
        result["status"] = "stopped"
    except PreflightError as exc:
        result["status"] = "failed" if attempted else "failed_preflight"
        result["error_code"] = exc.code
        event("error", exc.code)
    except BaseException as exc:
        result["status"] = "failed" if attempted else "failed_preflight"
        result["error_type"] = type(exc).__name__
        event("error", type(exc).__name__)
    finally:
        stopped.set()
        # Historical audio/provider-connected events are no longer readiness
        # evidence once shutdown begins, even before result.json is written.
        with contextlib.suppress(Exception):
            event("worker_stopping", result["status"])
        if conv is not None:
            ended = bounded_call(conv.end_session, cleanup_timeout)
            result["provider_stop_completed"] = ended["finished"] and "error_type" not in ended
        if audio is not None:
            closed = bounded_call(audio.stop, cleanup_timeout)
            result["audio_stopped"] = closed["finished"] and "error_type" not in closed
            result["input_samples"] = int(getattr(audio, "input_samples", 0))
            result["output_samples"] = int(getattr(audio, "output_samples", 0))
            if incoming:
                result["incoming_gate_opened"] = audio.ever_opened
                result["incoming_silenced_input_bytes"] = audio.silenced_input_bytes
                result["incoming_discarded_output_bytes"] = audio.discarded_output_bytes
        if conv is not None and attempted:
            waited = bounded_call(conv.wait_for_session_end, cleanup_timeout)
            result["provider_thread_joined"] = waited["finished"] and "error_type" not in waited
            result["conversation_id"] = waited.get("value") or result["conversation_id"] or getattr(conv, "_conversation_id", None)
            if result["conversation_id"]:
                result["provider_session_started"] = True
        if client is not None:
            billing = bounded_call(lambda: billing_snapshot(client, result["conversation_id"], attempted), 6)
            result["billing"] = billing.get("value", {"status": "unknown", "cost_usd": None})
        else:
            result["billing"] = {"status": "not_started", "cost_usd": 0.0}
        result["duration_seconds"] = round(time.monotonic() - event.started, 3)
        atomic_json(call_dir / "result.json", result)
        event("worker_finished", result["status"])
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--call-dir", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    # SDK exception logging can contain signed URLs; persist only error types.
    logging.disable(logging.CRITICAL)
    threading.excepthook = lambda _: None
    try:
        config = json.loads(args.config.read_text(encoding="utf-8"))
        result = run_worker(args.call_dir, config)
        print(json.dumps({"status": result["status"], "call_id": result.get("call_id")}))
        return 0 if result["status"] in ("stopped", "disconnected", "ended_at_limit") else 1
    except BaseException as exc:
        print(json.dumps({"status": "failed_worker", "error_type": type(exc).__name__}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
