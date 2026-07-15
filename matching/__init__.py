"""
matching/__init__.py — Public API of the matching package.

Import from here rather than individual submodules.
"""

from matching.engine import MatchInput, MatchingEngine
from matching.tolerance import compute_variance, within_tolerance

__all__ = [
    "MatchInput",
    "MatchingEngine",
    "compute_variance",
    "within_tolerance",
]
