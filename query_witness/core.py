"""Budgeted generation, DuckDB execution, bag comparison, and row reduction."""

from collections import Counter
from dataclasses import dataclass
import math
import threading
import time

import duckdb

from .subset import Input, Unsupported

POLICY = (
    "Columns by position; aliases ignored; matching column counts required. "
    "Order is compared as sequences only when both queries have ORDER BY; otherwise unordered bags. "
    "Duplicate counts are preserved; SQL NULLs compare equal. "
    "Equivalent integer widths compare by value. "
    "No outcome is an equivalence proof. DuckDB behavior only."
)
POLICY_ID = "integer-position-v2"
ORDER_NOTICE = "Order was not compared; bags only (exactly one query has ORDER BY)."
INTEGER_TYPES = {"TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT", "UTINYINT",
                 "USMALLINT", "UINTEGER", "UBIGINT", "UHUGEINT"}


class Limit(RuntimeError):
    pass


@dataclass(frozen=True)
class Config:
    max_rows: int = 4
    max_candidates: int = 64
    timeout_seconds: float = 5.0
    memory_mb: int = 64

    def __post_init__(self):
        for name, low, high in (("max_rows", 0, 1000), ("max_candidates", 1, 100_000),
                                ("memory_mb", 1, 4096)):
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"{name} must be an integer in [{low}, {high}]")
        if (type(self.timeout_seconds) not in (int, float)
                or not 0 < self.timeout_seconds <= 3600 or not math.isfinite(self.timeout_seconds)):
            raise ValueError("timeout_seconds must be finite, > 0 and <= 3600")


@dataclass(frozen=True)
class Result:
    types: tuple[str, ...]
    rows: tuple[tuple, ...]

    def __post_init__(self):
        if not self.types or any(t not in INTEGER_TYPES for t in self.types):
            raise ValueError("Expected integer output types")
        if any(len(row) != len(self.types) or any(v is not None and type(v) is not int for v in row)
               for row in self.rows):
            raise ValueError("Expected integer or NULL output values with matching column counts")

    def bag(self):
        # DuckDB returns supported integer widths as Python int, NULL as None.
        return Counter(self.rows)


def both_ordered(inputs):
    return all(inputs.ordered)


def differs(a, b, sequential=False):
    if len(a.types) != len(b.types):
        return True
    if sequential:
        return a.rows != b.rows
    return a.bag() != b.bag()


def validate_rows(rows, max_rows, inputs: Input):
    if len(rows) > max_rows:
        raise Limit("Data exceeds max_rows")
    for row in rows:
        if type(row) not in (list, tuple) or len(row) != len(inputs.columns):
            raise ValueError(f"Invalid data: expected {len(inputs.columns)} cells per row")
        for index, value in enumerate(row):
            if value is None:
                if not inputs.nullable[index]:
                    raise ValueError("Invalid data: NULL violates NOT NULL or PRIMARY KEY")
            elif type(value) is not int or not -(2**31) <= value < 2**31:
                raise ValueError("Invalid data: expected 32-bit INTEGER or NULL cells")
    if inputs.primary_key is not None:
        key_index = inputs.columns.index(inputs.primary_key)
        if len({row[key_index] for row in rows}) != len(rows):
            raise ValueError("Invalid data: duplicate values violate PRIMARY KEY")


def interesting_values(literals):
    """Stable base prefix, then accepted AST literals and in-range neighbors."""
    domain = dict.fromkeys((None, -1, 0, 1))
    for value in sorted(set(literals)):
        for neighbor in (value, value - 1, value + 1):
            if -(2**31) <= neighbor < 2**31:
                domain[neighbor] = None
    return tuple(domain)


def candidates(config, inputs: Input):
    """Empty, singletons, then at most 2 * len(domain) tables per larger size."""
    domain = interesting_values(inputs.literals)
    if len(inputs.columns) == 1:
        if not inputs.nullable[0]:
            domain = tuple(value for value in domain if value is not None)
    yield ()
    for size in range(1, config.max_rows + 1):
        if len(inputs.columns) == 2 and size == 1:
            for first in domain:
                for second in domain:
                    rows = ((first, second),)
                    try:
                        validate_rows(rows, config.max_rows, inputs)
                    except ValueError:
                        continue
                    yield rows
            continue
        if inputs.primary_key and size > len(domain):
            continue  # Every cycle would repeat a key; no legal table at this size.
        for offset, value in enumerate(domain):
            if len(inputs.columns) == 2:
                equal = ((value, value),) * size
                mixed = []
                for index in range(size):
                    first = domain[(offset + index) % len(domain)]
                    second = domain[(offset + index + 1) % len(domain)]
                    mixed.append((first, second))
                for rows in (equal, tuple(mixed)):
                    try:
                        validate_rows(rows, config.max_rows, inputs)
                    except ValueError:
                        continue
                    yield rows
                continue
            if not inputs.primary_key or size == 1:
                yield ((value,),) * size
            if size > 1:
                yield tuple((domain[(offset + i) % len(domain)],) for i in range(size))


