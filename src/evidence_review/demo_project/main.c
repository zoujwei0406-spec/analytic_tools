#include <stdio.h>
#include <stdlib.h>
#include "fees.h"

int main(int argc, char **argv) {
    if (argc != 2) {
        return 2;
    }
    int total = atoi(argv[1]);
    printf("%d\n", shipping_fee(total));
    return 0;
}
