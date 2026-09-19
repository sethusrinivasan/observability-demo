package com.example.observability;


import io.opentelemetry.api.OpenTelemetry;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.common.Attributes;
import io.opentelemetry.api.metrics.DoubleHistogram;
import io.opentelemetry.api.metrics.LongCounter;
import io.opentelemetry.api.metrics.Meter;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.Tracer;
import io.opentelemetry.sdk.autoconfigure.AutoConfiguredOpenTelemetrySdk;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Component;
import org.springframework.web.bind.annotation.*;
import org.springframework.web.servlet.HandlerInterceptor;
import org.springframework.web.servlet.config.annotation.InterceptorRegistry;
import org.springframework.web.servlet.config.annotation.WebMvcConfigurer;

import java.lang.management.ManagementFactory;
import java.lang.management.MemoryMXBean;
import java.lang.management.OperatingSystemMXBean;
import java.nio.file.Files;
import java.nio.file.Paths;
import java.util.HashMap;
import java.util.Map;

import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.assertEquals;
import org.junit.platform.launcher.Launcher;
import org.junit.platform.launcher.LauncherDiscoveryRequest;
import org.junit.platform.launcher.core.LauncherDiscoveryRequestBuilder;
import org.junit.platform.launcher.core.LauncherFactory;
import org.junit.platform.engine.discovery.DiscoverySelectors;
import org.junit.platform.launcher.listeners.SummaryGeneratingListener;
import org.junit.platform.launcher.listeners.TestExecutionSummary;

class AppSelfTest {
    @Test
    void testFibonacci() {
        AppController controller = new AppController();
        assertEquals(5L, controller.fibonacci(5));
        assertEquals(55L, controller.fibonacci(10));
    }

    @Test
    void testEvaluator() {
        AppController controller = new AppController();
        assertEquals(14.0, controller.evaluate("2+3*4"));
        assertEquals(1024.0, controller.evaluate("2^10"));
    }
}

@SpringBootApplication
public class ObservabilityJavaApp {
    public static void main(String[] args) {
        SpringApplication.run(ObservabilityJavaApp.class, args);
    }
}

@Configuration
class OTelConfig {
    @Bean
    public OpenTelemetry openTelemetry() {
        return AutoConfiguredOpenTelemetrySdk.initialize().getOpenTelemetrySdk();
    }

    @Bean
    public Tracer tracer(OpenTelemetry openTelemetry) {
        return openTelemetry.getTracer("com.example.observability");
    }

    private long readCgroupLong(String v2path, String v1path) {
        try {
            if (Files.exists(Paths.get(v2path))) {
                String val = Files.readString(Paths.get(v2path)).trim();
                return val.matches("\\d+") ? Long.parseLong(val) : 0L;
            }
            if (Files.exists(Paths.get(v1path))) {
                return Long.parseLong(Files.readString(Paths.get(v1path)).trim());
            }
        } catch (Exception ignored) {}
        return 0L;
    }

    private long getCgroupMemoryCurrent() {
        return readCgroupLong("/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory/memory.usage_in_bytes");
    }

    private long getCgroupMemoryLimit() {
        return readCgroupLong("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes");
    }

    private double getCgroupMemoryPercent() {
        long limit = getCgroupMemoryLimit();
        long current = getCgroupMemoryCurrent();
        return (limit > 0) ? (current * 100.0 / limit) : 0.0;
    }

    private long getCgroupCpuUsageNs() {
        try {
            if (Files.exists(Paths.get("/sys/fs/cgroup/cpu.stat"))) {
                for (String line : Files.readAllLines(Paths.get("/sys/fs/cgroup/cpu.stat"))) {
                    if (line.startsWith("usage_usec")) {
                        return Long.parseLong(line.split("\\s+")[1]) * 1000;
                    }
                }
            }
            if (Files.exists(Paths.get("/sys/fs/cgroup/cpuacct/cpuacct.usage"))) {
                return Long.parseLong(Files.readString(Paths.get("/sys/fs/cgroup/cpuacct/cpuacct.usage")).trim());
            }
        } catch (Exception ignored) {}
        return 0L;
    }

