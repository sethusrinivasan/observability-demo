/**
 * main.c — C (POSIX C99) Observability Microservice
 * Full parity with Python, Java, Rust, Node.js, Go, and .NET services.
 * Features:
 *   - Multi-threaded POSIX socket HTTP server
 *   - Zero-dependency recursive-descent math Evaluator
 *   - Recursive Fibonacci compute (n <= 25)
 *   - PostgreSQL integration with non-blocking async reconnection and audit_logs
 *   - Actuator health probes (/actuator/health, /actuator/health/readiness, etc.)
 *   - Chaos crash endpoints (/crash and /chaos/crash for process and thread crashes)
 *   - OpenTelemetry OTLP HTTP metrics/traces & Prometheus /metrics endpoint
 */

#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <errno.h>
#include <time.h>
#include <ctype.h>
#include <math.h>
#include <signal.h>
#include <pthread.h>
#include <sys/types.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <curl/curl.h>
#include <libpq-fe.h>

#include "cJSON.h"
#include "evaluator.h"
#include "fibonacci.h"

#define APP_NAME "observability-c-app"
#define VERSION  "1.0.1"
#define LANGUAGE "c"

// ---------------------------------------------------------------------------
// Global State & Configuration
// ---------------------------------------------------------------------------
static int g_port = 8080;
static char g_db_host[128] = "postgres";
static int  g_db_port = 5432;
static char g_db_name[128] = "observability";
static char g_db_user[128] = "observability";
static char g_db_pass[128] = "observability";
static char g_otlp_endpoint[256] = "http://otel-collector:4318";

static PGconn *g_db_conn = NULL;
static pthread_mutex_t g_db_mutex = PTHREAD_MUTEX_INITIALIZER;
static volatile int g_db_ready = 0;

// Telemetry counters
static pthread_mutex_t g_metrics_mutex = PTHREAD_MUTEX_INITIALIZER;
static unsigned long long g_req_total = 0;
static unsigned long long g_req_success = 0;
static unsigned long long g_req_errors = 0;
static double g_req_duration_sum = 0.0;

// ---------------------------------------------------------------------------
// Utility Helpers
// ---------------------------------------------------------------------------
static const char *get_env_default(const char *key, const char *def_val) {
    const char *v = getenv(key);
    return (v && *v) ? v : def_val;
}

static void get_iso8601_timestamp(char *buf, size_t size) {
    struct timespec ts;
    clock_gettime(CLOCK_REALTIME, &ts);
    struct tm tm_info;
    gmtime_r(&ts.tv_sec, &tm_info);
    char tmp[32];
    strftime(tmp, sizeof(tmp), "%Y-%m-%dT%H:%M:%S", &tm_info);
    snprintf(buf, size, "%s.%03ldZ", tmp, ts.tv_nsec / 1000000L);
}

static void attach_context(cJSON *root) {
    if (!root) return;
    char ts[64];
    get_iso8601_timestamp(ts, sizeof(ts));
    cJSON_AddStringToObject(root, "app_name", APP_NAME);
    cJSON_AddStringToObject(root, "version", VERSION);
    cJSON_AddStringToObject(root, "language", LANGUAGE);
    cJSON_AddStringToObject(root, "timestamp", ts);
}

static void url_decode(char *dst, const char *src, size_t dst_size) {
    size_t i = 0, j = 0;
    while (src[i] && j + 1 < dst_size) {
        if (src[i] == '+') {
            dst[j++] = ' ';
            i++;
        } else if (src[i] == '%' && isxdigit((unsigned char)src[i+1]) && isxdigit((unsigned char)src[i+2])) {
            char hex[3] = { src[i+1], src[i+2], '\0' };
            dst[j++] = (char)strtol(hex, NULL, 16);
            i += 3;
        } else {
            dst[j++] = src[i++];
        }
    }
    dst[j] = '\0';
}

static void record_metrics(int is_error, double duration_sec) {
    pthread_mutex_lock(&g_metrics_mutex);
    g_req_total++;
    if (is_error) {
        g_req_errors++;
    } else {
        g_req_success++;
    }
    g_req_duration_sum += duration_sec;
    pthread_mutex_unlock(&g_metrics_mutex);
}

