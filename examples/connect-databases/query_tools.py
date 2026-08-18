"""A parameterized tool set over PostgreSQL for an agent, instead of free-form SQL.

Pairs with setup_agent_role.sql in this directory. Requires psycopg 3:

    pip install "psycopg[binary]"
    export AGENT_DB_DSN="postgresql://agent_readonly:...@127.0.0.1:5432/app"
    python3 query_tools.py 1
    python3 query_tools.py 2   # same code, different rows

The shape worth copying: every tool is a fixed SQL string with placeholders,
the acting end user's id is carried into the session with set_config() as a
bound parameter, and the whole thing runs inside a read-only transaction so the
identity cannot outlive it on a pooled connection.
"""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row

# The agent may not choose this. Anything not in the map is not a query.
ALLOWED_STATUSES = frozenset({"pending", "shipped", "refunded", "cancelled"})

# Hard ceiling on rows returned to the model, enforced by FETCH rather than by
# editing the SQL text. See fetch_generated_sql() for why that distinction matters.
MAX_ROWS = 200


@contextmanager
def acting_as(conn: psycopg.Connection, user_id: int) -> Iterator[psycopg.Connection]:
    """Open a read-only transaction that carries one end user's identity.

    set_config(name, value, is_local=True) is the only correct way to do this:

    * It takes the value as a bound parameter. `SET app.acting_user_id = %s` is
      not valid SQL -- SET is a utility statement and cannot be prepared -- so
      the only way to write it is string concatenation, which is an injection
      hole in the exact place you were trying to close one.
    * is_local=True scopes the setting to this transaction. With False, the
      value survives on the connection and the next borrower of that pooled
      connection inherits somebody else's identity.
    """
    if not isinstance(user_id, int) or isinstance(user_id, bool):
        raise TypeError("user_id must be an int")
    with conn.transaction():
        conn.execute("SET TRANSACTION READ ONLY")
        conn.execute(
            "SELECT set_config('app.acting_user_id', %s, true)",
            (str(user_id),),
        )
        yield conn


def order_summary(conn: psycopg.Connection, user_id: int) -> list[dict[str, Any]]:
    """Tool 1: totals per account and status, for accounts this user belongs to."""
    with acting_as(conn, user_id) as session:
        cursor = session.execute(
            """
            SELECT account_id, status, order_count, total_cents
            FROM public.order_summary
            ORDER BY account_id, status
            """
        )
        return cursor.fetchall()


def orders_by_status(
    conn: psycopg.Connection, user_id: int, status: str
) -> list[dict[str, Any]]:
    """Tool 2: recent orders in one status.

    `status` is validated against a closed set before it is bound. Validation and
    parameterization answer different questions: the placeholder stops the value
    from becoming SQL, the allow-list stops the model from asking a question the
    tool was never meant to answer.
    """
    if status not in ALLOWED_STATUSES:
        raise ValueError(f"status must be one of {sorted(ALLOWED_STATUSES)}")

    with acting_as(conn, user_id) as session:
        cursor = session.execute(
            """
            SELECT id, account_id, status, total_cents, placed_at
            FROM public.orders
            WHERE status = %s
            ORDER BY placed_at DESC
            LIMIT %s
            """,
            (status, MAX_ROWS),
        )
        return cursor.fetchall()


def fetch_generated_sql(
    conn: psycopg.Connection, user_id: int, sql: str
) -> list[dict[str, Any]]:
    """Escape hatch: run one model-written SELECT under the same containment.

    Only reach for this when the decision table in the guide says text-to-SQL
    earns its risk. Four things are load-bearing here and none of them is the
    SQL string:

    * The connection belongs to agent_readonly, which holds SELECT on named
      relations and nothing else, so a generated INSERT fails on privilege.
    * The transaction is READ ONLY and carries the acting user's id, so RLS
      still decides which rows exist.
    * The generated query goes inside a subquery. Do not append `LIMIT` to model
      output -- a trailing `-- comment` swallows the clause, and a query that
      already ends in LIMIT or in a semicolon becomes a syntax error. Wrapping
      makes those same tricks fail closed instead: `SELECT 1; DROP TABLE t`
      raises `syntax error at or near ";"`, an unterminated comment raises
      `syntax error at end of input`, and a data-modifying CTE raises `WITH
      clause containing a data-modifying statement must be at the top level`.
    * The outer LIMIT is a bound parameter, so psycopg sends the statement over
      the extended query protocol, which refuses more than one command per
      execute() regardless of what the wrapper let through.

    What this does NOT do is make the SQL text safe. `sql` is interpolated,
    because a query cannot be a bound parameter. Containment is the role.
    """
    with acting_as(conn, user_id) as session:
        cursor = session.execute(
            f"SELECT * FROM ({sql}) AS agent_query LIMIT %s",
            (MAX_ROWS,),
        )
        return cursor.fetchall()


def main() -> None:
    user_id = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    dsn = os.environ["AGENT_DB_DSN"]

    with psycopg.connect(dsn, row_factory=dict_row, autocommit=True) as conn:
        # application_name lands in log_line_prefix %a, so every statement in the
        # server log is attributable to a run without parsing the SQL.
        conn.execute(
            "SELECT set_config('application_name', %s, false)",
            (f"agent:order-triage:user={user_id}",),
        )

        print(f"-- order_summary as user {user_id}")
        for row in order_summary(conn, user_id):
            print(row)

        print(f"-- shipped orders as user {user_id}")
        for row in orders_by_status(conn, user_id, "shipped"):
            print(row)


if __name__ == "__main__":
    main()