    @Bean
    public Meter meter(OpenTelemetry openTelemetry) {
        Meter meter = openTelemetry.getMeter("com.example.observability");
        
        OperatingSystemMXBean osBean = ManagementFactory.getOperatingSystemMXBean();
        MemoryMXBean memBean = ManagementFactory.getMemoryMXBean();

        Attributes javaAttrs = Attributes.of(
            AttributeKey.stringKey("language"), "java",
            AttributeKey.stringKey("service.version"), "1.0.1"
        );

        meter.gaugeBuilder("app.system.loadavg.1m")
                .setDescription("System load average (1 minute)")
                .buildWithCallback(obs -> obs.record(osBean.getSystemLoadAverage(), javaAttrs));

        meter.gaugeBuilder("app.process.memory.rss")
                .setDescription("Process memory usage in bytes")
                .setUnit("bytes")
                .buildWithCallback(obs -> {
                    long used = memBean.getHeapMemoryUsage().getUsed() + memBean.getNonHeapMemoryUsage().getUsed();
                    obs.record(used, javaAttrs);
                });

        meter.gaugeBuilder("app.process.cpu.seconds")
                .setDescription("Process CPU time in seconds")
                .setUnit("seconds")
                .buildWithCallback(obs -> {
                    if (osBean instanceof com.sun.management.OperatingSystemMXBean sunOs) {
                        obs.record(sunOs.getProcessCpuTime() / 1_000_000_000.0, javaAttrs);
                    } else {
                        obs.record(0.0, javaAttrs);
                    }
                });

        meter.gaugeBuilder("app.container.memory.current")
                .setDescription("Container memory current usage in bytes")
                .setUnit("bytes")
                .buildWithCallback(obs -> obs.record(getCgroupMemoryCurrent(), javaAttrs));

        meter.gaugeBuilder("app.container.memory.limit")
                .setDescription("Container memory limit in bytes")
                .setUnit("bytes")
                .buildWithCallback(obs -> obs.record(getCgroupMemoryLimit(), javaAttrs));

        meter.gaugeBuilder("app.container.memory.percent")
                .setDescription("Container memory usage as a percentage of limit")
                .buildWithCallback(obs -> obs.record(getCgroupMemoryPercent(), javaAttrs));

        meter.gaugeBuilder("app.container.cpu.usage.ns")
                .setDescription("Container CPU usage in nanoseconds")
                .buildWithCallback(obs -> obs.record(getCgroupCpuUsageNs(), javaAttrs));

        return meter;
    }

    @Bean
    public LongCounter requestCounter(Meter meter) {
        return meter.counterBuilder("app.requests.total")
                .setDescription("Total number of requests")
                .build();
    }

    @Bean
    public LongCounter requestSuccessCounter(Meter meter) {
        return meter.counterBuilder("app.requests.success")
                .setDescription("Total number of successful requests")
                .build();
    }

    @Bean
    public LongCounter requestErrorCounter(Meter meter) {
        return meter.counterBuilder("app.requests.errors")
                .setDescription("Total number of failed requests")
                .build();
    }

    @Bean
    public DoubleHistogram requestDuration(Meter meter) {
        return meter.histogramBuilder("app.request.duration")
                .setDescription("Request duration in seconds")
                .setUnit("s")
                .build();
    }
}

@Component
class MetricsInterceptor implements HandlerInterceptor {
    @Autowired private LongCounter requestCounter;
    @Autowired private LongCounter requestSuccessCounter;
    @Autowired private LongCounter requestErrorCounter;
    @Autowired private DoubleHistogram requestDuration;

    private static final String START_TIME_ATTR = "startTime";

    @Override
    public boolean preHandle(HttpServletRequest request, HttpServletResponse response, Object handler) {
        request.setAttribute(START_TIME_ATTR, System.nanoTime());
        return true;
    }

    @Override
    public void afterCompletion(HttpServletRequest request, HttpServletResponse response, Object handler, Exception ex) {
        String endpoint = request.getRequestURI();
        int statusCode = response.getStatus();
        String status = (statusCode < 400) ? "success" : "error";
        String journey = endpoint.startsWith("/compute") ? "compute" : endpoint.equals("/") ? "home" : "other";

        String route = endpoint;
        if (endpoint.equals("/")) route = "home";
        else if (endpoint.startsWith("/compute")) route = "compute";
        else if (endpoint.equals("/auditlog")) route = "auditlog";
        else if (endpoint.equals("/auditlog/stats")) route = "auditlog_stats";
        else if (endpoint.equals("/eval")) route = "eval_expression";

        Attributes labels = Attributes.of(
                AttributeKey.stringKey("endpoint"), endpoint,
                AttributeKey.stringKey("route"), route,
                AttributeKey.stringKey("status"), status,
                AttributeKey.stringKey("status_code"), String.valueOf(statusCode),
                AttributeKey.stringKey("journey"), journey,
                AttributeKey.stringKey("language"), "java"
        );

        requestCounter.add(1, labels);
        if (status.equals("error")) {
            requestErrorCounter.add(1, labels);
        } else {
            requestSuccessCounter.add(1, labels);
        }

        Long startNs = (Long) request.getAttribute(START_TIME_ATTR);
        if (startNs != null) {
            double durationSec = (System.nanoTime() - startNs) / 1_000_000_000.0;
            requestDuration.record(durationSec, labels);
        }
    }
}

