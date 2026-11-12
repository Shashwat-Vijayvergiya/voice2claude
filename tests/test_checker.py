import pytest

from app.checker import check, clean_fillers, destructive_words

T = ("Um so basically I want you to refactor the parse_config function in utils dot py, "
     "and don't change the public API. Use Python 3.12, set the timeout to thirty seconds, retries to 3. "
     "Never touch src/legacy/old_api.py. Only modify the ConfigLoader class, and name the flag 'dry run'. "
     "No, wait, also add tests.")
GOOD = ("TASK: Refactor parse_config in utils.py.\nREQUIREMENTS:\n- Use Python 3.12\n- Timeout 30 s, 3 retries\n"
        "- Flag name: \"dry run\"\n- Add tests\nCONSTRAINTS:\n- Do not change the public API\n"
        "- Never modify src/legacy/old_api.py\n- Modify only ConfigLoader")


def labels(report, kind=None):
    return {i.label for i in report.items if kind is None or i.kind == kind}


def missing(report):
    return {i.label for i in report.missing}


def test_extracts_all_kinds():
    r = check(T, GOOD)
    assert {"3.12", "3", "thirty"} <= labels(r, "number")
    assert {"utils.py", "src/legacy/old_api.py"} <= labels(r, "path")
    assert {"parse_config", "ConfigLoader"} <= labels(r, "identifier")
    assert '"dry run"' in labels(r, "quote")
    assert len(labels(r, "negation")) == 3  # don't…, never…, only…
    assert r.ok, missing(r)


def test_self_correction_is_not_a_negation():
    r = check(T, GOOD)
    assert not any(l.split()[0] == "no" for l in labels(r, "negation"))


def test_dropped_constraint_is_caught():
    bad = GOOD.replace("- Do not change the public API\n", "")
    assert missing(check(T, bad)) == {"not change the public"}


def test_negation_turned_positive_is_caught():
    bad = GOOD.replace("Do not change the public API", "Change the public API")
    assert "not change the public" in missing(check(T, bad))


def test_negation_on_another_line_does_not_count():
    out = "- Change the public API\n- Never modify old_api.py"
    assert "not change the public" in missing(check("don't change the public API. Never touch old_api.py", out))


def test_negation_with_different_word_is_accepted():
    # Real model output: "don't touch the tests" -> "without modifying tests"
    r = check("fix the login bug in auth.py but don't touch the tests", "Fix login bug in auth.py without modifying tests.")
    assert r.ok, missing(r)


def test_dropped_number_and_file():
    bad = GOOD.replace("3 retries", "retries").replace("utils.py", "the utils module")
    assert {"3", "utils.py"} <= missing(check(T, bad))


@pytest.mark.parametrize("src,out", [("three retries", "3 retries"), ("3 retries", "three retries"),
                                     ("twenty five items", "25 items"), ("port 8080", "port 8080")])
def test_number_words_and_digits_are_equivalent(src, out):
    assert check(src, out).ok


def test_number_not_matched_inside_other_number():
    assert missing(check("use 3 workers", "use 30 workers")) == {"3"}


def test_one_is_not_tracked():
    assert labels(check("fix this one and that one", "fix both"), "number") == set()


def test_version_numbers():
    r = check("upgrade to version 2.4.1", "Upgrade to 2.4.1")
    assert labels(r, "number") == {"2.4.1"} and r.ok


def test_identifiers_case_insensitive_but_whole_word():
    assert check("rename getUserName", "Rename getusername").ok
    assert not check("call MAX_RETRIES", "call MAX_RETRIES_COUNT").ok


def test_file_name_inside_path_not_double_counted():
    r = check("edit src/app/main_window.py", "Edit src/app/main_window.py")
    assert labels(r) == {"src/app/main_window.py"}


def test_dotfiles_and_dirs():
    r = check("add it to .env and ./scripts/setup", "Add to .env and ./scripts/setup")
    assert {".env", "./scripts/setup"} <= labels(r, "path") and r.ok