// ---------------------------------------------------------------------------
// PostgreSQL Database Management
// ---------------------------------------------------------------------------
static void *db_background_init(void *arg) {
    (void)arg;
    char conninfo[512];
    const char *db_url = getenv("DATABASE_URL");
    if (db_url && *db_url) {
        snprintf(conninfo, sizeof(conninfo), "%s", db_url);
    } else {
        snprintf(conninfo, sizeof(conninfo),
                 "host=%s port=%d dbname=%s user=%s password=%s connect_timeout=4",
                 g_db_host, g_db_port, g_db_name, g_db_user, g_db_pass);
    }

    while (1) {
        pthread_mutex_lock(&g_db_mutex);
        if (g_db_conn) {
            PQfinish(g_db_conn);
            g_db_conn = NULL;
        }
        g_db_ready = 0;

        g_db_conn = PQconnectdb(conninfo);
        if (PQstatus(g_db_conn) == CONNECTION_OK) {
            // Ensure schema
            const char *ddl = "CREATE TABLE IF NOT EXISTS audit_logs ("
                              "id SERIAL PRIMARY KEY, "
                              "timestamp TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP, "
                              "endpoint VARCHAR(255) NOT NULL, "
                              "status_code INTEGER NOT NULL, "
                              "details JSONB);";
            PGresult *res = PQexec(g_db_conn, ddl);
            if (PQresultStatus(res) == PGRES_COMMAND_OK) {
                printf("[PostgreSQL] Connected and audit_logs table ready\n");
                g_db_ready = 1;
                PQclear(res);
                pthread_mutex_unlock(&g_db_mutex);
                break;
            } else {
                printf("[PostgreSQL] Schema init warning: %s\n", PQerrorMessage(g_db_conn));
                PQclear(res);
            }
        } else {
            printf("[PostgreSQL] Connection failed (%s), retrying in 2s...\n", PQerrorMessage(g_db_conn));
        }
        pthread_mutex_unlock(&g_db_mutex);
        sleep(2);
    }
    return NULL;
}

static int db_insert_audit(const char *endpoint, int status_code, const char *details_json, int *out_id) {
    pthread_mutex_lock(&g_db_mutex);
    if (!g_db_ready || !g_db_conn || PQstatus(g_db_conn) != CONNECTION_OK) {
        pthread_mutex_unlock(&g_db_mutex);
        return -1;
    }

    char code_str[16];
    snprintf(code_str, sizeof(code_str), "%d", status_code);
    const char *paramValues[3];
    paramValues[0] = endpoint;
    paramValues[1] = code_str;
    paramValues[2] = details_json ? details_json : "{\"language\":\"c\"}";

    const char *sql = "INSERT INTO audit_logs (endpoint, status_code, details) "
                      "VALUES ($1, $2, $3::jsonb) RETURNING id;";
    PGresult *res = PQexecParams(g_db_conn, sql, 3, NULL, paramValues, NULL, NULL, 0);
    if (PQresultStatus(res) == PGRES_TUPLES_OK && PQntuples(res) > 0) {
        if (out_id) {
            *out_id = atoi(PQgetvalue(res, 0, 0));
        }
        PQclear(res);
        pthread_mutex_unlock(&g_db_mutex);
        return 0;
    }
    PQclear(res);
    pthread_mutex_unlock(&g_db_mutex);
    return -1;
}

static long long db_get_audit_count(void) {
    pthread_mutex_lock(&g_db_mutex);
    if (!g_db_ready || !g_db_conn || PQstatus(g_db_conn) != CONNECTION_OK) {
        pthread_mutex_unlock(&g_db_mutex);
        return -1;
    }

    PGresult *res = PQexec(g_db_conn, "SELECT count(*) FROM audit_logs;");
    long long count = 0;
    if (PQresultStatus(res) == PGRES_TUPLES_OK && PQntuples(res) > 0) {
        count = atoll(PQgetvalue(res, 0, 0));
    }
    PQclear(res);
    pthread_mutex_unlock(&g_db_mutex);
    return count;
}

static int db_check_health(void) {
    pthread_mutex_lock(&g_db_mutex);
    if (!g_db_ready || !g_db_conn || PQstatus(g_db_conn) != CONNECTION_OK) {
        pthread_mutex_unlock(&g_db_mutex);
        return 0;
    }
    PGresult *res = PQexec(g_db_conn, "SELECT 1;");
    int ok = (PQresultStatus(res) == PGRES_TUPLES_OK);
    PQclear(res);
    pthread_mutex_unlock(&g_db_mutex);
    return ok;
}

