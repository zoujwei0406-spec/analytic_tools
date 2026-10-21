#include <vector>

int read_past_end() {
    int values[3] = {1, 2, 3};
    return values[3];
}

int dereference_null() {
    int *value = nullptr;
    return *value;
}

int uninitialized_result() {
    int result;
    return result + 2;
}

void wrong_delete() {
    int *values = new int[4];
    delete values;
}

int read_after_delete() {
    int *value = new int(7);
    delete value;
    return *value;
}

void delete_twice() {
    int *value = new int(7);
    delete value;
    delete value;
}

int &local_reference() {
    int value = 5;
    return value;
}

int forget_allocation() {
    int *value = new int(8);
    return *value;
}

int invalidated_iterator() {
    std::vector<int> values{1, 2};
    auto first = values.begin();
    values.reserve(values.capacity() + 1);
    return *first;
}

int write_past_end() {
    int values[2] = {0, 0};
    values[2] = 9;
    return values[0];
}
