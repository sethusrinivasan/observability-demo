package main

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/url"
	"os"
	"runtime"
	"strconv"
	"strings"
	"time"

	_ "github.com/lib/pq"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/exporters/otlp/otlpmetric/otlpmetricgrpc"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracegrpc"
	"go.opentelemetry.io/otel/metric"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	semconv "go.opentelemetry.io/otel/semconv/v1.26.0"
	"go.opentelemetry.io/otel/trace"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
)

const (
	AppName  = "observability-go-app"
	Version  = "1.0.1"
	Language = "golang"
)

var (
	tracer                trace.Tracer
	meter                 metric.Meter
	requestCounter        metric.Int64Counter
	requestSuccessCounter metric.Int64Counter
	requestErrorCounter   metric.Int64Counter
	requestDuration       metric.Float64Histogram
	db                    *sql.DB
)

func getEnv(key, defaultVal string) string {
	if val := os.Getenv(key); val != "" {
		return val
	}
	return defaultVal
}

func initDb() (*sql.DB, error) {
	pgHost := getEnv("POSTGRES_HOST", "postgres")
	pgPort := getEnv("POSTGRES_PORT", "5432")
	pgDB := getEnv("POSTGRES_DB", "observability")
	pgUser := getEnv("POSTGRES_USER", "observability")
	pgPass := getEnv("POSTGRES_PASSWORD", "observability")

	connStr := fmt.Sprintf("host=%s port=%s user=%s password=%s dbname=%s sslmode=disable connect_timeout=5",
		pgHost, pgPort, pgUser, pgPass, pgDB)

	databaseUrl := os.Getenv("DATABASE_URL")
	if databaseUrl != "" {
		connStr = databaseUrl
	}

	var d *sql.DB
	var err error
	for attempt := 1; attempt <= 10; attempt++ {
		d, err = sql.Open("postgres", connStr)
		if err == nil {
			if err = d.Ping(); err == nil {
				log.Println("[PostgreSQL] Connected successfully")
				break
			}
		}
		log.Printf("[PostgreSQL] Connection attempt %d/10 failed (%v), retrying in 2s...", attempt, err)
		time.Sleep(2 * time.Second)
	}
	if err != nil {
		return nil, fmt.Errorf("could not connect to postgres: %w", err)
	}

	createTableSQL := `
	CREATE TABLE IF NOT EXISTS audit_logs (
		id SERIAL PRIMARY KEY,
		timestamp TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
		endpoint VARCHAR(255) NOT NULL,
		status_code INTEGER NOT NULL,
		details JSONB
	);`
	if _, err := d.Exec(createTableSQL); err != nil {
		log.Printf("[PostgreSQL] Warning creating audit_logs table: %v", err)
	} else {
		log.Println("[PostgreSQL] audit_logs table initialized")
	}

	return d, nil
}