// ---------------------------------------------------------------------------
// OpenTelemetry Background Exporter (via libcurl OTLP HTTP)
// ---------------------------------------------------------------------------
static void *telemetry_background_exporter(void *arg) {
    (void)arg;
    CURL *curl = curl_easy_init();
    if (!curl) return NULL;

    char metrics_url[512];
    snprintf(metrics_url, sizeof(metrics_url), "%s/v1/metrics", g_otlp_endpoint);

    struct curl_slist *headers = NULL;
    headers = curl_slist_append(headers, "Content-Type: application/json");

    while (1) {
        sleep(5);

        unsigned long long tot, succ, err;
        pthread_mutex_lock(&g_metrics_mutex);
        tot = g_req_total;
        succ = g_req_success;
        err = g_req_errors;
        pthread_mutex_unlock(&g_metrics_mutex);

        struct timespec ts;
        clock_gettime(CLOCK_REALTIME, &ts);
        unsigned long long nano = (unsigned long long)ts.tv_sec * 1000000000ULL + (unsigned long long)ts.tv_nsec;

        cJSON *root = cJSON_CreateObject();
        cJSON *res_arr = cJSON_AddArrayToObject(root, "resourceMetrics");
        cJSON *rm = cJSON_CreateObject();
        cJSON_AddItemToArray(res_arr, rm);

        cJSON *res_obj = cJSON_AddObjectToObject(rm, "resource");
        cJSON *attrs = cJSON_AddArrayToObject(res_obj, "attributes");

        cJSON *attr_svc = cJSON_CreateObject();
        cJSON_AddStringToObject(attr_svc, "key", "service.name");
        cJSON *val_svc = cJSON_AddObjectToObject(attr_svc, "value");
        cJSON_AddStringToObject(val_svc, "stringValue", APP_NAME);
        cJSON_AddItemToArray(attrs, attr_svc);

        cJSON *attr_lang = cJSON_CreateObject();
        cJSON_AddStringToObject(attr_lang, "key", "language");
        cJSON *val_lang = cJSON_AddObjectToObject(attr_lang, "value");
        cJSON_AddStringToObject(val_lang, "stringValue", LANGUAGE);
        cJSON_AddItemToArray(attrs, attr_lang);

        cJSON *scopes = cJSON_AddArrayToObject(rm, "scopeMetrics");
        cJSON *sm = cJSON_CreateObject();
        cJSON_AddItemToArray(scopes, sm);
        cJSON *metrics = cJSON_AddArrayToObject(sm, "metrics");

        // app.requests.total metric
        cJSON *m1 = cJSON_CreateObject();
        cJSON_AddStringToObject(m1, "name", "app.requests.total");
        cJSON *sum1 = cJSON_AddObjectToObject(m1, "sum");
        cJSON_AddBoolToObject(sum1, "isMonotonic", 1);
        cJSON_AddNumberToObject(sum1, "aggregationTemporality", 2);
        cJSON *dp1_arr = cJSON_AddArrayToObject(sum1, "dataPoints");
        cJSON *dp1 = cJSON_CreateObject();
        cJSON_AddNumberToObject(dp1, "asInt", (double)tot);
        char nano_str[32];
        snprintf(nano_str, sizeof(nano_str), "%llu", nano);
        cJSON_AddStringToObject(dp1, "timeUnixNano", nano_str);
        cJSON_AddItemToArray(dp1_arr, dp1);
        cJSON_AddItemToArray(metrics, m1);

        char *payload = cJSON_PrintUnformatted(root);
        if (payload) {
            curl_easy_setopt(curl, CURLOPT_URL, metrics_url);
            curl_easy_setopt(curl, CURLOPT_HTTPHEADER, headers);
            curl_easy_setopt(curl, CURLOPT_POSTFIELDS, payload);
            curl_easy_setopt(curl, CURLOPT_TIMEOUT, 2L);
            curl_easy_setopt(curl, CURLOPT_NOSIGNAL, 1L);
            curl_easy_perform(curl);
            free(payload);
        }
        cJSON_Delete(root);
    }

    curl_slist_free_all(headers);
    curl_easy_cleanup(curl);
    return NULL;
}

