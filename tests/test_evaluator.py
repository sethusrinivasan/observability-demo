"""
test_evaluator.py — Unit tests for the expression evaluator.

Covers every layer of the pipeline independently:
  TestTokeniser       – tokenise()
  TestToRPN           – to_rpn()
  TestEvaluateRPN     – evaluate_rpn()
  TestEvaluate        – evaluate() end-to-end (the public API)
  TestEvalEndpoint    – Flask /eval route (HTTP layer)
"""
import math
import pytest
from evaluator import (
    NUMBER, OPERATOR, LEFT_PAREN, RIGHT_PAREN,
    tokenise, to_rpn, evaluate_rpn, evaluate,
)


# ===========================================================================
# Helpers
# ===========================================================================

def _nums(tokens):
    """Extract just the numeric values from a token list."""
    return [v for t, v in tokens if t == NUMBER]

def _ops(tokens):
    """Extract just the operator symbols from a token list."""
    return [v for t, v in tokens if t == OPERATOR]


# ===========================================================================
# TestTokeniser
# ===========================================================================

class TestTokeniser:
    # --- basic numbers ---
    def test_single_integer(self):
        assert tokenise("42") == [(NUMBER, "42")]

    def test_decimal_number(self):
        assert tokenise("3.14") == [(NUMBER, "3.14")]

    def test_multi_digit(self):
        assert tokenise("100") == [(NUMBER, "100")]

    # --- operators ---
    def test_all_operators(self):
        tokens = tokenise("1+2-3*4/5^6")
        assert _ops(tokens) == ["+", "-", "*", "/", "^"]

    # --- parentheses ---
    def test_parens_tokenised(self):
        tokens = tokenise("(1+2)")
        types = [t for t, _ in tokens]
        assert types == [LEFT_PAREN, NUMBER, OPERATOR, NUMBER, RIGHT_PAREN]

    # --- whitespace ---
    def test_whitespace_ignored(self):
        assert tokenise("  3  +  4  ") == tokenise("3+4")

    # --- unary minus ---
    def test_unary_minus_at_start(self):
        tokens = tokenise("-5")
        assert tokens == [(NUMBER, "-5")]

    def test_unary_minus_after_operator(self):
        tokens = tokenise("3 * -2")
        assert tokens == [(NUMBER, "3"), (OPERATOR, "*"), (NUMBER, "-2")]

    def test_unary_minus_after_left_paren(self):
        tokens = tokenise("(-7)")
        assert tokens == [
            (LEFT_PAREN, "("), (NUMBER, "-7"), (RIGHT_PAREN, ")")
        ]

    def test_unary_minus_decimal(self):
        tokens = tokenise("-3.5 + 1")
        assert tokens[0] == (NUMBER, "-3.5")

    # --- error ---
    def test_invalid_character_raises(self):
        with pytest.raises(ValueError, match="Unrecognised character"):
            tokenise("3 @ 4")


# ===========================================================================
# TestToRPN
# ===========================================================================

class TestToRPN:
    # --- simple precedence ---
    def test_addition_stays_infix_order(self):
        # 3 + 4  →  3 4 +
        rpn = to_rpn(tokenise("3+4"))
        assert rpn == [(NUMBER, "3"), (NUMBER, "4"), (OPERATOR, "+")]

    def test_multiplication_before_addition(self):
        # 2 + 3 * 4  →  2 3 4 * +
        rpn = to_rpn(tokenise("2+3*4"))
        assert rpn == [
            (NUMBER, "2"), (NUMBER, "3"), (NUMBER, "4"),
            (OPERATOR, "*"), (OPERATOR, "+"),
        ]

    def test_addition_before_multiplication_with_parens(self):
        # (2+3)*4  →  2 3 + 4 *
        rpn = to_rpn(tokenise("(2+3)*4"))
        assert rpn == [
            (NUMBER, "2"), (NUMBER, "3"), (OPERATOR, "+"),
            (NUMBER, "4"), (OPERATOR, "*"),
        ]

    # --- exponentiation right-associativity ---
    def test_exponent_right_associative(self):
        # 2^3^2  →  2 3 2 ^ ^   (right-to-left: 2^(3^2))
        rpn = to_rpn(tokenise("2^3^2"))
        assert rpn == [
            (NUMBER, "2"), (NUMBER, "3"), (NUMBER, "2"),
            (OPERATOR, "^"), (OPERATOR, "^"),
        ]

    # --- parentheses errors ---
    def test_mismatched_close_paren_raises(self):
        with pytest.raises(ValueError, match="Mismatched parentheses"):
            to_rpn(tokenise("3+4)"))

    def test_mismatched_open_paren_raises(self):
        with pytest.raises(ValueError, match="Mismatched parentheses"):
            to_rpn([(LEFT_PAREN, "("), (NUMBER, "3"), (OPERATOR, "+"), (NUMBER, "4")])


