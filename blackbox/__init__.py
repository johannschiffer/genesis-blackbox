"""
Genesis Blackbox: Flight Recorder & AST Blast-Radius Causal Crash Compiler.
"""

__version__ = "0.1.1"
__author__ = "Johann Schiffer / Project Genesis"
__license__ = "MIT"

from .recorder.wal_ring import WalRingBuffer
from .recorder.exception_hook import ExceptionHook, SafeSerializer, install_exception_hook, uninstall_exception_hook
from .recorder.http_interceptor import BlackboxASGIMiddleware, BlackboxWSGIMiddleware, HttpTraceContext
from .recorder.sql_interceptor import TracedConnection, TracedCursor, SqlInterceptor, SqlTraceContext
from .recorder.process_hook import SubprocessInterceptor
from .instrumentation import auto_instrument
from .ast_engine.graph import CodebaseGraph
from .compiler.autopsy import CausalCrashCompiler
from .compiler.reproducer import CrashReproducer
from .mcp_server import BlackboxMCPServer

__all__ = [
    "WalRingBuffer",
    "ExceptionHook",
    "SafeSerializer",
    "install_exception_hook",
    "uninstall_exception_hook",
    "BlackboxASGIMiddleware",
    "BlackboxWSGIMiddleware",
    "HttpTraceContext",
    "TracedConnection",
    "TracedCursor",
    "SqlInterceptor",
    "SqlTraceContext",
    "SubprocessInterceptor",
    "auto_instrument",
    "CodebaseGraph",
    "CausalCrashCompiler",
    "CrashReproducer",
    "BlackboxMCPServer"
]

