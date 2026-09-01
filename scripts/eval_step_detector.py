#!/usr/bin/env python3
"""StepDetector 离线回放评测：用真实录制会话的 IMU 数据验证步态检测参数。

用法：
  .venv/bin/python scripts/eval_step_detector.py --self-test
  .venv/bin/python scripts/eval_step_detector.py --imu-csv <session>/imu.csv
  .venv/bin/python scripts/eval_step_detector.py --imu-csv <session>/imu.csv --sweep

保真度保证：`StepDetectorPort` 是 blindassist
`app/src/main/java/com/example/blindassist/spatialmem/StepDetector.kt`
的逐行移植（含 dt 越界重置、首采样对齐、上升沿 + 不应期），`--self-test`
复刻 StepDetectorTest.kt 全部 7 个用例；两处任一改动必须同步。
"""
from __future__ import annotations

import argparse
import csv
import math
import sys
from dataclasses import dataclass, field

import numpy as np


@dataclass
class StepDetectorPort:
    """Kotlin StepDetector 的精确移植（参数见同名常量）。"""

    min_threshold: float = 0.6
    thresh_factor: float = 1.6
    refractory_ns: int = 250_000_000
    hp_tau_s: float = 0.8
    lp_tau_s: float = 0.06
    thresh_tau_s: float = 2.0
    sample_period_ns: int = 20_000_000
    min_dt_ns: int = 5_000_000
    max_dt_ns: int = 200_000_000

    last_ts_ns: int | None = None
    dc_estimate: float = 0.0
    filtered: float = 0.0
    rectified_ema: float = 0.0
    converged: bool = False
    above_threshold: bool = False
    last_step_ts_ns: int | None = None
    # 诊断计数（Kotlin 没有的观测口，不影响算法）
    diag_resets: int = field(default=0, compare=False)

    def on_sample(self, ts_ns: int, ax: float, ay: float, az: float) -> bool:
        last = self.last_ts_ns
        if last is not None and (
            ts_ns < last or not (self.min_dt_ns <= ts_ns - last <= self.max_dt_ns)
        ):
            self.reset(keep_time=False)
            self.diag_resets += 1
        dt_ns = (ts_ns - last) if last is not None else self.sample_period_ns
        self.last_ts_ns = ts_ns

        dt_s = dt_ns / 1e9
        mag = math.sqrt(ax * ax + ay * ay + az * az)

        hp_alpha = 1.0 - math.exp(-dt_s / self.hp_tau_s)
        if not self.converged:
            self.dc_estimate = mag
            self.filtered = 0.0
            self.rectified_ema = 0.0
            self.converged = True
            return False
        self.dc_estimate += hp_alpha * (mag - self.dc_estimate)
        hp_out = mag - self.dc_estimate

        lp_alpha = 1.0 - math.exp(-dt_s / self.lp_tau_s)
        self.filtered += lp_alpha * (hp_out - self.filtered)

        abs_f = abs(self.filtered)
        self.rectified_ema += (1.0 - math.exp(-dt_s / self.thresh_tau_s)) * (
            abs_f - self.rectified_ema
        )
        threshold = max(self.min_threshold, self.thresh_factor * self.rectified_ema)

        now_above = self.filtered > threshold
        step = False
        if now_above and not self.above_threshold:
            last_step = self.last_step_ts_ns
            if last_step is None or ts_ns - last_step >= self.refractory_ns:
                step = True
                self.last_step_ts_ns = ts_ns
        self.above_threshold = now_above
        return step

    def reset(self, keep_time: bool = True) -> None:
        self.dc_estimate = 0.0
        self.filtered = 0.0
        self.rectified_ema = 0.0
        self.converged = False
        self.above_threshold = False
        if not keep_time:
            self.last_ts_ns = None
            self.last_step_ts_ns = None


# ---------------------------------------------------------------- self-test


def _feed(det: StepDetectorPort, count: int, magnitude_at, start_ns=1_000_000_000):
    steps = 0
    for i in range(count):
        t = i / 50.0
        if det.on_sample(start_ns + i * 20_000_000, 0.0, 0.0, magnitude_at(t)):
            steps += 1
    return steps


