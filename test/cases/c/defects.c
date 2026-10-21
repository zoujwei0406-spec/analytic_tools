#include <stdlib.h>
#include <string.h>

int read_past_end(void) {
    int values[4] = {10, 20, 30, 40};
    return values[4];
}

int write_past_end(void) {
    int values[2] = {0, 0};
    values[2] = 7;
    return values[0];
}

int divide_zero(void) {
    int divisor = 0;
    return 42 / divisor;
}

int dereference_null(void) {
    int *value = NULL;
    return *value;
}

int read_after_free(void) {
    int *value = malloc(sizeof(int));
    if (value == NULL) return -1;
    *value = 7;
    free(value);
    return *value;
}

void release_twice(void) {
    char *buffer = malloc(16);
    free(buffer);
    free(buffer);
}

int uninitialized_total(void) {
    int total;
    return total + 1;
}

int *local_address(void) {
    int result = 42;
    return &result;
}

int forget_allocation(void) {
    int *value = malloc(sizeof(int));
    if (value == NULL) return -1;
    *value = 23;
    return *value;
}

int copy_long_text(void) {
    char buffer[4];
    strcpy(buffer, "longer than four");
    return buffer[0];
}
