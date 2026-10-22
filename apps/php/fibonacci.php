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
    $a = 0;
    $b = 1;
    for ($i = 2; $i <= $n; $i++) {
        $temp = $a + $b;
        $a = $b;
        $b = $temp;
    }
    return $b;
}
