"""result.json 契约：仓库里的 JSON Schema 与模型一致，输出能通过校验，finding_id 稳定。"""

import importlib.util
import json
from pathlib import Path

import jsonschema

from tests.mock_api import Server, j, req, run, suite_text

SCRIPT = Path(__file__).parent.parent / "scripts" / "export_result_schema.py"


def _module():
    spec = importlib.util.spec_from_file_location("export_result_schema", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_committed_schema_is_up_to_date():
    module = _module()
    assert module.OUTPUT.read_text(encoding="utf-8") == module.render(), (
        "result.json 的模型变了：确认后运行 uv run python scripts/export_result_schema.py"
    )


def _keyed_server():
    base = '{"code": 0, "data": {"amount": 1.50, "items": [{"sku": "A", "n": 1}, {"sku": "B", "n": 1}]}}'
    cand = '{"code": 0, "data": {"amount": "1.5", "items": [{"sku": "A", "n": "1"}, {"sku": "B", "n": "1"}]}}'
    return Server().on("base", "/x", j(base)).on("cand", "/x", j(cand))


KEYED = req("a", extra="    rules:\n      - path: data.items\n        array: {keys: [sku]}\n")


def test_result_json_validates_against_schema(tmp_path):
    report = run(tmp_path, suite_text(KEYED), _keyed_server())
    data = json.loads((Path(report.out_dir) / "result.json").read_text())
    schema = json.loads(_module().OUTPUT.read_text())
    jsonschema.validate(data, schema)
    amount = next(f for f in data["requests"][0]["findings"] if f["path"] == "data.amount")
    assert amount["base_value"] == "1.50" and amount["base_type"] == "number"  # Decimal 输出为保留小数位的字符串
    assert amount["cand_value"] == "1.5" and amount["cand_type"] == "string"


def test_finding_id_stable_and_distinct(tmp_path):
    first = run(tmp_path / "1", suite_text(KEYED), _keyed_server())
    second = run(tmp_path / "2", suite_text(KEYED), _keyed_server())
    ids = [f.finding_id for f in first.result.requests[0].findings]
    assert ids == [f.finding_id for f in second.result.requests[0].findings]
    items = {f.path: f.finding_id for f in first.result.requests[0].findings if f.path.startswith("data.items")}
    assert set(items) == {"data.items[sku=A].n", "data.items[sku=B].n"}
    assert len(set(items.values())) == 2
    assert all(len(i) == 16 for i in ids)
