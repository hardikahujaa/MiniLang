func combine(int a, float b, bool c, int d): float {
    if (c) {
        return b + a;
    }
    return b - d;
}

func main(): void {
    print(combine(1, 2.5, true, 3));
    print(combine(1, 2.5, false, 3));
}
