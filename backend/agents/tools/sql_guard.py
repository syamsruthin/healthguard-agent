"""
Static validation of LLM-generated SQL (defence layer 1 of 4).

Layers: (1) this AST guard → (2) read-only `hg_agent_ro` role with SELECT
grants on `insurance` only and `set_config` revoked → (3) READ ONLY
transaction + statement timeout → (4) Row-Level Security on member tables.
"""

from __future__ import annotations

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from backend.core.data_sources import SQL_TABLE_NAMES


class SQLGuardError(ValueError):
    pass


# Typed sqlglot function classes are standard SQL functions; these are not allowed anyway.
_DENY_TYPED = {"ReadCSV", "ReadParquet", "Explode", "Unnest"}

# Functions sqlglot does not know (exp.Anonymous) must be explicitly allowed.
_ALLOWED_ANONYMOUS = {
    "age",
    "make_date",
    "make_interval",
    "date_part",
    "to_date",
    "to_number",
    "justify_interval",
    "concat_ws",
    "btrim",
    "ltrim",
    "rtrim",
    "lpad",
    "rpad",
    "strpos",
    "left",
    "right",
    "array_length",
    "cardinality",
    "string_to_array",
    "trunc",
    "mod",
    "power",
    "sqrt",
    "width_bucket",
    "bool_or",
    "bool_and",
    "every",
    "percentile_disc",
    "mode",
    "stddev",
    "variance",
    "date",
    "initcap",
    "position",
    "to_char",
    "round",
}

_FORBIDDEN_NODES = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.Command,
    exp.Merge,
    exp.Into,
    exp.Lock,
    exp.Copy,
    exp.Set,
    exp.Transaction,
    exp.Commit,
    exp.Rollback,
    exp.Grant,
    exp.Use,
    exp.TruncateTable,
)


def _function_name(node: exp.Func) -> str:
    if isinstance(node, exp.Anonymous):
        return str(node.name).lower()
    return node.sql_name().lower()


def validate_sql(sql: str, *, max_rows: int) -> str:
    """Return SQL safe to execute (original text, row-capped) or raise SQLGuardError."""
    cleaned = sql.strip().rstrip(";").strip()
    if not cleaned:
        raise SQLGuardError("empty SQL")
    try:
        statements = [s for s in sqlglot.parse(cleaned, read="postgres") if s is not None]
    except ParseError as exc:
        raise SQLGuardError(f"SQL could not be parsed: {str(exc).splitlines()[0][:200]}") from exc
    if len(statements) != 1:
        raise SQLGuardError("exactly one statement is allowed")
    root = statements[0]
    if not isinstance(root, exp.Query):
        raise SQLGuardError(f"only SELECT queries are allowed (got {type(root).__name__})")

    cte_names = {cte.alias_or_name.lower() for cte in root.find_all(exp.CTE)}
    for node in root.walk():
        if isinstance(node, _FORBIDDEN_NODES):
            raise SQLGuardError(f"forbidden SQL construct: {type(node).__name__}")
        if isinstance(node, exp.Table):
            name = (node.name or "").lower()
            if not name:  # e.g. unnest(...) AS t — table-valued functions are not allowed
                raise SQLGuardError("table-valued expressions are not allowed")
            schema = (node.db or "").lower()
            if node.catalog:
                raise SQLGuardError("cross-database references are not allowed")
            if name in cte_names and not schema:
                continue
            if schema not in ("", "insurance") or name not in SQL_TABLE_NAMES:
                raise SQLGuardError(f"table not allowed: {'.'.join(p for p in (schema, name) if p)}")
        if isinstance(node, exp.Func):
            if type(node).__name__ in _DENY_TYPED:
                raise SQLGuardError(f"function not allowed: {_function_name(node)}")
            if isinstance(node, exp.Anonymous) and _function_name(node) not in _ALLOWED_ANONYMOUS:
                raise SQLGuardError(f"function not allowed: {_function_name(node)}")

    limit = root.args.get("limit")
    limit_value = None
    if isinstance(limit, exp.Limit) and isinstance(limit.expression, exp.Literal):
        try:
            limit_value = int(limit.expression.this)
        except ValueError:
            limit_value = None
    if limit_value is not None and limit_value <= max_rows:
        return cleaned
    # Cap rows without rewriting the validated query text.
    return f"SELECT * FROM (\n{cleaned}\n) AS hg_capped LIMIT {max_rows}"