@Configuration
class WebConfig implements WebMvcConfigurer {
    @Autowired private MetricsInterceptor metricsInterceptor;
    @Override
    public void addInterceptors(InterceptorRegistry registry) {
        registry.addInterceptor(metricsInterceptor);
    }
}

@RestController
class AppController {
    private static final Logger logger = LoggerFactory.getLogger(AppController.class);

    @Autowired private Tracer tracer;
    @Autowired private JdbcTemplate jdbcTemplate;

    private Map<String, Object> withContext(Map<String, Object> data) {
        Map<String, Object> res = new HashMap<>(data);
        res.put("app_name", "observability-java-app");
        res.put("version", "1.0.1");
        res.put("language", "java");
        res.put("timestamp", java.time.Instant.now().toString());
        return res;
    }

    @GetMapping("/")
    public String home() {
        Span span = tracer.spanBuilder("home-endpoint").startSpan();
        try {
            logger.info("Home endpoint called (java)");
            return getHomeHtml();
        } finally {
            span.end();
        }
    }

    @GetMapping("/version")
    public ResponseEntity<?> version() {
        return ResponseEntity.ok(withContext(Map.of()));
    }

    @GetMapping("/selftest")
    public ResponseEntity<?> selftest() {
        LauncherDiscoveryRequest request = LauncherDiscoveryRequestBuilder.request()
            .selectors(DiscoverySelectors.selectClass(AppSelfTest.class))
            .build();
        Launcher launcher = LauncherFactory.create();
        SummaryGeneratingListener listener = new SummaryGeneratingListener();
        launcher.registerTestExecutionListeners(listener);
        launcher.execute(request);
        TestExecutionSummary summary = listener.getSummary();

        return ResponseEntity.status(summary.getTestsFailedCount() == 0 ? HttpStatus.OK : HttpStatus.INTERNAL_SERVER_ERROR)
            .body(withContext(Map.of(
                "tests_run", summary.getTestsFoundCount(),
                "success", summary.getTestsFailedCount() == 0,
                "failures", summary.getTestsFailedCount()
            )));
    }

    @GetMapping("/compute/{n}")
    public ResponseEntity<?> compute(@PathVariable int n) {
        if (n > 25) {
            return ResponseEntity.status(HttpStatus.BAD_REQUEST)
                    .body(withContext(Map.of("error", "Value too large, Max is 25 to prevent DoS")));
        }
        Span span = tracer.spanBuilder("compute-endpoint").startSpan();
        span.setAttribute("compute.value", n);
        try {
            long result = fibonacci(n);
            return ResponseEntity.ok(withContext(Map.of("input", n, "result", result)));
        } finally {
            span.end();
        }
    }

    @RequestMapping(value = "/auditlog", method = {RequestMethod.GET, RequestMethod.POST})
    public ResponseEntity<?> auditLog(HttpServletRequest request) {
        Span span = tracer.spanBuilder("auditlog-endpoint").startSpan();
        try {
            String endpoint = request.getRequestURI();
            int statusCode = 201;
            jdbcTemplate.update(
                    "INSERT INTO audit_logs (endpoint, status_code, details) VALUES (?, ?, ?::jsonb)",
                    endpoint, statusCode, "{\"language\": \"java\", \"service\": \"observability-java-app\"}"
            );
            return ResponseEntity.status(HttpStatus.CREATED).body(withContext(Map.of("status", "ok")));
        } catch (Exception e) {
            return ResponseEntity.status(HttpStatus.INTERNAL_SERVER_ERROR)
                    .body(withContext(Map.of("status", "error", "message", e.getMessage())));
        } finally {
            span.end();
        }
    }

    @GetMapping("/auditlog/stats")
    public ResponseEntity<?> auditLogStats() {
        Span span = tracer.spanBuilder("auditlog-stats-endpoint").startSpan();
        try {
            Map<String, Object> stats = jdbcTemplate.queryForMap("SELECT count(*) AS total_rows FROM audit_logs");
            return ResponseEntity.ok(withContext(stats));
        } finally {
            span.end();
        }
    }

