<?php
declare(strict_types=1);

/**
 * evaluator.php — Mathematical expression evaluator built from scratch in PHP.
 * Implements recursive-descent parser matching Python, Java, Rust, Node, and C implementations.
 * No third-party parser dependencies.
 */

class Evaluator {
    private string $str;
    private int $pos = -1;
    private ?string $ch = null;
    private int $len;

    public function __construct(string $str) {
        $this->str = $str;
        $this->len = strlen($str);
    }

    private function nextChar(): void {
        $this->pos++;
        $this->ch = ($this->pos < $this->len) ? $this->str[$this->pos] : null;
    }

    private function eat(string $charToEat): bool {
        while ($this->ch === ' ' || $this->ch === "\t" || $this->ch === "\r" || $this->ch === "\n") {
            $this->nextChar();
        }
        if ($this->ch === $charToEat) {
            $this->nextChar();
            return true;
        }
        return false;
    }

    public function parse(): float {
        $this->nextChar();
        $x = $this->parseExpression();
        while ($this->ch === ' ' || $this->ch === "\t" || $this->ch === "\r" || $this->ch === "\n") {
            $this->nextChar();
        }
        if ($this->pos < $this->len && $this->ch !== null) {
            throw new InvalidArgumentException("Unexpected: " . $this->ch);
        }
        if (!is_finite($x)) {
            throw new ArithmeticError("Arithmetic error: non-finite result");
        }
        return (float)$x;
    }

    private function parseExpression(): float {
        $x = $this->parseTerm();
        for (;;) {
            if ($this->eat('+')) {
                $x += $this->parseTerm();
            } elseif ($this->eat('-')) {
                $x -= $this->parseTerm();
            } else {
                return $x;
            }
        }
    }

    private function parseTerm(): float {
        $x = $this->parseFactor();
        for (;;) {
            if ($this->eat('*')) {
                $x *= $this->parseFactor();
            } elseif ($this->eat('/')) {
                $divisor = $this->parseFactor();
                if (abs($divisor) < 1e-15) {
                    throw new DivisionByZeroError("Division by zero");
                }
                $x /= $divisor;
            } else {
                return $x;
            }
        }
    }

    private function parseFactor(): float {
        if ($this->eat('+')) {
            return +$this->parseFactor();
        }
        if ($this->eat('-')) {
            return -$this->parseFactor();
        }

        $x = 0.0;
        $startPos = $this->pos;

        if ($this->eat('(')) {
            $x = $this->parseExpression();
            if (!$this->eat(')')) {
                throw new InvalidArgumentException("Missing ')'");
            }
        } elseif (($this->ch !== null && ctype_digit($this->ch)) || $this->ch === '.') {
            $dotSeen = false;
            while ($this->ch !== null && (ctype_digit($this->ch) || $this->ch === '.')) {
                if ($this->ch === '.') {
                    if ($dotSeen) {
                        throw new InvalidArgumentException("Invalid number");
                    }
                    $dotSeen = true;
                }
                $this->nextChar();
            }
            $literal = substr($this->str, $startPos, $this->pos - $startPos);
            if ($literal === '.' || $literal === '') {
                throw new InvalidArgumentException("Invalid number: " . $literal);
            }
            $x = (float)$literal;
        } elseif ($this->ch !== null && ctype_alpha($this->ch)) {
            while ($this->ch !== null && ctype_alpha($this->ch)) {
                $this->nextChar();
            }
            $func = strtolower(substr($this->str, $startPos, $this->pos - $startPos));
            if ($this->eat('(')) {
                $x = $this->parseExpression();
                if (!$this->eat(')')) {
                    throw new InvalidArgumentException("Missing ')' after argument to " . $func);
                }
            } else {
                $x = $this->parseFactor();
            }
            if ($func === 'sqrt') {
                if ($x < 0) {
                    throw new ArithmeticError("Square root of negative number");
                }
                $x = sqrt($x);
            } elseif ($func === 'sin') {
                $x = sin(($x * M_PI) / 180.0);
            } elseif ($func === 'cos') {
                $x = cos(($x * M_PI) / 180.0);
            } else {
                throw new InvalidArgumentException("Unknown function: " . $func);
            }
        } else {
            throw new InvalidArgumentException("Unexpected: " . ($this->ch ?? 'EOF'));
        }

        if ($this->eat('^')) {
            $exponent = $this->parseFactor();
            $x = pow($x, $exponent);
        }

        return (float)$x;
    }
}

function evaluate(string $str): float {
    if (trim($str) === '') {
        throw new InvalidArgumentException("Missing or empty expression");
    }
    $evaluator = new Evaluator($str);
    return $evaluator->parse();
}
