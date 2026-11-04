func inRange(int x): bool {
    bool result = x > 0 && x < 100;
    return result;
}

func main(): void {
    print(5 == 5);
    print(5 != 6);
    print(5 < 6);
    print(6 > 5);
    print(5 <= 5);
    print(6 >= 5);
    print(true && false);
    print(true || false);
    print(inRange(50));
}
