"""Recompute reviews/other-source-cases.json from a release's cases.jsonl.

Download the evaluated release and check it against releases/current.json:

    curl -LO https://huggingface.co/datasets/najdresearch/najd-benchmark/resolve/3fa471f6c8ed2ebc37a8b60d40c88170d682b569/datasets/najd-benchmark/2026.09.27.1/cases.jsonl
    uv run python scripts/audit_release_cases.py cases.jsonl

Rule families are recomputed from each case's own prompt, options, key and metadata. Explicit
families are checked against a SHA-256 of the prompt plus either the recorded key with a SHA-256
of the keyed option, or a SHA-256 of the expected answer, so no dataset text is stored and a
changed case is detected. The script prints whether the input is the evaluated release and exits 1
on any mismatch.
"""

import argparse
import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REVIEW = ROOT / "reviews/other-source-cases.json"
LATIN = "ABCDEFGH"
# Arabic letter keys in order: alef with hamza, ba, jeem, dal.
ARABIC = "أبجد"
# sha256 of the final prompt line shared by every commonsense-validation case: the yes/no
# question "are the two sentences consistent?".
YES_NO_QUESTION_SHA256 = "f46dcd0732a092775372a900763f73ddfc510915c30e812cbe8f7159f085cace"
# arbml-arabic-rc prompts are "<context label>\n<context>\n\n<question label>\n<question>".
CONTEXT_LABEL = "السياق:\n"
QUESTION_LABEL = "\n\nالسؤال:\n"
CONTEXT_CAP = 32_767
CONTEXT_CAP_WINDOW = 17
ARABIC_SCRIPT = re.compile("[؀-ۿݐ-ݿࢠ-ࣿﭐ-﷿ﹰ-﻿]")
NO_SURVEY_DATA = re.compile(r"\(No [^,]*, \{\}\)")


def sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def options(case):
    return [str(option) for option in case["expected"].get("options") or []]


def key_index(case):
    """Position of the keyed option; Latin letters and Arabic letters are both supported."""
    key = str(case["expected"].get("answerKey"))
    count = len(options(case))
    if len(key) == 1 and key in LATIN[:count]:
        return LATIN.index(key)
    if len(key) == 1 and key in ARABIC[:count]:
        return ARABIC.index(key)
    raise ValueError(f"{case['id']}: unsupported answer key")


def commonsense_index_key(case):
    """Yes/no question, a 0/1 key and no options: the key cannot be an answer to the question."""
    expected = case["expected"]
    lines = case["prompt"].split("\n")
    return (
        not expected.get("options")
        and str(expected.get("answerKey")) in {"0", "1"}
        and len(lines) == 3
        and sha256(lines[-1]) == YES_NO_QUESTION_SHA256
    )


def context_at_length_cap(case):
    """Context length within the last 17 characters below the 32,767-character cap."""
    head, separator, _ = case["prompt"].partition(QUESTION_LABEL)
    if not separator or not head.startswith(CONTEXT_LABEL):
        return False
    return CONTEXT_CAP - CONTEXT_CAP_WINDOW < len(head) - len(CONTEXT_LABEL) <= CONTEXT_CAP


def survey_no_data(case):
    """Every country entry is a no-data marker, so there is no reference distribution."""
    distributions = case["expected"].get("surveyDistributions") or {}
    return bool(distributions) and all(
        NO_SURVEY_DATA.fullmatch(str(value).strip()) for value in distributions.values()
    )


def english_labelled_arabic(case):
    """Language is "ar" but the prompt and every option contain no Arabic-script character."""
    text = case["prompt"] + " ".join(options(case))
    return (
        case.get("language") == "ar"
        and bool(options(case))
        and bool(text.strip())
        and not ARABIC_SCRIPT.search(text)
    )


RULES = {
    "commonsense_index_key": commonsense_index_key,
    "context_at_length_cap": context_at_length_cap,
    "survey_no_data": survey_no_data,
    "english_labelled_arabic": english_labelled_arabic,
}


def explicit_matches(case, check):
    if sha256(case["prompt"]) != check.get("prompt_sha256"):
        return False
    if "answer_sha256" in check:
        return sha256(str(case["expected"].get("answer"))) == check["answer_sha256"]
    try:
        index = key_index(case)
    except ValueError:
        return False
    keyed_option = options(case)[index]
    same_key = str(case["expected"]["answerKey"]) == check.get("key")
    return same_key and sha256(keyed_option) == check.get("keyed_option_sha256")


def audit(cases, review):
    """Return (family id, found but not listed, listed but not found) for each mismatch."""
    by_id = {case["id"]: case for case in cases}
    pool = {}
    for case in cases:
        pool.setdefault(case["provenance"].get("sourceId"), []).append(case)
    problems = []
    for family in review["families"]:
        listed = set(family["case_ids"])
        if family["method"] == "explicit":
            found = {
                cid
                for cid, check in family["checks"].items()
                if cid in by_id
                and by_id[cid]["provenance"].get("sourceId") == family["source_id"]
                and explicit_matches(by_id[cid], check)
            }
        else:
            rule = RULES[family["method"]]
            found = {case["id"] for case in pool.get(family["source_id"], []) if rule(case)}
        if found != listed or len(listed) != family["count"]:
            problems.append((family["id"], sorted(found - listed), sorted(listed - found)))
    return problems


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("cases", type=Path)
    parser.add_argument("--review", type=Path, default=REVIEW)
    args = parser.parse_args()
    data = args.cases.read_bytes()
    review = json.loads(args.review.read_text(encoding="utf-8"))
    digest = hashlib.sha256(data).hexdigest()
    print(
        f"cases.jsonl sha256 {digest}",
        "(evaluated release)"
        if digest == review["cases_sha256"]
        else "(differs from the evaluated release)",
    )
    cases = [json.loads(line) for line in data.decode("utf-8").splitlines() if line.strip()]
    problems = audit(cases, review)
    for family_id, extra, missing in problems:
        print(f"{family_id}: not listed {extra[:5]}, not found {missing[:5]}")
    flagged = {i for f in review["families"] for i in f["case_ids"]}
    if len(flagged) != review["distinct_case_count"]:
        problems.append(("distinct_case_count", [], []))
    families = len(review["families"])
    print(f"{families} families, {len(flagged)} distinct cases, {len(problems)} mismatches")
    raise SystemExit(1 if problems else 0)


if __name__ == "__main__":
    main()
