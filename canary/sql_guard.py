"""Allow only single read-only SELECTs against the canary SQL editor tables.

The editor connects as the database owner, so Postgres itself will not refuse
a write or a catalog read. This check runs before any user SQL is executed.
"""

from __future__ import annotations

ALLOWED_TABLES = frozenset({
    "audit_logs",
    "sql_saved_queries",
    "sql_query_audit",
})
ALLOWED_SCHEMAS = frozenset({"public"})

ALLOWED_FUNCTIONS = frozenset({
    "count", "sum", "avg", "min", "max", "round", "coalesce", "nullif",
    "date_trunc", "date_part", "extract", "lower", "upper", "left", "right",
    "length", "char_length", "octet_length", "substring", "trim", "btrim",
    "ltrim", "rtrim", "concat", "concat_ws", "replace", "position", "strpos",
    "abs", "ceil", "ceiling", "floor", "greatest", "least", "now",
    "to_char", "to_timestamp", "to_number", "age", "cast", "array_agg",
    "string_agg", "bool_and", "bool_or", "jsonb_typeof", "row_number",
    "rank", "dense_rank", "lag", "lead", "percentile_cont", "percentile_disc",
    "stddev", "stddev_pop", "stddev_samp", "variance", "var_pop", "var_samp",
    "width_bucket", "ntile", "date_bin", "corr",
})

# Function-style syntax whose parentheses do not introduce a table.
_CALL_WORDS = ALLOWED_FUNCTIONS | frozenset({
    "over", "filter", "within", "cast", "extract", "substring", "trim",
    "overlay", "position",
})

# These functions use a FROM keyword for an expression, not a table.
_EXPR_FROM_FUNCS = frozenset({
    "extract", "substring", "trim", "overlay", "position",
})

_FORBIDDEN_WORDS = frozenset({
    "insert", "update", "delete", "drop", "alter", "create", "truncate",
    "grant", "revoke", "copy", "call", "do", "execute", "vacuum", "analyse",
    "analyze", "comment", "listen", "notify", "lock", "set", "reset", "show",
    "begin", "commit", "rollback", "prepare", "deallocate", "discard",
    "reindex", "cluster", "refresh", "merge", "import", "export", "security",
    "into", "returning", "explain", "grant", "owner", "password", "role",
    "database", "extension", "procedure", "trigger", "policy", "tablespace",
    "publication", "subscription", "checkpoint", "load", "outfile", "infile",
    "pg_read_file", "pg_read_binary_file", "lo_import", "lo_export", "dblink",
})

_CLAUSE_END = frozenset({
    "where", "group", "order", "limit", "offset", "fetch", "union", "except",
    "intersect", "window", "having", "for", "on", "using", "join", "inner",
    "left", "right", "full", "cross", "natural",
})

_ALIAS_SKIP = _CLAUSE_END | frozenset({
    "as", "select", "from", "with", "and", "or", "not", "null", "is", "in",
    "like", "ilike", "between", "case", "when", "then", "else", "end",
    "distinct", "all", "asc", "desc", "by", "lateral", "only", "true", "false",
})


class SqlGuardError(Exception):
    pass


def guard_readonly_sql(query: str) -> None:
    """Raise SqlGuardError when the statement is not an allowed read."""
    if query is None or not str(query).strip():
        raise SqlGuardError("Query cannot be empty")
    if any(ord(ch) > 127 for ch in query):
        raise SqlGuardError("Only read-only SELECT statements are allowed")

    masked = _mask_literals(query)
    tokens = _tokenize(masked)
    statements = _split_statements(tokens)
    if len(statements) != 1:
        raise SqlGuardError("Only one SELECT statement is allowed")
    tokens = statements[0]
    if not tokens or tokens[0] != ("word", "select") and tokens[0] != ("word", "with"):
        raise SqlGuardError("Only read-only SELECT statements are allowed")

    for kind, value in tokens:
        if kind == "word" and (value in _FORBIDDEN_WORDS or value.startswith("pg_") or value.startswith("lo_")):
            raise SqlGuardError("Only read-only SELECT statements are allowed")

    _reject_unknown_functions(tokens)

    ctes = _cte_names(tokens)
    relations = _relations(tokens)
    if not relations:
        raise SqlGuardError("Query must read one of the application tables")

    for schema, name in relations:
        if name in ctes:
            continue
        if schema not in (None, "") and schema not in ALLOWED_SCHEMAS:
            raise SqlGuardError("Query must read one of the application tables")
        if name not in ALLOWED_TABLES:
            raise SqlGuardError("Query must read one of the application tables")


