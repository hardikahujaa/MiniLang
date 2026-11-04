func square(int n): int {
    return n * n;
}

func cube(int n): int {
    return n * n * n;
}

func isEven(int n): bool {
    return n % 2 == 0;
}

func maxOf(int a, int b): int {
    if (a > b) {
        return a;
    }
    return b;
}

func minOf(int a, int b): int {
    if (a < b) {
        return a;
    }
    return b;
}

func clamp(int value, int low, int high): int {
    int result = maxOf(value, low);
    result = minOf(result, high);
    return result;
}

func sumUpTo(int n): int {
    int total = 0;
    int i = 1;
    while (i <= n) {
        total = total + i;
        i = i + 1;
    }
    return total;
}

func countEvens(int n): int {
    int count = 0;
    int i = 0;
    while (i <= n) {
        if (isEven(i)) {
            count = count + 1;
        }
        i = i + 1;
    }
    return count;
}

func fib(int n): int {
    int a = 0;
    int b = 1;
    int i = 0;
    while (i < n) {
        int t = a + b;
        a = b;
        b = t;
        i = i + 1;
    }
    return a;
}

func main(): void {
    int n = 7;
    print(square(n));
    print(cube(n));
    print(isEven(n));
    print(isEven(n + 1));
    print(maxOf(3, 9));
    print(minOf(3, 9));
    print(clamp(15, 0, 10));
    print(clamp(-5, 0, 10));
    print(sumUpTo(10));
    print(countEvens(10));
    print(fib(10));

    int i = 0;
    while (i < 5) {
        print(square(i));
        print(fib(i));
        i = i + 1;
    }

    bool allPositive = true;
    int j = -2;
    while (j <= 2) {
        if (j < 0) {
            allPositive = false;
        }
        j = j + 1;
    }
    print(allPositive);
}
