"""
evaluator.py — Mathematical expression evaluator built from scratch.

Algorithm overview
------------------
We use the classic two-pass Shunting-Yard algorithm (Dijkstra, 1961):

  Pass 1 – Tokenise
    Walk the raw string character-by-character and emit a flat list of
    typed tokens: NUMBER, OPERATOR, LEFT_PAREN, RIGHT_PAREN.

  Pass 2 – Shunting-Yard → RPN - https://en.wikipedia.org/wiki/Shunting_yard_algorithm
    Convert the infix token stream to Reverse Polish Notation (postfix)
    using two data structures:
      • output_queue  – the growing RPN output (a list used as a queue)
      • op_stack      – a stack of pending operators / left-parentheses

    PEMDAS precedence is encoded in the OPERATORS table:
      P  – parentheses  (handled structurally, not by precedence number)
      E  – exponentiation  ^   precedence 4, RIGHT-associative
      MD – multiplication * and division /   precedence 3, left-associative
      AS – addition + and subtraction -       precedence 2, left-associative

  Pass 3 – Evaluate RPN
    Walk the RPN list with a single operand stack:
      • NUMBER  → push
      • OPERATOR → pop two operands, apply, push result

No third-party libraries are used.  Only built-in Python types (list, dict).
"""

from __future__ import annotations
from typing import List, Tuple

# ---------------------------------------------------------------------------
# Token types
# ---------------------------------------------------------------------------
NUMBER      = "NUMBER"
OPERATOR    = "OPERATOR"
LEFT_PAREN  = "LEFT_PAREN"
RIGHT_PAREN = "RIGHT_PAREN"

Token = Tuple[str, str]   # (type, value)

# ---------------------------------------------------------------------------
# Operator table
#
# Maps each operator symbol to a 2-tuple: (precedence: int, is_right_associative: bool)
#
# PRECEDENCE (first element — int)
# ---------------------------------
# Controls which operator "binds tighter" when two operators compete for the
# same operand.  Higher number = evaluated first.
#
#   Precedence 4 : ^   (exponentiation)   — highest, evaluated first
#   Precedence 3 : * / (multiplication, division)
#   Precedence 2 : + - (addition, subtraction)  — lowest, evaluated last
#
# Example: 2 + 3 * 4
#   '*' has precedence 3, '+' has precedence 2.
#   '*' wins → 3*4 is evaluated first → 2 + 12 = 14  (not (2+3)*4 = 20)
#
# IS_RIGHT_ASSOCIATIVE (second element — bool)
# ---------------------------------------------
# Associativity decides the evaluation order when two operators have the
# SAME precedence and appear consecutively without parentheses.
#
#   False (left-associative):
#     Operators of equal precedence are evaluated left-to-right.
#     This is the standard rule for + - * /.
#     Example: 10 - 3 - 2  →  (10 - 3) - 2  =  5   (not 10 - (3-2) = 9)
#     Example: 12 / 4 * 3  →  (12 / 4) * 3  =  9   (not 12 / (4*3) = 1)
#
#   True (right-associative):
#     Operators of equal precedence are evaluated right-to-left.
#     Exponentiation ^ is the classic case — this matches standard maths.
#     Example: 2^3^2  →  2^(3^2)  =  2^9  =  512   (not (2^3)^2 = 64)
#
# How the bool is used in the Shunting-Yard algorithm (to_rpn):
#   When deciding whether to pop operator o2 off the stack before pushing o1:
#     pop if:  prec(o2) > prec(o1)
#           OR prec(o2) == prec(o1) AND o1 is LEFT-associative (bool is False)
#   Do NOT pop if o1 is right-associative (bool is True) and precedences are
#   equal — this keeps o2 on the stack so it is applied after o1, achieving
#   right-to-left evaluation.
# ---------------------------------------------------------------------------
OPERATORS: dict[str, tuple[int, bool]] = {
    "+": (2, False),   # left-associative, lowest precedence
    "-": (2, False),   # left-associative, lowest precedence
    "*": (3, False),   # left-associative, medium precedence
    "/": (3, False),   # left-associative, medium precedence
    "^": (4, True),    # RIGHT-associative, highest precedence
}


