namespace DotnetApp;

public static class Fibonacci
{
    public static long Compute(int n)
    {
        if (n <= 0) return 0;
        if (n == 1) return 1;
        return Compute(n - 1) + Compute(n - 2);
    }
}
