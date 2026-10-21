#include <stddef.h>

int checked_read(const int *values, size_t count, size_t index, int *output) {
    if (values == NULL || output == NULL || index >= count) return 0;
    *output = values[index];
    return 1;
}

int checked_mean(const int *values, size_t count, double *output) {
    if (values == NULL || output == NULL || count == 0) return 0;
    double sum = 0;
    for (size_t i = 0; i < count; ++i) sum += values[i];
    *output = sum / (double)count;
    return 1;
}
