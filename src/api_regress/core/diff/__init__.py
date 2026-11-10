"""JSON 对比引擎（纯函数）：输入两段已解析的 JSON 和规则，输出 finding 和 warning。"""

from api_regress.core.diff.context import CompareMode
from api_regress.core.diff.walker import DiffResult, diff_json

__all__ = ["CompareMode", "DiffResult", "diff_json"]
