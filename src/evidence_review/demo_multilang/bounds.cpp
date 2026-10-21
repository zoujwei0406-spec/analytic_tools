namespace sample {
class Reader {
public:
    int read() {
        int values[3] = {1, 2, 3};
        return values[3]; // Deliberate out-of-bounds access.
    }
};
}
