import json
from pathlib import Path

from carmen_agent.prepare import contains_secret, normalize_text, prepare


def test_normalize_text_redacts_identifiers() -> None:
    text = "ping <@U123ABC> at carmen@example.com: https://example.com/a?id=1"
    assert normalize_text(text) == "ping @person at <EMAIL>: <LINK>"


def test_secret_detection() -> None:
    assert contains_secret("api_key = definitely-not-for-training")
    assert not contains_secret("the api key docs are confusing")


def test_prepare_writes_mlx_chat_splits(tmp_path: Path) -> None:
    raw = tmp_path / "raw.jsonl"
    rows = [
        {
            "id": f"D1:{index}",
            "channel_type": "dm" if index % 2 else "channel",
            "timestamp": str(index),
            "text": f"message number {index}!",
        }
        for index in range(80)
    ]
    raw.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    output = tmp_path / "processed"
    report = prepare(raw, output, seed=42)
    assert report["filtering"]["kept"] == 80
    assert sum(report["splits"].values()) == 80
    sample = json.loads((output / "train.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert sample["messages"][-1]["role"] == "assistant"
    assert (output / "style_profile.json").exists()


def test_prepare_teaches_email_as_a_distinct_writing_register(tmp_path: Path) -> None:
    raw = tmp_path / "raw.jsonl"
    rows = [
        {
            "id": f"gmail:{index}",
            "channel_type": "email",
            "timestamp": str(index),
            "text": f"Thank you for the update number {index}. I will review it tomorrow.",
        }
        for index in range(60)
    ]
    raw.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    output = tmp_path / "processed"

    report = prepare(raw, output, seed=42)

    assert report["style_profile"]["writing_registers"] == {"email": 60}
    examples = [
        json.loads(line)
        for split in ("train", "valid", "test")
        for line in (output / f"{split}.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert all("professional email" in item["messages"][1]["content"] for item in examples)
