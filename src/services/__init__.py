"""Business logic layer (UI-agnostic, ready for a future read-only MCP server)."""

from src.services.comparison import comparison_as_dicts, get_comparison_matrix
from src.services.digest import digest_kpis, list_digest
from src.services.feedback import record_feedback

__all__ = [
    "list_digest",
    "digest_kpis",
    "get_comparison_matrix",
    "comparison_as_dicts",
    "record_feedback",
]
