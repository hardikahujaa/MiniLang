func factorial(int n): int {
    if (n <= 1) {
        return 1;
    }
    return n * factorial(n - 1);
}

func add(int a, int b): int {
    return a + b;
}

func main(): void {
    print(factorial(5));
    print(add(factorial(3), factorial(4)));
    int p
    int q
}
