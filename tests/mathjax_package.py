"""The MathJax the MathJax tests run, found in one place.

The tests that run MathJax under Node.js need an unpacked MathJax 3.2.2
package, the directory holding its ``package.json`` and ``es5/``, named by
the environment variable ``AUTOFORM_MATHJAX_DIR``. MathJax is not part of
the repository. To run them::

    AUTOFORM_MATHJAX_DIR=/path/to/MathJax-3.2.2 pytest tests/test_site_math.py tests/test_testimony_mathjax.py

Only the package root is accepted, not its ``es5`` directory: the tests
check the release against ``package.json``, and one convention leaves no
setting that runs some of them and skips others. Without Node.js or the
variable they skip, saying which is missing; a variable that names anything
else fails them, so a run meant to check MathJax cannot pass by skipping.
"""

import json
import os
from pathlib import Path
import shutil

import pytest

MATHJAX_VERSION = "3.2.2"
NODE = shutil.which("node") or shutil.which("node", path="/opt/homebrew/bin")


def mathjax_package() -> Path:
    """The MathJax package root ``AUTOFORM_MATHJAX_DIR`` names, after checking
    that Node.js is installed and the package is MathJax 3.2.2."""

    if NODE is None:
        pytest.skip("Node.js is not on PATH or at /opt/homebrew/bin/node")
    setting = os.environ.get("AUTOFORM_MATHJAX_DIR", "")
    if not setting:
        pytest.skip(f"AUTOFORM_MATHJAX_DIR is not set; set it to an unpacked MathJax {MATHJAX_VERSION} package")
    root = Path(setting)
    if (root / "node-main.js").is_file() and (root.parent / "package.json").is_file():
        pytest.fail(f"AUTOFORM_MATHJAX_DIR names MathJax's es5 directory; set it to the package root, {root.parent}")
    if not (root / "package.json").is_file() or not (root / "es5" / "node-main.js").is_file():
        pytest.fail(f"AUTOFORM_MATHJAX_DIR={setting} is not a MathJax package: no package.json and es5/node-main.js")
    version = json.loads((root / "package.json").read_text(encoding="utf-8")).get("version")
    if version != MATHJAX_VERSION:
        pytest.fail(f"AUTOFORM_MATHJAX_DIR={setting} is MathJax {version}, not {MATHJAX_VERSION}")
    return root