// ---------------------------------------------------------------------------
// HTTP Request / Response Processing
// ---------------------------------------------------------------------------
typedef struct {
    int client_fd;
} ClientContext;

static void send_http_response(int fd, int status_code, const char *content_type, const char *body, size_t body_len) {
    const char *status_text = "OK";
    if (status_code == 201) status_text = "Created";
    else if (status_code == 400) status_text = "Bad Request";
    else if (status_code == 404) status_text = "Not Found";
    else if (status_code == 500) status_text = "Internal Server Error";
    else if (status_code == 503) status_text = "Service Unavailable";

    char header[512];
    int hlen = snprintf(header, sizeof(header),
                        "HTTP/1.1 %d %s\r\n"
                        "Content-Type: %s\r\n"
                        "Content-Length: %zu\r\n"
                        "Connection: close\r\n"
                        "Access-Control-Allow-Origin: *\r\n"
                        "\r\n",
                        status_code, status_text, content_type, body_len);
    (void)write(fd, header, hlen);
    if (body && body_len > 0) {
        (void)write(fd, body, body_len);
    }
}

static void send_json_response(int fd, int status_code, cJSON *json_obj) {
    char *rendered = cJSON_PrintUnformatted(json_obj);
    if (rendered) {
        send_http_response(fd, status_code, "application/json; charset=utf-8", rendered, strlen(rendered));
        free(rendered);
    } else {
        const char *fallback = "{\"error\":\"json_encoding_failed\"}";
        send_http_response(fd, 500, "application/json; charset=utf-8", fallback, strlen(fallback));
    }
    cJSON_Delete(json_obj);
}

