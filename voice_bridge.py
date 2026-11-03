#!/usr/bin/env python3
"""Bounded, authenticated ElevenLabs conversation across two Loopback devices."""
import argparse
import json
import os
import queue
import signal
import subprocess
import threading
import time
import wave
from pathlib import Path

import numpy as np
import sounddevice as sd
from elevenlabs import ElevenLabs
from elevenlabs.conversational_ai.conversation import AudioInterface, Conversation

ROOT = Path(__file__).resolve().parent
RATE = 16000


def device(name, direction):
    matches = [i for i, d in enumerate(sd.query_devices())
               if d["name"] == name and d[f"max_{direction}_channels"] > 0]
    if len(matches) != 1:
        raise RuntimeError(f"Expected one {direction} device named {name!r}; found {len(matches)}")
    return matches[0]


class RoutedAudio(AudioInterface):
    def __init__(self, incoming, outgoing):
        self.incoming, self.outgoing = incoming, outgoing
        self.pending = queue.Queue()
        self.done = threading.Event()
        self.generation = 0
        self.input_samples = self.output_samples = 0
        self.playing = False
        self.last_played_at = 0.0

    def start(self, input_callback):
        self.input_callback = input_callback
        self.speaker = sd.RawOutputStream(device=self.outgoing, samplerate=RATE,
                                         channels=1, dtype="int16", blocksize=1000)
        self.listener = sd.RawInputStream(device=self.incoming, samplerate=RATE,
                                         channels=1, dtype="int16", blocksize=4000,
                                         callback=self._capture)
        self.speaker.start()
        self.listener.start()
        self.worker = threading.Thread(target=self._play, daemon=True)
        self.worker.start()
        if hasattr(self, "on_started"):
            self.on_started()

    def _capture(self, raw, frames, timing, status):
        if not self.done.is_set():
            self.input_samples += frames
            self.input_callback(bytes(raw))

    def _play(self):
        while not self.done.is_set():
            try:
                generation, raw = self.pending.get(timeout=.1)
            except queue.Empty:
                continue
            self.playing = True
            for offset in range(0, len(raw), 2000):
                if self.done.is_set() or generation != self.generation:
                    break
                part = raw[offset:offset + 2000]
                self.speaker.write(part)
                self.output_samples += len(part) // 2
                self.last_played_at = time.monotonic()
            self.playing = False

    def output(self, audio):
        self.pending.put((self.generation, audio))

    def interrupt(self):
        self.generation += 1
        while True:
            try:
                self.pending.get_nowait()
            except queue.Empty:
                break

    def stop(self):
        if self.done.is_set():
            return
        self.done.set()
        if hasattr(self, "listener"):
            self.listener.stop()
            self.listener.close()
        if hasattr(self, "worker"):
            self.worker.join(timeout=2)
        if hasattr(self, "speaker"):
            self.speaker.stop()
            self.speaker.close()
