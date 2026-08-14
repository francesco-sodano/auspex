"""A tiny, safe arithmetic AST evaluator for Decimal-only monetary formulas.

Fee/cost formulas in ``config/fees.yaml`` are plain arithmetic expression
strings (e.g. ``"min(max(notional_usd * 0.0010, 10), 100)"``). Rather than
``eval()`` — which would happily accept a float literal or arbitrary code —
this module parses the expression with :mod:`ast`, walks a small whitelist of
node types, and evaluates every literal and every intermediate result as a
:class:`decimal.Decimal`. Any node outside the whitelist (attribute access,
calls other than ``min``/``max``, comprehensions, names not in the supplied
context, ...) raises :class:`CurrencyExpressionError` rather than silently
doing something unsafe.
"""

from __future__ import annotations

import ast as _pyast
from decimal import Decimal

_ALLOWED_FUNCTIONS = {"min", "max"}
_ALLOWED_BINOPS = (_pyast.Add, _pyast.Sub, _pyast.Mult, _pyast.Div)
_ALLOWED_UNARYOPS = (_pyast.UAdd, _pyast.USub)


class CurrencyExpressionError(ValueError):
    """Raised when a formula string contains a disallowed construct."""


def _num(node: _pyast.AST) -> bool:
    return isinstance(node, _pyast.Constant) and isinstance(node.value, int | float | str)


def parse(expression: str) -> _pyast.Expression:
    try:
        tree = _pyast.parse(expression, mode="eval")
    except SyntaxError as exc:  # pragma: no cover - defensive
        raise CurrencyExpressionError(f"invalid expression syntax: {expression!r}") from exc
    _validate(tree.body)
    return tree


def _validate(node: _pyast.AST) -> None:
    if isinstance(node, _pyast.Expression):
        _validate(node.body)
    elif isinstance(node, _pyast.BinOp):
        if not isinstance(node.op, _ALLOWED_BINOPS):
            raise CurrencyExpressionError(f"operator {type(node.op).__name__} is not allowed")
        _validate(node.left)
        _validate(node.right)
    elif isinstance(node, _pyast.UnaryOp):
        if not isinstance(node.op, _ALLOWED_UNARYOPS):
            raise CurrencyExpressionError(f"unary operator {type(node.op).__name__} is not allowed")
        _validate(node.operand)
    elif isinstance(node, _pyast.Call):
        if not isinstance(node.func, _pyast.Name) or node.func.id not in _ALLOWED_FUNCTIONS:
            raise CurrencyExpressionError("only min(...)/max(...) calls are allowed")
        if node.keywords:
            raise CurrencyExpressionError("keyword arguments are not allowed")
        for arg in node.args:
            _validate(arg)
    elif isinstance(node, _pyast.Name):
        return
    elif _num(node):
        if isinstance(node.value, float):
            raise CurrencyExpressionError(
                "float literals are not allowed in currency expressions — use a quoted decimal string or an integer"
            )
        if isinstance(node.value, str):
            # a quoted decimal literal, e.g. "0.0010" — validated at eval time
            return
    else:
        raise CurrencyExpressionError(f"node type {type(node).__name__} is not allowed")


def evaluate(expression: str, variables: dict[str, Decimal] | None = None) -> Decimal:
    """Evaluate a whitelisted arithmetic expression to a Decimal.

    ``variables`` supplies named values (already Decimal); any bare numeric
    literal in the expression is parsed via ``str()`` -> Decimal so no binary
    float ever participates in the arithmetic.
    """

    variables = variables or {}
    tree = parse(expression)
    return _eval_node(tree.body, variables)


def _eval_node(node: _pyast.AST, variables: dict[str, Decimal]) -> Decimal:
    if isinstance(node, _pyast.Constant):
        if isinstance(node.value, bool):  # bool is an int subclass; reject explicitly
            raise CurrencyExpressionError("boolean literals are not allowed")
        if isinstance(node.value, int):
            return Decimal(node.value)
        if isinstance(node.value, str):
            try:
                return Decimal(node.value)
            except Exception as exc:  # pragma: no cover - defensive
                raise CurrencyExpressionError(f"not a decimal literal: {node.value!r}") from exc
        raise CurrencyExpressionError("float literals are not allowed in currency expressions")
    if isinstance(node, _pyast.Name):
        if node.id not in variables:
            raise CurrencyExpressionError(f"undefined variable: {node.id}")
        return variables[node.id]
    if isinstance(node, _pyast.BinOp):
        left = _eval_node(node.left, variables)
        right = _eval_node(node.right, variables)
        if isinstance(node.op, _pyast.Add):
            return left + right
        if isinstance(node.op, _pyast.Sub):
            return left - right
        if isinstance(node.op, _pyast.Mult):
            return left * right
        if isinstance(node.op, _pyast.Div):
            return left / right
        raise CurrencyExpressionError("unsupported binary operator")  # pragma: no cover
    if isinstance(node, _pyast.UnaryOp):
        value = _eval_node(node.operand, variables)
        return value if isinstance(node.op, _pyast.UAdd) else -value
    if isinstance(node, _pyast.Call):
        args = [_eval_node(a, variables) for a in node.args]
        fn = node.func.id  # validated in _validate
        return min(args) if fn == "min" else max(args)
    raise CurrencyExpressionError(f"unsupported node: {type(node).__name__}")  # pragma: no cover
