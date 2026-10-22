/**
 * evaluator.js — Mathematical expression evaluator built from scratch for Node.js.
 * Implements recursive-descent parser matching the Python, Java, and Rust implementations.
 * No third-party parser libraries.
 */

function isDigit(c) {
  return typeof c === "string" && c >= "0" && c <= "9";
}

function isAlpha(c) {
  return typeof c === "string" && ((c >= "a" && c <= "z") || (c >= "A" && c <= "Z"));
}

function evaluate(str) {
  if (typeof str !== "string" || !str.trim()) {
    throw new Error("Missing or empty expression");
  }

  let pos = -1;
  let ch = null;

  function nextChar() {
    pos++;
    ch = pos < str.length ? str.charAt(pos) : null;
  }

  function eat(charToEat) {
    while (ch === " ") nextChar();
    if (ch === charToEat) {
      nextChar();
      return true;
    }
    return false;
  }

  function parseExpression() {
    let x = parseTerm();
    for (;;) {
      if (eat("+")) x += parseTerm();
      else if (eat("-")) x -= parseTerm();
      else return x;
    }
  }

  function parseTerm() {
    let x = parseFactor();
    for (;;) {
      if (eat("*")) {
        x *= parseFactor();
      } else if (eat("/")) {
        const divisor = parseFactor();
        if (divisor === 0) {
          throw new Error("Division by zero");
        }
        x /= divisor;
      } else {
        return x;
      }
    }
  }

  function parseFactor() {
    if (eat("+")) return +parseFactor();
    if (eat("-")) return -parseFactor();

    let x;
    const startPos = pos;

    if (eat("(")) {
      x = parseExpression();
      if (!eat(")")) throw new Error("Missing ')'");
    } else if (isDigit(ch) || ch === ".") {
      let dotSeen = false;
      while (isDigit(ch) || ch === ".") {
        if (ch === ".") {
          if (dotSeen) {
            throw new Error("Invalid number '" + str.substring(startPos, pos + 1) + "'");
          }
          dotSeen = true;
        }
        nextChar();
      }
      const literal = str.substring(startPos, pos);
      if (literal === "." || literal === "") {
        throw new Error("Invalid number '" + literal + "'");
      }
      x = Number(literal);
      if (!Number.isFinite(x)) {
        throw new Error("Invalid number '" + literal + "'");
      }
    } else if (isAlpha(ch)) {
      while (isAlpha(ch)) nextChar();
      const func = str.substring(startPos, pos).toLowerCase();
      if (eat("(")) {
        x = parseExpression();
        if (!eat(")")) throw new Error("Missing ')' after argument to " + func);
      } else {
        x = parseFactor();
      }
      if (func === "sqrt") x = Math.sqrt(x);
      else if (func === "sin") x = Math.sin((x * Math.PI) / 180);
      else if (func === "cos") x = Math.cos((x * Math.PI) / 180);
      else throw new Error("Unknown function: " + func);
    } else {
      throw new Error("Unexpected: " + ch);
    }

    if (eat("^")) {
      x = Math.pow(x, parseFactor());
    }

    return x;
  }

  nextChar();
  const result = parseExpression();
  while (ch === " ") nextChar();
  if (pos < str.length && ch !== null) {
    throw new Error("Unexpected: " + ch);
  }

  if (!Number.isFinite(result)) {
    throw new Error("Arithmetic error: non-finite result");
  }

  return result;
}

module.exports = { evaluate };
