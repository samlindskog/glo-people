"""Search: finding and storing information about people through one engine.

- `engine` (re-exported here): literal filters + per-property semantic similarity behind one
  `SearchRequest`, validated against the property catalog; the `searches` history.
- `ask`: agentic search — an agent plans a `SearchRequest` from a question, the engine runs it,
  an agent answers from the hits with citations.
- `store`: agentic storage — an agent splits a passage into statements, each filed under the
  property whose name is nearest (or a new free-text one, within caps), written through people.

The engine sits below the agents (they validate plans against it); `ask` and `store` sit above
them and are imported explicitly (`from app.search import ask`), never from this module, so the
package has no import cycle.
"""

from app.search.engine import (
    MAX_LIMIT,
    FieldFilter,
    FieldQuery,
    FilterOp,
    SearchHit,
    SearchRequest,
    SearchResponse,
    SemanticQuery,
    SortSpec,
    describe,
    filter_to_mongo,
    mongo_query,
    record_search,
    run_search,
    sort_path,
    validate,
)

__all__ = ["MAX_LIMIT", "FieldFilter", "FieldQuery", "FilterOp", "SearchHit", "SearchRequest",
           "SearchResponse", "SemanticQuery", "SortSpec", "describe", "filter_to_mongo",
           "mongo_query", "record_search", "run_search", "sort_path", "validate"]
