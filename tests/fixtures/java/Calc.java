package demo;

import java.util.List;

public class Calc {
    static int area(int w, int h) { return w * h; }

    static int helper(int x) {
        if (x > 0 && x < 10) { return area(x, x) + 1; }
        for (int i = 0; i < x; i++) { area(i, i); }
        return x;
    }

    public static void main(String[] args) {
        List<Integer> xs = List.of(1, 2);
        int t = helper(3);
        System.out.println(t + xs.size());
    }
}
