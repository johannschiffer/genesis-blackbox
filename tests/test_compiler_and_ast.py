import pytest
from blackbox.ast_engine.graph import CodebaseGraph
from blackbox.compiler.autopsy import CausalCrashCompiler
from blackbox.recorder.wal_ring import WalRingBuffer


def test_ast_engine_call_graph_and_blast_radius():
    sample_code = """
def database_query(query):
    return execute(query)

def fetch_user_data(user_id):
    q = f"SELECT * FROM users WHERE id = {user_id}"
    return database_query(q)

def handle_user_profile_request(request):
    uid = request['user_id']
    return fetch_user_data(uid)
"""
    graph = CodebaseGraph()
    graph.parse_source(sample_code, filename="service.py")

    # Blast radius for database_query:
    # upstream callers: fetch_user_data, handle_user_profile_request
    # downstream callees: execute
    radius = graph.get_blast_radius("database_query")
    assert radius["target"] == "database_query"
    assert "fetch_user_data" in radius["upstream_callers"]
    assert "handle_user_profile_request" in radius["upstream_callers"]
    assert "execute" in radius["downstream_callees"]
    assert radius["total_blast_radius"] == 3


def test_causal_crash_compiler_autopsy_capsule():
    ring = WalRingBuffer(db_path=":memory:")
    ring.record("HTTP_REQ", {"path": "/api/profile", "status": 500})
    ring.record("SQL_QUERY", {"sql": "SELECT * FROM users WHERE id = 42"})

    graph = CodebaseGraph()
    graph.parse_source("""
def crash_point():
    pass

def parent_caller():
    crash_point()
""", filename="app.py")

    compiler = CausalCrashCompiler(wal_ring=ring, ast_graph=graph)

    mock_exception = {
        "exception_type": "IndexError",
        "exception_message": "list index out of range",
        "root_cause": {
            "filename": "app.py",
            "lineno": 12,
            "function": "crash_point",
            "code_line": "item = items[0]",
            "locals": {"items": [], "user_id": 42}
        }
    }

    capsule = compiler.compile(mock_exception)
    assert "# 🚨 BLACKBOX CRASH AUTOPSY CAPSULE" in capsule
    assert "`IndexError`: list index out of range" in capsule
    assert "**Culprit Statement:** `item = items[0]`" in capsule
    assert "`items` | `[]`" in capsule
    assert "AST Blast-Radius Impact" in capsule
    assert "Flight Recorder Timeline" in capsule
    assert "HTTP_REQ" in capsule
    assert "SQL_QUERY" in capsule
