from classification.metadata import parse_session_metadata
from classification.state import StateManager


def test_parse_session_metadata_prefers_event_store_id():
    metadata = parse_session_metadata(
        {
            "session_id": "20260716-120000-1-ivan-00000000-0000-0000-0000-000000000000",
            "store_id": "42",
        }
    )

    assert metadata["store_id"] == "42"


def test_parse_session_metadata_reads_full_session_id():
    metadata = parse_session_metadata(
        {
            "session_id": "20260716-120000-7-ivan-00000000-0000-0000-0000-000000000000",
        }
    )

    assert metadata["store_id"] == "7"
    assert metadata["seller_id"] == "ivan"


def test_parse_session_metadata_reads_simple_session_id():
    metadata = parse_session_metadata({"session_id": "3-иванов_иван"})

    assert metadata["store_id"] == "3"
    assert metadata["seller_id"] == "иванов_иван"


def test_state_manager_isolates_store_runtime_state():
    manager = StateManager()

    store_one = manager.store("1")
    store_two = manager.store("2")

    store_one.threshold_sent = True
    store_one.active_sessions.add("session-1")

    assert store_two.threshold_sent is False
    assert store_two.active_sessions == set()
