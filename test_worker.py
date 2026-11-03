"""No network, credentials, audio devices, or phone calls are used here."""
import importlib.util
import fcntl
import json
from pathlib import Path
import tempfile
import threading
import time
import types
import unittest

spec = importlib.util.spec_from_file_location("call_worker", Path(__file__).with_name("worker.py"))
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


class FakeAudio:
    input_samples = 120
    output_samples = 60

    def __init__(self):
        self.stopped = 0
        self.on_started = lambda: None
        self.outputs = []
        self.interrupts = 0
        self.callback = None

    def start(self, callback):
        self.callback = callback
        self.on_started()

    def output(self, raw):
        self.outputs.append(raw)

    def interrupt(self):
        self.interrupts += 1

    def stop(self):
        self.stopped += 1


class FakeRuntime:
    def __init__(self, mode="disconnect", permission=True):
        self.mode, self.permission = mode, permission
        self.audio, self.calls, self.overrides = FakeAudio(), [], None
        self.conversation = None
        self.key_reads = 0
        self.input_received = []
        self.user_messages = []

    def make_audio(self, config, stopped):
        return self.audio

    def read_key(self, config, stopped, stop_file):
        self.key_reads += 1
        return "FAKE_SECRET_MUST_NOT_APPEAR"

    def make_client(self, key):
        def get_agent(**kwargs):
            return {"platform_settings": {"overrides": {"conversation_config_override": {
                "agent": {"prompt": {"prompt": self.permission}, "first_message": self.permission}}}}}

        def get_conversation(**kwargs):
            return {"status": "done", "metadata": {"cost_fiat": .012, "call_duration_secs": 1}}

        return types.SimpleNamespace(conversational_ai=types.SimpleNamespace(
            agents=types.SimpleNamespace(get=get_agent),
            conversations=types.SimpleNamespace(get=get_conversation)))

    def make_conversation(self, **kwargs):
        self.overrides = kwargs["overrides"]
        runtime = self

        class FakeConversation:
            _conversation_id = None

            def start_session(self):
                runtime.calls.append("start")
                if runtime.mode == "error":
                    raise RuntimeError("secret URL https://example.invalid/?key=FAKE_SECRET_MUST_NOT_APPEAR")
                self._conversation_id = "conv_fake"
                kwargs["audio_interface"].start(runtime.input_received.append)
                runtime.audio.callback(b"ringing")
                kwargs["audio_interface"].output(b"early speech")
                kwargs["callback_agent_response"]("A test answer")
                if runtime.mode == "disconnect":
                    kwargs["callback_disconnect"](types.SimpleNamespace(reason="agent"))

            def end_session(self):
                runtime.calls.append("end")

            def wait_for_session_end(self):
                return self._conversation_id

            def send_user_message(self, text):
                runtime.user_messages.append(text)
                kwargs["callback_user_transcript"](text)
                kwargs["audio_interface"].output(b"fresh greeting")
                if runtime.mode == "incoming":
                    kwargs["callback_disconnect"](types.SimpleNamespace(reason="agent"))

        self.conversation = FakeConversation()
        return self.conversation


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.config = {"agent_id": "agent_fake", "voice_name": "Elle"}
        self.request = {"call_id": "call_test", "owner": "elle", "recipient": "+15555550123",
                        "brief": "Confirm the test phrase.", "context": "Only this private context.",
                        "voice_name": "Elle", "max_seconds": 1,
                        "authorization": "User explicitly asked for this test.", "estimated_cost_usd": .5}
        self.save()

    def tearDown(self):
        self.temp.cleanup()

    def save(self):
        (self.directory / "request.json").write_text(json.dumps(self.request))

    def run_case(self, runtime):
        return worker.run_worker(self.directory, self.config, runtime=runtime, poll_interval=.005, cleanup_timeout=.05)

    def test_disconnect_closes_audio_and_records_final_bill(self):
        runtime = FakeRuntime()
        result = self.run_case(runtime)
        self.assertEqual(result["status"], "disconnected")
        self.assertTrue(result["audio_stopped"])
        self.assertGreater(runtime.audio.stopped, 0)
        self.assertEqual(result["conversation_id"], "conv_fake")
        self.assertEqual(result["billing"], {"status": "final", "cost_usd": .012,
                                           "provider_status": "done", "provider_duration_seconds": 1})
        self.assertEqual(result["phone_call_state"], "not_observed_by_worker")
        events = [json.loads(line) for line in (self.directory / "events.jsonl").read_text().splitlines()]
        self.assertIn("audio_connected", [event["kind"] for event in events])
        kinds = [event["kind"] for event in events]
        self.assertLess(kinds.index("worker_stopping"), kinds.index("worker_finished"))
        self.assertEqual((self.directory / "result.json").stat().st_mode & 0o777, 0o600)

    def test_stop_before_preflight_does_not_fetch_key_or_start(self):
        (self.directory / "STOP").touch()
        runtime = FakeRuntime()
        result = self.run_case(runtime)
        self.assertEqual(result["status"], "stopped")
        self.assertFalse(result["provider_session_started"])
        self.assertEqual(runtime.key_reads, 0)
        self.assertEqual(runtime.calls, [])

    def test_stop_file_during_session_closes_audio(self):
        runtime = FakeRuntime(mode="idle")
        timer = threading.Timer(.05, (self.directory / "STOP").touch)
        timer.start()
        result = self.run_case(runtime)
        timer.join()
        self.assertEqual(result["status"], "stopped")
        self.assertTrue(result["audio_stopped"])
        self.assertIn("end", runtime.calls)

    def test_stop_after_conversation_construction_prevents_provider_start(self):
        runtime = FakeRuntime()
        construct = runtime.make_conversation

        def make_then_cancel(**kwargs):
            conversation = construct(**kwargs)
            (self.directory / "STOP").touch()
            return conversation

        runtime.make_conversation = make_then_cancel
        result = self.run_case(runtime)
        self.assertEqual(result["status"], "stopped")
        self.assertNotIn("start", runtime.calls)
        self.assertFalse(result["provider_session_attempted"])
        self.assertEqual(result["billing"], {"status": "not_started", "cost_usd": 0.0})

    def test_stopping_event_exists_before_slow_cleanup(self):
        runtime = FakeRuntime()
        construct = runtime.make_conversation
        observed = []

        def make_with_cleanup_observer(**kwargs):
            conversation = construct(**kwargs)
            original_end = conversation.end_session

            def observe_end():
                events = [json.loads(line) for line in (self.directory / "events.jsonl").read_text().splitlines()]
                observed.append("worker_stopping" in [event["kind"] for event in events])
                original_end()

            conversation.end_session = observe_end
            return conversation

        runtime.make_conversation = make_with_cleanup_observer
        self.run_case(runtime)
        self.assertEqual(observed, [True])

    def test_time_limit_closes_audio(self):
        runtime = FakeRuntime(mode="idle")
        result = self.run_case(runtime)
        self.assertEqual(result["status"], "ended_at_limit")
        self.assertTrue(result["audio_stopped"])
        self.assertLess(result["duration_seconds"], 2)

    def test_start_failure_is_sanitized_and_closes_audio(self):
        runtime = FakeRuntime(mode="error")
        result = self.run_case(runtime)
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["audio_stopped"])
        self.assertEqual(result["billing"]["status"], "unknown")
        self.assertNotIn("FAKE_SECRET", (self.directory / "events.jsonl").read_text())
        self.assertNotIn("FAKE_SECRET", (self.directory / "result.json").read_text())

    def test_disabled_prompt_override_fails_before_session(self):
        runtime = FakeRuntime(permission=False)
        result = self.run_case(runtime)
        self.assertEqual(result["status"], "failed_preflight")
        self.assertEqual(result["error_code"], "enable_agent_security_overrides_system_prompt")
        self.assertFalse(result["provider_session_attempted"])
        self.assertEqual(runtime.calls, [])

    def test_first_message_requires_its_own_override_permission(self):
        agent = {"platform_settings": {"overrides": {"conversation_config_override": {
            "agent": {"prompt": {"prompt": True}, "first_message": False}}}}}
        with self.assertRaisesRegex(worker.PreflightError, "enable_agent_security_overrides_first_message"):
            worker.check_override_permissions(agent)

    def test_prompts_are_per_call_and_do_not_leak_authorization(self):
        first = worker.session_overrides(self.request)
        second_request = {**self.request, "brief": "Ask about opening hours.", "context": "Another caller."}
        second = worker.session_overrides(second_request)
        self.assertNotIn(self.request["context"], second["agent"]["prompt"]["prompt"])
        self.assertNotIn(self.request["authorization"], first["agent"]["prompt"]["prompt"])
        self.assertEqual(set(first), {"agent"})
        self.assertEqual(set(first["agent"]), {"prompt", "first_message"})
        self.assertEqual(first["agent"]["first_message"], "")

    def test_finished_request_is_not_replayed(self):
        first = self.run_case(FakeRuntime())
        second_runtime = FakeRuntime()
        second = self.run_case(second_runtime)
        self.assertEqual(first, second)
        self.assertEqual(second_runtime.key_reads, 0)
        self.assertEqual(second_runtime.calls, [])

    def test_active_worker_lock_prevents_second_start(self):
        runtime = FakeRuntime()
        with (self.directory / "worker.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.run_case(runtime)
        self.assertEqual(result["status"], "already_running")
        self.assertEqual(runtime.key_reads, 0)

    def test_crashed_attempt_without_result_is_not_replayed(self):
        (self.directory / "worker.json").write_text('{"pid": 123}')
        runtime = FakeRuntime()
        result = self.run_case(runtime)
        self.assertEqual(result["status"], "recovery_required")
        self.assertIsNone(result["provider_session_started"])
        self.assertEqual(runtime.key_reads, 0)

    def test_invalid_budget_or_length_cannot_start(self):
        self.request["max_seconds"] = 301
        self.save()
        runtime = FakeRuntime()
        result = self.run_case(runtime)
        self.assertEqual(result["error_code"], "max_seconds_must_be_1_to_300")
        self.assertEqual(runtime.key_reads, 0)

    def test_unconfigured_voice_cannot_silently_use_elle(self):
        self.request["voice_name"] = "Luna"
        self.save()
        result = self.run_case(FakeRuntime())
        self.assertEqual(result["error_code"], "voice_not_configured_for_this_worker")

    def test_bounded_wait_does_not_block_forever(self):
        start = time.monotonic()
        result = worker.bounded_call(lambda: time.sleep(.3), .01)
        self.assertFalse(result["finished"])
        self.assertLess(time.monotonic() - start, .1)

    def test_incoming_gate_silences_input_and_discards_unanswered_output(self):
        delegate, stopped = FakeAudio(), threading.Event()
        gate = worker.IncomingAudioGate(delegate, stopped)
        received = []
        gate.start(received.append)
        delegate.callback(b"ringing")
        gate.output(b"old speech")
        self.assertEqual(received, [bytes(len(b"ringing"))])
        self.assertEqual(delegate.outputs, [])
        self.assertTrue(gate.open_after_answer())
        self.assertFalse(gate.open_after_answer())
        delegate.callback(b"boundary block")
        self.assertEqual(received[-1], bytes(len(b"boundary block")))
        delegate.callback(b"caller speech")
        gate.output(b"new speech")
        self.assertEqual(received[-1], b"caller speech")
        self.assertEqual(delegate.outputs, [b"new speech"])
        self.assertEqual(gate.discarded_output_bytes, len(b"old speech"))
        self.assertEqual(delegate.interrupts, 1)
        stopped.set()
        delegate.callback(b"after stop")
        gate.output(b"after stop")
        self.assertEqual(delegate.outputs, [b"new speech"])
        self.assertEqual(received[-1], b"caller speech")

    def test_incoming_worker_greets_only_after_matching_answer_marker(self):
        self.request["direction"] = "incoming"
        self.save()
        runtime = FakeRuntime(mode="incoming")
        timer = threading.Timer(.05, lambda: worker.atomic_json(
            self.directory / "ANSWERED", {"call_id": self.request["call_id"]}))
        timer.start()
        result = self.run_case(runtime)
        timer.join()
        self.assertEqual(result["status"], "disconnected")
        self.assertTrue(result["incoming_gate_opened"])
        self.assertEqual(runtime.user_messages, [worker.INCOMING_CONNECTED_MESSAGE])
        self.assertEqual(runtime.audio.outputs, [b"fresh greeting"])
        self.assertEqual(runtime.input_received, [bytes(len(b"ringing"))])
        self.assertEqual(result["incoming_discarded_output_bytes"], len(b"early speech"))
        kinds = [json.loads(line)["kind"] for line in (self.directory / "events.jsonl").read_text().splitlines()]
        self.assertLess(kinds.index("provider_connected"), kinds.index("incoming_answered"))
        self.assertIn("host_control_echo", kinds)
        self.assertNotIn("transcript", kinds)
        self.assertIn("incoming_greeting_requested", kinds)

    def test_incoming_without_answer_never_sends_greeting(self):
        self.request["direction"] = "incoming"
        self.save()
        runtime = FakeRuntime(mode="idle")
        result = self.run_case(runtime)
        self.assertEqual(result["status"], "ended_at_limit")
        self.assertFalse(result["incoming_gate_opened"])
        self.assertEqual(runtime.audio.outputs, [])
        self.assertEqual(runtime.user_messages, [])

    def test_incoming_mismatched_marker_fails_closed(self):
        self.request["direction"] = "incoming"
        self.save()
        worker.atomic_json(self.directory / "ANSWERED", {"call_id": "another_call"})
        runtime = FakeRuntime(mode="idle")
        result = self.run_case(runtime)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_code"], "invalid_incoming_answer_marker")
        self.assertFalse(result["incoming_gate_opened"])
        self.assertEqual(runtime.audio.outputs, [])
        self.assertEqual(runtime.user_messages, [])

    def test_outgoing_audio_behavior_is_unchanged(self):
        runtime = FakeRuntime()
        result = self.run_case(runtime)
        self.assertEqual(result["direction"], "outgoing")
        self.assertEqual(runtime.input_received, [b"ringing"])
        self.assertEqual(runtime.audio.outputs, [b"early speech"])
        self.assertEqual(runtime.user_messages, [])
        self.assertNotIn("incoming_gate_opened", result)

    def test_incoming_prompt_does_not_claim_to_be_calling_out(self):
        prompt = worker.session_overrides({**self.request, "direction": "incoming"})["agent"]["prompt"]["prompt"]
        self.assertIn("answering an incoming call", prompt)
        self.assertIn("INCOMING_CALL_CONNECTED", prompt)
        self.assertIn("Caller ID is a routing label", prompt)
        self.assertNotIn("host handles dialing", prompt)


if __name__ == "__main__":
    unittest.main()