def test_sentence_period_is_not_a_file():
    assert labels(check("Fix the bug. Then deploy.", "Fix bug, deploy"), "path") == set()


def test_quotes():
    r = check('set the title to "Hello World"', "Set title: Hello World")
    assert r.ok
    assert not check('set the title to "Hello World"', "Set the title").ok


def test_only_restriction():
    assert check("only change the README", "Change README only").ok
    assert not check("only change the README", "Change the README").ok


def test_no_items():
    r = check("make it faster", "Make it faster")
    assert r.items == [] and r.ok


@pytest.mark.parametrize("text,word", [
    ("Delete the cache", "delete"), ("drop the users table", "drop"), ("run rm -rf build", "rm -rf"),
    ("git reset --hard HEAD", "git reset --hard"), ("force push to main", "force push"),
    ("git push --force", "push --force"), ("deploy --force", "--force"), ("Remove unused imports", "remove"), ("overwrite config.json", "overwrite"),
])
def test_destructive_words(text, word):
    assert word in destructive_words(text)


def test_not_destructive():
    assert destructive_words("Add a removal-safe helper and update docs") == []
    assert check("x", "Add tests").destructive_words == []


@pytest.mark.parametrize("src,out", [
    ("Um, so uh the the function is, erm, broken", "So the function is broken"),
    ("The umbrella and the user hmm.", "The umbrella and the user."),
    ("Make error handling better", "Make error handling better"),
    ("", ""),
])
def test_clean_fillers(src, out):
    assert clean_fillers(src) == out


def test_corrected_number_may_be_dropped():
    t = "Add a retry decorator with five retries, no wait, make it three retries, starting at 1 second"
    assert check(t, "Retry decorator: 3 retries, start at 1 s").ok


def test_uncorrected_number_still_required():
    assert {i.label for i in check("use 3 workers and also add logging", "Add logging").missing} == {"3"}


def test_hedge_is_not_a_constraint():
    assert check("Make it faster, I'm not sure how, but don't add new dependencies",
                 "Make it faster. Do not add new dependencies.").ok


def test_code_mode_skips_negations():
    t = "find python files, not in the tests folder, using find and wc, not any extra tools"
    out = "find . -name '*.py' -not -path '*/tests/*' | xargs wc -l"
    assert not check(t, out).ok
    assert check(t, out, "CODE").ok


D = ("Hi team, um, quick update on the voice project. The transcription part is working now and latency is "
     "around half a second. Next week I'll start on the compression layer, uh, and I'll share a demo on Friday.")


def test_low_coverage_catches_replaced_request():
    # Real gpt-oss reply that replaced a dictated message with a description of it.
    r = check(D, "The appropriate mode for the transcript is DICTATION.")
    assert r.items == [] or r.ok  # nothing for the item check to catch...
    assert r.low_coverage  # ...but coverage does


def test_low_coverage_catches_answered_request():
    t = ("Write a shell command that finds all python files under src modified in the last 7 days, excluding "
         "anything in the tests folder, and prints their line counts sorted from largest to smallest.")
    assert check(t, 'find src -name "*.py" -not -path "*/tests/*" -mtime -7 -exec wc -l {} + | sort -nr').low_coverage


def test_good_rewrite_has_normal_coverage():
    assert not check(D, D.replace("um, ", "").replace("uh, ", "")).low_coverage
    t = ("So the thing is the function is too slow, it takes like forever when there are a lot of users. Make it "
         "faster somehow, maybe add caching, but it has to stay compatible with the current callers.")
    out = "TASK: Make the slow function faster for many users.\n- Add caching\n- Keep compatibility with current callers"
    assert not check(t, out).low_coverage


def test_short_transcript_has_no_coverage_score():
    assert check("fix the typo", "Fix typo").coverage is None