# ===========================================================================
# TestEvaluateRPN
# ===========================================================================

class TestEvaluateRPN:
    def test_addition(self):
        assert evaluate_rpn([(NUMBER, "3"), (NUMBER, "4"), (OPERATOR, "+")]) == 7.0

    def test_subtraction(self):
        assert evaluate_rpn([(NUMBER, "10"), (NUMBER, "3"), (OPERATOR, "-")]) == 7.0

    def test_multiplication(self):
        assert evaluate_rpn([(NUMBER, "3"), (NUMBER, "4"), (OPERATOR, "*")]) == 12.0

    def test_division(self):
        assert evaluate_rpn([(NUMBER, "10"), (NUMBER, "4"), (OPERATOR, "/")]) == 2.5

    def test_exponentiation(self):
        assert evaluate_rpn([(NUMBER, "2"), (NUMBER, "10"), (OPERATOR, "^")]) == 1024.0

    def test_division_by_zero_raises(self):
        with pytest.raises(ZeroDivisionError):
            evaluate_rpn([(NUMBER, "5"), (NUMBER, "0"), (OPERATOR, "/")])

    def test_too_few_operands_raises(self):
        with pytest.raises(ValueError):
            evaluate_rpn([(NUMBER, "5"), (OPERATOR, "+")])

    def test_too_many_operands_raises(self):
        with pytest.raises(ValueError):
            evaluate_rpn([(NUMBER, "1"), (NUMBER, "2"), (NUMBER, "3"), (OPERATOR, "+")])


# ===========================================================================
# TestEvaluate — end-to-end public API
# ===========================================================================

