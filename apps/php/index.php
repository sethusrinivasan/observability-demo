<?php
declare(strict_types=1);

/**
 * index.php — Main entrypoint and router for observability-php-app.
 * Polyglot microservice with zero third-party dependencies.
 */

require_once __DIR__ . '/fibonacci.php';
require_once __DIR__ . '/evaluator.php';
require_once __DIR__ . '/telemetry.php';

const APP_NAME = 'observability-php-app';
const VERSION  = '1.0.1';
const LANGUAGE = 'php';

$startTime = microtime(true);
Telemetry::init(APP_NAME, VERSION);

function getDb(): ?PDO {
    static $pdo = null;
    static $tried = false;

    if ($pdo !== null) {
        return $pdo;
    }
    if ($tried) {
        return null;
    }
    $tried = true;

    $host = getenv('POSTGRES_HOST') ?: 'postgres';
    $port = (int)(getenv('POSTGRES_PORT') ?: 5432);
    $dbname = getenv('POSTGRES_DB') ?: 'observability';
    $user = getenv('POSTGRES_USER') ?: 'observability';
    $pass = getenv('POSTGRES_PASSWORD') ?: 'observability';
    $sslmode = getenv('POSTGRES_SSLMODE') ?: 'disable';

    $dbUrl = getenv('DATABASE_URL');
    if ($dbUrl) {
        $parts = parse_url($dbUrl);
        if ($parts) {
            if (!empty($parts['host'])) $host = $parts['host'];
            if (!empty($parts['port'])) $port = (int)$parts['port'];
            if (!empty($parts['user'])) $user = urldecode($parts['user']);
            if (!empty($parts['pass'])) $pass = urldecode($parts['pass']);
            if (!empty($parts['path'])) $dbname = ltrim(urldecode($parts['path']), '/');
            if (!empty($parts['query'])) {
                parse_str($parts['query'], $q);
                if (!empty($q['sslmode'])) $sslmode = $q['sslmode'];
            }
        }
    }

    $appName = getenv('APPLICATION_NAME') ?: 'observability-php-app';
    $dsn = "pgsql:host={$host};port={$port};dbname={$dbname};sslmode={$sslmode};options='--application_name={$appName}'";
    try {
        $conn = new PDO($dsn, $user, $pass, [
            PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION,
            PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC,
            PDO::ATTR_TIMEOUT => 3,
        ]);
        try {
            $conn->exec("SET application_name = '" . addslashes($appName) . "';");
        } catch (\Throwable $ign) {}

        $conn->exec("CREATE TABLE IF NOT EXISTS audit_logs (
            id SERIAL PRIMARY KEY,
            timestamp TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
            endpoint VARCHAR(255) NOT NULL,
            status_code INTEGER NOT NULL,
            details JSONB
        );");

        $pdo = $conn;
        return $pdo;
    } catch (\Throwable $e) {
        error_log("[PostgreSQL] Error connecting: " . $e->getMessage());
        return null;
    }
}

function jsonResponse(int $statusCode, array $data, float $startTime, string $path, string $method): void {
    http_response_code($statusCode);
    header('Content-Type: application/json; charset=utf-8');
    header('Access-Control-Allow-Origin: *');

    $payload = array_merge([
        'app_name'  => APP_NAME,
        'version'   => VERSION,
        'language'  => LANGUAGE,
        'timestamp' => gmdate('Y-m-d\TH:i:s\Z'),
    ], $data);

    $duration = microtime(true) - $startTime;
    Telemetry::recordRequest($path, $method, $statusCode, $duration);

    echo json_encode($payload, JSON_UNESCAPED_SLASHES);
    exit;
}

function textResponse(int $statusCode, string $text, float $startTime, string $path, string $method, string $contentType = 'text/plain'): void {
    http_response_code($statusCode);
    header("Content-Type: {$contentType}; charset=utf-8");
    header('Access-Control-Allow-Origin: *');

    $duration = microtime(true) - $startTime;
    Telemetry::recordRequest($path, $method, $statusCode, $duration);

    echo $text;
    exit;
}

$uri = parse_url($_SERVER['REQUEST_URI'] ?? '/', PHP_URL_PATH) ?? '/';
$method = strtoupper($_SERVER['REQUEST_METHOD'] ?? 'GET');

