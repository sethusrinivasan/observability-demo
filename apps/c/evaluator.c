#include "evaluator.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <ctype.h>
#include <math.h>

typedef struct {
    const char *src;
    size_t pos;
    char err[256];
    int has_error;
} Parser;

static void set_error(Parser *p, const char *msg) {
    if (!p->has_error) {
        p->has_error = 1;
        snprintf(p->err, sizeof(p->err), "%s", msg);
    }
}

static void skip_whitespace(Parser *p) {
    while (p->src[p->pos] && isspace((unsigned char)p->src[p->pos])) {
        p->pos++;
    }
}

static char peek(Parser *p) {
    skip_whitespace(p);
    return p->src[p->pos];
}

static char get(Parser *p) {
    skip_whitespace(p);
    return p->src[p->pos++];
}

static double parse_expression(Parser *p);

static double parse_factor(Parser *p) {
    skip_whitespace(p);
    char c = peek(p);

    if (c == '(') {
        get(p); // consume '('
        double val = parse_expression(p);
        if (p->has_error) return 0.0;
        if (get(p) != ')') {
            set_error(p, "Mismatched parentheses: expected ')'");
            return 0.0;
        }
        return val;
    }

    if (isalpha((unsigned char)c)) {
        char fn[32];
        size_t i = 0;
        while (isalpha((unsigned char)p->src[p->pos]) && i < sizeof(fn) - 1) {
            fn[i++] = p->src[p->pos++];
        }
        fn[i] = '\0';

        skip_whitespace(p);
        if (get(p) != '(') {
            set_error(p, "Expected '(' after function name");
            return 0.0;
        }
        double arg = parse_expression(p);
        if (p->has_error) return 0.0;
        if (get(p) != ')') {
            set_error(p, "Expected ')' after function arguments");
            return 0.0;
        }

        if (strcmp(fn, "sqrt") == 0) {
            if (arg < 0.0) {
                set_error(p, "Cannot take square root of negative number");
                return 0.0;
            }
            return sqrt(arg);
        } else if (strcmp(fn, "sin") == 0) {
            return sin(arg);
        } else if (strcmp(fn, "cos") == 0) {
            return cos(arg);
        } else {
            char buf[64];
            snprintf(buf, sizeof(buf), "Unknown function: %s", fn);
            set_error(p, buf);
            return 0.0;
        }
    }

    if (isdigit((unsigned char)c) || c == '.') {
        const char *start = &p->src[p->pos];
        char *end = NULL;
        double val = strtod(start, &end);
        if (end == start) {
            set_error(p, "Failed to parse number");
            return 0.0;
        }
        p->pos += (end - start);
        return val;
    }

    if (c == '\0') {
        set_error(p, "Unexpected end of expression");
    } else {
        char buf[64];
        snprintf(buf, sizeof(buf), "Unexpected character: '%c'", c);
        set_error(p, buf);
    }
    return 0.0;
}

static double parse_unary(Parser *p) {
    skip_whitespace(p);
    char c = peek(p);
    if (c == '+') {
        get(p);
        return parse_unary(p);
    } else if (c == '-') {
        get(p);
        return -parse_unary(p);
    }
    return parse_factor(p);
}

static double parse_power(Parser *p) {
    double base = parse_unary(p);
    if (p->has_error) return 0.0;

    skip_whitespace(p);
    if (peek(p) == '^') {
        get(p);
        double exponent = parse_power(p); // right-associative
        if (p->has_error) return 0.0;
        return pow(base, exponent);
    }
    return base;
}

static double parse_term(Parser *p) {
    double val = parse_power(p);
    if (p->has_error) return 0.0;

    while (!p->has_error) {
        char op = peek(p);
        if (op == '*') {
            get(p);
            double rhs = parse_power(p);
            if (p->has_error) return 0.0;
            val *= rhs;
        } else if (op == '/') {
            get(p);
            double rhs = parse_power(p);
            if (p->has_error) return 0.0;
            if (rhs == 0.0 || fabs(rhs) < 1e-15) {
                set_error(p, "Division by zero");
                return 0.0;
            }
            val /= rhs;
        } else {
            break;
        }
    }
    return val;
}

static double parse_expression(Parser *p) {
    double val = parse_term(p);
    if (p->has_error) return 0.0;

    while (!p->has_error) {
        char op = peek(p);
        if (op == '+') {
            get(p);
            double rhs = parse_term(p);
            if (p->has_error) return 0.0;
            val += rhs;
        } else if (op == '-') {
            get(p);
            double rhs = parse_term(p);
            if (p->has_error) return 0.0;
            val -= rhs;
        } else {
            break;
        }
    }
    return val;
}

int evaluate(const char *expr, double *result, char *err_buf, size_t err_buf_size) {
    if (!expr || *expr == '\0') {
        if (err_buf && err_buf_size > 0) {
            snprintf(err_buf, err_buf_size, "Missing expression");
        }
        return -1;
    }

    Parser p;
    memset(&p, 0, sizeof(p));
    p.src = expr;
    p.pos = 0;

    skip_whitespace(&p);
    if (p.src[p.pos] == '\0') {
        if (err_buf && err_buf_size > 0) {
            snprintf(err_buf, err_buf_size, "Missing expression");
        }
        return -1;
    }

    double val = parse_expression(&p);
    if (p.has_error) {
        if (err_buf && err_buf_size > 0) {
            snprintf(err_buf, err_buf_size, "%s", p.err);
        }
        return -1;
    }

    skip_whitespace(&p);
    if (p.src[p.pos] != '\0') {
        char buf[64];
        snprintf(buf, sizeof(buf), "Unexpected character: '%c'", p.src[p.pos]);
        if (err_buf && err_buf_size > 0) {
            snprintf(err_buf, err_buf_size, "%s", buf);
        }
        return -1;
    }

    if (isnan(val) || isinf(val)) {
        if (err_buf && err_buf_size > 0) {
            snprintf(err_buf, err_buf_size, "Division by zero or non-finite result");
        }
        return -1;
    }

    if (result) {
        *result = val;
    }
    return 0;
}
