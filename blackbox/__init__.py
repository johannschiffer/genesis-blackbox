"""
Genesis Blackbox: Flight Recorder & AST Blast-Radius Causal Crash Compiler.
100% Free & Open Source under the MIT License.
"""

__version__ = "0.1.0"
__author__ = "Johann Schiffer / Project Genesis"
__license__ = "MIT"

from .recorder.wal_ring import WalRingBuffer
from .recorder.exception_hook import ExceptionHook, SafeSerializer

__all__ = ["WalRingBuffer", "ExceptionHook", "SafeSerializer"]
