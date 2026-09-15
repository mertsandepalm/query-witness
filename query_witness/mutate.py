"""Named in-subset rewrite mistakes. Execution still uses each query's source."""

from sqlglot import exp

from .subset import Unsupported, identifier, parse, statement


def ungroup(node):
    while type(node) is exp.Paren:
        node = node.this
    return node


def render(tree):
    return tree.sql(dialect="duckdb")


def accepted_pair(schema, query_a, query_b):
    try:
        parse(schema, query_a, query_b)
    except Unsupported:
        return False
    return query_a.strip() != query_b.strip()


def proposals(schema, query_a):
    """Yield (name, query_b) for mechanical rewrites that stay in the subset."""
    parse(schema, query_a, query_a)
    tree = statement(query_a)
    seen = set()

    def emit(name, mutated):
        query_b = mutated if type(mutated) is str else render(mutated)
        if query_b in seen or not accepted_pair(schema, query_a, query_b):
            return
        seen.add(query_b)
        yield name, query_b

    yield from _count_star_to_column(schema, query_a, tree, emit)
    yield from _drop_distinct(schema, query_a, tree, emit)
    yield from _flip_comparisons(schema, query_a, tree, emit)
    yield from _push_sum_filter(schema, query_a, tree, emit)
    yield from _self_join_on_column(schema, query_a, tree, emit)


def _count_star_to_column(schema, query_a, tree, emit):
    meta = parse(schema, query_a, query_a)
    stars = [
        node for node in tree.find_all(exp.Count) if type(ungroup(node.this)) is exp.Star
    ]
    if not stars:
        return
    for column in meta.columns:
        mutated = tree.copy()
        replaced = False
        for node in mutated.find_all(exp.Count):
            if type(ungroup(node.this)) is exp.Star:
                node.set("this", exp.column(column))
                replaced = True
                break
        if replaced:
            yield from emit(f"count-star-to-column-{column}", mutated)


def _drop_distinct(schema, query_a, tree, emit):
    if tree.args.get("distinct") is None:
        return
    mutated = tree.copy()
    mutated.set("distinct", None)
    yield from emit("drop-distinct", mutated)


_FLIPS = {
    exp.GT: ("gt-to-gte", exp.GTE),
    exp.GTE: ("gte-to-gt", exp.GT),
    exp.LT: ("lt-to-lte", exp.LTE),
    exp.LTE: ("lte-to-lt", exp.LT),
}


def _flip_comparisons(schema, query_a, tree, emit):
    targets = [node for node in tree.find_all(*_FLIPS) if type(node) in _FLIPS]
    for index, original in enumerate(targets):
        name_prefix, flipped = _FLIPS[type(original)]
        mutated = tree.copy()
        current = [node for node in mutated.find_all(*_FLIPS) if type(node) in _FLIPS][index]
        current.replace(
            flipped(this=current.this.copy(), expression=current.expression.copy()),
        )
        yield from emit(f"{name_prefix}-{index + 1}", mutated)


def _sum_column_and_literal(node):
    if type(node) not in (exp.GT, exp.GTE, exp.LT, exp.LTE):
        return None
    sides = ((ungroup(node.this), ungroup(node.expression)),
             (ungroup(node.expression), ungroup(node.this)))
    for aggregate, other in sides:
        if type(aggregate) is exp.Sum and type(other) is exp.Literal:
            column = ungroup(aggregate.this)
            if type(column) is exp.Column:
                return type(node), identifier(column.this), other
    return None


def _push_sum_filter(schema, query_a, tree, emit):
    having = tree.args.get("having")
    if having is None or tree.args.get("group") is None:
        return
    pending = [ungroup(having.this)]
    found = []
    while pending:
        node = ungroup(pending.pop())
        if type(node) is exp.And:
            pending.extend((node.this, node.expression))
            continue
        if type(node) is exp.Or:
            return
        match = _sum_column_and_literal(node)
        if match is not None:
            found.append(match)
    for index, (operator, column, literal) in enumerate(found):
        mutated = tree.copy()
        predicate = operator(this=exp.column(column), expression=literal.copy())
        where = mutated.args.get("where")
        if where is None:
            mutated.set("where", exp.Where(this=predicate))
        else:
            where.set("this", exp.And(this=where.this.copy(), expression=predicate))
        yield from emit(f"push-sum-filter-to-where-{index + 1}", mutated)


def _qualify(tree, alias):
    for column in tree.find_all(exp.Column):
        if column.args.get("table") is None:
            column.set("table", exp.to_identifier(alias))


def _self_join_on_column(schema, query_a, tree, emit):
    if tree.args.get("joins") or tree.args.get("group") is not None:
        return
    if tree.args.get("distinct") is not None or tree.args.get("order") is not None:
        return
    if tree.args.get("having") is not None:
        return
    if any(tree.find_all(exp.Count, exp.Sum, exp.Min, exp.Max)):
        return
    meta = parse(schema, query_a, query_a)
    table = meta.table
    projections = []
    for projection in tree.expressions:
        if type(projection) is exp.Alias:
            column = identifier(projection.this.this)
            alias = identifier(projection.args["alias"])
            projections.append(f"a.{column} AS {alias}")
        else:
            projections.append(f"a.{identifier(projection.this)}")
    where_sql = ""
    if tree.args.get("where") is not None:
        predicate = tree.args["where"].this.copy()
        _qualify(predicate, "a")
        where_sql = " WHERE " + render(predicate)
    select_sql = ", ".join(projections)
    for column in meta.columns:
        query_b = (
            f"SELECT {select_sql} FROM {table} AS a JOIN {table} AS b "
            f"ON a.{column} = b.{column}{where_sql}"
        )
        yield from emit(f"self-join-on-{column}", query_b)