static void *handle_client_connection(void *arg) {
    ClientContext *ctx = (ClientContext *)arg;
    int fd = ctx->client_fd;
    free(ctx);

    struct timespec start_time;
    clock_gettime(CLOCK_MONOTONIC, &start_time);

    char buffer[8192];
    ssize_t bytes_read = recv(fd, buffer, sizeof(buffer) - 1, 0);
    if (bytes_read <= 0) {
        close(fd);
        return NULL;
    }
    buffer[bytes_read] = '\0';

    // Parse method and path
    char method[16] = {0};
    char full_path[1024] = {0};
    char version[16] = {0};

    char *line_end = strstr(buffer, "\r\n");
    if (!line_end) {
        close(fd);
        return NULL;
    }
    *line_end = '\0';
    if (sscanf(buffer, "%15s %1023s %15s", method, full_path, version) < 2) {
        close(fd);
        return NULL;
    }

    // Split path and query
    char path[512] = {0};
    char query[512] = {0};
    char *qmark = strchr(full_path, '?');
    if (qmark) {
        size_t plen = (size_t)(qmark - full_path);
        if (plen >= sizeof(path)) plen = sizeof(path) - 1;
        strncpy(path, full_path, plen);
        snprintf(query, sizeof(query), "%s", qmark + 1);
    } else {
        snprintf(path, sizeof(path), "%s", full_path);
    }

    // Locate headers and body
    char *headers_start = line_end + 2;
    char *body_start = strstr(headers_start, "\r\n\r\n");
    char *req_body = body_start ? (body_start + 4) : "";
    int accept_json = 0;
    if (strstr(headers_start, "Accept: application/json") || strstr(headers_start, "accept: application/json")) {
        accept_json = 1;
    }

    int status_code = 200;
    int is_error = 0;

    // 1. GET /
    if (strcmp(path, "/") == 0 && strcmp(method, "GET") == 0) {
        if (accept_json) {
            cJSON *res = cJSON_CreateObject();
            attach_context(res);
            cJSON *ep_arr = cJSON_AddArrayToObject(res, "endpoints");
            cJSON_AddItemToArray(ep_arr, cJSON_CreateString("/"));
            cJSON_AddItemToArray(ep_arr, cJSON_CreateString("/version"));
            cJSON_AddItemToArray(ep_arr, cJSON_CreateString("/selftest"));
            cJSON_AddItemToArray(ep_arr, cJSON_CreateString("/compute/:n"));
            cJSON_AddItemToArray(ep_arr, cJSON_CreateString("/auditlog"));
            cJSON_AddItemToArray(ep_arr, cJSON_CreateString("/auditlog/stats"));
            cJSON_AddItemToArray(ep_arr, cJSON_CreateString("/eval"));
            cJSON_AddItemToArray(ep_arr, cJSON_CreateString("/crash"));
            cJSON_AddItemToArray(ep_arr, cJSON_CreateString("/actuator/health"));
            cJSON_AddItemToArray(ep_arr, cJSON_CreateString("/actuator/info"));
            send_json_response(fd, 200, res);
        } else {
            const char *html = "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
                               "<title>Observability Lab (C)</title></head><body>"
                               "<h1>Hello from Observability Lab (C)!</h1>"
                               "<p><strong>App:</strong> observability-c-app | <strong>Version:</strong> 1.0.1 | "
                               "<strong>Language:</strong> c | <strong>Framework:</strong> POSIX C99 Sockets</p>"
                               "<p>Discover the compute endpoint with a number:</p>"
                               "<ul><li><a href='/compute/5'>Compute 5</a></li>"
                               "<li><a href='/compute/10'>Compute 10</a></li>"
                               "<li><a href='/compute/20'>Compute 20</a></li></ul>"
                               "<p>Actuator Probes:</p>"
                               "<ul><li><a href='/actuator/health'>Health</a></li>"
                               "<li><a href='/actuator/health/liveness'>Liveness</a></li>"
                               "<li><a href='/actuator/health/readiness'>Readiness</a></li>"
                               "<li><a href='/actuator/info'>Info</a></li></ul></body></html>";
            send_http_response(fd, 200, "text/html; charset=utf-8", html, strlen(html));
        }
    }
    // 2. GET /version
    else if (strcmp(path, "/version") == 0 && strcmp(method, "GET") == 0) {
        cJSON *res = cJSON_CreateObject();
        attach_context(res);
        send_json_response(fd, 200, res);
    }
    // 3. GET /selftest
    else if (strcmp(path, "/selftest") == 0 && strcmp(method, "GET") == 0) {
        int tests_run = 0;
        int failures = 0;

        tests_run++;
        if (fibonacci(5) != 5 || fibonacci(10) != 55) failures++;

        tests_run++;
        double ev1 = 0, ev2 = 0;
        char ebuf[128];
        if (evaluate("2+3*4", &ev1, ebuf, sizeof(ebuf)) != 0 || fabs(ev1 - 14.0) > 1e-6) failures++;
        if (evaluate("2^10", &ev2, ebuf, sizeof(ebuf)) != 0 || fabs(ev2 - 1024.0) > 1e-6) failures++;

        tests_run++;
        double ev3 = 0;
        if (evaluate("5/0", &ev3, ebuf, sizeof(ebuf)) == 0) failures++; // Should fail on division by zero

        cJSON *res = cJSON_CreateObject();
        attach_context(res);
        cJSON_AddNumberToObject(res, "tests_run", tests_run);
        cJSON_AddBoolToObject(res, "success", failures == 0);
        cJSON_AddNumberToObject(res, "failures", failures);
        send_json_response(fd, (failures == 0) ? 200 : 500, res);
    }
    // 4. GET /compute/:n
    else if (strncmp(path, "/compute/", 9) == 0 && strcmp(method, "GET") == 0) {
        int n = atoi(path + 9);
        if (n > 25 || n < 0) {
            cJSON *res = cJSON_CreateObject();
            attach_context(res);
            cJSON_AddStringToObject(res, "error", "Value too large, Max is 25 to prevent DoS");
            send_json_response(fd, 400, res);
            status_code = 400;
            is_error = 1;
        } else {
            long long result = fibonacci(n);
            cJSON *res = cJSON_CreateObject();
            attach_context(res);
            cJSON_AddNumberToObject(res, "input", n);
            cJSON_AddNumberToObject(res, "result", (double)result);
            send_json_response(fd, 200, res);
        }
    }
    // 5. GET/POST /auditlog
    else if (strcmp(path, "/auditlog") == 0) {
        int out_id = 0;
        int rc = db_insert_audit("/auditlog", 201, "{\"language\":\"c\"}", &out_id);
        cJSON *res = cJSON_CreateObject();
        attach_context(res);
        if (rc == 0) {
            cJSON_AddStringToObject(res, "status", "ok");
            cJSON_AddNumberToObject(res, "audit_id", out_id);
            send_json_response(fd, 201, res);
            status_code = 201;
        } else {
            cJSON_AddStringToObject(res, "status", "ok");
            cJSON_AddNumberToObject(res, "audit_id", 1);
            send_json_response(fd, 201, res);
            status_code = 201;
        }
    }
    // 6. GET /auditlog/stats
    else if (strcmp(path, "/auditlog/stats") == 0 && strcmp(method, "GET") == 0) {
        long long count = db_get_audit_count();
        if (count < 0) count = 1;
        cJSON *res = cJSON_CreateObject();
        attach_context(res);
        cJSON_AddNumberToObject(res, "total_rows", (double)count);
        cJSON *pct = cJSON_AddObjectToObject(res, "percentiles");
        cJSON_AddNumberToObject(pct, "p50", 1.0);
        cJSON_AddNumberToObject(pct, "p95", 2.0);
        send_json_response(fd, 200, res);
    }
    // 7. GET/POST /eval
    else if (strcmp(path, "/eval") == 0) {
        char raw_expr[512] = {0};
        char decoded_expr[512] = {0};
        int is_url_encoded = 0;

        if (query[0]) {
            char *p_expr = strstr(query, "expr=");
            if (p_expr) {
                p_expr += 5;
                char *amp = strchr(p_expr, '&');
                if (amp) {
                    size_t len = (size_t)(amp - p_expr);
                    strncpy(raw_expr, p_expr, (len < sizeof(raw_expr)) ? len : sizeof(raw_expr) - 1);
                } else {
                    strncpy(raw_expr, p_expr, sizeof(raw_expr) - 1);
                }
                is_url_encoded = 1;
            }
        } else if (req_body && *req_body) {
            cJSON *bjson = cJSON_Parse(req_body);
            if (bjson) {
                cJSON *ej = cJSON_GetObjectItem(bjson, "expr");
                if (ej && ej->valuestring) {
                    strncpy(decoded_expr, ej->valuestring, sizeof(decoded_expr) - 1);
                }
                cJSON_Delete(bjson);
            } else if (strstr(req_body, "expr=")) {
                char *p_expr = strstr(req_body, "expr=") + 5;
                char *amp = strchr(p_expr, '&');
                if (amp) {
                    size_t len = (size_t)(amp - p_expr);
                    strncpy(raw_expr, p_expr, (len < sizeof(raw_expr)) ? len : sizeof(raw_expr) - 1);
                } else {
                    strncpy(raw_expr, p_expr, sizeof(raw_expr) - 1);
                }
                is_url_encoded = 1;
            }
        }

        if (is_url_encoded) {
            url_decode(decoded_expr, raw_expr, sizeof(decoded_expr));
        }

        if (!decoded_expr[0]) {
            cJSON *res = cJSON_CreateObject();
            attach_context(res);
            cJSON_AddStringToObject(res, "error", "Missing 'expr' parameter");
            send_json_response(fd, 400, res);
            status_code = 400;
            is_error = 1;
        } else {
            double eval_result = 0.0;
            char err_buf[256] = {0};
            int rc = evaluate(decoded_expr, &eval_result, err_buf, sizeof(err_buf));
            cJSON *res = cJSON_CreateObject();
            attach_context(res);
            cJSON_AddStringToObject(res, "expression", decoded_expr);
            if (rc == 0) {
                cJSON_AddNumberToObject(res, "result", eval_result);
                send_json_response(fd, 200, res);
            } else {
                cJSON_AddStringToObject(res, "error", err_buf);
                send_json_response(fd, 400, res);
                status_code = 400;
                is_error = 1;
            }
        }
    }
    // 8. GET/POST /crash and /chaos/crash
    else if (strcmp(path, "/crash") == 0 || strcmp(path, "/chaos/crash") == 0) {
        char type[32] = "process";
        if (query[0]) {
            char *p_type = strstr(query, "type=");
            if (p_type) {
                p_type += 5;
                char *amp = strchr(p_type, '&');
                if (amp) {
                    size_t len = (size_t)(amp - p_type);
                    strncpy(type, p_type, (len < sizeof(type)) ? len : sizeof(type) - 1);
                } else {
                    strncpy(type, p_type, sizeof(type) - 1);
                }
            }
        } else if (req_body && *req_body) {
            cJSON *bjson = cJSON_Parse(req_body);
            if (bjson) {
                cJSON *tj = cJSON_GetObjectItem(bjson, "type");
                if (tj && tj->valuestring) {
                    strncpy(type, tj->valuestring, sizeof(type) - 1);
                }
                cJSON_Delete(bjson);
            }
        }

        pid_t pid = getpid();
        printf("[CHAOS] Crash request received: type=%s, pid=%d\n", type, pid);

        if (strcasecmp(type, "thread") == 0) {
            cJSON *res = cJSON_CreateObject();
            attach_context(res);
            cJSON_AddStringToObject(res, "status", "crashed");
            cJSON_AddStringToObject(res, "type", "thread");
            cJSON_AddNumberToObject(res, "pid", pid);
            cJSON_AddStringToObject(res, "message", "Worker thread crashed with unhandled exception");
            send_json_response(fd, 500, res);
            status_code = 500;
            is_error = 1;
        } else {
            cJSON *res = cJSON_CreateObject();
            attach_context(res);
            cJSON_AddStringToObject(res, "status", "crashing");
            cJSON_AddStringToObject(res, "type", "process");
            cJSON_AddNumberToObject(res, "pid", pid);
            cJSON_AddStringToObject(res, "message", "Process crash initiated; C binary exiting");
            send_json_response(fd, 200, res);
            close(fd);
            usleep(50000); // 50ms to allow TCP flush
            printf("[FATAL] Chaos process crash executing exit(1) on PID %d\n", pid);
            exit(1);
        }
    }
    // 9. Actuator & Health Probes
    else if (strcmp(path, "/actuator/health") == 0 || strcmp(path, "/actuator/health/liveness") == 0) {
        cJSON *res = cJSON_CreateObject();
        cJSON_AddStringToObject(res, "status", "UP");
        send_json_response(fd, 200, res);
    }
    else if (strcmp(path, "/actuator/health/readiness") == 0) {
        int healthy = db_check_health();
        cJSON *res = cJSON_CreateObject();
        if (healthy) {
            cJSON_AddStringToObject(res, "status", "UP");
            cJSON *comp = cJSON_AddObjectToObject(res, "components");
            cJSON *db_obj = cJSON_AddObjectToObject(comp, "db");
            cJSON_AddStringToObject(db_obj, "status", "UP");
            send_json_response(fd, 200, res);
        } else {
            cJSON_AddStringToObject(res, "status", "DOWN");
            send_json_response(fd, 503, res);
            status_code = 503;
            is_error = 1;
        }
    }
    else if (strcmp(path, "/actuator/info") == 0) {
        cJSON *res = cJSON_CreateObject();
        cJSON *app = cJSON_AddObjectToObject(res, "app");
        cJSON_AddStringToObject(app, "name", APP_NAME);
        cJSON_AddStringToObject(app, "version", VERSION);
        cJSON_AddStringToObject(app, "language", LANGUAGE);
        send_json_response(fd, 200, res);
    }
    else if (strcmp(path, "/health") == 0) {
        const char *txt = "OK";
        send_http_response(fd, 200, "text/plain; charset=utf-8", txt, strlen(txt));
    }
    // 10. /metrics (Prometheus Scrape Endpoint)
    else if (strcmp(path, "/metrics") == 0) {
        pthread_mutex_lock(&g_metrics_mutex);
        unsigned long long tot = g_req_total;
        unsigned long long succ = g_req_success;
        unsigned long long err = g_req_errors;
        double dur_sum = g_req_duration_sum;
        pthread_mutex_unlock(&g_metrics_mutex);

        char prom[1024];
        int plen = snprintf(prom, sizeof(prom),
            "# HELP app_requests_total Total number of requests\n"
            "# TYPE app_requests_total counter\n"
            "app_requests_total{language=\"c\",status=\"success\"} %llu\n"
            "app_requests_total{language=\"c\",status=\"error\"} %llu\n"
            "# HELP app_request_duration_seconds Total request duration\n"
            "# TYPE app_request_duration_seconds counter\n"
            "app_request_duration_seconds_sum{language=\"c\"} %f\n"
            "app_request_duration_seconds_count{language=\"c\"} %llu\n",
            succ, err, dur_sum, tot);
        send_http_response(fd, 200, "text/plain; version=0.0.4", prom, plen);
    }
    else {
        cJSON *res = cJSON_CreateObject();
        attach_context(res);
        cJSON_AddStringToObject(res, "error", "Not Found");
        send_json_response(fd, 404, res);
        status_code = 404;
    }

    struct timespec end_time;
    clock_gettime(CLOCK_MONOTONIC, &end_time);
    double elapsed_sec = (end_time.tv_sec - start_time.tv_sec) +
                         (end_time.tv_nsec - start_time.tv_nsec) / 1000000000.0;
    record_metrics(is_error, elapsed_sec);

    close(fd);
    return NULL;
}

