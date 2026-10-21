"""Copy the landing page from its own project (../podsen-landing) into landing/, which
app.py serves at /. Run after editing the landing page, then commit landing/.

    python sync_landing.py [path/to/podsen-landing]
"""

import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
src = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE.parent / "podsen-landing"
dst = HERE / "landing"

if not (src / "index.html").is_file():
    sys.exit(f"No index.html in {src}")
shutil.rmtree(dst, ignore_errors=True)
shutil.copytree(src, dst, ignore=shutil.ignore_patterns(".git", ".vercel", "vercel.json", "node_modules"))
print(f"Copied {src} -> {dst}")
