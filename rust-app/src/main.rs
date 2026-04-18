use axum::{
    extract::{Path, Query, State},
    http::StatusCode,
    middleware::{self, Next},
    response::{Html, IntoResponse, Json, Response},
    routing::{get, post},
    Router,
};
use opentelemetry::{global, KeyValue};
use opentelemetry::metrics::{Counter, Histogram, ObservableGauge};
use opentelemetry_otlp::WithExportConfig;
use serde::Deserialize;
use sqlx::postgres::PgPoolOptions;
use sqlx::{Pool, Postgres};
use std::net::SocketAddr;
use std::sync::Arc;
use std::time::Instant;
use tracing::info;

#[derive(Clone)]
struct AppState {
    db: Pool<Postgres>,
    request_counter: Counter<u64>,
    request_success_counter: Counter<u64>,
    request_error_counter: Counter<u64>,
    request_duration: Histogram<f64>,
    // Keep gauges alive so the meter doesn't drop them
    _gauges: Arc<Vec<ObservableGauge<f64>>>,
}

#[derive(Deserialize)]
struct EvalQuery {
    expr: Option<String>,
}

#[derive(Deserialize)]
struct EvalBody {
    expr: Option<String>,
}

// ── cgroup helpers ────────────────────────────────────────────────────────────

fn read_cgroup_u64(v2: &str, v1: &str) -> f64 {
    if let Ok(s) = std::fs::read_to_string(v2) {
        let s = s.trim();
        if let Ok(n) = s.parse::<u64>() { return n as f64; }
    }
    if let Ok(s) = std::fs::read_to_string(v1) {
        if let Ok(n) = s.trim().parse::<u64>() { return n as f64; }
    }
    0.0
}

fn cgroup_memory_current() -> f64 {
    read_cgroup_u64("/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory/memory.usage_in_bytes")
}

fn cgroup_memory_limit() -> f64 {
    read_cgroup_u64("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes")
}

fn cgroup_memory_percent() -> f64 {
    let limit = cgroup_memory_limit();
    let current = cgroup_memory_current();
    if limit > 0.0 { current * 100.0 / limit } else { 0.0 }
}

fn cgroup_cpu_usage_ns() -> f64 {
    if let Ok(s) = std::fs::read_to_string("/sys/fs/cgroup/cpu.stat") {
        for line in s.lines() {
            if let Some(rest) = line.strip_prefix("usage_usec ") {
                if let Ok(n) = rest.trim().parse::<u64>() {
                    return (n * 1000) as f64;
                }
            }
        }
    }
    if let Ok(s) = std::fs::read_to_string("/sys/fs/cgroup/cpuacct/cpuacct.usage") {
        if let Ok(n) = s.trim().parse::<u64>() { return n as f64; }
    }
    0.0
}

fn process_memory_rss() -> f64 {
    if let Ok(s) = std::fs::read_to_string("/proc/self/status") {
        for line in s.lines() {
            if let Some(rest) = line.strip_prefix("VmRSS:") {
                if let Ok(kb) = rest.trim().trim_end_matches(" kB").trim().parse::<u64>() {
                    return (kb * 1024) as f64;
                }
            }
        }
    }
    0.0
}

fn process_cpu_seconds() -> f64 {
    if let Ok(s) = std::fs::read_to_string("/proc/self/stat") {
        let fields: Vec<&str> = s.split_whitespace().collect();
        if fields.len() >= 15 {
            let utime: u64 = fields[13].parse().unwrap_or(0);
            let stime: u64 = fields[14].parse().unwrap_or(0);
            return (utime + stime) as f64 / 100.0; // jiffies → seconds (HZ=100)
        }
    }
    0.0
}

fn system_loadavg_1m() -> f64 {
    if let Ok(s) = std::fs::read_to_string("/proc/loadavg") {
        if let Some(first) = s.split_whitespace().next() {
            if let Ok(f) = first.parse::<f64>() { return f; }
        }
    }
    0.0
}