def self_test() -> bool:
    ok = True

    def check(name, cond, detail=""):
        nonlocal ok
        status = "PASS" if cond else "FAIL"
        if not cond:
            ok = False
        print(f"  [{status}] {name}{(' — ' + detail) if detail else ''}")

    check(
        "constantGravityProducesNoSteps",
        _feed(StepDetectorPort(), 500, lambda t: 9.81) == 0,
    )

    steps = _feed(
        StepDetectorPort(), 500, lambda t: 9.81 + 2.8 * max(0.0, math.sin(2 * math.pi * 1.8 * t))
    )
    check("syntheticWalkingWaveDetectsExpectedSteps", 15 <= steps <= 20, f"steps={steps}")

    def burst(t_s):
        mag = 9.81
        for t0 in (0.3, 0.9, 1.5, 2.1, 2.7):
            for offset in (0.0, 0.1):
                d = (t_s - t0 - offset) / 0.03
                mag += 3.0 * math.exp(-d * d)
        return mag

    steps = _feed(StepDetectorPort(), 175, burst)
    check("refractoryMergesDoublePulseBursts", 4 <= steps <= 6, f"steps={steps}")

    check(
        "highFrequencyVibrationProducesNoSteps",
        _feed(StepDetectorPort(), 500, lambda t: 9.81 + 1.5 * math.sin(2 * math.pi * 30.0 * t)) == 0,
    )

    walk = lambda t: 9.81 + 2.8 * max(0.0, math.sin(2 * math.pi * 1.8 * (t % 3.0)))
    det = StepDetectorPort()
    steps = false_burst = 0
    for i in range(350):
        t = i / 50.0
        mag = 9.81 if 150 <= i <= 199 else walk(t)
        hit = det.on_sample(1_000_000_000 + i * 20_000_000, 0.0, 0.0, mag)
        if hit:
            steps += 1
            if 200 <= i <= 214:
                false_burst += 1
    check(
        "samplingGapResetsFiltersWithoutFalseBurst",
        false_burst <= 1 and 6 <= steps <= 13,
        f"steps={steps} falseBurst={false_burst}",
    )

    det = StepDetectorPort()
    _feed(det, 250, lambda t: 9.81 + 2.8 * max(0.0, math.sin(2 * math.pi * 1.8 * t)))
    det.reset(keep_time=True)
    steps = 0
    for i in range(250):
        t = (250 + i) / 50.0
        if det.on_sample(
            1_000_000_000 + (250 + i) * 20_000_000,
            0.0,
            0.0,
            9.81 + 2.8 * max(0.0, math.sin(2 * math.pi * 1.8 * t)),
        ):
            steps += 1
    check("resetKeepsRefractoryAnchor", 7 <= steps <= 10, f"steps={steps}")

    det = StepDetectorPort()
    _feed(det, 100, lambda t: 9.81)
    check(
        "outOfOrderTimestampResetsWithoutCrash",
        det.on_sample(1_000_000_000 + 50 * 20_000_000, 0.0, 0.0, 13.0) is False,
    )
    return ok


# ------------------------------------------------------------------- replay


def load_accelerometer(path: str) -> tuple[np.ndarray, np.ndarray]:
    ts, xyz = [], []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["sensor_type"] != "accelerometer":
                continue
            ts.append(int(row["capture_timestamp_ns"]))
            xyz.append((float(row["x"]), float(row["y"]), float(row["z"])))
    order = np.argsort(ts, kind="stable")
    ts_arr = np.asarray(ts, dtype=np.int64)[order]
    xyz_arr = np.asarray(xyz, dtype=np.float64)[order]
    # 时间戳完全重复的采样保留首个（Kotlin 对 dt=0 也会走重置分支，离线剔除更贴近回放语义）
    keep = np.concatenate([[True], np.diff(ts_arr) > 0])
    return ts_arr[keep], xyz_arr[keep]


def replay(ts: np.ndarray, xyz: np.ndarray, **params) -> dict:
    det = StepDetectorPort(**params)
    step_ts = []
    for t_ns, (ax, ay, az) in zip(ts, xyz):
        if det.on_sample(int(t_ns), float(ax), float(ay), float(az)):
            step_ts.append(int(t_ns))
    step_ts_arr = np.asarray(step_ts, dtype=np.int64)
    intervals_ms = (
        np.diff(step_ts_arr) / 1e6 if len(step_ts_arr) >= 2 else np.array([])
    )
    return {
        "steps": len(step_ts_arr),
        "step_ts": step_ts_arr,
        "intervals_ms": intervals_ms,
        "resets": det.diag_resets,
        "filtered": det.filtered,
        "rectified_ema": det.rectified_ema,
    }


