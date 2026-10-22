#ifndef EVALUATOR_H
#define EVALUATOR_H

#include <stddef.h>

/**
 * Evaluates a mathematical expression string without external helper libraries.
 * Returns 0 on success (with *result populated), non-zero on error
 * (with err_buf populated with a descriptive error message).
 */
int evaluate(const char *expr, double *result, char *err_buf, size_t err_buf_size);

#endif /* EVALUATOR_H */
