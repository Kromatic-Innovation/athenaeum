
from athenaeum.push_metrics import build_push_record, record_push

def test_calls_record_push_with_no_explicit_cache_dir():
    # No cache_dir kwarg — the exact no-arg resolution the
    # athenaeum#791 defect took (75 synthetic rows in the
    # real ~/.cache/athenaeum/_push_records.jsonl).
    record = build_push_record(
        session_id="nested-pytest-pollution-probe-791",
        query="probe query",
        backend="fts5",
        hits=[
            ("probe.md", {"uid": "probe0001"}, 4),
        ],
    )
    assert record_push(record) is True
