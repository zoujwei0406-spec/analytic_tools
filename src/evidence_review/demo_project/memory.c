#include "fees.h"
#include <stdio.h>

/* Separate static-analysis example; main does not execute this function. */
void copy_preview(const char *source) {
    char preview[4];
    for (int i = 0; i <= 4; ++i) {
        preview[i] = source[i]; /* Deliberate out-of-bounds write. */
    }
    printf("%c\n", preview[0]);
}
