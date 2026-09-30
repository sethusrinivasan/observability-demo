using System.Diagnostics;
using System.Diagnostics.Metrics;
using System.Globalization;
using System.Text.Json;
using System.Text.Json.Nodes;
using DotnetApp;
using Npgsql;
using OpenTelemetry.Metrics;
using OpenTelemetry.Resources;
using OpenTelemetry.Trace;

var builder = WebApplication.CreateBuilder(args);

const string AppName = "observability-dotnet-app";
const string Version = "1.0.1";
const string Language = "csharp";

// ---------------------------------------------------------------------------
// Telemetry & Metrics Definition
// ---------------------------------------------------------------------------
var activitySource = new ActivitySource(AppName, Version);
var meter = new Meter(AppName, Version);

var requestsTotalCounter = meter.CreateCounter<long>("app.requests.total");
var requestsSuccessCounter = meter.CreateCounter<long>("app.requests.success");
var requestsErrorsCounter = meter.CreateCounter<long>("app.requests.errors");
var requestDurationHistogram = meter.CreateHistogram<double>("app.request.duration", unit: "s");

meter.CreateObservableGauge("app.system.loadavg.1m", () =>
{
    try
    {
        if (File.Exists("/proc/loadavg"))
        {
            var content = File.ReadAllText("/proc/loadavg");
            var parts = content.Split(' ', StringSplitOptions.RemoveEmptyEntries);
            if (parts.Length > 0 && double.TryParse(parts[0], NumberStyles.Float, CultureInfo.InvariantCulture, out var load))
            {
                return load;
            }
        }
    }
    catch
    {
        // ignored
    }
    return 0.0;
});

meter.CreateObservableGauge("app.process.memory.rss", () =>
{
    try
    {
        return Process.GetCurrentProcess().WorkingSet64;
    }
    catch
    {
        return 0L;
    }
});

var otelEndpoint = Environment.GetEnvironmentVariable("OTEL_EXPORTER_OTLP_ENDPOINT") ?? "http://otel-collector:4317";
if (!otelEndpoint.StartsWith("http://") && !otelEndpoint.StartsWith("https://"))
{
    otelEndpoint = "http://" + otelEndpoint;
}

builder.Services.AddOpenTelemetry()
    .ConfigureResource(r => r
        .AddService(serviceName: AppName, serviceVersion: Version)
        .AddAttributes([
            new KeyValuePair<string, object>("language", Language),
            new KeyValuePair<string, object>("framework", "ASP.NET Core")
        ]))
    .WithTracing(tracing => tracing
        .AddSource(AppName)
        .AddAspNetCoreInstrumentation(opts =>
        {
            opts.RecordException = true;
        })
        .AddHttpClientInstrumentation()
        .AddOtlpExporter(opt =>
        {
            opt.Endpoint = new Uri(otelEndpoint);
            opt.Protocol = OpenTelemetry.Exporter.OtlpExportProtocol.Grpc;
        }))
    .WithMetrics(metrics => metrics
        .AddMeter(AppName)
        .AddAspNetCoreInstrumentation()
        .AddRuntimeInstrumentation()
        .AddOtlpExporter(opt =>
        {
            opt.Endpoint = new Uri(otelEndpoint);
            opt.Protocol = OpenTelemetry.Exporter.OtlpExportProtocol.Grpc;
        }));

// ---------------------------------------------------------------------------
// Database Setup
// ---------------------------------------------------------------------------
string GetConnectionString()
{
    var databaseUrl = Environment.GetEnvironmentVariable("DATABASE_URL");
    if (!string.IsNullOrEmpty(databaseUrl))
    {
        try
        {
            var uri = new Uri(databaseUrl);
            var userInfo = uri.UserInfo.Split(':');
            var user = userInfo.Length > 0 ? userInfo[0] : "observability";
            var pass = userInfo.Length > 1 ? userInfo[1] : "observability";
            var db = uri.AbsolutePath.TrimStart('/');
            if (string.IsNullOrEmpty(db)) db = "observability";
            var port = uri.Port > 0 ? uri.Port : 5432;

            return new NpgsqlConnectionStringBuilder
            {
                Host = uri.Host,
                Port = port,
                Username = user,
                Password = pass,
                Database = db,
                SslMode = SslMode.Disable,
                Timeout = 5,
                CommandTimeout = 5,
                Pooling = true
            }.ConnectionString;
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[PostgreSQL] Failed parsing DATABASE_URL: {ex.Message}");
        }
    }

    var host = Environment.GetEnvironmentVariable("POSTGRES_HOST") ?? "postgres";
    var portStr = Environment.GetEnvironmentVariable("POSTGRES_PORT") ?? "5432";
    int.TryParse(portStr, out var p);
    if (p <= 0) p = 5432;
    var userEnv = Environment.GetEnvironmentVariable("POSTGRES_USER") ?? "observability";
    var passEnv = Environment.GetEnvironmentVariable("POSTGRES_PASSWORD") ?? "observability";
    var dbEnv = Environment.GetEnvironmentVariable("POSTGRES_DB") ?? "observability";

    return new NpgsqlConnectionStringBuilder
    {
        Host = host,
        Port = p,
        Username = userEnv,
        Password = passEnv,
        Database = dbEnv,
        SslMode = SslMode.Disable,
        Timeout = 5,
        CommandTimeout = 5,
        Pooling = true
    }.ConnectionString;
}

