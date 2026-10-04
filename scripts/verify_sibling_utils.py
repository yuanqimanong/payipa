"""用兄弟目录的工具库验证两个仓库；不修改 pyproject、lock 或已安装依赖。"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    project = Path(__file__).resolve().parents[1]
    utilities = project.parent / "jianbing_utils"
    source = utilities / "src"
    if not (source / "jianbing_utils" / "__init__.py").is_file():
        print(f"Missing sibling utility checkout: {utilities}", file=sys.stderr)
        return 2
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (str(source), env.get("PYTHONPATH", ""))))
    print(f"Validating payipa with local utilities: {source}", flush=True)
    for directory in (utilities, project):
        result = subprocess.run([sys.executable, "-m", "pytest", "-ra"], cwd=directory, env=env, check=False)
        if result.returncode:
            return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
