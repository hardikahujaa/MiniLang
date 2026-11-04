func compute(): int {
    int a = 10;
    int b = 3;
    int c = (a + b) * (a - b) / b % 4;
    return c;
}

func main(): void {
    print(compute());
    print(2 + 3 * 4 - 1);
    print((2 + 3) * (4 - 1));
}
