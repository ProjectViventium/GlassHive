"""Recognizable credentials shared by output and failure-diagnostic redaction paths."""

import re


# Arbitrary label:value strings include source IDs, hashes and host:port URLs. They are
# not evidence of credentials. Match credential formats and URL userinfo explicitly.
CREDENTIAL_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"(?i)([a-z][a-z0-9+.-]*://)[^/\s<>@\"']*:[^/\s<>@\"']+@"),
        r"\1[REDACTED_CREDENTIAL]@",
    ),
    (
        re.compile(r"(?i)(?<![A-Za-z0-9_])(bot)?[0-9]{6,}:[A-Za-z0-9_-]{30,}"),
        r"\1[REDACTED_CREDENTIAL]",
    ),
    (re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"), "[REDACTED_AWS_ACCESS_KEY]"),
    (re.compile(r"\bghp_[A-Za-z0-9_]{8,}\b"), "ghp_[REDACTED]"),
    (re.compile(r"\bxoxb-[A-Za-z0-9-]{8,}\b"), "xoxb-[REDACTED]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"), "[REDACTED_JWT]"),
    (re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----[\s\S]*?-----END (?:[A-Z ]+ )?PRIVATE KEY-----"), "[REDACTED_PRIVATE_KEY]"),
    (re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----[\s\S]*\Z"), "[REDACTED_PRIVATE_KEY]"),
)
