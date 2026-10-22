import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import audit_release_cases as audit_script  # noqa: E402
from audit_release_cases import REVIEW, RULES, audit, sha256  # noqa: E402

ID_PATTERNS = {
    "commonsense-validation": r"commonsense-validation-\d{5}",
    "QCRI/IslamicFaithQA": r"islamicfaithqa-ar-\d{5}",
    "arbml-arabic-rc": r"arbml-rc-\d{5}",
    "mena-values": r"mena-values-\d{4}-review-copy",
    "dialectal-arabic-mmlu": r"dialectal-arabic-mmlu-\d{5}",
    "arabic-exams": r"arabic-exams-\d{5}",
    "aratrust": r"aratrust-\d{4}",
    "arabicmmlu": r"arabicmmlu-\d+-[0-9a-f]{8}",
    "almrsal-riddles": r"scraped-almrsal-riddles-\d{3}",
}
HEX64 = re.compile(r"^[0-9a-f]{64}$")
ARABIC_SCRIPT = audit_script.ARABIC_SCRIPT
# Answer keys may be Arabic letters; no other Arabic text may appear in the review.
KEY_FIELD = re.compile(r'"key": "[^"]*"')
YES_NO_QUESTION = "Are the two sentences consistent?"


def case(cid, source, prompt="Question?", language="ar", **expected):
    return {
        "id": cid,
        "language": language,
        "prompt": prompt,
        "provenance": {"sourceId": source},
        "expected": expected,
    }


def commonsense(cid, key="0", lines=3, question=YES_NO_QUESTION, **extra):
    prompt = "\n".join(["First: a", "Second: b", question][-lines:])
    return case(cid, "commonsense-validation", prompt, answerKey=key, **extra)


def rc(cid, context_length, label_ok=True):
    head = audit_script.CONTEXT_LABEL if label_ok else "Context:\n"
    prompt = head + "x" * context_length + audit_script.QUESTION_LABEL + "who?"
    return case(cid, "arbml-arabic-rc", prompt, answer="a")


def mena(cid, **countries):
    return case(cid, "mena-values", scale="y", surveyDistributions=countries)


def mmlu(cid, prompt, options, language="ar"):
    return case(cid, "dialectal-arabic-mmlu", prompt, language, answerKey="0", options=options)


def test_review_flags_without_changing_labels():
    review = json.loads(REVIEW.read_text(encoding="utf-8"))
    assert review["evaluated_version"] == "2026.09.27.1"
    assert review["evaluated_revision"] == "3fa471f6c8ed2ebc37a8b60d40c88170d682b569"
    assert HEX64.match(review["cases_sha256"])
    assert review["reviewer"].endswith("not independent Arabic expert review")
    families = review["families"]
    assert len({f["id"] for f in families}) == len(families)
    distinct = set()
    for f in families:
        assert f["label_changed"] is False
        assert f["count"] == len(f["case_ids"]) == len(set(f["case_ids"]))
        assert f["source_id"] in ID_PATTERNS
        assert all(re.fullmatch(ID_PATTERNS[f["source_id"]], i) for i in f["case_ids"])
        assert not any(i.startswith("humain-") for i in f["case_ids"])
        assert f["status"] and f["finding"] and f["proposed_next_revision"]
        if f["method"] == "explicit":
            assert set(f["checks"]) == set(f["case_ids"])
            for check in f["checks"].values():
                assert HEX64.match(check["prompt_sha256"])
                if "answer_sha256" in check:
                    assert HEX64.match(check["answer_sha256"]) and "key" not in check
                else:
                    assert check["key"] and HEX64.match(check["keyed_option_sha256"])
        else:
            assert f["method"] in RULES
        distinct |= set(f["case_ids"])
    assert len(distinct) == review["distinct_case_count"]


def test_review_stores_no_dataset_text():
    text = REVIEW.read_text(encoding="utf-8")
    assert not ARABIC_SCRIPT.search(KEY_FIELD.sub("", text))


def test_commonsense_rule_needs_yes_no_prompt_and_index_key_without_options(monkeypatch):
    monkeypatch.setattr(audit_script, "YES_NO_QUESTION_SHA256", sha256(YES_NO_QUESTION))
    rule = RULES["commonsense_index_key"]
    assert rule(commonsense("a", "0"))
    assert rule(commonsense("b", "1"))
    assert not rule(commonsense("c", "2"))
    assert not rule(commonsense("d", "0", question="Which sentence makes sense?"))
    assert not rule(commonsense("e", "0", lines=2))
    assert not rule(commonsense("f", "0", options=["A. x", "B. y"]))


def test_context_cap_rule_uses_the_context_not_the_prompt():
    rule = RULES["context_at_length_cap"]
    assert rule(rc("a", 32_767))
    assert rule(rc("b", 32_751))
    assert not rule(rc("c", 32_750))
    assert not rule(rc("d", 31_173))
    assert not rule(rc("e", 32_768))
    assert not rule(rc("f", 32_767, label_ok=False))
    assert not rule(case("g", "arbml-arabic-rc", "x" * 40_000, answer="a"))


def test_survey_rule_needs_every_country_without_data():
    rule = RULES["survey_no_data"]
    assert rule(mena("a", Egypt="(No data, {})", Iraq="(No data, {})"))
    assert rule(mena("b", Egypt="(No values <= 3, {})"))
    assert not rule(mena("c", Egypt="(No data, {})", Iraq="(2.5, {'1': '50.00%'})"))
    assert not rule(mena("d"))


