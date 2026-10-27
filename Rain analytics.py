import os
import shutil
import subprocess
import sys
from pathlib import Path

NEEDED = ["pandas", "numpy", "matplotlib"]


def _ensure_packages():
    try:
        import pandas, numpy, matplotlib  # noqa: F401
        return
    except ImportError:
        pass
    if os.environ.get("CROP_BOOTSTRAPPED") == "1":
        sys.exit("Packages are still missing after setup. Please send me the messages above.")

    here = Path(__file__).resolve().parent
    venv = here / ".venv_crop"
    py = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    uv = shutil.which("uv")

    if not py.exists():
        print("First run: creating a private environment (one time only)...")
        try:
            subprocess.check_call([sys.executable, "-m", "venv", str(venv)])
        except Exception:
            if not uv:
                sys.exit("Could not create an environment. Please install Python 3.12 from "
                         "python.org and run again.")
            subprocess.check_call([uv, "venv", str(venv)])

    print("Installing", ", ".join(NEEDED), "(one time, about a minute)...")
    try:
        subprocess.check_call([str(py), "-m", "pip", "install", *NEEDED])
    except Exception:
        if not uv:
            sys.exit("Package install failed. Check your internet connection and run again.")
        subprocess.check_call([uv, "pip", "install", "--python", str(py), *NEEDED])

    print("Setup done. Starting the analysis...\n")
    env = dict(os.environ, CROP_BOOTSTRAPPED="1")
    sys.exit(subprocess.call([str(py), str(Path(__file__).resolve()), *sys.argv[1:]], env=env))


_ensure_packages()
# ---------------------------------------------------------------------------------------

import hashlib
import json
import urllib.request

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

# ===================== SETTINGS =====================
CSV_NAME = "tamilnadu_crop_yield_1965_2025.csv"
SEARCH_DIRS = [
    Path(__file__).resolve().parent,
    Path(__file__).resolve().parent / "Data",
    Path(r"C:\Users\Kalyan V\Downloads\Data"),
    Path(r"C:\Users\Kalyan V\Downloads\files"),
]
OUTPUT = Path(__file__).resolve().parent / "results"
OUTPUT.mkdir(parents=True, exist_ok=True)

START_YEAR = 1990
END_YEAR = 2024
TREND = "quadratic"      # or "linear"
N_PERM = 2000            # permutations for p-values

CROPS = ["Paddy", "Groundnut", "Cumbu (Pearl millet)",
         "Cholam (Sorghum)", "Ragi (Finger millet)"]

POINTS = [("Chennai", 13.08, 80.27), ("Vellore", 12.92, 79.13),
          ("Salem", 11.65, 78.16), ("Coimbatore", 11.00, 76.96),
          ("Thanjavur", 10.78, 79.14), ("Trichy", 10.79, 78.70),
          ("Madurai", 9.92, 78.12), ("Tirunelveli", 8.73, 77.70)]

RNG = np.random.default_rng(42)
plt.rcParams.update({"axes.grid": True, "grid.alpha": 0.25, "font.size": 10})


# ===================== HELPERS =====================
def corr(x, y):
    return float(np.corrcoef(x, y)[0, 1])


def perm_p(x, y, n=N_PERM):
    """Two-sided permutation p-value for correlation."""
    r = abs(corr(x, y))
    cnt = sum(abs(corr(x, RNG.permutation(y))) >= r for _ in range(n))
    return (cnt + 1) / (n + 1)


def ols(X, y):
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ beta
    ss_res, ss_tot = (resid ** 2).sum(), ((y - y.mean()) ** 2).sum()
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else np.nan
    return beta, r2


def loo_q2(X, y):
    """Leave-one-out Q2: 1 = perfect, 0 = no better than the mean, below 0 = worse."""
    hat = X @ np.linalg.pinv(X.T @ X) @ X.T
    resid = y - hat @ y
    press = ((resid / (1 - np.diag(hat))) ** 2).sum()
    return 1 - press / ((y - y.mean()) ** 2).sum()


