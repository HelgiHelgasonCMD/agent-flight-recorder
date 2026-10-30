"""Fake credentials for redaction tests.

Assembled at runtime from fragments so that no credential-shaped literal appears in the
source tree (keeps secret scanners quiet and makes it obvious these are not real keys).
"""


def _j(*parts: str) -> str:
    return "".join(parts)


GITHUB = _j("gh", "p_", "abcdefghijklmnopqrstuvwxyz", "0123456789AB")
ANTHROPIC = _j("sk-", "ant-", "api03-", "AbCdEfGhIjKlMnOpQrStUvWxYz", "0123456789")
ANTHROPIC_2 = _j("sk-", "ant-", "api03-", "Z" * 32)
OPENAI = _j("sk-", "proj-", "AbCdEfGhIjKlMnOpQrStUvWx12")
AWS = _j("AK", "IA", "IOSFODNN7", "EXAMPLE")
SLACK = _j("xo", "xb-", "1234567890-abcdefghij")
JWT = _j("ey", "JhbGciOiJIUzI1NiJ9.", "ey", "JzdWIiOiIxMjM0NTY3ODkwIn0.", "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U")
URL_PASSWORD = _j("not-a-real-", "pass")
KEY_BEGIN = _j("-----BEGIN OPENSSH ", "PRIVATE KEY-----")
KEY_END = _j("-----END OPENSSH ", "PRIVATE KEY-----")
