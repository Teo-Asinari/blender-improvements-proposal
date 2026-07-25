import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "impasto_performance", ROOT / "performance.py")
performance = importlib.util.module_from_spec(spec)
spec.loader.exec_module(performance)


def check(label, condition):
    if not condition:
        raise AssertionError(label)
    print("  ok ", label)


check("one 4K RGBA16F channel is 128 MiB",
      performance.texture_bytes(4096) == 128 * 1024 * 1024)
estimate = performance.session_memory_estimate(4096, 7)
check("seven-channel 4K canvas is 896 MiB",
      estimate["canvas_bytes"] == 896 * 1024 * 1024)
check("shared brush scratch is included once",
      estimate["resident_bytes"] == 1024 * 1024 * 1024)
check("Blender float image backing is reported separately",
      estimate["cpu_image_bytes"] == 1792 * 1024 * 1024)
check("legacy full-copy traffic scales by target channels",
      performance.full_copy_bytes_per_dab(4096, 4)
      == 512 * 1024 * 1024)
matrix = performance.benchmark_matrix()
check("benchmark matrix covers four modes at 1/4/8 channels",
      len(matrix) == 12
      and {row["mode"] for row in matrix}
      == {"PAINT", "ERASE", "SOFTEN", "SMEAR"}
      and {row["channels"] for row in matrix} == {1, 4, 8})

print("IMPASTO_PERFORMANCE_ESTIMATES_PASSED")