# ===================== RAINFALL =====================
def monthly_to_seasons(daily):
    df = daily.to_frame("rain")
    df["year"], df["month"] = df.index.year, df.index.month
    sw = df[df.month.between(6, 9)].groupby("year")["rain"].sum(min_count=1).rename("sw_mm")
    ne = df[df.month.between(10, 12)].groupby("year")["rain"].sum(min_count=1).rename("ne_mm")
    return pd.concat([sw, ne], axis=1).dropna().reset_index()


def get_rainfall():
    manual = OUTPUT / "rainfall_manual.csv"
    if manual.exists():
        print("Using manual rainfall file:", manual)
        return pd.read_csv(manual)

    key = hashlib.md5(str(POINTS).encode()).hexdigest()[:8]
    cache = OUTPUT / f"rainfall_{START_YEAR}_{END_YEAR}_{key}.csv"
    saved = next((d / cache.name for d in [OUTPUT, *SEARCH_DIRS]
                  if (d / cache.name).exists()), None)
    if saved is not None:
        print("Using saved rainfall:", saved)
        return pd.read_csv(saved)

    print("Downloading NASA POWER rainfall (about a minute)...")
    series = []
    for name, lat, lon in POINTS:
        url = ("https://power.larc.nasa.gov/api/temporal/daily/point"
               f"?parameters=PRECTOTCORR&community=AG&longitude={lon}&latitude={lat}"
               f"&start={START_YEAR}0101&end={END_YEAR}1231&format=JSON")
        try:
            with urllib.request.urlopen(url, timeout=180) as r:
                values = json.load(r)["properties"]["parameter"]["PRECTOTCORR"]
        except Exception as e:
            sys.exit(f"\nRainfall download failed for {name}: {e}\n"
                     f"Create {manual} with columns year,sw_mm,ne_mm and run again.")
        s = pd.Series(values, dtype=float)
        s.index = pd.to_datetime(s.index, format="%Y%m%d")
        series.append(s.where(s >= 0).rename(name))
        print("  downloaded", name)
    rain = monthly_to_seasons(pd.concat(series, axis=1).mean(axis=1))
    rain.to_csv(cache, index=False)
    return rain


# ===================== YIELD =====================
def get_yields():
    path = next((d / CSV_NAME for d in SEARCH_DIRS if (d / CSV_NAME).exists()), None)
    if path is None:
        sys.exit(f"Cannot find {CSV_NAME}. Put it in the same folder as this script.")
    print("Yield file:", path)
    df = pd.read_csv(path)
    df.columns = df.columns.str.strip()
    need = {"Crop", "Start year", "Yield recomputed"}
    if not need <= set(df.columns):
        sys.exit(f"CSV needs columns {need}; found {list(df.columns)}")
    df = df.rename(columns={"Start year": "year", "Yield recomputed": "yield"})
    df["Crop"] = df["Crop"].astype(str).str.strip()
    df["year"] = pd.to_numeric(df["year"], errors="coerce")
    df["yield"] = pd.to_numeric(df["yield"], errors="coerce")
    df = df[df.Crop.isin(CROPS) & df.year.between(START_YEAR, END_YEAR) & (df["yield"] > 0)]
    df = df.dropna(subset=["year", "yield"])
    df["year"] = df["year"].astype(int)
    df = df.groupby(["Crop", "year"], as_index=False)["yield"].mean()
    if df.empty:
        sys.exit("No matching crop-yield records found.")
    return df


def fit_trend(g):
    t = (g["year"] - g["year"].mean()).to_numpy(float)
    ly = np.log(g["yield"].to_numpy(float))
    coef = np.polyfit(t, ly, 2 if TREND == "quadratic" else 1)
    fit = np.polyval(coef, t)
    r2 = 1 - ((ly - fit) ** 2).sum() / ((ly - ly.mean()) ** 2).sum()
    return np.exp(fit), r2


# ===================== PREPARE =====================
rain = get_rainfall()
rain = rain[rain.year.between(START_YEAR, END_YEAR)].copy()
rain["total_mm"] = rain.sw_mm + rain.ne_mm
for c in ["sw_mm", "ne_mm", "total_mm"]:
    m, sd = rain[c].mean(), rain[c].std()
    rain[c + "_anom"] = 100 * (rain[c] / m - 1)
    rain[c + "_z"] = (rain[c] - m) / sd