    @RequestMapping(value = "/eval", method = {RequestMethod.GET, RequestMethod.POST})
    public ResponseEntity<?> eval(@RequestParam(required = false) String expr, @RequestBody(required = false) Map<String, String> body) {
        String expressionStr = expr;
        if (expressionStr == null && body != null) {
            expressionStr = body.get("expr");
        }
        if (expressionStr == null || expressionStr.isEmpty()) {
            return ResponseEntity.status(HttpStatus.BAD_REQUEST).body(withContext(Map.of("error", "Missing 'expr' parameter")));
        }
        Span span = tracer.spanBuilder("eval-endpoint").startSpan();
        span.setAttribute("eval.expression", expressionStr);
        try {
            double result = evaluate(expressionStr);
            return ResponseEntity.ok(withContext(Map.of("expression", expressionStr, "result", result)));
        } catch (Exception e) {
            return ResponseEntity.status(HttpStatus.BAD_REQUEST).body(withContext(Map.of("error", e.getMessage())));
        } finally {
            span.end();
        }
    }

    private String getHomeHtml() {
        return "<!doctype html><html lang='en'><head><meta charset='utf-8'><title>Observability Lab (Java)</title></head><body><h1>Hello from Observability Lab (Java)!</h1><p><strong>App:</strong> observability-java-app | <strong>Version:</strong> 1.0.1 | <strong>Language:</strong> java</p><p>Discover the compute endpoint with a number:</p><ul><li><a href='/compute/5'>Compute 5</a></li><li><a href='/compute/10'>Compute 10</a></li><li><a href='/compute/20'>Compute 20</a></li></ul></body></html>";
    }

    public long fibonacci(int n) {
        if (n <= 1) return n;
        return fibonacci(n - 1) + fibonacci(n - 2);
    }

    // Simplified recursive descent parser for expression evaluation (no external libs)
    public double evaluate(final String str) {
        return new Object() {
            int pos = -1, ch;

            void nextChar() {
                ch = (++pos < str.length()) ? str.charAt(pos) : -1;
            }

            boolean eat(int charToEat) {
                while (ch == ' ') nextChar();
                if (ch == charToEat) {
                    nextChar();
                    return true;
                }
                return false;
            }

            double parse() {
                nextChar();
                double x = parseExpression();
                if (pos < str.length()) throw new RuntimeException("Unexpected: " + (char)ch);
                return x;
            }

            double parseExpression() {
                double x = parseTerm();
                for (;;) {
                    if      (eat('+')) x += parseTerm(); // addition
                    else if (eat('-')) x -= parseTerm(); // subtraction
                    else return x;
                }
            }

            double parseTerm() {
                double x = parseFactor();
                for (;;) {
                    if      (eat('*')) x *= parseFactor(); // multiplication
                    else if (eat('/')) x /= parseFactor(); // division
                    else return x;
                }
            }

            double parseFactor() {
                if (eat('+')) return +parseFactor(); // unary plus
                if (eat('-')) return -parseFactor(); // unary minus

                double x;
                int startPos = this.pos;
                if (eat('(')) { // parentheses
                    x = parseExpression();
                    if (!eat(')')) throw new RuntimeException("Missing ')'");
                } else if ((ch >= '0' && ch <= '9') || ch == '.') { // numbers
                    while ((ch >= '0' && ch <= '9') || ch == '.') nextChar();
                    x = Double.parseDouble(str.substring(startPos, this.pos));
                } else if (ch >= 'a' && ch <= 'z') { // functions
                    while (ch >= 'a' && ch <= 'z') nextChar();
                    String func = str.substring(startPos, this.pos);
                    if (eat('(')) {
                        x = parseExpression();
                        if (!eat(')')) throw new RuntimeException("Missing ')' after argument to " + func);
                    } else {
                        x = parseFactor();
                    }
                    if (func.equals("sqrt")) x = Math.sqrt(x);
                    else if (func.equals("sin")) x = Math.sin(Math.toRadians(x));
                    else if (func.equals("cos")) x = Math.cos(Math.toRadians(x));
                    else throw new RuntimeException("Unknown function: " + func);
                } else {
                    throw new RuntimeException("Unexpected: " + (char)ch);
                }

                if (eat('^')) x = Math.pow(x, parseFactor()); // exponentiation

                return x;
            }
        }.parse();
    }
}
