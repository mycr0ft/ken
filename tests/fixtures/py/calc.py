import os
from dataclasses import dataclass


def area(w, h):
    return w * h


def helper(x):
    return area(x, x) + 1


@dataclass
class Shape:
    w: int
    h: int

    def scale(self, k):
        return helper(self.w) * k


def main():
    s = Shape(2, 3)
    return s.scale(2)