func initOtel(ctx context.Context) (func(), error) {
	endpoint := getEnv("OTEL_EXPORTER_OTLP_ENDPOINT", "otel-collector:4317")
	endpoint = strings.TrimPrefix(endpoint, "http://")
	endpoint = strings.TrimPrefix(endpoint, "https://")

	res, err := resource.New(ctx,
		resource.WithAttributes(
			semconv.ServiceName(AppName),
			semconv.ServiceVersion(Version),
			semconv.DeploymentEnvironment("local-dev"),
			attribute.String("language", Language),
		),
	)
	if err != nil {
		return nil, fmt.Errorf("failed to create resource: %w", err)
	}

	traceExp, err := otlptracegrpc.New(ctx,
		otlptracegrpc.WithInsecure(),
		otlptracegrpc.WithEndpoint(endpoint),
		otlptracegrpc.WithDialOption(grpc.WithTransportCredentials(insecure.NewCredentials())),
	)
	if err != nil {
		log.Printf("[OTel] Trace exporter warning: %v", err)
	}

	tp := sdktrace.NewTracerProvider(
		sdktrace.WithBatcher(traceExp),
		sdktrace.WithResource(res),
	)
	otel.SetTracerProvider(tp)
	tracer = tp.Tracer(AppName)

	metricExp, err := otlpmetricgrpc.New(ctx,
		otlpmetricgrpc.WithInsecure(),
		otlpmetricgrpc.WithEndpoint(endpoint),
		otlpmetricgrpc.WithDialOption(grpc.WithTransportCredentials(insecure.NewCredentials())),
	)
	if err != nil {
		log.Printf("[OTel] Metric exporter warning: %v", err)
	}

	mp := sdkmetric.NewMeterProvider(
		sdkmetric.WithReader(sdkmetric.NewPeriodicReader(metricExp, sdkmetric.WithInterval(5*time.Second))),
		sdkmetric.WithResource(res),
	)
	otel.SetMeterProvider(mp)
	meter = mp.Meter(AppName)

	requestCounter, _ = meter.Int64Counter("app.requests.total",
		metric.WithDescription("Total number of requests"))
	requestSuccessCounter, _ = meter.Int64Counter("app.requests.success",
		metric.WithDescription("Total number of successful requests"))
	requestErrorCounter, _ = meter.Int64Counter("app.requests.errors",
		metric.WithDescription("Total number of failed requests"))
	requestDuration, _ = meter.Float64Histogram("app.request.duration",
		metric.WithDescription("Request duration in seconds"),
		metric.WithUnit("s"))

	// System & Process Gauges
	systemAttrs := metric.WithAttributes(
		attribute.String("language", Language),
		attribute.String("service.version", Version),
	)

	_, _ = meter.Float64ObservableGauge("app.system.loadavg.1m",
		metric.WithDescription("System load average (1 minute)"),
		metric.WithFloat64Callback(func(_ context.Context, obs metric.Float64Observer) error {
			obs.Observe(getSystemLoadAvg(), systemAttrs)
			return nil
		}),
	)

	_, _ = meter.Int64ObservableGauge("app.process.memory.rss",
		metric.WithDescription("Process memory usage in bytes"),
		metric.WithUnit("bytes"),
		metric.WithInt64Callback(func(_ context.Context, obs metric.Int64Observer) error {
			var m runtime.MemStats
			runtime.ReadMemStats(&m)
			obs.Observe(int64(m.Sys), systemAttrs)
			return nil
		}),
	)

	shutdown := func() {
		_ = tp.Shutdown(context.Background())
		_ = mp.Shutdown(context.Background())
	}
	return shutdown, nil
}

func getSystemLoadAvg() float64 {
	data, err := os.ReadFile("/proc/loadavg")
	if err != nil {
		return 0.0
	}
	fields := strings.Fields(string(data))
	if len(fields) > 0 {
		val, err := strconv.ParseFloat(fields[0], 64)
		if err == nil {
			return val
		}
	}
	return 0.0
}

func withContextMap(extra map[string]interface{}) map[string]interface{} {
	resp := map[string]interface{}{
		"app_name":  AppName,
		"version":   Version,
		"language":  Language,
		"timestamp": time.Now().UTC().Format(time.RFC3339),
	}
	for k, v := range extra {
		resp[k] = v
	}
	return resp
}

type statusResponseWriter struct {
	http.ResponseWriter
	statusCode int
}

func (w *statusResponseWriter) WriteHeader(code int) {
	w.statusCode = code
	w.ResponseWriter.WriteHeader(code)
}

func telemetryMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		rw := &statusResponseWriter{ResponseWriter: w, statusCode: http.StatusOK}

		next.ServeHTTP(rw, r)

		durationSec := time.Since(start).Seconds()
		path := r.URL.Path
		method := r.Method
		code := rw.statusCode
		route := path
		if path == "/" {
			route = "home"
		} else if strings.HasPrefix(path, "/compute") {
			route = "compute"
		} else if path == "/auditlog" {
			route = "auditlog"
		} else if path == "/auditlog/stats" {
			route = "auditlog_stats"
		} else if path == "/eval" {
			route = "eval_expression"
		}

		journey := "other"
		if strings.HasPrefix(path, "/compute") {
			journey = "compute"
		} else if path == "/" {
			journey = "home"
		}

		status := "success"
		if code >= 400 {
			status = "error"
		}

		ctx := r.Context()
		metricAttrs := metric.WithAttributes(
			attribute.String("endpoint", path),
			attribute.String("route", route),
			attribute.String("method", method),
			attribute.String("status", status),
			attribute.String("status_code", strconv.Itoa(code)),
			attribute.String("journey", journey),
			attribute.String("language", Language),
		)

		if requestCounter != nil {
			requestCounter.Add(ctx, 1, metricAttrs)
			if status == "error" {
				requestErrorCounter.Add(ctx, 1, metricAttrs)
			} else {
				requestSuccessCounter.Add(ctx, 1, metricAttrs)
			}
			requestDuration.Record(ctx, durationSec, metricAttrs)
		}
	})
}