def bandpass_cadence_hz(ts: np.ndarray, xyz: np.ndarray) -> float | None:
    """独立交叉验证：带通（0.5–3Hz）幅值信号的 dominant frequency。"""
    mag = np.linalg.norm(xyz, axis=1)
    mag = mag - np.convolve(mag, np.ones(41) / 41, mode="same")  # 去直流
    n = len(mag)
    if n < 128:
        return None
    spec = np.abs(np.fft.rfft(mag * np.hanning(n)))
    freqs = np.fft.rfftfreq(n, d=float(np.median(np.diff(ts))) / 1e9)
    band = (freqs >= 0.5) & (freqs <= 3.0)
    if not band.any():
        return None
    return float(freqs[band][np.argmax(spec[band])])


def fmt_pct(a: np.ndarray, q):
    return "n/a" if len(a) == 0 else f"{np.percentile(a, q):.0f}"


def report(ts, xyz, result, label=""):
    t0, t1 = ts[0], ts[-1]
    dur = (t1 - t0) / 1e9
    steps = result["steps"]
    iv = result["intervals_ms"]
    print(f"\n== StepDetector 回放 {label} ==")
    print(
        f"samples={len(ts)} duration={dur:.1f}s rate={len(ts)/dur:.1f}Hz "
        f"dtResets={result['resets']}"
    )
    print(f"steps={steps}")
    if steps >= 2:
        print(
            f"step intervals ms: p05={fmt_pct(iv,5)} p50={fmt_pct(iv,50)} "
            f"p95={fmt_pct(iv,95)} min={iv.min():.0f} max={iv.max():.0f}"
        )
        # 稳健步距（1–2s，即 0.5–1 步/s 正常行走区间）占比
        robust = float(np.mean((iv >= 350) & (iv <= 1500))) if len(iv) else 0.0
        print(f"步距在 350–1500ms 行走区间的比例: {robust*100:.0f}%")
        cad = bandpass_cadence_hz(ts, xyz)
        if cad:
            print(f"带通主频交叉验证: {cad:.2f} Hz（检出步率 {steps/dur:.2f} 步/s 含静止段）")
    # 10s 桶步数分布，定位行走段
    if steps:
        edges = np.arange(t0, t1 + 10_000_000_000, 10_000_000_000)
        counts, _ = np.histogram(result["step_ts"], bins=edges)
        bar = " ".join(f"{c:2d}" for c in counts)
        print(f"每 10s 步数: [{bar}]")


def sweep(ts, xyz):
    print("\n== 参数扫描（steps 总数 / 与基准差） ==")
    base = replay(ts, xyz)["steps"]
    print(f"基准(MIN={StepDetectorPort().min_threshold}, "
          f"F={StepDetectorPort().thresh_factor}, REF=250ms): {base} steps")
    print(f"{'MIN_TH':>6} {'FAC':>5} {'REFms':>6} | {'steps':>6} {'Δ':>5}")
    for min_th in (0.8, 1.0, 1.2, 1.5, 2.0):
        for fac in (1.3, 1.6, 1.9):
            for ref_ms in (200, 250, 300):
                r = replay(
                    ts, xyz, min_threshold=min_th, thresh_factor=fac,
                    refractory_ns=ref_ms * 1_000_000,
                )
                print(
                    f"{min_th:>6.1f} {fac:>5.1f} {ref_ms:>6d} | "
                    f"{r['steps']:>6d} {r['steps']-base:>+5d}"
                )


# ------------------------------------------------------------------ hybrid


def gait_wave(t: np.ndarray, cadence_hz: float, amplitude: float) -> np.ndarray:
    """步态波形：每次脚落地一个峰（半波正弦），叠加二次谐波削尖。"""
    phase = 2 * math.pi * cadence_hz * t
    base = np.maximum(0.0, np.sin(phase))
    return amplitude * (0.8 * base + 0.2 * base**2)


def hybrid_replay(ts, noise_mag, walk_mask, cadence_hz, amplitude, **params):
    """真实噪声 + 合成步态回放。返回 (总步数, 行走段步数, 静止段步数)。"""
    t = (ts - ts[0]) / 1e9
    gait = np.zeros_like(t)
    gait[walk_mask] = gait_wave(t[walk_mask], cadence_hz, amplitude)
    det = StepDetectorPort(**params)
    steps_all = steps_walk = steps_still = 0
    for i in range(len(ts)):
        hit = det.on_sample(int(ts[i]), 0.0, 0.0, float(noise_mag[i] + gait[i]))
        if hit:
            steps_all += 1
            if walk_mask[i]:
                steps_walk += 1
            else:
                steps_still += 1
    return steps_all, steps_walk, steps_still


