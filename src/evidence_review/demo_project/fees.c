#include "fees.h"

/* Requirement: orders of 100 or more have free shipping. */
int shipping_fee(int total) {
    if (total > 100) {  /* Deliberate boundary bug: should be >=. */
        return 0;
    }
    return 10;
}