// ── main ──────────────────────────────────────────────────────────────────────

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    // Initializing subscriber later with OTel layer

    let otlp_endpoint = std::env::var("OTEL_EXPORTER_OTLP_ENDPOINT")
        .unwrap_or_else(|_| "http://otel-collector:4317".to_string());

    let meter_provider = opentelemetry_otlp::new_pipeline()
        .metrics(opentelemetry_sdk::runtime::Tokio)
        .with_exporter(
            opentelemetry_otlp::new_exporter()
                .tonic()
                .with_endpoint(&otlp_endpoint),
        )
        .with_resource(opentelemetry_sdk::Resource::new(vec![
            KeyValue::new("service.name", "observability-rust-app"),
            KeyValue::new("language", "rust"),
        ]))
        .build()?;
    global::set_meter_provider(meter_provider.clone());

    let log_provider = opentelemetry_otlp::new_pipeline()
        .logging()
        .with_resource(opentelemetry_sdk::Resource::new(vec![
            KeyValue::new("service.name", "observability-rust-app"),
            KeyValue::new("language", "rust"),
        ]))
        .with_exporter(
            opentelemetry_otlp::new_exporter()
                .tonic()
                .with_endpoint(&otlp_endpoint),
        )
        .build_log_handler(opentelemetry_sdk::runtime::Tokio)?;

    let otel_log_appender = opentelemetry_appender_tracing::layer::OpenTelemetryTracingBridge::new(&log_provider);

    use tracing_subscriber::layer::SubscriberExt;
    let subscriber = tracing_subscriber::Registry::default()
        .with(tracing_subscriber::fmt::Layer::default())
        .with(otel_log_appender);
    tracing::subscriber::set_global_default(subscriber)?;

    let meter = global::meter("observability-rust-app");

    let request_counter = meter
        .u64_counter("app.requests.total")
        .with_description("Total number of requests")
        .init();
    let request_success_counter = meter
        .u64_counter("app.requests.success")
        .with_description("Total number of successful requests")
        .init();
    let request_error_counter = meter
        .u64_counter("app.requests.errors")
        .with_description("Total number of failed requests")
        .init();
    let request_duration = meter
        .f64_histogram("app.request.duration")
        .with_description("Request duration in seconds")
        .with_unit(opentelemetry::metrics::Unit::new("s"))
        .init();

    // Observable gauges – keep handles alive in Arc so they aren't dropped
    let mut gauges: Vec<ObservableGauge<f64>> = Vec::new();

    let rust_lang = vec![KeyValue::new("language", "rust")];

    let rl = rust_lang.clone();
    gauges.push(meter.f64_observable_gauge("app.process.memory.rss")
        .with_description("Process RSS memory in bytes")
        .with_callback(move |obs| obs.observe(process_memory_rss(), &rl))
        .init());

    let rl = rust_lang.clone();
    gauges.push(meter.f64_observable_gauge("app.process.cpu.seconds")
        .with_description("Process CPU time in seconds")
        .with_callback(move |obs| obs.observe(process_cpu_seconds(), &rl))
        .init());

    let rl = rust_lang.clone();
    gauges.push(meter.f64_observable_gauge("app.system.loadavg.1m")
        .with_description("System load average (1 minute)")
        .with_callback(move |obs| obs.observe(system_loadavg_1m(), &rl))
        .init());

    let rl = rust_lang.clone();
    gauges.push(meter.f64_observable_gauge("app.container.memory.current")
        .with_description("Container memory current usage in bytes")
        .with_callback(move |obs| obs.observe(cgroup_memory_current(), &rl))
        .init());

    let rl = rust_lang.clone();
    gauges.push(meter.f64_observable_gauge("app.container.memory.limit")
        .with_description("Container memory limit in bytes")
        .with_callback(move |obs| obs.observe(cgroup_memory_limit(), &rl))
        .init());

    let rl = rust_lang.clone();
    gauges.push(meter.f64_observable_gauge("app.container.memory.percent")
        .with_description("Container memory usage as a percent of limit")
        .with_callback(move |obs| obs.observe(cgroup_memory_percent(), &rl))
        .init());

    let rl = rust_lang.clone();
    gauges.push(meter.f64_observable_gauge("app.container.cpu.usage.ns")
        .with_description("Container CPU usage in nanoseconds")
        .with_callback(move |obs| obs.observe(cgroup_cpu_usage_ns(), &rl))
        .init());

    let db_url = std::env::var("DATABASE_URL")
        .unwrap_or_else(|_| "postgres://observability:observability@postgres:5432/observability".to_string());
    let db = PgPoolOptions::new()
        .max_connections(5)
        .connect(&db_url)
        .await?;

    let state = AppState {
        db,
        request_counter,
        request_success_counter,
        request_error_counter,
        request_duration,
        _gauges: Arc::new(gauges),
    };

    let app = Router::new()
        .route("/", get(home))
        .route("/compute/:n", get(compute))
        .route("/auditlog", get(audit_log).post(audit_log))
        .route("/auditlog/stats", get(audit_log_stats))
        .route("/eval", get(eval_get).post(eval_post))
        .with_state(state);

    let addr = SocketAddr::from(([0, 0, 0, 0], 8081));
    info!("Rust App listening on {}", addr);
    let listener = tokio::net::TcpListener::bind(addr).await?;
    axum::serve(listener, app).await?;

    Ok(())
}

