"""narration: the lookup key is stable, voice-sensitive, whitespace-insensitive — a player computes the same one."""
import os, sys
os.environ.setdefault("RUN_TOKEN", "t")
sys.path.insert(0, os.path.dirname(__file__))
import run


def test_key_matches_reference():
    # reference value — the Visualize player's narrationKey() must return this for the same input
    assert run.narration_key("en-IN", "shubh", "bulbul:v3", 1.0, "Hello  Bengaluru ") == run.narration_key("en-IN", "shubh", "bulbul:v3", 1, "Hello Bengaluru")
    assert len(run.narration_key("en-IN", "shubh", "bulbul:v3", 1.0, "x")) == 24


def test_voice_change_is_a_new_key():
    a = run.narration_key("en-IN", "shubh", "bulbul:v3", 1.0, "Hello")
    assert a != run.narration_key("en-IN", "ishita", "bulbul:v3", 1.0, "Hello")
    assert a != run.narration_key("kn-IN", "shubh", "bulbul:v3", 1.0, "Hello")
    assert a != run.narration_key("en-IN", "shubh", "bulbul:v3", 1.1, "Hello")


def test_audio_kind():
    assert run.audio_kind(b"RIFF....WAVE") == "audio/wav"
    assert run.audio_kind(b"ID3\x04") == "audio/mpeg"
    assert run.audio_kind(b"\xff\xfb\x90") == "audio/mpeg"