class TestEvaluate:
    # --- basic arithmetic ---
    def test_addition(self):
        assert evaluate("3+4") == 7.0

    def test_subtraction(self):
        assert evaluate("10-3") == 7.0

    def test_multiplication(self):
        assert evaluate("3*4") == 12.0

    def test_division(self):
        assert evaluate("10/4") == 2.5

    def test_exponentiation(self):
        assert evaluate("2^10") == 1024.0

    # --- PEMDAS precedence ---
    def test_multiplication_before_addition(self):
        # 2 + 3*4 = 2 + 12 = 14  (not 20)
        assert evaluate("2+3*4") == 14.0

    def test_division_before_subtraction(self):
        # 10 - 6/2 = 10 - 3 = 7  (not 2)
        assert evaluate("10-6/2") == 7.0

    def test_exponent_before_multiplication(self):
        # 2 * 3^2 = 2 * 9 = 18  (not 36)
        assert evaluate("2*3^2") == 18.0

    def test_left_to_right_same_precedence_add_sub(self):
        # 10 - 3 + 2 = 9  (left-to-right, not 10 - 5 = 5)
        assert evaluate("10-3+2") == 9.0

    def test_left_to_right_same_precedence_mul_div(self):
        # 12 / 4 * 3 = 9  (left-to-right, not 12 / 12 = 1)
        assert evaluate("12/4*3") == 9.0

    # --- right-associative exponentiation ---
    def test_exponent_right_associative(self):
        # 2^3^2 = 2^(3^2) = 2^9 = 512  (not (2^3)^2 = 64)
        assert evaluate("2^3^2") == 512.0

    # --- parentheses override precedence ---
    def test_parens_override_precedence(self):
        # (2+3)*4 = 20  (not 14)
        assert evaluate("(2+3)*4") == 20.0

    def test_nested_parens(self):
        # ((2+3)*4) = 20
        assert evaluate("((2+3)*4)") == 20.0

    # --- the spec example: ((3+4)*6^2/5)+1-7 ---
    def test_spec_nested_expression(self):
        # Step by step:
        #   inner: 3+4 = 7
        #   6^2 = 36
        #   7 * 36 = 252
        #   252 / 5 = 50.4
        #   50.4 + 1 = 51.4
        #   51.4 - 7 = 44.4
        assert evaluate("((3+4)*6^2/5)+1-7") == pytest.approx(44.4)

    # --- unary minus ---
    def test_unary_minus_number(self):
        assert evaluate("-5+3") == -2.0

    def test_unary_minus_in_parens(self):
        assert evaluate("(-7)+10") == 3.0

    def test_unary_minus_with_multiplication(self):
        assert evaluate("3*-2") == -6.0

    # --- decimals ---
    def test_decimal_operands(self):
        assert evaluate("1.5+2.5") == pytest.approx(4.0)

    def test_decimal_division(self):
        assert evaluate("7.5/2.5") == pytest.approx(3.0)

    # --- complex nested expressions ---
    def test_deeply_nested(self):
        # (((1+2)*3)+4)*5 = ((3*3)+4)*5 = (9+4)*5 = 13*5 = 65
        assert evaluate("(((1+2)*3)+4)*5") == 65.0

    def test_mixed_all_operators(self):
        # 2+3*4-8/2^2 = 2+12-8/4 = 2+12-2 = 12
        assert evaluate("2+3*4-8/2^2") == 12.0

    def test_single_number(self):
        assert evaluate("42") == 42.0

    def test_whitespace_in_expression(self):
        assert evaluate("  3  +  4  ") == 7.0

    # --- error cases ---
    def test_empty_expression_raises(self):
        with pytest.raises(ValueError):
            evaluate("")

    def test_whitespace_only_raises(self):
        with pytest.raises(ValueError):
            evaluate("   ")

    def test_division_by_zero_raises(self):
        with pytest.raises(ZeroDivisionError):
            evaluate("5/0")

    def test_mismatched_parens_raises(self):
        with pytest.raises(ValueError):
            evaluate("(3+4")

    def test_invalid_character_raises(self):
        with pytest.raises(ValueError):
            evaluate("3$4")

    # --- additional PEMDAS edge cases ---
    @pytest.mark.parametrize("expr,expected", [
        ("1+2+3+4",        10.0),
        ("2^2^2",          16.0),   # right-assoc: 2^(2^2) = 2^4 = 16
        ("(2+3)^2",        25.0),
        ("100/10/2",        5.0),   # left-assoc: (100/10)/2 = 5
        ("2*(3+4*(5-2))",  30.0),   # 2*(3+4*3) = 2*(3+12) = 2*15 = 30
        ("-3+10",           7.0),   # unary minus
        ("4^0.5",           2.0),   # fractional exponent → square root
    ])
    def test_parametrized(self, expr, expected):
        assert evaluate(expr) == pytest.approx(expected)


# ===========================================================================
# TestEvalEndpoint — Flask /eval HTTP route
# ===========================================================================

class TestEvalEndpoint:
    @pytest.fixture()
    def client(self):
        from app import app
        app.config["TESTING"] = True
        with app.test_client() as c:
            yield c

    def test_get_simple_expression(self, client):
        resp = client.get("/eval?expr=3%2B4")   # 3+4
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["result"] == 7.0
        assert data["expression"] == "3+4"

    def test_post_json_expression(self, client):
        resp = client.post("/eval", json={"expr": "2*3"})
        assert resp.status_code == 200
        assert resp.get_json()["result"] == 6.0

    def test_post_form_expression(self, client):
        resp = client.post("/eval", data={"expr": "10-4"})
        assert resp.status_code == 200
        assert resp.get_json()["result"] == 6.0

    def test_spec_nested_expression_via_http(self, client):
        resp = client.get("/eval?expr=((3%2B4)*6^2/5)%2B1-7")
        assert resp.status_code == 200
        assert resp.get_json()["result"] == pytest.approx(44.4)

    def test_missing_expr_returns_400(self, client):
        resp = client.get("/eval")
        assert resp.status_code == 400
        assert "error" in resp.get_json()

    def test_division_by_zero_returns_400(self, client):
        resp = client.get("/eval?expr=5/0")
        assert resp.status_code == 400
        assert "zero" in resp.get_json()["error"].lower()

    def test_invalid_expression_returns_400(self, client):
        resp = client.get("/eval?expr=3%244")   # 3$4
        assert resp.status_code == 400
        assert "error" in resp.get_json()

    def test_response_contains_expression_field(self, client):
        resp = client.get("/eval?expr=1%2B1")
        assert "expression" in resp.get_json()

    def test_exponentiation_via_http(self, client):
        resp = client.get("/eval?expr=2^8")
        assert resp.status_code == 200
        assert resp.get_json()["result"] == 256.0
