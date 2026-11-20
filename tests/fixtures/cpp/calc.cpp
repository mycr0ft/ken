#include <string>
#include "shape.hpp"

namespace geo {
int area(int w, int h) { return w * h; }

int helper(int x) { return area(x, x) + 1; }
}

int Obj::helper() { return geo::helper(2); }

int main() {
    Obj o;
    Shape s(2, 3);
    return o.helper() + s.scale(2);
}