var connString = GetConnectionString();
NpgsqlDataSource? dataSource = null;

try
{
    var dataSourceBuilder = new NpgsqlDataSourceBuilder(connString);
    dataSourceBuilder.EnableDynamicJson();
    dataSource = dataSourceBuilder.Build();

    _ = Task.Run(async () =>
    {
        for (int attempt = 1; attempt <= 15; attempt++)
        {
            try
            {
                await using var conn = await dataSource.OpenConnectionAsync();
                await using var cmd = conn.CreateCommand();
                cmd.CommandText = @"
                    CREATE TABLE IF NOT EXISTS audit_logs (
                        id SERIAL PRIMARY KEY,
                        timestamp TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                        endpoint VARCHAR(255) NOT NULL,
                        status_code INTEGER NOT NULL,
                        details JSONB
                    );";
                await cmd.ExecuteNonQueryAsync();
                Console.WriteLine("[PostgreSQL] Connected and audit_logs table ready");
                break;
            }
            catch (Exception ex)
            {
                Console.WriteLine($"[PostgreSQL] Attempt {attempt}/15 failed ({ex.Message}), retrying in 2s...");
                await Task.Delay(2000);
            }
        }
    });
}
catch (Exception ex)
{
    Console.WriteLine($"[PostgreSQL] Connection setup error: {ex.Message}");
}

var app = builder.Build();

// ---------------------------------------------------------------------------
// Telemetry Middleware
// ---------------------------------------------------------------------------
app.Use(async (context, next) =>
{
    var stopwatch = Stopwatch.StartNew();
    var path = context.Request.Path.Value ?? "/";
    var method = context.Request.Method;

    await next();

    stopwatch.Stop();
    var durationSec = stopwatch.Elapsed.TotalSeconds;
    var statusCode = context.Response.StatusCode;

    var route = path;
    var journey = "other";

    if (path == "/")
    {
        route = "home";
        journey = "home";
    }
    else if (path.StartsWith("/compute"))
    {
        route = "compute";
        journey = "compute";
    }
    else if (path == "/auditlog")
    {
        route = "auditlog";
    }
    else if (path == "/auditlog/stats")
    {
        route = "auditlog_stats";
    }
    else if (path.StartsWith("/eval"))
    {
        route = "eval_expression";
    }

    var status = statusCode >= 400 ? "error" : "success";

    var tags = new TagList
    {
        { "endpoint", path },
        { "route", route },
        { "method", method },
        { "status", status },
        { "status_code", statusCode.ToString() },
        { "journey", journey },
        { "language", Language }
    };

    requestsTotalCounter.Add(1, tags);
    if (status == "error")
    {
        requestsErrorsCounter.Add(1, tags);
    }
    else
    {
        requestsSuccessCounter.Add(1, tags);
    }
    requestDurationHistogram.Record(durationSec, tags);
});

// Helper for standardized JSON response
Dictionary<string, object?> WithContext(Dictionary<string, object?>? extra = null)
{
    var dict = new Dictionary<string, object?>
    {
        ["app_name"] = AppName,
        ["version"] = Version,
        ["language"] = Language,
        ["timestamp"] = DateTime.UtcNow.ToString("o")
    };
    if (extra != null)
    {
        foreach (var (k, v) in extra)
        {
            dict[k] = v;
        }
    }
    return dict;
}

// ---------------------------------------------------------------------------
// Endpoints
// ---------------------------------------------------------------------------

