#pragma once
struct Obj { int helper(); };
class Shape {
public:
    Shape(int w, int h);
    int scale(int k);
private:
    int w_, h_;
};