def hybrid(ts, xyz):
    noise_mag = np.linalg.norm(xyz, axis=1)
    n = len(ts)
    dur = (ts[-1] - ts[0]) / 1e9

    # 场景 1：检测灵敏度地图（全程行走）
    print(f"\n== 混合测试 1：灵敏度地图（真实噪声 + 全程 {dur:.0f}s 行走） ==")
    print("幅值A m/s² \\ 步频Hz | " + " | ".join(f"{c:>5.1f}" for c in (1.0, 1.4, 1.8, 2.2, 2.6)))
    walk_all = np.ones(n, dtype=bool)
    for amp in (0.8, 1.0, 1.2, 1.5, 1.8, 2.2, 2.8, 3.5):
        cells = []
        for cad in (1.0, 1.4, 1.8, 2.2, 2.6):
            true_steps = round(dur * cad)
            _, sw, ss = hybrid_replay(ts, noise_mag, walk_all, cad, amp)
            recall = 100.0 * sw / true_steps if true_steps else 0.0
            fp_note = f"+{ss}虚" if ss else ""
            cells.append(f"{recall:4.0f}%{fp_note}")
        print(f"{amp:>19.1f} | " + " | ".join(f"{c:>7}" for c in cells))

    # 场景 2：导航典型时序（静止→行走→静止）
    print("\n== 混合测试 2：导航时序（10s 静止 → 行走 → 10s 静止） ==")
    t = (ts - ts[0]) / 1e9
    for cad, amp, walk_s in ((1.8, 2.0, 20.0), (1.5, 1.5, 30.0), (2.2, 1.2, 20.0)):
        mask = (t >= 10) & (t < 10 + walk_s)
        true_steps = round(walk_s * cad)
        sa, sw, ss = hybrid_replay(ts, noise_mag, mask, cad, amp)
        print(
            f"步频{cad}Hz 幅值{amp} 段长{walk_s:.0f}s: 真值{true_steps}步 → "
            f"行走段检出{sw}（{100.0*sw/true_steps:.0f}%），静止段虚警{ss}，总{sa}"
        )

    # 场景 3：边界工况下的参数扫描（弱步幅 1.2 + 慢步频 1.0）
    print("\n== 混合测试 3：边界工况参数扫描（全程行走 1.0Hz / 1.2 m/s²，真值 "
          f"{round(dur*1.0)} 步） ==")
    mask = np.ones(n, dtype=bool)
    print(f"{'MIN_TH':>6} {'FAC':>5} {'REFms':>6} | 行走段检出 虚警")
    for min_th in (0.8, 1.0, 1.2, 1.5):
        for fac in (1.3, 1.6, 1.9):
            for ref_ms in (200, 250, 300):
                _, sw, ss = hybrid_replay(
                    ts, noise_mag, mask, 1.0, 1.2,
                    min_threshold=min_th, thresh_factor=fac,
                    refractory_ns=ref_ms * 1_000_000,
                )
                print(f"{min_th:>6.1f} {fac:>5.1f} {ref_ms:>6d} | {sw:>8d} {ss:>4d}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--self-test", action="store_true", help="复刻 Kotlin 单测校验移植")
    ap.add_argument("--imu-csv", help="录制会话 imu.csv 路径")
    ap.add_argument("--sweep", action="store_true", help="MIN/FAC/REF 参数扫描")
    ap.add_argument("--hybrid", action="store_true",
                    help="真实噪声+合成步态混合测试（灵敏度/导航时序/参数扫描）")
    args = ap.parse_args()

    if args.self_test:
        print("== 移植保真度自检（复刻 StepDetectorTest.kt） ==")
        sys.exit(0 if self_test() else 1)
    if not args.imu_csv:
        ap.error("需要 --imu-csv 或 --self-test")

    ts, xyz = load_accelerometer(args.imu_csv)
    result = replay(ts, xyz)
    report(ts, xyz, result, label=args.imu_csv.split("/")[-2])
    if args.hybrid:
        hybrid(ts, xyz)
    if args.sweep:
        sweep(ts, xyz)


if __name__ == "__main__":
    main()
