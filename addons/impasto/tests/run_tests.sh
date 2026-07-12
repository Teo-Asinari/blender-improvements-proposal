#!/usr/bin/env bash
# Real-Blender test runner. Blender may exit zero after a Python traceback,
# so success is determined by explicit sentinels.

set -u

BLENDER="${1:-/mnt/c/Program Files/Blender Foundation/Blender 5.1/blender.exe}"
TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ ! -e "$BLENDER" ]; then
    echo "ERROR: Blender binary not found at: $BLENDER" >&2
    exit 2
fi

run_one() {
    local script="$1"
    local sentinel="$2"
    local out
    echo "=== Running $(basename "$script") ==="
    out="$("$BLENDER" --background --factory-startup --python "$(wslpath -w "$script")" 2>&1)"
    echo "$out" | sed -n '/^  /p;/IMPASTO_/p;/Traceback/,+15p'
    if echo "$out" | grep -q "$sentinel"; then
        echo "--- $(basename "$script"): PASS"
        return 0
    fi
    echo "--- $(basename "$script"): FAIL (sentinel '$sentinel' not found)"
    echo "$out"
    return 1
}

status=0
run_one "$TESTS_DIR/test_integration.py" "IMPASTO_INTEGRATION_PASSED" || status=1
run_one "$TESTS_DIR/test_native_paint.py" "IMPASTO_NATIVE_PAINT_PASSED" || status=1
run_one "$TESTS_DIR/test_scalar_channels.py" "IMPASTO_SCALAR_CHANNELS_PASSED" || status=1
run_one "$TESTS_DIR/test_persistence.py" "IMPASTO_PERSISTENCE_PASSED" || status=1
run_one "$TESTS_DIR/test_restore.py" "IMPASTO_RESTORE_PASSED" || status=1
run_one "$TESTS_DIR/test_undo.py" "IMPASTO_UNDO_PASSED" || status=1
if [ "$status" -eq 0 ]; then
    echo "ALL_TESTS_PASSED"
else
    echo "TESTS_FAILED"
fi
exit "$status"