# ===========================================================================
# Pass 1 – Tokeniser
# ===========================================================================

def tokenise(expression: str) -> List[Token]:
    """
    Convert a raw expression string into a flat list of typed tokens.

    Handles:
      • Multi-digit and decimal numbers  (e.g. 3.14, 100)
      • Unary minus  (e.g. -3, (-7), 5 * -2)
        Detected when '-' appears at the start of the expression or
        immediately after an operator or left-parenthesis.
      • Whitespace is silently skipped.

    Raises ValueError for any unrecognised character.
    """
    tokens: List[Token] = []
    i = 0
    n = len(expression)

    while i < n:
        ch = expression[i]

        # --- skip whitespace ---
        if ch.isspace():
            i += 1
            continue

        # --- number (integer or decimal) ---
        if ch.isdigit() or ch == ".":
            j = i
            # consume all digits and at most one decimal point
            while j < n and (expression[j].isdigit() or expression[j] == "."):
                j += 1
            tokens.append((NUMBER, expression[i:j]))
            i = j
            continue

        # --- unary minus: treat as part of the next number token ---
        # A '-' is unary when it is the very first token, or when the
        # previous token was an operator or a left-parenthesis.
        if ch == "-" and (
            not tokens
            or tokens[-1][0] == OPERATOR
            or tokens[-1][0] == LEFT_PAREN
        ):
            # peek ahead to collect the numeric value
            j = i + 1
            while j < n and (expression[j].isdigit() or expression[j] == "."):
                j += 1
            if j > i + 1:
                # e.g. "-3.5" → NUMBER "-3.5"
                tokens.append((NUMBER, expression[i:j]))
                i = j
                continue
            # '-' with no digits after it falls through to operator handling

        # --- operator ---
        if ch in OPERATORS:
            tokens.append((OPERATOR, ch))
            i += 1
            continue

        # --- parentheses ---
        if ch == "(":
            tokens.append((LEFT_PAREN, ch))
            i += 1
            continue
        if ch == ")":
            tokens.append((RIGHT_PAREN, ch))
            i += 1
            continue

        raise ValueError(f"Unrecognised character '{ch}' at position {i}")

    return tokens


# ===========================================================================
# Pass 2 – Shunting-Yard: infix tokens → RPN (postfix) token list
# ===========================================================================

