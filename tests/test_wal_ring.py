import time
import threading
import pytest
from blackbox.recorder.wal_ring import WalRingBuffer


def test_wal_ring_basic_record_and_query():
    ring = WalRingBuffer(db_path=":memory:", max_records=100)
    eid1 = ring.record(event_type="HTTP_REQ", payload={"path": "/api/v1/users"}, source="gateway")
    eid2 = ring.record(event_type="SQL_QUERY", payload={"sql": "SELECT * FROM users"}, source="db")

    assert eid1 == 1
    assert eid2 == 2
    assert ring.count() == 2

    recent = ring.query_recent(limit=10)
    assert len(recent) == 2
    assert recent[0]["id"] == 2
    assert recent[0]["event_type"] == "SQL_QUERY"
    assert recent[0]["payload"]["sql"] == "SELECT * FROM users"
    assert recent[1]["id"] == 1


def test_wal_ring_circular_pruning():
    max_recs = 50
    prune_batch = 10
    ring = WalRingBuffer(db_path=":memory:", max_records=max_recs, prune_batch_size=prune_batch)

    # Insert 100 records
    for i in range(100):
        ring.record(event_type="LOG", payload={"index": i})

    # Total records should have been pruned and remain under capacity
    count = ring.count()
    assert count <= max_recs + prune_batch
    # Oldest events (0..20) should have been pruned
    oldest = ring.query_recent(limit=100)[-1]
    assert oldest["payload"]["index"] > 0


def test_wal_ring_sub_millisecond_write_latency():
    ring = WalRingBuffer(db_path=":memory:", max_records=1000)
    iterations = 300

    start_time = time.perf_counter()
    for i in range(iterations):
        ring.record("PERF_TEST", {"num": i})
    total_time = time.perf_counter() - start_time

    avg_latency_ms = (total_time / iterations) * 1000.0
    print(f"\nAverage write latency: {avg_latency_ms:.3f} ms per record")
    # Sub-millisecond requirement (< 1.0 ms)
    assert avg_latency_ms < 1.0


def test_wal_ring_timeline_filtering():
    ring = WalRingBuffer(db_path=":memory:")
    t0 = time.time()
    ring.record("EVENT_1", {"v": 1})
    time.sleep(0.01)
    t1 = time.time()
    ring.record("EVENT_2", {"v": 2})

    timeline = ring.get_timeline(start_time=t1)
    assert len(timeline) == 1
    assert timeline[0]["event_type"] == "EVENT_2"


def test_wal_ring_thread_safety():
    ring = WalRingBuffer(db_path=":memory:", max_records=2000)
    errors = []

    def _worker(thread_id):
        try:
            for i in range(50):
                ring.record(f"THREAD_{thread_id}", {"step": i})
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(errors) == 0
    assert ring.count() == 250