func writeJSON(w http.ResponseWriter, statusCode int, data interface{}) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(statusCode)
	_ = json.NewEncoder(w).Encode(data)
}

// ---------------------------------------------------------------------------
// Route Handlers
// ---------------------------------------------------------------------------

func handleHome(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/" {
		http.NotFound(w, r)
		return
	}
	_, span := tracer.Start(r.Context(), "home-endpoint")
	defer span.End()

	html := fmt.Sprintf(`<!doctype html><html lang='en'><head><meta charset='utf-8'><title>Observability Lab (Go)</title></head><body><h1>Hello from Observability Lab (Go)!</h1><p><strong>App:</strong> %s | <strong>Version:</strong> %s | <strong>Language:</strong> %s | <strong>Framework:</strong> net/http</p><p>Discover the compute endpoint with a number:</p><ul><li><a href='/compute/5'>Compute 5</a></li><li><a href='/compute/10'>Compute 10</a></li><li><a href='/compute/20'>Compute 20</a></li></ul><p>Actuator Probes:</p><ul><li><a href='/actuator/health'>Health</a></li><li><a href='/actuator/health/liveness'>Liveness</a></li><li><a href='/actuator/health/readiness'>Readiness</a></li><li><a href='/actuator/info'>Info</a></li></ul></body></html>`,
		AppName, Version, Language)

	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write([]byte(html))
}

func handleVersion(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, withContextMap(nil))
}

func handleSelfTest(w http.ResponseWriter, r *http.Request) {
	testsRun := 0
	failures := 0

	testsRun++
	if fibonacci(5) != 5 || fibonacci(10) != 55 {
		failures++
	}

	testsRun++
	val, err := evaluate("2+3*4")
	if err != nil || val != 14.0 {
		failures++
	}
	val, err = evaluate("2^10")
	if err != nil || val != 1024.0 {
		failures++
	}

	testsRun++
	_, err = evaluate("5/0")
	if err == nil {
		failures++
	}

	success := failures == 0
	statusCode := http.StatusOK
	if !success {
		statusCode = http.StatusInternalServerError
	}

	writeJSON(w, statusCode, withContextMap(map[string]interface{}{
		"tests_run": testsRun,
		"success":   success,
		"failures":  failures,
	}))
}

func handleCompute(w http.ResponseWriter, r *http.Request) {
	paramStr := strings.TrimPrefix(r.URL.Path, "/compute/")
	n, err := strconv.Atoi(paramStr)
	if err != nil || n > 25 || n < 0 {
		writeJSON(w, http.StatusBadRequest, withContextMap(map[string]interface{}{
			"error": "Value too large, Max is 25 to prevent DoS",
		}))
		return
	}

	ctx, span := tracer.Start(r.Context(), "compute-endpoint")
	span.SetAttributes(attribute.Int("compute.value", n))
	defer span.End()

	res := fibonacci(n)
	_ = ctx
	writeJSON(w, http.StatusOK, withContextMap(map[string]interface{}{
		"input":  n,
		"result": res,
	}))
}

func handleAuditLog(w http.ResponseWriter, r *http.Request) {
	_, span := tracer.Start(r.Context(), "auditlog-endpoint")
	defer span.End()

	if db == nil {
		writeJSON(w, http.StatusInternalServerError, withContextMap(map[string]interface{}{
			"status":  "error",
			"message": "database not connected",
		}))
		return
	}

	endpoint := "/auditlog"
	statusCode := http.StatusCreated
	details := `{"source": "cli"}`

	_, err := db.Exec(`INSERT INTO audit_logs (endpoint, status_code, details) VALUES ($1, $2, $3::jsonb)`,
		endpoint, statusCode, details)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, withContextMap(map[string]interface{}{
			"status":  "error",
			"message": err.Error(),
		}))
		return
	}

	writeJSON(w, http.StatusCreated, withContextMap(map[string]interface{}{
		"status": "ok",
	}))
}

