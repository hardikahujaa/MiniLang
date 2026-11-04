func greet(): void {
    print(1);
}

func announce(int code): void {
    print(code);
    return;
}

func main(): void {
    greet();
    announce(42);
}
