#include <stdio.h>

int* get_data() {
    int local_var = 42;
    return &local_var;
}

int main() {
    int arr[3] = {1, 2, 3};
    int *ptr;
    
    arr[3] = 4;
    
    *ptr = 100;
    
    int check = 0;
    if (check = 1) {
        printf("Check passed!\n")
    }
    
    int *dangling_ptr = get_data();
    
    int *dynamic_arr = malloc(5 * sizeof(int));
    
    return 0
}