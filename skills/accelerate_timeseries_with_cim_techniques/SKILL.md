---
name: accelerate-timeseries-with-cim-techniques
description: Optimize Python time-series backtesting code by applying COTS-compatible computing-in-memory (CIM) inspired techniques such as SIMD vectorization, XOR delta detection, block scans, and rolling-window incremental updates when they provide measurable performance improvements.
---

# Skill: Accelerate Time-Series Backtesting with CIM Techniques

## Purpose
This skill analyzes Python time-series backtesting code and applies performance optimizations inspired by computing-in-memory (CIM) research. The goal is to reduce runtime for long simulations involving rolling-window computations such as moving averages, volatility calculations, and signal generation.

The skill should only implement these optimizations **when they produce a measurable performance improvement** over the baseline implementation.

The system must remain compatible with **commodity hardware (COTS)** and standard Python environments.

---

# When to Use This Skill

Use this skill when code contains:

- Large time-series datasets (hundreds of thousands to millions of rows)
- Long-running backtests
- Rolling window calculations
- Signal detection over price streams
- Repeated recomputation across overlapping windows
- Python loops iterating over time-series data

Typical workloads include:

- Moving averages
- Rolling standard deviation
- Momentum indicators
- Signal crossover detection
- Event flag generation
- Backtest simulation loops

---

# Optimization Techniques Available

The following CIM-inspired techniques may be used.

## 1. SIMD Vectorization

Replace Python loops with NumPy vectorized operations.

Example:

Baseline:

```python
for i in range(len(prices)):
    if prices[i] > threshold:
        signal[i] = 1
```

Vectorized:

```python
signal = prices > threshold
```

---

## 2. Incremental Rolling Window Updates

Avoid recomputing full rolling windows when possible.

Instead of recomputing the mean every iteration:

```python
mean(prices[i-window:i])
```

Use incremental updates:

```python
new_avg = old_avg + (new_value - old_value) / window
```

This reduces complexity from:

O(n * window)

to:

O(n)

---

## 3. XOR Delta Detection (Gorilla-style)

Use XOR or diff logic to detect changes instead of recomputing signals.

Example:

```python
delta = np.bitwise_xor(series, np.roll(series, 1))
```

Useful for:

- detecting state transitions
- identifying signal changes
- compressing event streams

---

## 4. Bitmask Filtering

Binary event streams can be represented as boolean arrays.

Example:

```python
buy_mask = short_ma > long_ma
```

Operations such as:

- event detection
- signal filtering
- state transitions

can be performed using vectorized boolean masks.

---

## 5. Block Processing (BitWeaving-style)

Process time-series in fixed blocks to exploit CPU cache locality.

Example workflow:

```python
blocks = series.reshape(-1, block_size)
result = blocks.sum(axis=1)
```

Useful for:

- threshold scans
- density detection
- rolling signal aggregation

---

## 6. Numba JIT Acceleration

If vectorization is not possible, apply JIT compilation using Numba.

Example:

```python
from numba import njit

@njit
def rolling_mean(data, window):
    pass
```

This removes Python loop overhead.

---

# Constraints

## Hardware Constraints

Allowed:

- CPU SIMD via NumPy
- Numba JIT
- vectorized Python libraries

Not allowed:

- GPUs
- custom memory hardware
- FPGA acceleration
- non-portable system dependencies

---

## Maintain Correctness

Optimized implementations must produce results equivalent to the original code.

Floating-point precision differences must be documented.

---

## Implement Only When Beneficial

Before replacing code:

1. Estimate complexity reduction
2. Evaluate memory tradeoffs
3. Confirm that speed improvement is likely

If no meaningful gain is expected, return the original code unchanged.

---

# Required Output Format

When this skill is applied, respond with:

1. Original Code Summary
2. Identified Bottleneck
3. Optimization Strategy
4. Optimized Implementation
5. Expected Performance Impact

---

# Example

## Input

```python
moving_avg = np.zeros_like(prices)

for i in range(window, len(prices)):
    moving_avg[i] = np.mean(prices[i-window:i])
```

---

## Optimized Implementation

```python
import numpy as np

def rolling_mean(prices, window):
    cumsum = np.cumsum(prices)
    cumsum[window:] = cumsum[window:] - cumsum[:-window]
    result = cumsum[window-1:] / window
    return result
```

---

## Expected Performance Impact

Time complexity reduced from:

O(n * window)

to

O(n)

Typical speedup for large series:

10x – 50x

---

# Additional Notes

These optimizations are inspired by techniques used in:

- SIMDRAM research
- BitWeaving
- Gorilla time-series compression
- vectorized database engines

However they must remain compatible with standard Python environments and commodity CPUs.

The goal is to bring CIM-style data-parallel thinking into software implementations of time-series backtesting engines.
