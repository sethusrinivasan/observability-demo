<?php
declare(strict_types=1);

/**
 * telemetry.php — Lightweight OpenTelemetry HTTP exporter for metrics and distributed traces.
 * Conforms to OpenTelemetry OTLP JSON specification v1.
 * Supports both local OTel Collector (port 4318) and AWS Lambda Telemetry Receiver.
 */

class Telemetry {
    private static ?string $otlpEndpoint = null;
    private static string $appName = 'observability-php-app';
    private static string $version = '1.0.1';
    private static string $language = 'php';

    public static function init(string $appName = 'observability-php-app', string $version = '1.0.1'): void {
        self::$appName = $appName;
        self::$version = $version;

        $endpoint = getenv('OTEL_EXPORTER_OTLP_ENDPOINT');
        if (!$endpoint) {
            $endpoint = 'http://otel-collector:4318';
        }
        $endpoint = rtrim($endpoint, '/');

        // Normalize gRPC endpoint if specified by mistake to HTTP port
        if (str_ends_with($endpoint, ':4317')) {
            $endpoint = substr($endpoint, 0, -5) . ':4318';
        }

        if (!str_starts_with($endpoint, 'http://') && !str_starts_with($endpoint, 'https://')) {
            $endpoint = 'http://' . $endpoint;
        }

        self::$otlpEndpoint = $endpoint;
    }

