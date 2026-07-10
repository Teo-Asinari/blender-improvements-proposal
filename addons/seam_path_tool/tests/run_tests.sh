#!/usr/bin/env bash
# Run the seam_path_tool headless test suite against the Windows Blender
# binary from WSL. Blender always exits 0 even when a --python script
# raises, so each test prints a sentinel and we grep for it.
#
# Usage: ./run_tests.sh  [path-to-blender.exe]

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
    out="$("$BLENDER" --background --factory-startup \
            --python "$(wslpath -w "$script")" 2>&1)"
    echo "$out" | sed -n '/^  /p;/TESTS_/p;/Traceback/,+15p'

    if echo "$out" | grep -q "$sentinel"; then
        echo "--- $(basename "$script"): PASS"
        return 0
    else
        echo "--- $(basename "$script"): FAIL (sentinel '$sentinel' not found)"
        echo "$out"
        return 1
    fi
}

# NOTE: profile_commit.py is a profiling harness, not a test — it is
# deliberately not run here.
status=0
run_one "$TESTS_DIR/test_core.py" "CORE_TESTS_PASSED" || status=1
run_one "$TESTS_DIR/test_incremental.py" "INCREMENTAL_TESTS_PASSED" || status=1
run_one "$TESTS_DIR/test_backends.py" "BACKEND_TESTS_PASSED" || status=1
run_one "$TESTS_DIR/test_session.py" "SESSION_TESTS_PASSED" || status=1
run_one "$TESTS_DIR/test_register.py" "REGISTER_TESTS_PASSED" || status=1

echo
if [ "$status" -eq 0 ]; then
    echo "ALL_TESTS_PASSED"
else
    echo "TESTS_FAILED"
fi
exit "$status"
