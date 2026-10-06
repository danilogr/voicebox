"""What a plugin's telemetry tables look like, as far as storing and shipping need.

Every plugin's contract has the same `installs` table, a `sessions` table with the
same bookkeeping columns (`session_id`, `install_id`, `started_at`, `ended_at`,
`harness`, `harness_version`, and a local `shipped_at`), and one table of rows it
ships - `utterances` for one plugin, `jobs` for another. The store and shipper handle
that table through this description and never by name.
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RowTable:
    """The one table of rows a plugin ships. Its name is also the key its rows travel
    under in the request body. Every row table has local `shipped_at` and
    `rejected_at` columns, which is how the store tracks its backlog."""

    name: str
    id_column: str
    # Oldest-first order for shipping and for pruning the backlog.
    order_column: str
    # Every column of the table, local ones included, in the table's order.
    columns: tuple[str, ...]
    # Columns that never leave the machine: the contract's `-- LOCAL` ones.
    local_columns: frozenset[str]
    # Stored as 0/1, typed `boolean` on the wire.
    boolean_columns: tuple[str, ...] = ()
    # A per-session sequence number assigned inside the INSERT, when the table has one.
    seq_column: str | None = None


@dataclass(frozen=True, slots=True)
class TelemetrySchema:
    """A plugin's whole local schema: its DDL and what the store needs to know of it."""

    # The contract's full local `schema.sql`, `-- LOCAL` columns included.
    sql: str
    # Every `sessions` column the client writes and uploads; the table's one local
    # column, `shipped_at`, is not among them.
    session_columns: tuple[str, ...]
    rows: RowTable
    session_boolean_columns: tuple[str, ...] = ()
