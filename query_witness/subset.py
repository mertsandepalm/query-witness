"""A fail-closed AST gate. Accepted source is executed verbatim, never translated."""

from dataclasses import dataclass
from collections import Counter
import re

import sqlglot
from sqlglot import exp
from sqlglot.tokens import Tokenizer, TokenType


class Unsupported(ValueError):
    pass


def require(ok, message):
    if not ok:
        raise Unsupported(message)


def shape(node, cls, allowed):
    require(type(node) is cls, f"Expected {cls.__name__}; outside the supported subset")
    require(all(k in allowed or not v for k, v in node.args.items()),
            f"Unsupported option on {cls.__name__}")


def identifier(node):
    shape(node, exp.Identifier, {"this", "quoted"})
    require(not node.args.get("quoted") and re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", node.name),
            "Only simple unquoted identifiers are supported")
    return node.name.lower()


def token_counts(sql):
    ignored = {TokenType.COMMENT, TokenType.SEMICOLON, TokenType.BREAK}
    tokenizer = Tokenizer(dialect="duckdb")
    return Counter(token.token_type for token in tokenizer.tokenize(sql)
                   if token.token_type not in ignored)


def statement(sql):
    require(isinstance(sql, str) and 0 < len(sql) <= 16_384, "SQL must be 1–16384 characters")
    try:
        nodes = sqlglot.parse(sql, read="duckdb")
        require(len(nodes) == 1 and nodes[0] is not None, "Exactly one SQL statement is required")
        source_counts = token_counts(sql)
        rendered_counts = token_counts(nodes[0].sql(dialect="duckdb"))
        for token_type, count in source_counts.items():
            require(count <= rendered_counts[token_type],
                    f"SQLGlot's DuckDB rendering drops source token {token_type.name}")
    except sqlglot.errors.SqlglotError as exc:
        raise Unsupported(f"Cannot parse DuckDB SQL: {exc}") from exc
    except RecursionError as exc:
        raise Unsupported("SQL exceeds parser nesting capacity") from exc
    return nodes[0]


@dataclass(frozen=True)
class Input:
    schema: str
    query_a: str
    query_b: str
    table: str
    columns: tuple[str, ...]
    literals: tuple[int, ...] = ()
    nullable: tuple[bool, ...] = (True,)
    primary_key: str | None = None
    ordered: tuple[bool, bool] = (False, False)


