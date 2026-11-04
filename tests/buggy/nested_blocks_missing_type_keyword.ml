func sumGrid(int rows, cols): int {
    int total = 0;
    int i = 0;
    while (i < rows) {
        int j = 0;
        while (j < cols) {
            if (i == j) {
                total = total + 1;
            }
            j = j + 1;
        }
        i = i + 1;
    }
    return total;
}

func main(): void {
    print(sumGrid(3, 3));
}