def _mask_literals(sql: str) -> str:
    out: list[str] = []
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        if ch == "-" and i + 1 < n and sql[i + 1] == "-":
            i += 2
            while i < n and sql[i] not in "\n\r":
                i += 1
            out.append(" ")
            continue
        if ch == "/" and i + 1 < n and sql[i + 1] == "*":
            end = sql.find("*/", i + 2)
            if end < 0:
                raise SqlGuardError("Only read-only SELECT statements are allowed")
            i = end + 2
            out.append(" ")
            continue
        if ch == "'":
            i += 1
            while i < n:
                if sql[i] == "'" and i + 1 < n and sql[i + 1] == "'":
                    i += 2
                    continue
                if sql[i] == "'":
                    i += 1
                    break
                i += 1
            else:
                raise SqlGuardError("Only read-only SELECT statements are allowed")
            out.append(" ")
            continue
        if ch == "$":
            tag_end = _dollar_tag_end(sql, i)
            if tag_end is not None:
                tag = sql[i:tag_end]
                close = sql.find(tag, tag_end)
                if close < 0:
                    raise SqlGuardError("Only read-only SELECT statements are allowed")
                i = close + len(tag)
                out.append(" ")
                continue
        if ch == '"':
            j = i + 1
            while j < n:
                if sql[j] == '"' and j + 1 < n and sql[j + 1] == '"':
                    j += 2
                    continue
                if sql[j] == '"':
                    j += 1
                    break
                j += 1
            else:
                raise SqlGuardError("Only read-only SELECT statements are allowed")
            out.append(sql[i:j])
            i = j
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _dollar_tag_end(sql: str, i: int) -> int | None:
    if not sql.startswith("$", i):
        return None
    j = i + 1
    while j < len(sql) and (sql[j].isalnum() or sql[j] == "_"):
        j += 1
    if j < len(sql) and sql[j] == "$":
        return j + 1
    return None