def parse(schema, query_a, query_b):
    root = statement(schema)
    shape(root, exp.Create, {"this", "kind"})
    require(root.args.get("kind") == "TABLE", "Only CREATE TABLE is supported")
    definition = root.this
    shape(definition, exp.Schema, {"this", "expressions"})
    shape(definition.this, exp.Table, {"this"})
    table = identifier(definition.this.this)
    require(1 <= len(definition.expressions) <= 2, "Only one or two INTEGER columns are supported")
    columns = []
    nullable = []
    primary_key = None
    for col in definition.expressions:
        shape(col, exp.ColumnDef, {"this", "kind", "constraints"})
        column = identifier(col.this)
        require(column not in columns, "Duplicate column names are unsupported")
        columns.append(column)
        dtype = col.args.get("kind")
        shape(dtype, exp.DataType, {"this"})
        require(dtype.this == exp.DataType.Type.INT, "This slice supports only INTEGER (INT)")
        constraint_kinds = set()
        for constraint in col.args.get("constraints") or []:
            shape(constraint, exp.ColumnConstraint, {"kind"})
            kind = constraint.args.get("kind")
            require(type(kind) in (exp.NotNullColumnConstraint, exp.PrimaryKeyColumnConstraint),
                    "Only column-level NOT NULL and PRIMARY KEY constraints are supported")
            # Explicit NULL shares the NOT NULL node type with allow_null=True.
            # Reject all options, including PK ordering and explicit NULL.
            require(all(value is None or value == [] for value in kind.args.values()),
                    "Constraint options and explicit NULL are unsupported")
            require(type(kind) not in constraint_kinds, "Repeated constraints are unsupported")
            constraint_kinds.add(type(kind))
        if exp.PrimaryKeyColumnConstraint in constraint_kinds:
            require(primary_key is None, "Only one column-level PRIMARY KEY is supported")
            primary_key = column
            nullable.append(False)
        else:
            nullable.append(exp.NotNullColumnConstraint not in constraint_kinds)
    for token in Tokenizer(dialect="duckdb").tokenize(schema):
        if token.token_type == TokenType.INT:
            require(token.text.upper() in ("INT", "INTEGER"),
                    "This slice supports only INTEGER (INT)")

    def column_ref(node, aliases=None):
        if aliases:
            shape(node, exp.Column, {"this", "table"})
            require(identifier(node.args.get("table")) in aliases, "Unknown join alias")
        else:
            shape(node, exp.Column, {"this"})
        require(identifier(node.this) in columns, f"Unknown column; expected one of {columns}")

    def aggregate(node):
        if type(node) is exp.Count:
            shape(node, exp.Count, {"this", "big_int"})
            argument = node.this
            if type(argument) is exp.Star:
                shape(argument, exp.Star, set())
            elif type(argument) is exp.Distinct:
                shape(argument, exp.Distinct, {"expressions", "on"})
                require(not argument.args.get("on"), "DISTINCT ON is unsupported")
                require(len(argument.expressions) == 1, "COUNT DISTINCT requires one column")
                column_ref(argument.expressions[0])
            else:
                column_ref(argument)
        elif type(node) in (exp.Sum, exp.Min, exp.Max):
            shape(node, type(node), {"this"})
            column_ref(node.this)
        else:
            raise Unsupported("Unsupported aggregate")

    literals = set()

    def ungroup(node):
        while type(node) is exp.Paren:
            shape(node, exp.Paren, {"this"})
            node = node.this
        return node

    def operand(node, having=False, aliases=None):
        """Validate an operand, harvest signed literals, and report column or aggregate use."""
        node = ungroup(node)
        if type(node) is exp.Column:
            column_ref(node, aliases)
            if having:
                require(identifier(node.this) in group_keys, "HAVING columns must be GROUP BY keys")
            return True
        if having and type(node) in (exp.Count, exp.Sum, exp.Min, exp.Max):
            aggregate(node)
            return True
        if type(node) is exp.Null:
            shape(node, exp.Null, set())
            return False
        sign = 1
        if type(node) is exp.Neg:
            shape(node, exp.Neg, {"this"})
            sign, node = -1, node.this
        shape(node, exp.Literal, {"this", "is_string"})
        require(not node.is_string and node.this.isascii() and node.this.isdecimal(),
                "Comparison literals must be signed 32-bit integers or NULL")
        # Bound before int conversion, including arbitrarily many leading zeros.
        digits = node.this.lstrip("0") or "0"
        require(len(digits) <= 10, "Integer literal is outside signed 32-bit range")
        value = sign * int(digits)
        require(-(2**31) <= value < 2**31, "Integer literal is outside signed 32-bit range")
        literals.add(value)
        return False

    def predicate(root, having=False, aliases=None):
        pending = [root]
        while pending:
            node = ungroup(pending.pop())
            kind = type(node)
            if kind in (exp.And, exp.Or):
                shape(node, kind, {"this", "expression"})
                pending.extend((node.this, node.expression))
            elif kind is exp.Not:
                shape(node, exp.Not, {"this"})
                pending.append(node.this)
            elif kind is exp.Is:
                shape(node, exp.Is, {"this", "expression"})
                subject = ungroup(node.this)
                if having:
                    is_reference = operand(subject, having=True)
                    require(is_reference, "HAVING null checks require a group key or supported aggregate")
                else:
                    column_ref(subject, aliases)
                shape(node.expression, exp.Null, set())
            else:
                require(kind in (exp.EQ, exp.NEQ, exp.LT, exp.LTE, exp.GT, exp.GTE),
                        "Unsupported predicate")
                shape(node, kind, {"this", "expression"})
                left_reference = operand(node.this, having, aliases)
                right_reference = operand(node.expression, having, aliases)
                require(left_reference or right_reference, "Comparisons must reference a column or allowed aggregate")

    ordered = []
    for query in (query_a, query_b):
        select = statement(query)
        shape(select, exp.Select, {"expressions", "from_", "where", "distinct", "order", "group", "having", "joins"})
        source = select.args.get("from_")
        shape(source, exp.From, {"this"})
        joins = select.args.get("joins") or []
        aliases = set()
        if joins:
            require(len(joins) == 1, "Only one inner self-join is supported")
            join = joins[0]
            shape(join, exp.Join, {"this", "on", "kind"})
            require(join.args.get("kind") in (None, "INNER"), "Only INNER JOIN is supported")
            require(join.args.get("on") is not None, "JOIN requires ON")
            for clause in ("distinct", "order", "group", "having"):
                require(not select.args.get(clause), f"{clause.upper()} is unsupported on join queries")
            for target in (source.this, join.this):
                shape(target, exp.Table, {"this", "alias"})
                require(identifier(target.this) == table, f"Expected table {table}")
                alias = target.args.get("alias")
                shape(alias, exp.TableAlias, {"this"})
                name = identifier(alias.this)
                require(name not in aliases, "Join aliases must be distinct")
                aliases.add(name)
            predicate(join.args["on"], aliases=aliases)
        else:
            shape(source.this, exp.Table, {"this"})
            require(identifier(source.this.this) == table, f"Expected table {table}")
        distinct = select.args.get("distinct")
        if distinct is not None:
            shape(distinct, exp.Distinct, {"on"})
            require(not distinct.args.get("on"), "DISTINCT ON is unsupported")
        group = select.args.get("group")
        group_keys = set()
        if group is not None:
            shape(group, exp.Group, {"expressions"})
            require(1 <= len(group.expressions) <= 2, "GROUP BY requires one or two columns")
            for key in group.expressions:
                column_ref(key)
                name = identifier(key.this)
                require(name not in group_keys, "Duplicate GROUP BY keys are unsupported")
                group_keys.add(name)
            require(distinct is None, "SELECT DISTINCT on grouped queries is unsupported")
            require(select.args.get("order") is None, "ORDER BY on grouped queries is unsupported")
        require(1 <= len(select.expressions) <= 16, "Expected 1–16 projections")
        aggregate_count = 0
        for projection in select.expressions:
            if type(projection) is exp.Alias:
                shape(projection, exp.Alias, {"this", "alias"})
                identifier(projection.args["alias"])
                projection = projection.this
            if type(projection) in (exp.Count, exp.Sum, exp.Min, exp.Max):
                require(not joins, "Aggregates are unsupported on join queries")
                aggregate(projection)
                aggregate_count += 1
            else:
                column_ref(projection, aliases)
                if group is not None:
                    require(identifier(projection.this) in group_keys,
                            "Projected columns must be GROUP BY keys")
        if group is None and aggregate_count:
            require(aggregate_count == len(select.expressions), "Cannot mix columns and aggregates")
            require(distinct is None, "SELECT DISTINCT on aggregate queries is unsupported")
            require(select.args.get("order") is None, "ORDER BY on aggregate queries is unsupported")
        where = select.args.get("where")
        if where is not None:
            shape(where, exp.Where, {"this"})
            predicate(where.this, aliases=aliases)
        having = select.args.get("having")
        if having is not None:
            require(group is not None, "HAVING requires GROUP BY")
            shape(having, exp.Having, {"this"})
            predicate(having.this, having=True)
        order = select.args.get("order")
        ordered.append(order is not None)
        if order is not None:
            shape(order, exp.Order, {"expressions"})
            require(len(order.expressions) == 1, "Only one ORDER BY column is supported")
            key = order.expressions[0]
            shape(key, exp.Ordered, {"this", "desc", "nulls_first"})
            column_ref(key.this)
            require(key.args.get("desc") in (None, False, True), "Unsupported ORDER BY direction")
            require(not key.args.get("nulls_first"), "NULLS FIRST is unsupported")
    return Input(schema, query_a, query_b, table, tuple(columns), tuple(sorted(literals)),
                 nullable=tuple(nullable), primary_key=primary_key, ordered=tuple(ordered))