def to_rpn(tokens: List[Token]) -> List[Token]:
    """
    Apply Dijkstra's Shunting-Yard algorithm to convert an infix token
    list to Reverse Polish Notation.

    Data structures
    ---------------
    output_queue : list  – accumulates the RPN output left-to-right
    op_stack     : list  – stack of pending operators and left-parens
                           (top of stack = last element, accessed via [-1])

    Rules (applied for each token in order)
    ----------------------------------------
    NUMBER      → append directly to output_queue

    OPERATOR o1 → while the top of op_stack is an operator o2 AND
                    (o2 has higher precedence than o1, OR
                     o2 has equal precedence AND o1 is left-associative)
                  pop o2 to output_queue.
                  Then push o1 onto op_stack.

    LEFT_PAREN  → push onto op_stack (acts as a barrier)

    RIGHT_PAREN → pop from op_stack to output_queue until a LEFT_PAREN
                  is found; discard the LEFT_PAREN.
                  Mismatched ')' raises ValueError.

    End of input → pop all remaining operators from op_stack to output_queue.
                   A remaining LEFT_PAREN means mismatched '(' → ValueError.
    """
    output_queue: List[Token] = []
    op_stack:     List[Token] = []

    for token_type, token_val in tokens:

        # --- NUMBER: goes straight to output ---
        if token_type == NUMBER:
            output_queue.append((NUMBER, token_val))

        # --- OPERATOR: respect precedence and associativity ---
        elif token_type == OPERATOR:
            prec_o1, right_assoc_o1 = OPERATORS[token_val]

            # Pop operators from the stack that should execute before o1
            while op_stack:
                top_type, top_val = op_stack[-1]
                if top_type != OPERATOR:
                    break
                prec_o2, _ = OPERATORS[top_val]
                # Pop if o2 has strictly higher precedence, OR equal
                # precedence and o1 is left-associative (not right-assoc)
                if prec_o2 > prec_o1 or (prec_o2 == prec_o1 and not right_assoc_o1):
                    output_queue.append(op_stack.pop())
                else:
                    break

            op_stack.append((OPERATOR, token_val))

        # --- LEFT_PAREN: push as a barrier ---
        elif token_type == LEFT_PAREN:
            op_stack.append((LEFT_PAREN, token_val))

        # --- RIGHT_PAREN: drain stack until matching LEFT_PAREN ---
        elif token_type == RIGHT_PAREN:
            # Pop operators until we hit the matching left-paren
            while op_stack and op_stack[-1][0] != LEFT_PAREN:
                output_queue.append(op_stack.pop())

            if not op_stack:
                raise ValueError("Mismatched parentheses: unexpected ')'")

            # Discard the LEFT_PAREN
            op_stack.pop()

    # --- End of input: flush remaining operators ---
    while op_stack:
        top_type, top_val = op_stack.pop()
        if top_type == LEFT_PAREN:
            raise ValueError("Mismatched parentheses: unclosed '('")
        output_queue.append((top_type, top_val))

    return output_queue


# ===========================================================================
# Pass 3 – RPN evaluator
# ===========================================================================

def evaluate_rpn(rpn: List[Token]) -> float:
    """
    Evaluate a Reverse Polish Notation token list using a single operand stack.

    For each token:
      NUMBER   → convert to float and push onto the stack
      OPERATOR → pop the top two operands (right operand first, then left),
                 apply the operator, push the result

    Division by zero raises ZeroDivisionError.
    An ill-formed RPN (wrong number of operands) raises ValueError.
    """
    stack: List[float] = []

    for token_type, token_val in rpn:

        if token_type == NUMBER:
            # Push numeric value onto the operand stack
            stack.append(float(token_val))

        elif token_type == OPERATOR:
            # Need at least two operands on the stack
            if len(stack) < 2:
                raise ValueError(f"Not enough operands for operator '{token_val}'")

            # Pop right operand first (it was pushed last)
            right = stack.pop()
            left  = stack.pop()

            # Apply the operator and push the result
            if token_val == "+":
                stack.append(left + right)
            elif token_val == "-":
                stack.append(left - right)
            elif token_val == "*":
                stack.append(left * right)
            elif token_val == "/":
                if right == 0:
                    raise ZeroDivisionError("Division by zero")
                stack.append(left / right)
            elif token_val == "^":
                stack.append(left ** right)

    # After processing all tokens exactly one value must remain
    if len(stack) != 1:
        raise ValueError("Invalid expression: too many operands")

    return stack[0]


# ===========================================================================
# Public entry point
# ===========================================================================

def evaluate(expression: str) -> float:
    """
    Evaluate a mathematical expression string and return a float result.

    Full pipeline:
      expression  →  tokenise()  →  to_rpn()  →  evaluate_rpn()  →  float

    Supported operators : + - * / ^
    Supported grouping  : ( )
    Supports            : integers, decimals, unary minus, nested parens
    PEMDAS order        : ^ > * / > + -  (^ is right-associative)

    Raises
    ------
    ValueError        – unrecognised character, mismatched parens, bad syntax
    ZeroDivisionError – division by zero
    """
    if not expression or not expression.strip():
        raise ValueError("Expression must not be empty")

    tokens = tokenise(expression)
    rpn    = to_rpn(tokens)
    result = evaluate_rpn(rpn)
    return result