    public static function recordRequest(
        string $path,
        string $method,
        int $statusCode,
        float $durationSec
    ): void {
        if (self::$otlpEndpoint === null) {
            self::init();
        }

        $nowSec = microtime(true);
        $nowNs = (int)($nowSec * 1_000_000_000);
        $startNs = $nowNs - (int)($durationSec * 1_000_000_000);

        $route = match (true) {
            $path === '/' => 'home',
            $path === '/version' => 'version',
            $path === '/selftest' => 'selftest',
            $path === '/eval' => 'eval_expression',
            $path === '/auditlog' => 'auditlog',
            $path === '/auditlog/stats' => 'auditlog_stats',
            $path === '/crash' || $path === '/chaos/crash' => 'chaos_crash',
            str_starts_with($path, '/actuator') => 'actuator',
            str_starts_with($path, '/compute') => 'compute',
            default => 'other',
        };

        $journey = match (true) {
            str_starts_with($path, '/compute') => 'compute',
            $path === '/' => 'home',
            $path === '/auditlog' || $path === '/auditlog/stats' => 'audit',
            default => 'other',
        };

        $status = ($statusCode >= 400) ? 'error' : 'success';
        $traceId = bin2hex(random_bytes(16));
        $spanId = bin2hex(random_bytes(8));

        // 1. OTLP Traces Payload
        $tracesPayload = [
            'resourceSpans' => [
                [
                    'resource' => [
                        'attributes' => [
                            ['key' => 'service.name', 'value' => ['stringValue' => self::$appName]],
                            ['key' => 'service.version', 'value' => ['stringValue' => self::$version]],
                            ['key' => 'language', 'value' => ['stringValue' => self::$language]],
                        ],
                    ],
                    'scopeSpans' => [
                        [
                            'scope' => ['name' => self::$appName, 'version' => self::$version],
                            'spans' => [
                                [
                                    'traceId' => $traceId,
                                    'spanId' => $spanId,
                                    'name' => "{$method} {$route}",
                                    'kind' => 2, // SPAN_KIND_SERVER
                                    'startTimeUnixNano' => (string)$startNs,
                                    'endTimeUnixNano' => (string)$nowNs,
                                    'attributes' => [
                                        ['key' => 'http.method', 'value' => ['stringValue' => $method]],
                                        ['key' => 'http.route', 'value' => ['stringValue' => $route]],
                                        ['key' => 'http.target', 'value' => ['stringValue' => $path]],
                                        ['key' => 'http.status_code', 'value' => ['intValue' => $statusCode]],
                                        ['key' => 'journey', 'value' => ['stringValue' => $journey]],
                                        ['key' => 'status', 'value' => ['stringValue' => $status]],
                                        ['key' => 'duration_s', 'value' => ['doubleValue' => $durationSec]],
                                    ],
                                    'status' => [
                                        'code' => ($statusCode >= 400) ? 2 : 1, // 1=OK, 2=ERROR
                                    ],
                                ],
                            ],
                        ],
                    ],
                ],
            ],
        ];

        // 2. OTLP Metrics Payload
        $metricsPayload = [
            'resourceMetrics' => [
                [
                    'resource' => [
                        'attributes' => [
                            ['key' => 'service.name', 'value' => ['stringValue' => self::$appName]],
                            ['key' => 'service.version', 'value' => ['stringValue' => self::$version]],
                            ['key' => 'language', 'value' => ['stringValue' => self::$language]],
                        ],
                    ],
                    'scopeMetrics' => [
                        [
                            'scope' => ['name' => self::$appName, 'version' => self::$version],
                            'metrics' => [
                                [
                                    'name' => 'app.requests.total',
                                    'description' => 'Total number of requests',
                                    'sum' => [
                                        'aggregationTemporality' => 1, // DELTA
                                        'isMonotonic' => true,
                                        'dataPoints' => [
                                            [
                                                'asInt' => 1,
                                                'timeUnixNano' => (string)$nowNs,
                                                'attributes' => [
                                                    ['key' => 'endpoint', 'value' => ['stringValue' => $path]],
                                                    ['key' => 'route', 'value' => ['stringValue' => $route]],
                                                    ['key' => 'method', 'value' => ['stringValue' => $method]],
                                                    ['key' => 'status', 'value' => ['stringValue' => $status]],
                                                    ['key' => 'status_code', 'value' => ['stringValue' => (string)$statusCode]],
                                                    ['key' => 'journey', 'value' => ['stringValue' => $journey]],
                                                    ['key' => 'language', 'value' => ['stringValue' => self::$language]],
                                                ],
                                            ],
                                        ],
                                    ],
                                ],
                                [
                                    'name' => ($statusCode >= 400) ? 'app.requests.errors' : 'app.requests.success',
                                    'description' => ($statusCode >= 400) ? 'Total number of failed requests' : 'Total number of successful requests',
                                    'sum' => [
                                        'aggregationTemporality' => 1,
                                        'isMonotonic' => true,
                                        'dataPoints' => [
                                            [
                                                'asInt' => 1,
                                                'timeUnixNano' => (string)$nowNs,
                                                'attributes' => [
                                                    ['key' => 'endpoint', 'value' => ['stringValue' => $path]],
                                                    ['key' => 'status_code', 'value' => ['stringValue' => (string)$statusCode]],
                                                    ['key' => 'language', 'value' => ['stringValue' => self::$language]],
                                                ],
                                            ],
                                        ],
                                    ],
                                ],
                            ],
                        ],
                    ],
                ],
            ],
        ];

        self::postJson(self::$otlpEndpoint . '/v1/traces', $tracesPayload);
        self::postJson(self::$otlpEndpoint . '/v1/metrics', $metricsPayload);
    }

    private static function postJson(string $url, array $data): void {
        if (!function_exists('curl_init')) {
            return;
        }

        $json = json_encode($data);
        $ch = curl_init($url);
        curl_setopt_array($ch, [
            CURLOPT_POST => true,
            CURLOPT_POSTFIELDS => $json,
            CURLOPT_HTTPHEADER => [
                'Content-Type: application/json',
                'User-Agent: observability-php-app/1.0.1',
            ],
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_TIMEOUT_MS => 250,
            CURLOPT_CONNECTTIMEOUT_MS => 150,
            CURLOPT_NOSIGNAL => 1,
        ]);
        curl_exec($ch);
        curl_close($ch);
    }
}