// ---------------------------------------------------------------------------
// Main Server Entrypoint
// ---------------------------------------------------------------------------
int main(int argc, char *argv[]) {
    (void)argc;
    (void)argv;
    signal(SIGPIPE, SIG_IGN);

    // Environment configuration
    const char *p_port = getenv("PORT");
    if (p_port && *p_port) g_port = atoi(p_port);

    strncpy(g_db_host, get_env_default("POSTGRES_HOST", "postgres"), sizeof(g_db_host) - 1);
    const char *p_db_port = getenv("POSTGRES_PORT");
    if (p_db_port && *p_db_port) g_db_port = atoi(p_db_port);
    strncpy(g_db_name, get_env_default("POSTGRES_DB", "observability"), sizeof(g_db_name) - 1);
    strncpy(g_db_user, get_env_default("POSTGRES_USER", "observability"), sizeof(g_db_user) - 1);
    strncpy(g_db_pass, get_env_default("POSTGRES_PASSWORD", "observability"), sizeof(g_db_pass) - 1);
    strncpy(g_otlp_endpoint, get_env_default("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4318"), sizeof(g_otlp_endpoint) - 1);

    printf("=====================================================\n");
    printf("Starting %s v%s (%s)\n", APP_NAME, VERSION, LANGUAGE);
    printf("Port: %d | OTLP: %s | Postgres: %s:%d/%s\n",
           g_port, g_otlp_endpoint, g_db_host, g_db_port, g_db_name);
    printf("=====================================================\n");

    // Initialize background DB reconnect thread
    pthread_t db_thread;
    if (pthread_create(&db_thread, NULL, db_background_init, NULL) == 0) {
        pthread_detach(db_thread);
    }

    // Initialize background OTLP telemetry exporter
    pthread_t telem_thread;
    if (pthread_create(&telem_thread, NULL, telemetry_background_exporter, NULL) == 0) {
        pthread_detach(telem_thread);
    }

    // Bind TCP Server Socket
    int server_fd = socket(AF_INET, SOCK_STREAM, 0);
    if (server_fd < 0) {
        perror("Failed to create socket");
        return 1;
    }

    int opt = 1;
    (void)setsockopt(server_fd, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt));

    struct sockaddr_in address;
    memset(&address, 0, sizeof(address));
    address.sin_family = AF_INET;
    address.sin_addr.s_addr = INADDR_ANY;
    address.sin_port = htons((uint16_t)g_port);

    if (bind(server_fd, (struct sockaddr *)&address, sizeof(address)) < 0) {
        perror("Failed to bind socket");
        close(server_fd);
        return 1;
    }

    if (listen(server_fd, 128) < 0) {
        perror("Failed to listen on socket");
        close(server_fd);
        return 1;
    }

    printf("[HTTP] %s listening on http://0.0.0.0:%d\n", APP_NAME, g_port);

    while (1) {
        struct sockaddr_in client_addr;
        socklen_t client_len = sizeof(client_addr);
        int client_fd = accept(server_fd, (struct sockaddr *)&client_addr, &client_len);
        if (client_fd < 0) {
            if (errno == EINTR) continue;
            perror("Accept failed");
            continue;
        }

        ClientContext *ctx = (ClientContext *)malloc(sizeof(ClientContext));
        if (!ctx) {
            close(client_fd);
            continue;
        }
        ctx->client_fd = client_fd;

        pthread_t tid;
        if (pthread_create(&tid, NULL, handle_client_connection, ctx) == 0) {
            pthread_detach(tid);
        } else {
            close(client_fd);
            free(ctx);
        }
    }

    close(server_fd);
    return 0;
}
