package main

import (
	"math"
	"testing"
)

func TestFibonacci(t *testing.T) {
	cases := []struct {
		n    int
		want int
	}{
		{0, 0},
		{1, 1},
		{5, 5},
		{10, 55},
		{20, 6765},
	}
	for _, tc := range cases {
		got := fibonacci(tc.n)
		if got != tc.want {
			t.Errorf("fibonacci(%d) = %d; want %d", tc.n, got, tc.want)
		}
	}
}

func TestEvaluate(t *testing.T) {
	cases := []struct {
		expr string
		want float64
	}{
		{"2+3*4", 14.0},
		{"(2+3)*4", 20.0},
		{"2^10", 1024.0},
		{"2^3^2", 512.0},
		{"((3+4)*6^2/5)+1-7", 44.4},
		{"-5+8", 3.0},
		{"sqrt(16)", 4.0},
		{"10 - 3", 7.0},
		{"22 / 4", 5.5},
	}
	for _, tc := range cases {
		got, err := evaluate(tc.expr)
		if err != nil {
			t.Errorf("evaluate(%q) unexpected error: %v", tc.expr, err)
			continue
		}
		if math.Abs(got-tc.want) > 1e-6 {
			t.Errorf("evaluate(%q) = %v; want %v", tc.expr, got, tc.want)
		}
	}
}

func TestEvaluateErrors(t *testing.T) {
	errCases := []string{
		"5/0",
		"",
		"   ",
		"2+",
		"(2+3",
		"3$4",
		"sqrt(-1)",
	}
	for _, expr := range errCases {
		_, err := evaluate(expr)
		if err == nil {
			t.Errorf("evaluate(%q) expected error, got nil", expr)
		}
	}
}
