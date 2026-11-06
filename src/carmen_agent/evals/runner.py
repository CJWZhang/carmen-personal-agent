from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from carmen_agent.actions.extractor import extract_action
from carmen_agent.connectors.base import SourceEvent
from carmen_agent.observability.telemetry import TelemetryStore

DEFAULT_CASES = Path(__file__).resolve().parents[3] / "evals" / "cases" / "extraction.json"


class CaseProvider:
    def __init__(self, output: str) -> None:
        self.output = output

    def generate(self, messages, **_):
        assert messages
        return self.output


@dataclass(frozen=True)
class EvalReport:
    cases: int
    true_positive: int
    false_positive: int
    true_negative: int
    false_negative: int
    type_correct: int

    @property
    def precision(self) -> float:
        denominator = self.true_positive + self.false_positive
        return self.true_positive / denominator if denominator else 1.0

    @property
    def recall(self) -> float:
        denominator = self.true_positive + self.false_negative
        return self.true_positive / denominator if denominator else 1.0

    @property
    def type_accuracy(self) -> float:
        positives = self.true_positive + self.false_negative
        return self.type_correct / positives if positives else 1.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "cases": self.cases,
            "precision": round(self.precision, 4),
            "recall": round(self.recall, 4),
            "type_accuracy": round(self.type_accuracy, 4),
            "confusion": {
                "true_positive": self.true_positive,
                "false_positive": self.false_positive,
                "true_negative": self.true_negative,
                "false_negative": self.false_negative,
            },
        }


def run_extraction_eval(path: Path = DEFAULT_CASES) -> EvalReport:
    records = json.loads(path.read_text(encoding="utf-8"))
    tp = fp = tn = fn = type_correct = 0
    for record in records:
        event = SourceEvent(**record["event"])
        result = extract_action(CaseProvider(record["model_output"]), event)  # type: ignore[arg-type]
        expected = bool(record["expected"]["is_actionable"])
        if result.is_actionable and expected:
            tp += 1
            type_correct += int(result.action_type == record["expected"]["action_type"])
        elif result.is_actionable:
            fp += 1
        elif expected:
            fn += 1
        else:
            tn += 1
    return EvalReport(len(records), tp, fp, tn, fn, type_correct)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run frozen Carmen quality evaluations.")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--min-precision", type=float, default=0.90)
    parser.add_argument("--min-recall", type=float, default=0.75)
    args = parser.parse_args()
    report = run_extraction_eval(args.cases)
    TelemetryStore().record(
        "quality_eval",
        cases=report.cases,
        precision=round(report.precision, 4),
        recall=round(report.recall, 4),
        type_accuracy=round(report.type_accuracy, 4),
        status="ok" if report.precision >= args.min_precision and report.recall >= args.min_recall else "failed",
    )
    print(json.dumps(report.as_dict(), indent=2))
    if report.precision < args.min_precision or report.recall < args.min_recall:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
