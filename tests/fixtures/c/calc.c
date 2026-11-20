#include <stdio.h>
#include "util.h"

int add(int a, int b) { return a + b; }

int helper(int x) { return add(x, x); }

int main(void) {
    printf("%d\n", helper(3));
    return util_check(1) ? 0 : 1;
}