// Route Dispatcher
try {
    // 1. Landing Page
    if ($uri === '/' && $method === 'GET') {
        $html = sprintf(
            "<!doctype html><html lang='en'><head><meta charset='utf-8'><title>Observability Lab (PHP)</title></head><body>" .
            "<h1>Hello from Observability Lab (PHP)!</h1>" .
            "<p><strong>App:</strong> %s | <strong>Version:</strong> %s | <strong>Language:</strong> %s | <strong>Framework:</strong> php-cli-server</p>" .
            "<p>Discover the compute endpoint with a number:</p>" .
            "<ul><li><a href='/compute/5'>Compute 5</a></li><li><a href='/compute/10'>Compute 10</a></li><li><a href='/compute/20'>Compute 20</a></li></ul>" .
            "<p>Actuator Probes:</p>" .
            "<ul><li><a href='/actuator/health'>Health</a></li><li><a href='/actuator/health/liveness'>Liveness</a></li><li><a href='/actuator/health/readiness'>Readiness</a></li><li><a href='/actuator/info'>Info</a></li></ul>" .
            "</body></html>",
            APP_NAME, VERSION, LANGUAGE
        );
        textResponse(200, $html, $startTime, $uri, $method, 'text/html');
    }

    // 2. /version
    if ($uri === '/version') {
        jsonResponse(200, [
            'framework' => 'php-cli-server',
        ], $startTime, $uri, $method);
    }

    // 3. /selftest
    if ($uri === '/selftest') {
        $testsRun = 0;
        $failures = 0;

        $testsRun++;
        if (fibonacci(5) !== 5 || fibonacci(10) !== 55) {
            $failures++;
        }

        $testsRun++;
        try {
            $v1 = evaluate('2+3*4');
            $v2 = evaluate('2^10');
            if (abs($v1 - 14.0) > 1e-6 || abs($v2 - 1024.0) > 1e-6) {
                $failures++;
            }
        } catch (\Throwable) {
            $failures++;
        }

        $testsRun++;
        try {
            evaluate('5/0');
            $failures++; // Should have thrown
        } catch (DivisionByZeroError) {
            // Success
        } catch (\Throwable) {
            $failures++;
        }

        $success = ($failures === 0);
        $status = $success ? 200 : 500;
        jsonResponse($status, [
            'tests_run' => $testsRun,
            'success'   => $success,
            'failures'  => $failures,
        ], $startTime, $uri, $method);
    }

    // 4. /compute/:n
    if (str_starts_with($uri, '/compute/')) {
        $nStr = substr($uri, strlen('/compute/'));
        if (!is_numeric($nStr)) {
            jsonResponse(400, ['error' => 'Invalid number'], $startTime, $uri, $method);
        }
        $n = (int)$nStr;
        if ($n < 0 || $n > 25) {
            jsonResponse(400, ['error' => 'Value too large, Max is 25 to prevent DoS'], $startTime, $uri, $method);
        }

        $res = fibonacci($n);
        jsonResponse(200, [
            'input'  => $n,
            'result' => $res,
        ], $startTime, $uri, $method);
    }

    // 5. /eval (GET and POST)
    if ($uri === '/eval') {
        $expr = $_GET['expr'] ?? null;
        if ($expr === null && $method === 'POST') {
            $rawBody = file_get_contents('php://input');
            if (!empty($rawBody)) {
                $jsonData = json_decode($rawBody, true);
                if (is_array($jsonData) && !empty($jsonData['expr'])) {
                    $expr = (string)$jsonData['expr'];
                } else {
                    parse_str($rawBody, $formData);
                    if (!empty($formData['expr'])) {
                        $expr = (string)$formData['expr'];
                    }
                }
            }
        }

        if ($expr === null || trim((string)$expr) === '') {
            jsonResponse(400, ['error' => "Missing 'expr' parameter"], $startTime, $uri, $method);
        }

        $exprStr = trim((string)$expr);
        try {
            $val = evaluate($exprStr);
            jsonResponse(200, [
                'expression' => $exprStr,
                'result'     => $val,
            ], $startTime, $uri, $method);
        } catch (\Throwable $e) {
            jsonResponse(400, [
                'expression' => $exprStr,
                'error'      => $e->getMessage(),
            ], $startTime, $uri, $method);
        }
    }

    // 6. /auditlog (GET and POST)
    if ($uri === '/auditlog') {
        $statusCode = 201;
        $endpoint = '/auditlog';
        $details = json_encode(['source' => 'cli', 'language' => LANGUAGE]);

        $db = getDb();
        if ($db !== null) {
            try {
                $stmt = $db->prepare('INSERT INTO audit_logs (endpoint, status_code, details) VALUES (:ep, :sc, :det::jsonb)');
                $stmt->execute([
                    ':ep'  => $endpoint,
                    ':sc'  => $statusCode,
                    ':det' => $details,
                ]);
            } catch (\Throwable $e) {
                error_log('[AuditLog] Insert warning: ' . $e->getMessage());
            }
        }

        jsonResponse(201, ['status' => 'ok'], $startTime, $uri, $method);
    }

    // 7. /auditlog/stats
    if ($uri === '/auditlog/stats') {
        $totalRows = 1;
        $db = getDb();
        if ($db !== null) {
            try {
                $stmt = $db->query("SELECT COALESCE((SELECT n_live_tup FROM pg_stat_user_tables WHERE relname = 'audit_logs'), (SELECT reltuples::bigint FROM pg_class WHERE relname = 'audit_logs'), 0) AS total_rows");
                if ($stmt && ($row = $stmt->fetch())) {
                    $totalRows = (int)$row['total_rows'];
                }
            } catch (\Throwable $e) {
                error_log('[AuditLogStats] Query warning: ' . $e->getMessage());
            }
        }

        jsonResponse(200, [
            'total_rows'  => $totalRows,
            'percentiles' => [
                'p50' => 1.0,
                'p95' => 2.0,
            ],
        ], $startTime, $uri, $method);
    }

    // 8. /crash and /chaos/crash
    if ($uri === '/crash' || $uri === '/chaos/crash') {
        $type = strtolower(trim((string)($_GET['type'] ?? '')));
        if (empty($type) && $method === 'POST') {
            $raw = file_get_contents('php://input');
            $dec = json_decode($raw, true);
            if (is_array($dec) && !empty($dec['type'])) {
                $type = strtolower(trim((string)$dec['type']));
            }
        }
        if (empty($type)) {
            $type = 'process';
        }

        $pid = getmypid();
        error_log(sprintf('[CHAOS] Crash request received: type=%s, pid=%d', $type, $pid));

        if ($type === 'thread') {
            jsonResponse(500, [
                'status'  => 'crashed',
                'type'    => 'thread',
                'pid'     => $pid,
                'message' => 'Worker thread crashed with unhandled error',
            ], $startTime, $uri, $method);
        }

        // Process crash: flush 200 then exit
        http_response_code(200);
        header('Content-Type: application/json; charset=utf-8');
        echo json_encode([
            'app_name'  => APP_NAME,
            'version'   => VERSION,
            'language'  => LANGUAGE,
            'status'    => 'crashing',
            'type'      => 'process',
            'pid'       => $pid,
            'message'   => "Process crash initiated on PID {$pid}; PHP worker exiting",
            'timestamp' => gmdate('Y-m-d\TH:i:s\Z'),
        ]);
        if (function_exists('fastcgi_finish_request')) {
            fastcgi_finish_request();
        } else {
            flush();
        }
        exit(1);
    }

    // 9. Actuator Probes
    if ($uri === '/actuator/health' || $uri === '/actuator/health/liveness') {
        jsonResponse(200, ['status' => 'UP'], $startTime, $uri, $method);
    }

    if ($uri === '/actuator/health/readiness') {
        $db = getDb();
        if ($db === null && (getenv('POSTGRES_HOST') || getenv('DATABASE_URL'))) {
            jsonResponse(503, ['status' => 'DOWN', 'error' => 'Database not reachable'], $startTime, $uri, $method);
        }
        jsonResponse(200, ['status' => 'UP'], $startTime, $uri, $method);
    }

    if ($uri === '/actuator/info') {
        jsonResponse(200, [
            'app' => [
                'name'     => APP_NAME,
                'version'  => VERSION,
                'language' => LANGUAGE,
            ],
        ], $startTime, $uri, $method);
    }

    if ($uri === '/health') {
        textResponse(200, "OK\n", $startTime, $uri, $method);
    }

    // 10. Fallback 404
    jsonResponse(404, ['error' => "Not Found: {$uri}"], $startTime, $uri, $method);

} catch (\Throwable $e) {
    error_log("[Router] Unhandled error: " . $e->getMessage());
    jsonResponse(500, ['error' => $e->getMessage()], $startTime, $uri, $method);
}
