import json

from takeover.core.types import Color
from takeover.record.eventlog import EventLog, mask_secrets


def test_mask_secrets_nested():
    record = {"line": "LOGIN me game,TRIP", "nested": {"url": "http://x/?token=TOK"}, "list": ["TRIP", 1]}
    masked = mask_secrets(record, ("TRIP", "TOK", ""))
    assert masked == {"line": "LOGIN me game,***", "nested": {"url": "http://x/?token=***"}, "list": ["***", 1]}
    assert record["line"].endswith("TRIP")  # 元は変更しない


def test_write_jsonl_with_monotonic_time(tmp_path, fake_clock):
    path = tmp_path / "logs" / "a.jsonl"
    log = EventLog(path, secrets=("SECRET",), now=fake_clock)
    log.write("usi_send", color=Color.BLACK, line="go SECRET")
    fake_clock.advance(250)
    log.write("mode_change", mode="manual")
    rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()]  # flush 済み
    log.close()
    assert [r["kind"] for r in rows] == ["usi_send", "mode_change"]
    assert rows[0]["line"] == "go ***" and rows[0]["color"] == "b"
    assert rows[1]["t"] - rows[0]["t"] == 250
    assert "wall" in rows[0]


def test_memory_mode_and_dump(tmp_path):
    log = EventLog(None)
    log.write("commit", usi="7g7f")
    assert log.records[0]["usi"] == "7g7f"
    log.dump(tmp_path / "x.jsonl")
    assert json.loads((tmp_path / "x.jsonl").read_text(encoding="utf-8"))["kind"] == "commit"