class Engine:
    def __init__(self, inputs: Input, config: Config, deadline):
        self.inputs, self.config, self.deadline = inputs, config, deadline
        self.connection = None
        self.timer = None
        self.expired = threading.Event()
        self.stopped = threading.Event()

    def check_time(self):
        if self.expired.is_set() or time.monotonic() >= self.deadline:
            raise Limit("Elapsed-time budget reached")

    def _expire(self):
        if self.stopped.wait(max(0, self.deadline - time.monotonic())):
            return
        self.expired.set()
        # Repeat to cover expiration between the caller's deadline check and
        # DuckDB starting the next statement (interrupt only affects pending work).
        while not self.stopped.is_set():
            self.connection.interrupt()
            if self.stopped.wait(0.01):
                break

    def __enter__(self):
        self.check_time()
        self.connection = duckdb.connect(":memory:", config={
            "memory_limit": f"{self.config.memory_mb}MB", "threads": "1",
            "enable_external_access": "false", "max_temp_directory_size": "0B",
        })
        try:
            self.timer = threading.Thread(target=self._expire, daemon=True)
            try:
                self.timer.start()
            except RuntimeError as exc:
                if str(exc) != "can't start new thread":
                    raise
                raise Limit("Cannot start DuckDB time-budget watchdog: thread resources exhausted") from exc
            for source in (self.inputs.schema, self.inputs.query_a, self.inputs.query_b):
                try:
                    self.connection.extract_statements(source)
                except duckdb.ParserException as exc:
                    raise Unsupported(f"DuckDB cannot parse source SQL: {exc}") from exc
            self.execute(self.inputs.schema)
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *_):
        if self.timer:
            self.stopped.set()
            if self.timer.ident is not None:
                self.timer.join()
        if self.connection:
            self.connection.close()

    def execute(self, sql, params=None):
        self.check_time()
        try:
            cursor = self.connection.execute(sql, params) if params is not None else self.connection.execute(sql)
            description = cursor.description
            rows = cursor.fetchall()
        except duckdb.OutOfMemoryException as exc:
            raise Limit(f"DuckDB memory budget reached: {exc}") from exc
        except duckdb.InterruptException as exc:
            raise Limit("DuckDB execution interrupted by time budget") from exc
        except RuntimeError as exc:
            if isinstance(exc.__cause__, KeyboardInterrupt) or str(exc) == "Query interrupted":
                raise KeyboardInterrupt from None
            raise
        self.check_time()
        return description, rows

    def compare(self, rows):
        validate_rows(rows, self.config.max_rows, self.inputs)
        self.execute(f'DELETE FROM "{self.inputs.table}"')
        placeholders = ", ".join("?" for column in self.inputs.columns)
        for row in rows:
            self.execute(f'INSERT INTO "{self.inputs.table}" VALUES ({placeholders})', row)
        results = []
        for query in (self.inputs.query_a, self.inputs.query_b):
            description, values = self.execute(query)
            results.append(Result(tuple(str(d[1]) for d in description), tuple(map(tuple, values))))
        return tuple(results)


def reduce_rows(engine, rows):
    """Greedy deletion, maintaining valid data and an observed discrepancy."""
    rows = tuple(rows)
    results = engine.compare(rows)
    sequential = both_ordered(engine.inputs)
    if not differs(*results, sequential=sequential):
        raise ValueError("Cannot reduce data without a discrepancy")
    index = 0
    while index < len(rows):
        trial = rows[:index] + rows[index + 1:]
        observed = engine.compare(trial)
        if differs(*observed, sequential=sequential):
            rows, results = trial, observed
            index = 0
        else:
            index += 1
    engine.check_time()
    return rows, results


def search(engine):
    checked = 0
    sequential = both_ordered(engine.inputs)
    for rows in candidates(engine.config, engine.inputs):
        engine.check_time()
        if checked == engine.config.max_candidates:
            engine.check_time()
            return None, checked, "candidate budget exhausted"
        results = engine.compare(rows)
        checked += 1
        if differs(*results, sequential=sequential):
            reduced, observed = reduce_rows(engine, rows)
            return (reduced, observed), checked, "row-deletion reduction completed"
    engine.check_time()
    return None, checked, "bounded candidate schedule exhausted"