// ── helpers ───────────────────────────────────────────────────────────────────

fn record_metrics(state: &AppState, endpoint: &str, route: &str, status: &str, journey: &str, duration: f64) {
    let labels = [
        KeyValue::new("endpoint", endpoint.to_string()),
        KeyValue::new("route", route.to_string()),
        KeyValue::new("language", "rust"),
        KeyValue::new("status", status.to_string()),
        KeyValue::new("journey", journey.to_string()),
    ];
    state.request_counter.add(1, &labels);
    state.request_duration.record(duration, &labels);
    if status == "error" {
        state.request_error_counter.add(1, &labels);
    } else {
        state.request_success_counter.add(1, &labels);
    }
}

// ── handlers ──────────────────────────────────────────────────────────────────

async fn home(State(state): State<AppState>) -> impl IntoResponse {
    let t = Instant::now();
    let resp = Html("<h1>Hello from Rust</h1>");
    record_metrics(&state, "/", "home", "success", "home", t.elapsed().as_secs_f64());
    resp
}

async fn compute(State(state): State<AppState>, Path(n): Path<usize>) -> impl IntoResponse {
    let t = Instant::now();
    if n > 25 {
        record_metrics(&state, "/compute", "compute", "error", "compute", t.elapsed().as_secs_f64());
        return (StatusCode::BAD_REQUEST, "Too large").into_response();
    }
    let res = fibonacci(n);
    record_metrics(&state, "/compute", "compute", "success", "compute", t.elapsed().as_secs_f64());
    Json(serde_json::json!({"result": res, "language": "rust"})).into_response()
}

async fn audit_log(State(state): State<AppState>) -> impl IntoResponse {
    let t = Instant::now();
    let res = sqlx::query("INSERT INTO audit_logs (endpoint, status_code, details) VALUES ($1, $2, $3)")
        .bind("/auditlog")
        .bind(201)
        .bind(serde_json::json!({"language": "rust"}))
        .execute(&state.db)
        .await;

    match res {
        Ok(_) => {
            record_metrics(&state, "/auditlog", "auditlog", "success", "other", t.elapsed().as_secs_f64());
            (StatusCode::CREATED, "ok").into_response()
        }
        Err(e) => {
            record_metrics(&state, "/auditlog", "auditlog", "error", "other", t.elapsed().as_secs_f64());
            (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()).into_response()
        }
    }
}

async fn audit_log_stats(State(state): State<AppState>) -> impl IntoResponse {
    let t = Instant::now();
    let row: Result<(i64,), _> = sqlx::query_as("SELECT count(*) FROM audit_logs")
        .fetch_one(&state.db)
        .await;
    match row {
        Ok(r) => {
            record_metrics(&state, "/auditlog/stats", "auditlog_stats", "success", "other", t.elapsed().as_secs_f64());
            Json(serde_json::json!({"total_rows": r.0})).into_response()
        }
        Err(e) => {
            record_metrics(&state, "/auditlog/stats", "auditlog_stats", "error", "other", t.elapsed().as_secs_f64());
            (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()).into_response()
        }
    }
}

async fn eval_get(State(state): State<AppState>, Query(q): Query<EvalQuery>) -> impl IntoResponse {
    handle_eval(state, q.expr).await
}

async fn eval_post(State(state): State<AppState>, Json(body): Json<EvalBody>) -> impl IntoResponse {
    handle_eval(state, body.expr).await
}