def test_english_label_rule_needs_no_arabic_script_anywhere():
    rule = RULES["english_labelled_arabic"]
    assert rule(mmlu("a", "What is two plus two?", ["3", "4"]))
    assert not rule(mmlu("b", "What is two plus two?", ["3", "٤"]))
    assert not rule(mmlu("c", "ما هو؟", ["3", "4"]))
    assert not rule(mmlu("d", "What is two plus two?", ["3", "4"], language="en"))
    assert not rule(mmlu("e", "What is two plus two?", []))


def test_key_index_reads_latin_and_arabic_letters_and_rejects_the_rest():
    latin = case("a", "arabic-exams", answerKey="C", options=["x", "y", "z", "w"])
    arabic = case("b", "aratrust", answerKey="ج", options=["x", "y", "z"])
    assert audit_script.key_index(latin) == 2
    assert audit_script.key_index(arabic) == 2
    for key, options in [("D", ["x", "y", "z"]), ("", ["x"]), ("AB", ["x", "y"]), (None, ["x"])]:
        with pytest.raises(ValueError):
            audit_script.key_index(case("c", "arabic-exams", answerKey=key, options=options))


def explicit_family(cid, check, source="arabic-exams"):
    return {
        "id": "f",
        "source_id": source,
        "method": "explicit",
        "count": 1,
        "case_ids": [cid],
        "checks": {cid: check},
    }


def test_explicit_checks_detect_a_changed_key_option_prompt_or_answer():
    kept = case("arabic-exams-00001", "arabic-exams", "Q one", answerKey="B", options=["x", "y"])
    check = {"prompt_sha256": sha256("Q one"), "key": "B", "keyed_option_sha256": sha256("y")}
    review = {"families": [explicit_family("arabic-exams-00001", check)]}
    assert audit([kept], review) == []

    def changed(**patch):
        data = json.loads(json.dumps(kept))
        data["prompt"] = patch.get("prompt", data["prompt"])
        data["expected"].update(patch.get("expected", {}))
        return data

    gone = [("f", [], ["arabic-exams-00001"])]
    assert audit([changed(expected={"answerKey": "A"})], review) == gone
    assert audit([changed(expected={"options": ["x", "z"]})], review) == gone
    assert audit([changed(prompt="Q one, now with context")], review) == gone
    assert audit([], review) == gone
    other_source = json.loads(json.dumps(kept))
    other_source["provenance"]["sourceId"] = "aratrust"
    assert audit([other_source], review) == gone

    answer = case("islamicfaithqa-ar-00001", "QCRI/IslamicFaithQA", "Q", answer="four shares")
    answer_check = {"prompt_sha256": sha256("Q"), "answer_sha256": sha256("four shares")}
    answer_review = {
        "families": [
            explicit_family("islamicfaithqa-ar-00001", answer_check, "QCRI/IslamicFaithQA")
        ]
    }
    assert audit([answer], answer_review) == []
    answer["expected"]["answer"] = "five shares"
    assert audit([answer], answer_review) == [("f", [], ["islamicfaithqa-ar-00001"])]


def test_rule_families_report_missing_and_unlisted_cases():
    listed = mena("mena-values-0001-review-copy", Egypt="(No data, {})")
    other = mena("mena-values-0002-review-copy", Egypt="(No data, {})")
    fine = mena("mena-values-0003-review-copy", Egypt="(2.0, {'1': '100.00%'})")
    family = {
        "id": "g",
        "source_id": "mena-values",
        "method": "survey_no_data",
        "count": 1,
        "case_ids": [listed["id"]],
    }
    assert audit([listed, fine], {"families": [family]}) == []
    assert audit([listed, other, fine], {"families": [family]}) == [("g", [other["id"]], [])]
    assert audit([fine], {"families": [family]}) == [("g", [], [listed["id"]])]
    # A rule only looks at the family's own source.
    stray = case("arabic-exams-00009", "arabic-exams", surveyDistributions={})
    assert audit([listed, stray], {"families": [family]}) == []


def write_release(tmp_path, cases):
    path = tmp_path / "cases.jsonl"
    path.write_text("".join(json.dumps(c) + "\n" for c in cases), encoding="utf-8")
    return path


def run_command(monkeypatch, *args):
    monkeypatch.setattr(sys, "argv", ["audit_release_cases.py", *map(str, args)])
    with pytest.raises(SystemExit) as stop:
        audit_script.main()
    return stop.value.code


def test_command_fails_on_a_release_without_the_flagged_cases(tmp_path, monkeypatch, capsys):
    cases = write_release(tmp_path, [case("arabic-exams-00001", "arabic-exams", answerKey="A")])
    assert run_command(monkeypatch, cases) == 1
    assert "differs from the evaluated release" in capsys.readouterr().out


def test_command_passes_on_a_release_that_matches_its_review(tmp_path, monkeypatch, capsys):
    flagged = mena("mena-values-0001-review-copy", Egypt="(No data, {})")
    cases = write_release(tmp_path, [flagged])
    digest = sha256(cases.read_text(encoding="utf-8"))
    review = {
        "cases_sha256": digest,
        "distinct_case_count": 1,
        "families": [
            {
                "id": "g",
                "source_id": "mena-values",
                "method": "survey_no_data",
                "count": 1,
                "case_ids": [flagged["id"]],
            }
        ],
    }
    path = tmp_path / "review.json"
    path.write_text(json.dumps(review), encoding="utf-8")
    assert run_command(monkeypatch, cases, "--review", path) == 0
    assert "(evaluated release)" in capsys.readouterr().out
    review["distinct_case_count"] = 2
    path.write_text(json.dumps(review), encoding="utf-8")
    assert run_command(monkeypatch, cases, "--review", path) == 1
