"""
Genesis Blackbox: Flight Recorder & AST Blast-Radius Causal Crash Compiler.
100% Free & Open Source under the MIT License.
"""

__version__ = "0.2.0"
__author__ = "Johann Schiffer / Project Genesis"
__license__ = "MIT"

from .recorder.wal_ring import WalRingBuffer
from .recorder.exception_hook import ExceptionHook, SafeSerializer
from .recorder.http_interceptor import BlackboxASGIMiddleware, BlackboxWSGIMiddleware, HttpTraceContext
from .recorder.sql_interceptor import TracedConnection, TracedCursor, SqlInterceptor, SqlTraceContext
from .ast_engine.graph import CodebaseGraph
from .compiler.autopsy import CausalCrashCompiler
from .mcp_server import BlackboxMCPServer

__all__ = [
    "WalRingBuffer",
    "ExceptionHook",
    "SafeSerializer",
    "BlackboxASGIMiddleware",
    "BlackboxWSGIMiddleware",
    "HttpTraceContext",
    "TracedConnection",
    "TracedCursor",
    "SqlInterceptor",
    "SqlTraceContext",
    "CodebaseGraph",
    "CausalCrashCompiler",
    "BlackboxMCPServer"
]