df = get_yields().merge(rain, on="year", how="inner")
if df.empty:
    sys.exit("No overlapping years between yield and rainfall.")

parts, trend_r2 = [], {}
for crop, g in df.groupby("Crop"):
    g = g.sort_values("year").copy()
    if len(g) < 8:
        print("Skipping (too few years):", crop)
        continue
    g["yield_trend"], trend_r2[crop] = fit_trend(g)
    g["yield_anom"] = (g["yield"] / g["yield_trend"] - 1) * 100
    parts.append(g)
df = pd.concat(parts, ignore_index=True)
df["rain_class"] = pd.cut(df.total_mm_z, [-np.inf, -1, 1, np.inf],
                          labels=["Deficit", "Normal", "Excess"])
df.to_csv(OUTPUT / "merged_data.csv", index=False)
crops_used = [c for c in CROPS if c in trend_r2]

# ===================== CORRELATION =====================
rows = []
for crop, g in df.groupby("Crop"):
    for rt in ["sw_mm", "ne_mm", "total_mm"]:
        x, y = g[rt + "_anom"].to_numpy(), g["yield_anom"].to_numpy()
        rows.append(dict(Crop=crop, Rainfall=rt, N_years=len(g),
                         Pearson_r=corr(x, y), P_value=perm_p(x, y),
                         Spearman_rho=corr(pd.Series(x).rank(), pd.Series(y).rank())))
corr_df = pd.DataFrame(rows)
corr_df["Significant_5pct"] = corr_df.P_value < 0.05
corr_df.to_csv(OUTPUT / "correlations.csv", index=False)

# ===================== REGRESSION (Model A: SW + NE, linear) =====================
rows = []
for crop, g in df.groupby("Crop"):
    X = np.column_stack([np.ones(len(g)), g.sw_mm_z, g.ne_mm_z])
    y = g["yield_anom"].to_numpy()
    beta, r2 = ols(X, y)
    n, k = len(y), 2
    adj = 1 - (1 - r2) * (n - 1) / (n - k - 1)
    perm = np.array([ols(X, RNG.permutation(y))[0] for _ in range(N_PERM)])
    p = [(np.sum(np.abs(perm[:, i]) >= abs(beta[i])) + 1) / (N_PERM + 1) for i in (1, 2)]
    rows.append(dict(Crop=crop, R_squared=r2, Adjusted_R2=adj,
                     SW_coefficient=beta[1], SW_p_value=p[0],
                     NE_coefficient=beta[2], NE_p_value=p[1]))
reg = pd.DataFrame(rows)
reg.to_csv(OUTPUT / "regression_results.csv", index=False)

classes = df.pivot_table(index="Crop", columns="rain_class", values="yield_anom",
                         aggfunc="mean", observed=False)
classes.to_csv(OUTPUT / "rainfall_class_analysis.csv")

# ===================== REGRESSION (Model B: total rainfall + total rainfall squared) =====================
# yield_anom = a + b1 * total_z + b2 * total_z^2
# A negative b2 means a hump: yield is highest near normal rainfall and falls in both
# deficit and excess years. This is run after Model A so Model A's results do not change.
rows = []
for crop, g in df.groupby("Crop"):
    z = g.total_mm_z.to_numpy()
    y = g["yield_anom"].to_numpy()
    X = np.column_stack([np.ones(len(g)), z, z ** 2])
    beta, r2 = ols(X, y)
    n, k = len(y), 2
    adj = 1 - (1 - r2) * (n - 1) / (n - k - 1)
    perm = [ols(X, RNG.permutation(y)) for _ in range(N_PERM)]
    perm_beta = np.array([p[0] for p in perm])
    perm_r2 = np.array([p[1] for p in perm])
    p_lin, p_quad = [(np.sum(np.abs(perm_beta[:, i]) >= abs(beta[i])) + 1) / (N_PERM + 1)
                     for i in (1, 2)]
    p_model = (np.sum(perm_r2 >= r2) + 1) / (N_PERM + 1)
    rows.append(dict(Crop=crop, R_squared=r2, Adjusted_R2=adj, Model_p_value=p_model,
                     Linear_coefficient=beta[1], Linear_p_value=p_lin,
                     Quadratic_coefficient=beta[2], Quadratic_p_value=p_quad,
                     Shape="Hump (peak)" if beta[2] < 0 else "U-shape",
                     Turning_point_z=-beta[1] / (2 * beta[2]),
                     LOO_Q2=loo_q2(X, y)))
