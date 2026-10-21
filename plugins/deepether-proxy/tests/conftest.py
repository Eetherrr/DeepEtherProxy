"""测试夹具。

把两个路径加入 `sys.path`：
1. 插件根目录 —— 让 `deepether` 包可被直接导入；
2. 测试实例根目录（如果存在）—— 让"用宿主自带 ManifestValidator 校验 manifest"的用例可运行。
"""

from pathlib import Path

import os
import sys
import tempfile

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = PLUGIN_ROOT.parent.parent
MAIBOT_TEST_ROOT = REPO_ROOT / ".testenv" / "maibot"

# 某些用例会 import src.plugin_runtime.runner.manifest_validator，进而触发
# src.common.logger 在"当前工作目录"创建 logs/。把工作目录切到临时目录，
# 避免跑一次单测就在仓库里留下 logs/ 垃圾。
_SANDBOX_CWD = Path(tempfile.mkdtemp(prefix="deepether-tests-"))
os.chdir(_SANDBOX_CWD)

for candidate in (PLUGIN_ROOT, MAIBOT_TEST_ROOT):
    if candidate.exists() and str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))
