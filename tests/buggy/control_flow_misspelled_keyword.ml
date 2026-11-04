fun sign(int x): int {
    if (x > 0) {
        return 1;
    } else {
        if (x < 0) {
            return -1;
        } else {
            return 0;
        }
    }
}

func main(): void {
    int i = 0;
    while (i < 5) {
        print(sign(i - 2));
        i = i + 1;
    }
}
