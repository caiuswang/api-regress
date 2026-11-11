"""导出 result.json 的 JSON Schema 到 schema/result.schema.json。

result.json 是对外的数据契约。改了 core/result.py（或它引用的 Finding、WarningItem 等模型）后运行
    uv run python scripts/export_result_schema.py
并在 review 时检查 git diff。tests/test_result_schema.py 会拦住没有重新导出的情况。
"""

from __future__ import annotations

import json
from pathlib import Path

from api_regress.core.result import RunResult

OUTPUT = Path(__file__).resolve().parent.parent / "schema" / "result.schema.json"


def render() -> str:
    schema = RunResult.model_json_schema(mode="serialization")
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["title"] = "api-regress result.json"
    return json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


if __name__ == "__main__":
    OUTPUT.write_text(render(), encoding="utf-8")
    print(f"写入 {OUTPUT}")
