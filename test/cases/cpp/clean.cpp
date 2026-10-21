#include <optional>
#include <numeric>
#include <vector>

std::optional<double> checked_mean(const std::vector<int> &values) {
    if (values.empty()) return std::nullopt;
    double total = std::accumulate(values.begin(), values.end(), 0.0);
    return total / static_cast<double>(values.size());
}

std::optional<int> checked_read(const std::vector<int> &values, unsigned index) {
    if (index >= values.size()) return std::nullopt;
    return values[index];
}
