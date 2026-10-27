import sys
import pytest
from blackbox.recorder.exception_hook import ExceptionHook, SafeSerializer
from blackbox.recorder.wal_ring import WalRingBuffer


def test_safe_serializer_primitives_and_truncation():
    serializer = SafeSerializer(max_str_len=20, max_items=5)
    res = serializer.serialize("A very long string that should be truncated")
    assert "..." in res
    assert len(res) < 40

    large_list = list(range(20))
    res_list = serializer.serialize(large_list)
    assert len(res_list) == 6
    assert "... +15 more items" in res_list[-1]


def test_safe_serializer_circular_reference_safety():
    serializer = SafeSerializer()

    # Self-referencing list
    circular_list = [1, 2]
    circular_list.append(circular_list)
    res_list = serializer.serialize(circular_list)
    assert res_list[0] == 1
    assert res_list[1] == 2
    assert res_list[2] == "<circular-reference>"

    # Self-referencing dict
    circular_dict = {"a": 10}
    circular_dict["self"] = circular_dict
    res_dict = serializer.serialize(circular_dict)
    assert res_dict["a"] == 10
    assert res_dict["self"] == "<circular-reference>"


def test_safe_serializer_sensitive_key_redaction():
    serializer = SafeSerializer()
    payload = {
        "username": "johann",
        "password": "super_secret_password_123",
        "api_key": "sk-123456789",
        "auth_token": "bearer xyz",
        "normal_field": "visible_value"
    }
    serialized = serializer.serialize(payload)
    assert serialized["username"] == "johann"
    assert serialized["normal_field"] == "visible_value"
    assert "REDACTED" in serialized["password"]
    assert "REDACTED" in serialized["api_key"]
    assert "REDACTED" in serialized["auth_token"]


def test_exception_hook_deep_inspection_and_wal_integration():
    ring = WalRingBuffer(db_path=":memory:")
    hook = ExceptionHook(wal_ring=ring)

    def _failing_function(user_id, token):
        secret_pin = 4321
        normal_counter = 999
        raise ValueError(f"User {user_id} unauthorized")

    try:
        _failing_function(42, "secret_token_val")
    except ValueError:
        exc_type, exc_value, tb = sys.exc_info()
        data = hook.handle_exception(exc_type, exc_value, tb)

    # Verify serialized exception structure
    assert data["exception_type"] == "ValueError"
    assert "User 42 unauthorized" in data["exception_message"]
    assert data["total_frames"] >= 1

    root = data["root_cause"]
    assert root is not None
    assert root["function"] == "_failing_function"
    assert root["locals"]["user_id"] == 42
    assert "REDACTED" in root["locals"]["token"]
    assert "REDACTED" in root["locals"]["secret_pin"]
    assert root["locals"]["normal_counter"] == 999

    # Verify event was recorded to WAL ring buffer
    events = ring.query_recent(limit=5)
    assert len(events) == 1
    assert events[0]["event_type"] == "EXCEPTION"
    assert events[0]["status"] == "error"
    assert events[0]["payload"]["exception_type"] == "ValueError"
