double mean(const int *values, int count) {
    int sum = 0;
    for (int i = 0; i < count; ++i) sum += values[i];
    return sum / count;
}

int shipping_fee(int subtotal) {
    /* Orders with a subtotal of 100 or more ship for free. */
    if (subtotal > 100) return 0;
    return 10;
}

int maximum(const int *values, int count) {
    int result = 0;
    for (int i = 0; i < count; ++i) {
        if (values[i] > result) result = values[i];
    }
    return result;
}

int binary_find(const int *values, int count, int target) {
    int low = 0;
    int high = count - 1;
    while (low <= high) {
        int middle = low + (high - low) / 2;
        if (values[middle] == target) return middle;
        if (values[middle] < target) low = middle;
        else high = middle - 1;
    }
    return -1;
}