reg_q = pd.DataFrame(rows)
reg_q.to_csv(OUTPUT / "regression_results_quadratic.csv", index=False)

# ===================== CHARTS =====================
# 1 rainfall
plt.figure(figsize=(11, 5))
plt.plot(rain.year, rain.sw_mm, label="Southwest monsoon (Jun-Sep)")
plt.plot(rain.year, rain.ne_mm, label="Northeast monsoon (Oct-Dec)")
plt.title("Tamil Nadu monsoon rainfall"); plt.xlabel("Year"); plt.ylabel("Rainfall (mm)")
plt.legend(); plt.tight_layout(); plt.savefig(OUTPUT / "01_rainfall_trends.png", dpi=200); plt.close()

# 2 yield + trend
fig, axes = plt.subplots(len(crops_used), 1, figsize=(12, 3 * len(crops_used)), squeeze=False)
for ax, crop in zip(axes[:, 0], crops_used):
    g = df[df.Crop == crop].sort_values("year")
    ax.plot(g.year, g["yield"], marker="o", ms=3, label="Observed")
    ax.plot(g.year, g.yield_trend, "--", label=f"{TREND} trend")
    ax.set_title(f"{crop} (trend R2 = {trend_r2[crop]:.2f})"); ax.set_ylabel("Yield (kg/ha)"); ax.legend()
axes[-1, 0].set_xlabel("Year")
plt.tight_layout(); plt.savefig(OUTPUT / "02_crop_yield_trends.png", dpi=200); plt.close()

# 3 heatmap
heat = corr_df.pivot(index="Crop", columns="Rainfall", values="Pearson_r")
pv = corr_df.pivot(index="Crop", columns="Rainfall", values="P_value")
fig, ax = plt.subplots(figsize=(9, 5))
im = ax.imshow(heat.values, cmap="coolwarm", vmin=-1, vmax=1, aspect="auto")
ax.set_xticks(range(len(heat.columns)), heat.columns)
ax.set_yticks(range(len(heat.index)), heat.index)
for i in range(heat.shape[0]):
    for j in range(heat.shape[1]):
        star = "*" if pv.iloc[i, j] < 0.05 else ""
        ax.text(j, i, f"{heat.iloc[i, j]:.2f}{star}", ha="center", va="center")
plt.colorbar(im, ax=ax, label="Pearson correlation")
plt.title("Rainfall vs yield correlation (* = p < 0.05)")
plt.tight_layout(); plt.savefig(OUTPUT / "03_correlation_heatmap.png", dpi=200); plt.close()

# 4 scatter
fig, axes = plt.subplots(len(crops_used), 2, figsize=(13, 3.2 * len(crops_used)), squeeze=False)
for i, crop in enumerate(crops_used):
    g = df[df.Crop == crop]
    for j, (col, name) in enumerate([("sw_mm_anom", "Southwest"), ("ne_mm_anom", "Northeast")]):
        ax = axes[i, j]
        ax.scatter(g[col], g.yield_anom, alpha=0.7)
        b = np.polyfit(g[col], g.yield_anom, 1)
        xs = np.linspace(g[col].min(), g[col].max(), 50)
        ax.plot(xs, np.polyval(b, xs), "r--")
        ax.axhline(0, color="gray", lw=0.7)
        ax.set_title(f"{crop} - {name}"); ax.set_xlabel("Rainfall anomaly (%)"); ax.set_ylabel("Yield anomaly (%)")
