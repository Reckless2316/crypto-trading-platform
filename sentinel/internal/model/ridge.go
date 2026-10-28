package model

import (
	"errors"
	"math"
)

// FitRidgeLogistic fits an L2-regularised logistic regression by Newton's
// method (iteratively reweighted least squares). X rows are feature vectors
// without the bias; the bias is added internally and is not penalised. w is
// an optional per-row sample weight (nil for uniform). Returns weights with
// the bias first.
func FitRidgeLogistic(X [][]float64, y []float64, w []float64, lambda float64, iters int) ([]float64, error) {
	if len(X) == 0 || len(X) != len(y) {
		return nil, errors.New("ridge: empty or mismatched data")
	}
	d := len(X[0]) + 1
	beta := make([]float64, d)
	// Start the bias at the weighted base rate's logit.
	var sw, sy float64
	for i := range y {
		wi := 1.0
		if w != nil {
			wi = w[i]
		}
		sw += wi
		sy += wi * y[i]
	}
	if sw <= 0 {
		return nil, errors.New("ridge: zero total weight")
	}
	br := math.Min(math.Max(sy/sw, 1e-4), 1-1e-4)
	beta[0] = math.Log(br / (1 - br))

	H := make([][]float64, d)
	for i := range H {
		H[i] = make([]float64, d)
	}
	g := make([]float64, d)
	xi := make([]float64, d)
	for it := 0; it < iters; it++ {
		for i := range H {
			for j := range H[i] {
				H[i][j] = 0
			}
			g[i] = 0
		}
		for r := range X {
			xi[0] = 1
			copy(xi[1:], X[r])
			z := 0.0
			for j := 0; j < d; j++ {
				z += beta[j] * xi[j]
			}
			p := sigmoid(z)
			wi := 1.0
			if w != nil {
				wi = w[r]
			}
			res := wi * (p - y[r])
			s := wi * p * (1 - p)
			for a := 0; a < d; a++ {
				if xi[a] == 0 {
					continue
				}
				g[a] += res * xi[a]
				sa := s * xi[a]
				for b := a; b < d; b++ {
					H[a][b] += sa * xi[b]
				}
			}
		}
		for a := 1; a < d; a++ {
			g[a] += lambda * beta[a]
			H[a][a] += lambda
		}
		H[0][0] += 1e-9
		for a := 0; a < d; a++ {
			for b := 0; b < a; b++ {
				H[a][b] = H[b][a]
			}
		}
		step, err := solveSPD(H, g)
		if err != nil {
			return nil, err
		}
		maxStep := 0.0
		for j := range beta {
			beta[j] -= step[j]
			maxStep = math.Max(maxStep, math.Abs(step[j]))
		}
		if maxStep < 1e-7 {
			break
		}
	}
	return beta, nil
}

// solveSPD solves A x = b for symmetric positive-definite A by Cholesky.
func solveSPD(A [][]float64, b []float64) ([]float64, error) {
	n := len(A)
	L := make([][]float64, n)
	for i := range L {
		L[i] = make([]float64, n)
	}
	for i := 0; i < n; i++ {
		for j := 0; j <= i; j++ {
			s := A[i][j]
			for k := 0; k < j; k++ {
				s -= L[i][k] * L[j][k]
			}
			if i == j {
				if s <= 0 {
					return nil, errors.New("ridge: matrix not positive definite")
				}
				L[i][i] = math.Sqrt(s)
			} else {
				L[i][j] = s / L[j][j]
			}
		}
	}
	yv := make([]float64, n)
	for i := 0; i < n; i++ {
		s := b[i]
		for k := 0; k < i; k++ {
			s -= L[i][k] * yv[k]
		}
		yv[i] = s / L[i][i]
	}
	x := make([]float64, n)
	for i := n - 1; i >= 0; i-- {
		s := yv[i]
		for k := i + 1; k < n; k++ {
			s -= L[k][i] * x[k]
		}
		x[i] = s / L[i][i]
	}
	return x, nil
}
