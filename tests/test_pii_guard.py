import hashlib

import pytest

from tools import pii_guard as g

CFG = {"allowed_email_domains": ["contoso.com", "invalid"]}


def scan(text, rel="x.txt", cfg=CFG, denied=frozenset()):
    return [(c, d) for _, c, d in g.scan_text(text, rel, cfg, set(denied))]


def codes(text, **kw):
    return [c for c, _ in scan(text, **kw)]


def verhoeff_number(prefix="23456789012"):
    return next(prefix + str(d) for d in range(10) if g.verhoeff_ok(prefix + str(d)))


def test_email_domains():
    assert codes("mail me@contoso.com") == []
    assert codes("mail rhea@demo.contoso.invalid") == []
    assert codes("mail someone@gmail.com") == ["EMAIL"]
    assert "gmail" not in scan("someone@gmail.com")[0][1]          # masked in the output


def test_pan():
    assert codes("pan ABCDE1234F here") == ["PAN"]
    assert codes("token ABCDE1234FG") == []                         # inside a longer token


def test_aadhaar_needs_a_valid_check_digit_and_a_clean_boundary():
    n = verhoeff_number()
    assert codes(f"id {n}") == ["AADHAAR"]
    assert codes(f"id {n[:4]} {n[4:8]} {n[8:]}") == ["AADHAAR"]
    bad = n[:-1] + str((int(n[-1]) + 1) % 10)
    assert codes(f"id {bad}") == []
    assert codes(f"sha256: c{n}d7725be") == []                      # inside a hash


def test_phone_needs_the_country_code():
    assert codes("call +91 9876543210") == ["PHONE"]
    assert codes("amount 9876543210 paise") == []


def test_card_needs_luhn():
    assert codes("card 4111111111111111") == ["CARD"]
    assert codes("card 4111111111111112") == []
    assert codes("sha c4111111111111111d") == []


@pytest.mark.parametrize("text", [
    "-----BEGIN " + "RSA PRIVATE KEY-----",
    "key = " + "AK" + "IA" + "A" * 16,
    "token " + "gh" + "p_" + "a" * 36,
    "AccountKey=" + "A" * 44,
    "https://x.blob.core.windows.net/c?sv=1&sig=" + "a" * 40,
    "client_secret = " + "a" * 20,
    "eyJ" + "a" * 12 + "." + "b" * 12 + "." + "c" * 12,
])
def test_secrets(text):
    assert codes(text) == ["SECRET"]


def test_denied_ids_are_matched_by_hash_only():
    guid = "11111111-2222-3333-4444-555555555555"
    denied = {hashlib.sha256(guid.encode()).hexdigest()}
    assert codes(f"tenant {guid}", denied=denied) == ["DENIED-ID"]
    assert codes("tenant 66666666-2222-3333-4444-555555555555", denied=denied) == []
    assert guid not in scan(f"tenant {guid}", denied=denied)[0][1]


def test_rule_scoped_ignores_and_binary_files(tmp_path):
    (tmp_path / "kit" / "tests").mkdir(parents=True)
    (tmp_path / "kit" / "tests" / "t.py").write_text("x = 'a@b.com'\n", encoding="utf-8")
    (tmp_path / "doc.md").write_text("write to a@b.com\n", encoding="utf-8")
    (tmp_path / "blob.bin").write_bytes(b"\0a@b.com")
    cfg = {"allowed_email_domains": [], "ignore_rule_paths": {"EMAIL": ["kit/tests/"]}}
    found = g.scan(tmp_path, cfg, set())
    assert [(r, c) for r, _, c, _ in found] == [("doc.md", "EMAIL")]


def test_repo_is_clean():
    assert g.scan() == []