def _tokenize(masked: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    i = 0
    n = len(masked)
    while i < n:
        ch = masked[i]
        if ch.isspace():
            i += 1
            continue
        if ch == '"':
            j = i + 1
            buf = []
            while j < n:
                if masked[j] == '"' and j + 1 < n and masked[j + 1] == '"':
                    buf.append('"')
                    j += 2
                    continue
                if masked[j] == '"':
                    j += 1
                    break
                buf.append(masked[j])
                j += 1
            tokens.append(("word", "".join(buf).lower()))
            i = j
            continue
        if ch.isalpha() or ch == "_":
            j = i + 1
            while j < n and (masked[j].isalnum() or masked[j] == "_"):
                j += 1
            tokens.append(("word", masked[i:j].lower()))
            i = j
            continue
        if ch.isdigit():
            j = i + 1
            while j < n and (masked[j].isdigit() or masked[j] == "."):
                j += 1
            tokens.append(("num", masked[i:j]))
            i = j
            continue
        if ch in "(),.;*":
            tokens.append(("punct", ch))
            i += 1
            continue
        if ch in "+-*/%<>=!|&^~:":
            j = i + 1
            while j < n and masked[j] in "+-*/%<>=!|&:":
                j += 1
            tokens.append(("op", masked[i:j]))
            i = j
            continue
        raise SqlGuardError("Only read-only SELECT statements are allowed")
    return tokens


def _split_statements(tokens: list[tuple[str, str]]) -> list[list[tuple[str, str]]]:
    parts: list[list[tuple[str, str]]] = []
    current: list[tuple[str, str]] = []
    for token in tokens:
        if token == ("punct", ";"):
            if current:
                parts.append(current)
                current = []
            continue
        current.append(token)
    if current:
        parts.append(current)
    return parts


def _reject_unknown_functions(tokens: list[tuple[str, str]]) -> None:
    for i, token in enumerate(tokens[:-1]):
        if token[0] != "word" or tokens[i + 1] != ("punct", "("):
            continue
        name = token[1]
        if name in _CALL_WORDS or name in {
            "as", "and", "or", "not", "in", "exists", "any", "all", "some",
            "array", "row", "from", "where", "join", "on", "select", "with",
            "by", "when", "then", "else", "end", "case", "distinct", "is",
            "null", "like", "ilike", "between", "true", "false", "asc", "desc",
            "recursive", "only", "lateral", "using", "having", "group", "order",
            "limit", "offset", "window", "union", "except", "intersect",
            "inner", "left", "right", "full", "cross", "natural", "filter",
            "over", "within",
        }:
            continue
        raise SqlGuardError("That function is not allowed in the SQL editor")


def _cte_names(tokens: list[tuple[str, str]]) -> set[str]:
    names: set[str] = set()
    if not tokens or tokens[0] != ("word", "with"):
        return names
    i = 1
    if i < len(tokens) and tokens[i] == ("word", "recursive"):
        i += 1
    while i < len(tokens):
        if tokens[i][0] != "word":
            break
        name = tokens[i][1]
        i += 1
        if i < len(tokens) and tokens[i] == ("punct", "("):
            i = _skip_parens(tokens, i)
        if i + 1 < len(tokens) and tokens[i] == ("word", "as") and tokens[i + 1] == ("punct", "("):
            names.add(name)
            i = _skip_parens(tokens, i + 1)
        else:
            break
        if i < len(tokens) and tokens[i] == ("punct", ","):
            i += 1
            continue
        break
    return names


def _relations(tokens: list[tuple[str, str]]) -> list[tuple[str | None, str]]:
    relations: list[tuple[str | None, str]] = []
    ctx = ["query"]
    i = 0
    while i < len(tokens):
        kind, value = tokens[i]
        if kind == "punct" and value == "(":
            nxt = _next_word(tokens, i + 1)
            prev = _prev_word(tokens, i)
            if nxt in ("select", "with"):
                ctx.append("query")
            elif prev in _EXPR_FROM_FUNCS:
                ctx.append("expr")
            elif prev in _CALL_WORDS:
                ctx.append("call")
            else:
                ctx.append("query")
            i += 1
            continue
        if kind == "punct" and value == ")":
            if len(ctx) > 1:
                ctx.pop()
            i += 1
            continue
        if kind == "word" and value in ("from", "join") and ctx[-1] == "query":
            i = _consume_table_list(tokens, i + 1, relations)
            continue
        i += 1
    return relations


def _consume_table_list(tokens, i, relations) -> int:
    while i < len(tokens):
        while i < len(tokens) and tokens[i] in (("word", "only"), ("word", "lateral")):
            i += 1
        if i >= len(tokens):
            break
        if tokens[i] == ("punct", "("):
            return i
        schema, name, i = _qualified_name(tokens, i)
        if not name:
            break
        relations.append((schema, name))
        i = _skip_alias(tokens, i)
        if i < len(tokens) and tokens[i] == ("punct", ","):
            i += 1
            continue
        break
    return i


def _qualified_name(tokens, i):
    if i >= len(tokens) or tokens[i][0] != "word":
        return None, None, i
    first = tokens[i][1]
    i += 1
    if i + 1 < len(tokens) and tokens[i] == ("punct", ".") and tokens[i + 1][0] == "word":
        second = tokens[i + 1][1]
        i += 2
        if i + 1 < len(tokens) and tokens[i] == ("punct", ".") and tokens[i + 1][0] == "word":
            # catalog.schema.table is never an application table
            return "blocked", tokens[i + 1][1], i + 2
        return first, second, i
    return None, first, i


def _skip_alias(tokens, i) -> int:
    if i < len(tokens) and tokens[i] == ("word", "as"):
        i += 1
        if i < len(tokens) and tokens[i][0] == "word":
            i += 1
    elif i < len(tokens) and tokens[i][0] == "word" and tokens[i][1] not in _ALIAS_SKIP:
        i += 1
    if i < len(tokens) and tokens[i] == ("punct", "("):
        i = _skip_parens(tokens, i)
    return i


def _skip_parens(tokens, i) -> int:
    if i >= len(tokens) or tokens[i] != ("punct", "("):
        return i
    depth = 0
    while i < len(tokens):
        if tokens[i] == ("punct", "("):
            depth += 1
        elif tokens[i] == ("punct", ")"):
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return i


def _next_word(tokens, i):
    while i < len(tokens):
        if tokens[i][0] == "word":
            return tokens[i][1]
        if tokens[i][0] != "op":
            return None
        i += 1
    return None


def _prev_word(tokens, i):
    j = i - 1
    while j >= 0:
        if tokens[j][0] == "word":
            return tokens[j][1]
        j -= 1
    return None
