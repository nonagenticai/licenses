"""Count the SQL statements a block of code actually issues on an engine.

Extracted from ``tests/unit/test_introspect_query_count.py`` (which still owns
the introspect hot-path assertions) so a second caller does not fork a second
implementation. The other caller is ``tests/unit/test_endpoints.py``, which uses
it for the opposite assertion: ``/health`` must issue ZERO statements, because
it is an internet-reachable unauthenticated endpoint on a ``NullPool`` engine
and a query there is a connection-exhaustion amplifier.
"""

from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from types import SimpleNamespace

from sqlalchemy import event


@contextmanager
def count_statements(engine):
    """Capture SQL statements issued on ``engine`` within the block.

    Yields a namespace with ``.verbs`` (a ``Counter`` of lowercased SQL verbs,
    e.g. ``verbs["select"]``) and ``.statements`` (the raw statement list, for
    assertion messages).
    """
    rec = SimpleNamespace(verbs=Counter(), statements=[])

    def _before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        verb = statement.lstrip().split(None, 1)[0].lower() if statement.strip() else "other"
        rec.statements.append(statement)
        rec.verbs[verb] += 1

    event.listen(engine, "before_cursor_execute", _before_cursor_execute)
    try:
        yield rec
    finally:
        event.remove(engine, "before_cursor_execute", _before_cursor_execute)
