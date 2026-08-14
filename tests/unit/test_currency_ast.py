"""Unit tests for the Decimal-only currency expression AST (arc42 TC-06).

``auspex.currency.ast.evaluate`` powers the configured fee-schedule formulas in
``config/fees.yaml``. These tests assert that only Decimal-safe arithmetic is
accepted and that unsafe constructs (float literals, arbitrary calls,
attribute access, undefined names) are rejected.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from auspex.currency.ast import CurrencyExpressionError, evaluate, parse


class TestBasicArithmetic:
    def test_addition(self):
        assert evaluate('"1" + "2"') == Decimal("3")

    def test_subtraction(self):
        assert evaluate('"5" - "2"') == Decimal("3")

    def test_multiplication_with_variable(self):
        result = evaluate('notional_usd * "0.0010"', {"notional_usd": Decimal("10000")})
        assert result == Decimal("10.0000")

    def test_division(self):
        assert evaluate('"10" / "4"') == Decimal("2.5")

    def test_parentheses_and_precedence(self):
        assert evaluate('("2" + "3") * "4"') == Decimal("20")

    def test_unary_minus(self):
        assert evaluate('-"5"') == Decimal("-5")

    def test_integer_literals_allowed(self):
        assert evaluate("2 + 3") == Decimal(5)


class TestMinMax:
    def test_min(self):
        assert evaluate('min("3", "7")') == Decimal("3")

    def test_max(self):
        assert evaluate('max("3", "7")') == Decimal("7")

    def test_nested_min_max(self):
        formula = 'min(max(notional_usd * "0.0010", "10"), "100")'
        assert evaluate(formula, {"notional_usd": Decimal("5000")}) == Decimal("10")
        assert evaluate(formula, {"notional_usd": Decimal("50000")}) == Decimal("50.0000")
        assert evaluate(formula, {"notional_usd": Decimal("500000")}) == Decimal("100")


class TestRejectsUnsafeConstructs:
    def test_rejects_unquoted_float_literal(self):
        with pytest.raises(CurrencyExpressionError):
            evaluate("notional_usd * 0.0010", {"notional_usd": Decimal("5000")})

    def test_rejects_boolean_literal(self):
        with pytest.raises(CurrencyExpressionError):
            evaluate("True")

    def test_rejects_arbitrary_function_call(self):
        with pytest.raises(CurrencyExpressionError):
            evaluate('__import__("os")')

    def test_rejects_attribute_access(self):
        with pytest.raises(CurrencyExpressionError):
            evaluate("notional_usd.bit_length()", {"notional_usd": Decimal("5000")})

    def test_rejects_undefined_variable(self):
        with pytest.raises(CurrencyExpressionError):
            evaluate("undefined_var + 1")

    def test_rejects_invalid_syntax(self):
        with pytest.raises(CurrencyExpressionError):
            evaluate("1 +")

    def test_rejects_comparison_operators(self):
        with pytest.raises(CurrencyExpressionError):
            evaluate('"1" < "2"')

    def test_rejects_list_literal(self):
        with pytest.raises(CurrencyExpressionError):
            evaluate("[1, 2, 3]")

    def test_parse_validates_without_evaluating(self):
        # parse() alone should raise for unsafe expressions too
        with pytest.raises(CurrencyExpressionError):
            parse("os.system('echo hi')")


class TestDeterminism:
    def test_same_expression_same_result(self):
        formula = 'min(max(notional_usd * "0.0010", "10"), "100")'
        variables = {"notional_usd": Decimal("25000")}
        assert evaluate(formula, variables) == evaluate(formula, variables)

    def test_result_is_always_decimal(self):
        result = evaluate('"1" + "2"')
        assert isinstance(result, Decimal)