// 1. GET /
app.MapGet("/", (HttpContext context) =>
{
    using var activity = activitySource.StartActivity("home-endpoint");

    var accept = context.Request.Headers.Accept.ToString();
    if (accept.Contains("application/json"))
    {
        return Results.Ok(WithContext(new Dictionary<string, object?>
        {
            ["endpoints"] = new[]
            {
                "/",
                "/version",
                "/compute/:n",
                "/auditlog",
                "/auditlog/stats",
                "/eval",
                "/selftest",
                "/actuator/health",
                "/actuator/info"
            }
        }));
    }

    var html = $@"<!doctype html><html lang='en'><head><meta charset='utf-8'><title>Observability Lab (C# .NET)</title></head><body><h1>Hello from Observability Lab (C# .NET)!</h1><p><strong>App:</strong> {AppName} | <strong>Version:</strong> {Version} | <strong>Language:</strong> {Language} | <strong>Framework:</strong> ASP.NET Core</p><p>Discover the compute endpoint with a number:</p><ul><li><a href='/compute/5'>Compute 5</a></li><li><a href='/compute/10'>Compute 10</a></li><li><a href='/compute/20'>Compute 20</a></li></ul><p>Actuator Probes:</p><ul><li><a href='/actuator/health'>Health</a></li><li><a href='/actuator/health/liveness'>Liveness</a></li><li><a href='/actuator/health/readiness'>Readiness</a></li><li><a href='/actuator/info'>Info</a></li></ul></body></html>";

    return Results.Content(html, "text/html; charset=utf-8");
});

// 2. GET /version
app.MapGet("/version", () => Results.Ok(WithContext()));

// 3. GET /selftest
app.MapGet("/selftest", () =>
{
    var testsRun = 0;
    var failures = 0;

    testsRun++;
    if (Fibonacci.Compute(5) != 5 || Fibonacci.Compute(10) != 55)
    {
        failures++;
    }

    testsRun++;
    try
    {
        var evalRes = Evaluator.Evaluate("2+3*4");
        if (Math.Abs(evalRes - 14.0) > 1e-6) failures++;
    }
    catch
    {
        failures++;
    }

    testsRun++;
    try
    {
        Evaluator.Evaluate("5/0");
        failures++; // Should have thrown
    }
    catch
    {
        // Expected
    }

    var success = failures == 0;
    var code = success ? 200 : 500;
    return Results.Json(WithContext(new Dictionary<string, object?>
    {
        ["tests_run"] = testsRun,
        ["success"] = success,
        ["failures"] = failures
    }), statusCode: code);
});

// 4. GET /compute/{n}
app.MapGet("/compute/{n}", (string n) =>
{
    if (!int.TryParse(n, out var num) || num < 0 || num > 25)
    {
        return Results.Json(WithContext(new Dictionary<string, object?>
        {
            ["error"] = "Value too large, Max is 25 to prevent DoS"
        }), statusCode: 400);
    }

    using var activity = activitySource.StartActivity("compute-endpoint");
    activity?.SetTag("compute.value", num);

    var res = Fibonacci.Compute(num);
    return Results.Ok(WithContext(new Dictionary<string, object?>
    {
        ["input"] = num,
        ["result"] = res
    }));
});

// 5. GET & POST /auditlog
var handleAuditLog = (HttpContext context) =>
{
    using var activity = activitySource.StartActivity("auditlog-endpoint");

    if (dataSource == null)
    {
        return Results.Json(WithContext(new Dictionary<string, object?>
        {
            ["status"] = "error",
            ["message"] = "database not connected"
        }), statusCode: 500);
    }

    try
    {
        using var conn = dataSource.OpenConnection();
        using var cmd = conn.CreateCommand();
        cmd.CommandText = "INSERT INTO audit_logs (endpoint, status_code, details) VALUES ($1, $2, $3::jsonb)";
        cmd.Parameters.AddWithValue("/auditlog");
        cmd.Parameters.AddWithValue(201);
        cmd.Parameters.AddWithValue("{\"source\": \"cli\"}");
        cmd.ExecuteNonQuery();

        return Results.Json(WithContext(new Dictionary<string, object?>
        {
            ["status"] = "ok"
        }), statusCode: 201);
    }
    catch (Exception ex)
    {
        return Results.Json(WithContext(new Dictionary<string, object?>
        {
            ["status"] = "error",
            ["message"] = ex.Message
        }), statusCode: 500);
    }
};

app.MapGet("/auditlog", handleAuditLog);
app.MapPost("/auditlog", handleAuditLog);

// 6. GET /auditlog/stats
app.MapGet("/auditlog/stats", () =>
{
    using var activity = activitySource.StartActivity("auditlog-stats-endpoint");

    if (dataSource == null)
    {
        return Results.Json(WithContext(new Dictionary<string, object?>
        {
            ["status"] = "error",
            ["message"] = "database not connected"
        }), statusCode: 500);
    }

    try
    {
        using var conn = dataSource.OpenConnection();
        using var cmd = conn.CreateCommand();
        cmd.CommandText = "SELECT count(*) FROM audit_logs";
        var totalRows = Convert.ToInt64(cmd.ExecuteScalar());

        return Results.Ok(WithContext(new Dictionary<string, object?>
        {
            ["status"] = "ok",
            ["total_rows"] = totalRows
        }));
    }
    catch (Exception ex)
    {
        return Results.Json(WithContext(new Dictionary<string, object?>
        {
            ["status"] = "error",
            ["message"] = ex.Message
        }), statusCode: 500);
    }
});

