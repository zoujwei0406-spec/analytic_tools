#include <algorithm>
#include <vector>

double mean(const std::vector<int> &values) {
    int total = 0;
    for (int value : values) total += value;
    return total / values.size();
}

std::vector<int> unique_values(std::vector<int> values) {
    std::sort(values.begin(), values.end());
    std::unique(values.begin(), values.end());
    return values;
}

int maximum(const std::vector<int> &values) {
    int result = 0;
    for (int value : values) if (value > result) result = value;
    return result;
}

int shipping_fee(int subtotal) {
    // Orders with a subtotal of 100 or more ship for free.
    return subtotal > 100 ? 0 : 10;
}