async fn handle_eval(state: AppState, expr: Option<String>) -> impl IntoResponse {
    let t = Instant::now();
    let expr = match expr {
        Some(e) if !e.is_empty() => e,
        _ => {
            record_metrics(&state, "/eval", "eval_expression", "error", "other", t.elapsed().as_secs_f64());
            return (StatusCode::BAD_REQUEST, "Missing expr").into_response();
        }
    };
    match evaluate(&expr) {
        Ok(res) => {
            record_metrics(&state, "/eval", "eval_expression", "success", "other", t.elapsed().as_secs_f64());
            Json(serde_json::json!({"result": res, "expression": expr})).into_response()
        }
        Err(e) => {
            record_metrics(&state, "/eval", "eval_expression", "error", "other", t.elapsed().as_secs_f64());
            (StatusCode::BAD_REQUEST, e).into_response()
        }
    }
}

// ── fibonacci ─────────────────────────────────────────────────────────────────

fn fibonacci(n: usize) -> usize {
    if n <= 1 { return n; }
    fibonacci(n - 1) + fibonacci(n - 2)
}

// ── expression evaluator ──────────────────────────────────────────────────────

fn evaluate(s: &str) -> Result<f64, String> {
    let mut tokens = s.chars().filter(|c| !c.is_whitespace()).peekable();
    let result = parse_expression(&mut tokens)?;
    if tokens.peek().is_some() {
        return Err(format!("Unexpected character: {}", tokens.next().unwrap()));
    }
    Ok(result)
}

fn parse_expression<I>(tokens: &mut std::iter::Peekable<I>) -> Result<f64, String>
where
    I: Iterator<Item = char>,
{
    let mut x = parse_term(tokens)?;
    loop {
        match tokens.peek() {
            Some('+') => { tokens.next(); x += parse_term(tokens)?; }
            Some('-') => { tokens.next(); x -= parse_term(tokens)?; }
            _ => break,
        }
    }
    Ok(x)
}

fn parse_term<I>(tokens: &mut std::iter::Peekable<I>) -> Result<f64, String>
where
    I: Iterator<Item = char>,
{
    let mut x = parse_factor(tokens)?;
    loop {
        match tokens.peek() {
            Some('*') => { tokens.next(); x *= parse_factor(tokens)?; }
            Some('/') => {
                tokens.next();
                let denom = parse_factor(tokens)?;
                if denom == 0.0 { return Err("Division by zero".to_string()); }
                x /= denom;
            }
            _ => break,
        }
    }
    Ok(x)
}

fn parse_factor<I>(tokens: &mut std::iter::Peekable<I>) -> Result<f64, String>
where
    I: Iterator<Item = char>,
{
    match tokens.peek() {
        Some('+') => { tokens.next(); parse_factor(tokens) }
        Some('-') => { tokens.next(); Ok(-parse_factor(tokens)?) }
        _ => {
            let mut x = parse_primary(tokens)?;
            if let Some('^') = tokens.peek() {
                tokens.next();
                x = x.powf(parse_factor(tokens)?);
            }
            Ok(x)
        }
    }
}

fn parse_primary<I>(tokens: &mut std::iter::Peekable<I>) -> Result<f64, String>
where
    I: Iterator<Item = char>,
{
    match tokens.next() {
        Some('(') => {
            let x = parse_expression(tokens)?;
            if tokens.next() != Some(')') { return Err("Missing ')'".to_string()); }
            Ok(x)
        }
        Some(c) if c.is_ascii_digit() || c == '.' => {
            let mut s = String::new();
            s.push(c);
            while let Some(&next) = tokens.peek() {
                if next.is_ascii_digit() || next == '.' { s.push(tokens.next().unwrap()); } else { break; }
            }
            s.parse::<f64>().map_err(|e| e.to_string())
        }
        Some(c) if c.is_ascii_alphabetic() => {
            let mut func = String::new();
            func.push(c);
            while let Some(&next) = tokens.peek() {
                if next.is_ascii_alphanumeric() { func.push(tokens.next().unwrap()); } else { break; }
            }
            if tokens.next() != Some('(') { return Err(format!("Expected '(' after {}", func)); }
            let x = parse_expression(tokens)?;
            if tokens.next() != Some(')') { return Err(format!("Missing ')' after {}", func)); }
            match func.as_str() {
                "sqrt" => Ok(x.sqrt()),
                "sin"  => Ok(x.to_radians().sin()),
                "cos"  => Ok(x.to_radians().cos()),
                _      => Err(format!("Unknown function: {}", func)),
            }
        }
        Some(c) => Err(format!("Unexpected character: {}", c)),
        None => Err("Unexpected end of expression".to_string()),
    }
}