func handleAuditLogStats(w http.ResponseWriter, r *http.Request) {
	_, span := tracer.Start(r.Context(), "auditlog-stats-endpoint")
	defer span.End()

	if db == nil {
		writeJSON(w, http.StatusInternalServerError, withContextMap(map[string]interface{}{
			"status":  "error",
			"message": "database not connected",
		}))
		return
	}

	var totalRows int
	err := db.QueryRow(`SELECT count(*) AS total_rows FROM audit_logs`).Scan(&totalRows)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, withContextMap(map[string]interface{}{
			"status":  "error",
			"message": err.Error(),
		}))
		return
	}

	writeJSON(w, http.StatusOK, withContextMap(map[string]interface{}{
		"total_rows": totalRows,
	}))
}

func handleEval(w http.ResponseWriter, r *http.Request) {
	expr := r.URL.Query().Get("expr")
	if expr == "" && r.Method == http.MethodPost {
		bodyBytes, err := io.ReadAll(r.Body)
		if err == nil && len(bodyBytes) > 0 {
			var bodyData struct {
				Expr string `json:"expr"`
			}
			if json.Unmarshal(bodyBytes, &bodyData) == nil && bodyData.Expr != "" {
				expr = bodyData.Expr
			} else {
				form, err := url.ParseQuery(string(bodyBytes))
				if err == nil && form.Get("expr") != "" {
					expr = form.Get("expr")
				}
			}
		}
	}

	if strings.TrimSpace(expr) == "" {
		writeJSON(w, http.StatusBadRequest, withContextMap(map[string]interface{}{
			"error": "Missing 'expr' parameter",
		}))
		return
	}

	ctx, span := tracer.Start(r.Context(), "eval-endpoint")
	span.SetAttributes(attribute.String("eval.expression", expr))
	defer span.End()
	_ = ctx

	val, err := evaluate(expr)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, withContextMap(map[string]interface{}{
			"expression": expr,
			"error":      err.Error(),
		}))
		return
	}

	writeJSON(w, http.StatusOK, withContextMap(map[string]interface{}{
		"expression": expr,
		"result":     val,
	}))
}

// ---------------------------------------------------------------------------
// Actuator Probes
// ---------------------------------------------------------------------------

func handleHealth(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "UP"})
}

func handleLiveness(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "UP"})
}

func handleReadiness(w http.ResponseWriter, r *http.Request) {
	if db != nil {
		if err := db.Ping(); err != nil {
			writeJSON(w, http.StatusServiceUnavailable, map[string]interface{}{
				"status": "DOWN",
				"error":  err.Error(),
			})
			return
		}
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "UP"})
}

func handleInfo(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]interface{}{
		"app": map[string]string{
			"name":     AppName,
			"version":  Version,
			"language": Language,
		},
	})
}

func handlePlainHealth(w http.ResponseWriter, r *http.Request) {
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write([]byte("OK"))
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

func main() {
	portStr := getEnv("PORT", "8080")
	ctx := context.Background()

	shutdownOtel, err := initOtel(ctx)
	if err != nil {
		log.Printf("[OTel] Init error: %v", err)
	} else {
		defer shutdownOtel()
		log.Println("[OTel] OpenTelemetry initialized")
	}

	database, err := initDb()
	if err != nil {
		log.Printf("[PostgreSQL] Error: %v", err)
	} else {
		db = database
		defer db.Close()
	}

	mux := http.NewServeMux()

	mux.HandleFunc("/", handleHome)
	mux.HandleFunc("/version", handleVersion)
	mux.HandleFunc("/selftest", handleSelfTest)
	mux.HandleFunc("/compute/", handleCompute)
	mux.HandleFunc("/auditlog", handleAuditLog)
	mux.HandleFunc("/auditlog/stats", handleAuditLogStats)
	mux.HandleFunc("/eval", handleEval)

	// Actuator Probes
	mux.HandleFunc("/actuator/health", handleHealth)
	mux.HandleFunc("/actuator/health/liveness", handleLiveness)
	mux.HandleFunc("/actuator/health/readiness", handleReadiness)
	mux.HandleFunc("/actuator/info", handleInfo)
	mux.HandleFunc("/health", handlePlainHealth)

	server := &http.Server{
		Addr:    ":" + portStr,
		Handler: telemetryMiddleware(mux),
	}

	log.Printf("[HTTP] %s listening on http://0.0.0.0:%s\n", AppName, portStr)
	if err := server.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		log.Fatalf("Server error: %v", err)
	}
}
