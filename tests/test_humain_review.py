import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import audit_humain_cases as audit_script  # noqa: E402
from audit_humain_cases import REVIEW, RULES, audit, keyed_sha256  # noqa: E402

ID = re.compile(r"^humain-(aratruthfulqa|arapro|aramath)-\d{5}$")


def case(cid, source, options, **key):
    return {"id": cid, "provenance": {"sourceId": source}, "expected": {"options": options, **key}}


def tqa(cid, options, letter):
    return case(cid, "humain-aratruthfulqa", options, answerKey=letter)


def math(cid, options, letter):
    return case(cid, "humain-aramath", options, answerKey=letter)


def pro(cid, options, index):
    return case(cid, "humain-arapro", options, answerIndex=index)


def test_review_flags_without_changing_labels():
    review = json.loads(REVIEW.read_text(encoding="utf-8"))
    assert review["evaluated_revision"] == "3fa471f6c8ed2ebc37a8b60d40c88170d682b569"
    assert len(review["cases_sha256"]) == 64
    families = review["families"]
    assert len({f["id"] for f in families}) == len(families)
    distinct = set()
    for f in families:
        assert f["label_changed"] is False
        assert f["count"] == len(f["case_ids"]) == len(set(f["case_ids"]))
        assert all(ID.match(i) and i.startswith(f["source_id"]) for i in f["case_ids"])
        assert f["finding"] and f["proposed_next_revision"]
        if f["method"] == "explicit":
            assert set(f["checks"]) == set(f["case_ids"])
        else:
            assert f["method"] in RULES
        distinct |= set(f["case_ids"])
    assert len(distinct) == review["distinct_case_count"] == 196


def test_rules_fire_only_on_planted_faults():
    five = ["A. ok", "B. no", "C. no", "D. no", "E. ok"]
    assert RULES["last_option_key"](tqa("a", five, "E"))
    assert not RULES["last_option_key"](tqa("b", five, "A"))
    assert not RULES["last_option_key"](tqa("c", five[:4], "D"))
    twin = pro("d", ["red", "red", "blue", "green"], 1)
    other_twin = pro("e", ["red", "blue", "blue", "green"], 1)
    assert RULES["duplicate_options"](twin) and RULES["keyed_text_repeated"](twin)
    assert RULES["duplicate_options"](other_twin) and not RULES["keyed_text_repeated"](other_twin)
    assert RULES["float_artefact"](pro("f", ["0.30000000000000004", "0.1"], 1))
    assert not RULES["float_artefact"](pro("g", ["1.5", "2.25"], 1))


def test_shortest_and_equal_number_rules():
    shortest = math("h", ["(A) 12.75", "(B) 9", "(C) 13.25", "(D) 7.50"], "B")
    tied = math("i", ["(A) 12", "(B) 13", "(C) 14", "(D) 15"], "B")
    longest = math("j", ["(A) 3", "(B) 12.5", "(C) 4", "(D) 5"], "B")
    assert RULES["unique_shortest_key"](shortest)
    assert not RULES["unique_shortest_key"](tied) and not RULES["unique_shortest_key"](longest)
    assert RULES["numerically_equal_options"](
        math("k", ["(A) 6", "(B) 10.0", "(C) 3", "(D) 10"], "D")
    )
    assert not RULES["numerically_equal_options"](tied)
    assert not RULES["numerically_equal_options"](math("l", ["(A) x", "(B) x"], "A"))


def test_labels_are_stripped_only_at_their_own_position():
    assert audit_script.options(tqa("m", ["A. U.S. army", "B. B. text"], "A")) == [
        "U.S. army",
        "B. text",
    ]


def test_unsupported_keys_fail_loudly():
    with pytest.raises(ValueError):
        audit_script.key(tqa("n", ["A. x", "B. y"], "Z"))
    with pytest.raises(ValueError):
        audit_script.key(pro("o", ["x", "y"], 3))
    with pytest.raises(ValueError):
        audit_script.key(case("p", "other-source", ["x", "y"], answerKey="A"))


def test_audit_reports_missing_unlisted_and_changed_cases():
    kept = pro("humain-arapro-00001", ["x", "x"], 1)
    changed = pro("humain-arapro-00009", ["x", "y"], 2)
    review = {
        "families": [
            {
                "id": "f",
                "source_id": "humain-arapro",
                "method": "duplicate_options",
                "count": 1,
                "case_ids": ["humain-arapro-00001"],
            },
            {
                "id": "g",
                "source_id": "humain-arapro",
                "method": "explicit",
                "count": 1,
                "case_ids": ["humain-arapro-00009"],
                "checks": {
                    "humain-arapro-00009": {"key": "A", "keyed_option_sha256": keyed_sha256(kept)}
                },
            },
        ]
    }
    extra = pro("humain-arapro-00002", ["y", "y"], 2)
    assert audit([kept, changed, extra], review) == [
        ("f", ["humain-arapro-00002"], []),
        ("g", [], ["humain-arapro-00009"]),
    ]


def test_command_fails_on_a_release_without_the_flagged_cases(tmp_path, monkeypatch, capsys):
    cases = tmp_path / "cases.jsonl"
    cases.write_text(json.dumps(pro("humain-arapro-00001", ["x", "y"], 1)) + "\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["audit_humain_cases.py", str(cases)])
    with pytest.raises(SystemExit) as stop:
        audit_script.main()
    assert stop.value.code == 1
    assert "differs from the evaluated release" in capsys.readouterr().out