plt.tight_layout(); plt.savefig(OUTPUT / "04_scatter_plots.png", dpi=200); plt.close()

# 5 regression coefficients
x, w = np.arange(len(reg)), 0.35
plt.figure(figsize=(11, 6))
plt.bar(x - w / 2, reg.SW_coefficient, w, label="Southwest")
plt.bar(x + w / 2, reg.NE_coefficient, w, label="Northeast")
plt.xticks(x, reg.Crop, rotation=20, ha="right"); plt.axhline(0, color="black")
plt.ylabel("Yield change (% points) per +1 SD rainfall"); plt.title("Regression coefficients")
plt.legend(); plt.tight_layout(); plt.savefig(OUTPUT / "05_regression.png", dpi=200); plt.close()

# 6 rainfall classes
classes.plot(kind="bar", figsize=(11, 6))
plt.title("Mean yield anomaly by rainfall class"); plt.ylabel("Yield anomaly (%)")
plt.xticks(rotation=20, ha="right"); plt.axhline(0, color="black")
plt.tight_layout(); plt.savefig(OUTPUT / "06_rainfall_classes.png", dpi=200); plt.close()

# 7 quadratic model: fitted curves + adjusted R2 comparison
ncols = 3
nrows = int(np.ceil((len(crops_used) + 1) / ncols))
fig, axes = plt.subplots(nrows, ncols, figsize=(14, 4 * nrows), squeeze=False)
flat = axes.ravel()
for ax, crop in zip(flat, crops_used):
    g = df[df.Crop == crop]
    z, y = g.total_mm_z.to_numpy(), g.yield_anom.to_numpy()
    beta, _ = ols(np.column_stack([np.ones(len(g)), z, z ** 2]), y)
    zs = np.linspace(z.min(), z.max(), 100)
    row = reg_q[reg_q.Crop == crop].iloc[0]
    ax.scatter(z, y, alpha=0.7)
    ax.plot(zs, beta[0] + beta[1] * zs + beta[2] * zs ** 2, "r-", label="quadratic fit")
    ax.axhline(0, color="gray", lw=0.7)
    ax.set_title(f"{crop}\nR2 = {row.R_squared:.2f}, adj R2 = {row.Adjusted_R2:.2f}")
    ax.set_xlabel("Total rainfall (z-score)"); ax.set_ylabel("Yield anomaly (%)"); ax.legend()
ax = flat[len(crops_used)]
xb, wb = np.arange(len(crops_used)), 0.4
ra = reg.set_index("Crop").loc[crops_used, "Adjusted_R2"]
rb = reg_q.set_index("Crop").loc[crops_used, "Adjusted_R2"]
ax.bar(xb - wb / 2, ra, wb, label="Model A: SW + NE")
ax.bar(xb + wb / 2, rb, wb, label="Model B: total + total$^2$")
ax.axhline(0, color="black")
ax.set_xticks(xb, [c.split(" (")[0] for c in crops_used], rotation=20, ha="right")
ax.set_title("Adjusted R2 comparison"); ax.set_ylabel("Adjusted R2"); ax.legend()
for ax in flat[len(crops_used) + 1:]:
    ax.axis("off")
plt.tight_layout(); plt.savefig(OUTPUT / "07_quadratic_model.png", dpi=200); plt.close()

# ===================== PRINT RESULTS =====================
print("\nDONE. Years:", df.year.min(), "-", df.year.max(), "| trend:", TREND)
print("\nCORRELATIONS (p from permutation test)")
print(corr_df.round(3).to_string(index=False))
print(f"\n{len(corr_df)} correlations tested, so about {len(corr_df) * 0.05:.1f} may look "
      "significant by chance. Trust strong results (|r| above about 0.5) seen in SW, NE and total.")
print("\nREGRESSION - Model A (SW + NE, linear)")
print(reg.round(3).to_string(index=False))
print("\nREGRESSION - Model B (total rainfall + total rainfall squared)")
print(reg_q.round(3).to_string(index=False))
print("\nRainfall class means (%)")
print(classes.round(1).to_string())
print("\nSaved to:", OUTPUT)