// 7. GET & POST /eval
var handleEval = async (HttpContext context) =>
{
    using var activity = activitySource.StartActivity("eval-endpoint");

    string? expr = context.Request.Query["expr"];
    if (string.IsNullOrEmpty(expr) && context.Request.Method == "POST")
    {
        try
        {
            using var reader = new StreamReader(context.Request.Body);
            var body = await reader.ReadToEndAsync();
            if (!string.IsNullOrWhiteSpace(body))
            {
                try
                {
                    var doc = JsonNode.Parse(body);
                    if (doc != null && doc["expr"] != null)
                    {
                        expr = doc["expr"]!.ToString();
                    }
                    else
                    {
                        expr = body;
                    }
                }
                catch
                {
                    expr = body;
                }
            }
        }
        catch
        {
            // ignore
        }
    }

    if (string.IsNullOrWhiteSpace(expr))
    {
        return Results.Json(WithContext(new Dictionary<string, object?>
        {
            ["expression"] = expr ?? "",
            ["error"] = "missing expression"
        }), statusCode: 400);
    }

    activity?.SetTag("eval.expression", expr);

    try
    {
        var val = Evaluator.Evaluate(expr);
        return Results.Ok(WithContext(new Dictionary<string, object?>
        {
            ["expression"] = expr,
            ["result"] = val
        }));
    }
    catch (Exception ex)
    {
        return Results.Json(WithContext(new Dictionary<string, object?>
        {
            ["expression"] = expr,
            ["error"] = ex.Message
        }), statusCode: 400);
    }
};

app.MapGet("/eval", handleEval);
app.MapPost("/eval", handleEval);

// ---------------------------------------------------------------------------
// Chaos Crash Endpoints (Process / Thread Crash)
// ---------------------------------------------------------------------------
var handleCrash = async (HttpContext context) =>
{
    var type = context.Request.Query["type"].ToString();
    if (string.IsNullOrWhiteSpace(type) && context.Request.HasJsonContentType())
    {
        try
        {
            var body = await context.Request.ReadFromJsonAsync<Dictionary<string, string>>();
            if (body != null && body.TryGetValue("type", out var t)) type = t;
        }
        catch { }
    }
    if (string.IsNullOrWhiteSpace(type)) type = "process";
    type = type.ToLowerInvariant().Trim();

    var pid = Environment.ProcessId;
    Console.WriteLine($"[CHAOS] Crash request received: type={type}, pid={pid}");

    if (type == "thread")
    {
        new Thread(() =>
        {
            Console.WriteLine($"[CHAOS] Worker thread crashed on pid {pid}");
        }) { IsBackground = true }.Start();

        return Results.Json(WithContext(new Dictionary<string, object?>
        {
            ["status"] = "crashed",
            ["type"] = "thread",
            ["pid"] = pid,
            ["message"] = "Worker thread crashed"
        }), statusCode: 500);
    }

    // Process crash
    _ = Task.Run(async () =>
    {
        await Task.Delay(50);
        Console.Error.WriteLine($"[FATAL] Chaos process crash executing Environment.FailFast on PID {pid}");
        Environment.FailFast($"Chaos process crash requested on PID {pid}");
    });

    return Results.Ok(WithContext(new Dictionary<string, object?>
    {
        ["status"] = "crashing",
        ["type"] = "process",
        ["pid"] = pid,
        ["message"] = $"Process crash initiated on PID {pid}; .NET runtime terminating"
    }));
};

app.MapGet("/crash", handleCrash);
app.MapPost("/crash", handleCrash);
app.MapGet("/chaos/crash", handleCrash);
app.MapPost("/chaos/crash", handleCrash);

// ---------------------------------------------------------------------------
// Actuator Probes
// ---------------------------------------------------------------------------
app.MapGet("/actuator/health", () => Results.Ok(new { status = "UP" }));
app.MapGet("/actuator/health/liveness", () => Results.Ok(new { status = "UP" }));
app.MapGet("/actuator/health/readiness", () =>
{
    if (dataSource != null)
    {
        try
        {
            using var conn = dataSource.OpenConnection();
            using var cmd = conn.CreateCommand();
            cmd.CommandText = "SELECT 1";
            cmd.ExecuteScalar();
        }
        catch (Exception ex)
        {
            return Results.Json(new { status = "DOWN", error = ex.Message }, statusCode: 503);
        }
    }
    return Results.Ok(new { status = "UP" });
});

app.MapGet("/actuator/info", () => Results.Ok(new
{
    app = new
    {
        name = AppName,
        version = Version,
        language = Language
    }
}));

app.Run();
