/* Generates testdata/talib_golden.csv from testdata/btc_usdt_1h.csv using the
 * TA-Lib C library. The Go tests in internal/ta compare Sentinel's streaming
 * indicators against this file. Regenerate with tools/talibgold/gen.sh. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <ta-lib/ta_libc.h>

#define MAXN 10000
static double O[MAXN], H[MAXN], L[MAXN], C[MAXN], V[MAXN];

typedef struct { const char *name; double v[MAXN]; } col;
static col cols[32];
static int ncols = 0;

static double *newcol(const char *name) {
  col *c = &cols[ncols++];
  c->name = name;
  for (int i = 0; i < MAXN; i++) c->v[i] = NAN;
  return c->v;
}

static void place(double *dst, const double *src, int beg, int nb) {
  for (int i = 0; i < nb; i++) dst[beg + i] = src[i];
}

int main(int argc, char **argv) {
  if (argc != 3) { fprintf(stderr, "usage: gen in.csv out.csv\n"); return 2; }
  FILE *f = fopen(argv[1], "r");
  if (!f) { perror("open"); return 1; }
  char line[512];
  int n = 0;
  if (!fgets(line, sizeof line, f)) { fclose(f); return 1; } /* header */
  while (fgets(line, sizeof line, f) && n < MAXN) {
    long long ts;
    if (sscanf(line, "%lld,%lf,%lf,%lf,%lf,%lf", &ts, &O[n], &H[n], &L[n], &C[n], &V[n]) == 6) n++;
  }
  fclose(f);
  TA_Initialize();
  static double a[MAXN], b[MAXN], c[MAXN];
  int beg, nb;

#define ONE(NAME, CALL) do { double *d = newcol(NAME); CALL; place(d, a, beg, nb); } while (0)
  ONE("sma20", TA_SMA(0, n - 1, C, 20, &beg, &nb, a));
  ONE("ema20", TA_EMA(0, n - 1, C, 20, &beg, &nb, a));
  ONE("wma20", TA_WMA(0, n - 1, C, 20, &beg, &nb, a));
  ONE("kama10", TA_KAMA(0, n - 1, C, 10, &beg, &nb, a));
  ONE("rsi14", TA_RSI(0, n - 1, C, 14, &beg, &nb, a));
  ONE("atr14", TA_ATR(0, n - 1, H, L, C, 14, &beg, &nb, a));
  ONE("adx14", TA_ADX(0, n - 1, H, L, C, 14, &beg, &nb, a));
  ONE("pdi14", TA_PLUS_DI(0, n - 1, H, L, C, 14, &beg, &nb, a));
  ONE("mdi14", TA_MINUS_DI(0, n - 1, H, L, C, 14, &beg, &nb, a));
  ONE("mfi14", TA_MFI(0, n - 1, H, L, C, V, 14, &beg, &nb, a));
  ONE("obv", TA_OBV(0, n - 1, C, V, &beg, &nb, a));
  ONE("roc10", TA_ROC(0, n - 1, C, 10, &beg, &nb, a));
  ONE("slope14", TA_LINEARREG_SLOPE(0, n - 1, C, 14, &beg, &nb, a));
  ONE("stddev20", TA_STDDEV(0, n - 1, C, 20, 1.0, &beg, &nb, a));
  {
    double *m = newcol("macd"), *s = newcol("macd_signal"), *h = newcol("macd_hist");
    TA_MACD(0, n - 1, C, 12, 26, 9, &beg, &nb, a, b, c);
    place(m, a, beg, nb); place(s, b, beg, nb); place(h, c, beg, nb);
  }
  {
    double *u = newcol("bb_upper"), *m = newcol("bb_middle"), *l = newcol("bb_lower");
    TA_BBANDS(0, n - 1, C, 20, 2.0, 2.0, TA_MAType_SMA, &beg, &nb, a, b, c);
    place(u, a, beg, nb); place(m, b, beg, nb); place(l, c, beg, nb);
  }
  {
    double *k = newcol("stochrsi_k"), *d = newcol("stochrsi_d");
    TA_STOCHRSI(0, n - 1, C, 14, 14, 3, TA_MAType_SMA, &beg, &nb, a, b);
    place(k, a, beg, nb); place(d, b, beg, nb);
  }
  TA_Shutdown();

  FILE *o = fopen(argv[2], "w");
  if (!o) { perror("out"); return 1; }
  fprintf(o, "i");
  for (int j = 0; j < ncols; j++) fprintf(o, ",%s", cols[j].name);
  fprintf(o, "\n");
  for (int i = 0; i < n; i++) {
    fprintf(o, "%d", i);
    for (int j = 0; j < ncols; j++) {
      if (isnan(cols[j].v[i])) fprintf(o, ",");
      else fprintf(o, ",%.10g", cols[j].v[i]);
    }
    fprintf(o, "\n");
  }
  fclose(o);
  return 0;
}
