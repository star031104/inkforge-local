"""Run a versioned fixture suite, or a user-supplied candidate suite, offline."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.evaluation import evaluate_prompt_suite, evaluate_suite  # noqa: E402

parser = argparse.ArgumentParser()
parser.add_argument("--cases", type=Path, default=ROOT / "tests/fixtures/writing_cases.json")
parser.add_argument(
    "--prompt-cases",
    type=Path,
    default=ROOT / "tests/fixtures/prompt_cases.json",
)
parser.add_argument("--output", type=Path, default=ROOT / "output/evaluation.json")
args = parser.parse_args()
writing = evaluate_suite(json.loads(args.cases.read_text(encoding="utf-8")))
prompts = evaluate_prompt_suite(
    json.loads(args.prompt_cases.read_text(encoding="utf-8"))
)
report = {
    "schema_version": 2,
    "passed": writing["passed"] + prompts["passed"],
    "total": writing["total"] + prompts["total"],
    "writing": writing,
    "prompts": prompts,
}
args.output.parent.mkdir(parents=True, exist_ok=True)
args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"{report['passed']}/{report['total']} cases passed; {args.output}")
sys.exit(0 if report["passed"] == report["total"] else 1)
