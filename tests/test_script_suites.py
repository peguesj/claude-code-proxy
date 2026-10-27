import os
import subprocess
import sys

import pytest

from conftest import SCRIPT_SUITES

HERE = os.path.dirname(os.path.abspath(__file__))


@pytest.mark.parametrize("script", SCRIPT_SUITES)
def test_script_suite(script):
    proc = subprocess.run([sys.executable, os.path.join(HERE, script)], cwd=os.path.dirname(HERE),
                          capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, f"{script} failed:\n{proc.stdout[-3000:]}\n{proc.stderr[-3000:]}"
