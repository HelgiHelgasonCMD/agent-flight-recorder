from afr.redaction import MASK, redact, redact_obj, sanitize, truncate_middle

from . import fakes

SAMPLES = {
    "anthropic": f"key {fakes.ANTHROPIC}",
    "openai": f"OPENAI {fakes.OPENAI}",
    "github": f"token {fakes.GITHUB}",
    "aws": f"id {fakes.AWS} used",
    "auth_header": "Authorization: Bearer abc.def.ghi-jkl_mno",
    "basic": "authorization=Basic dXNlcjpwYXNzd29yZA==",
    "url": f"git clone https://alice:{fakes.URL_PASSWORD}@example.com/repo.git",
    "assign": "DATABASE_PASSWORD=supersecretvalue ./run",
    "flag": "mysql --password=s3cr3t-pass -u root",
    "jwt": fakes.JWT,
    "slack": fakes.SLACK,
}


def test_known_secret_patterns_are_masked() -> None:
    for name, text in SAMPLES.items():
        r = redact(text, environ={})
        assert r.count >= 1, name
        assert MASK in r.text, name
    assert fakes.URL_PASSWORD not in redact(SAMPLES["url"], environ={}).text
    assert "supersecretvalue" not in redact(SAMPLES["assign"], environ={}).text


def test_private_key_block() -> None:
    text = f"before\n{fakes.KEY_BEGIN}\nb3BlbnNzaC1rZXktdjEAAAA\nAAAA\n{fakes.KEY_END}\nafter"
    r = redact(text, environ={})
    assert "b3BlbnNzaC1rZXktdjEAAAA" not in r.text
    assert r.text.startswith("before") and r.text.endswith("after")


def test_env_values_of_secret_named_vars() -> None:
    env = {"MY_SERVICE_TOKEN": "tok-12345678", "HOME": "/home/user"}
    r = redact("calling with tok-12345678 from /home/user", environ=env)
    assert "tok-12345678" not in r.text
    assert "/home/user" in r.text  # non-secret env values are kept


def test_git_sha_and_ordinary_text_untouched() -> None:
    text = "HEAD is 3f2c1a9b8e7d6c5b4a39281706f5e4d3c2b1a090 and ModuleNotFoundError: No module named 'yaml'"
    assert redact(text, environ={}).text == text


def test_redact_obj_masks_secret_keys() -> None:
    obj = {"headers": {"Authorization": "Bearer abcdefghijkl"}, "api_key": "zzzzzzzz", "n": 3}
    out, n = redact_obj(obj, environ={})
    assert out["api_key"] == MASK and out["n"] == 3
    assert "abcdefghijkl" not in str(out) and n >= 2


def test_truncation_after_redaction_cannot_split_secret() -> None:
    secret = fakes.GITHUB
    text = "x" * 50 + secret + "y" * 5000
    out = sanitize(text, 200, environ={}).text
    assert len(out) <= 260
    assert fakes.GITHUB[:8] not in out


def test_truncate_middle_marks_omission() -> None:
    assert "omitted" in truncate_middle("a" * 1000, 100)
    assert truncate_middle("short", 100) == "short"
