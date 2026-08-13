"""High-precision contract intelligence domain.

The package is deliberately separate from the document execution Agent.  It owns
read-only knowledge ingestion, stable evidence, retrieval and verification contracts.
"""

from .domain import ParseResult, ParsedBlock, stable_evidence_id
from .providers import ParserProvider, ParserRegistry

__all__ = [
    "ParseResult",
    "ParsedBlock",
    "ParserProvider",
    "ParserRegistry",
    "stable_evidence_id",
]
