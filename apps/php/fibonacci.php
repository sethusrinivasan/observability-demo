<?php
declare(strict_types=1);

/**
 * fibonacci.php — Recursive Fibonacci calculation matching other polyglot microservices.
 */
function fibonacci(int $n): int {
    if ($n <= 0) {
        return 0;
    }
    if ($n === 1) {
        return 1;
    }
    return fibonacci($n - 1) + fibonacci($n - 2);
}
