"""
Genesis Blackbox: Flight Recorder & AST Blast-Radius Causal Crash Compiler.
"""

__version__ = "0.2.0"
__author__ = "Johann Schiffer / Project Genesis"
__license__ = "MIT"

from .recorder.wal_ring import WalRingBuffer
from .recorder.exception_hook import ExceptionHook, SafeSerializer, install_exception_hook, uninstall_exception_hook
from .recorder.http_interceptor import BlackboxASGIMiddleware, BlackboxWSGIMiddleware, HttpTraceContext
from .recorder.sql_interceptor import TracedConnection, TracedCursor, SqlInterceptor, SqlTraceContext
from .recorder.process_hook import SubprocessInterceptor
from .instrumentation import auto_instrument
from .integrations import instrument_fastapi, instrument_flask, instrument_engine
from .ast_engine.graph import CodebaseGraph
from .ast_engine.polyglot import PolyglotParser, parse_v8_stack_trace
from .ast_engine.impact import ImpactTestSelector
from .compiler.autopsy import CausalCrashCompiler
from .compiler.reproducer import CrashReproducer
from .compiler.verifier import CausalVerifier
from .compiler.patch_validator import PatchValidator
from .compiler.state_differ import StateDiffer
from .ci.github_summary import export_github_step_summary
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
    "instrument_fastapi",
    "instrument_flask",
    "instrument_engine",
    "CodebaseGraph",
    "PolyglotParser",
    "parse_v8_stack_trace",
    "ImpactTestSelector",
    "CausalCrashCompiler",
    "CrashReproducer",
    "CausalVerifier",
    "PatchValidator",
    "StateDiffer",
    "export_github_step_summary",
    "BlackboxMCPServer"
]



