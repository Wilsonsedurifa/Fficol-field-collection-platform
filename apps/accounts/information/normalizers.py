"""Conservative value normalization; these helpers do not establish validity."""

import hashlib
import json
import re
import unicodedata


def normalize_text(value):
    return " ".join(unicodedata.normalize("NFC", value).split())


def normalize_phone(value):
    # TODO(decision): Confirm country-code equivalence and extension policy.
    # Preserve country prefixes; do not guess a country from local digits.
    return re.sub(r"[\s().-]", "", normalize_text(value))


def normalize_email(value):
    # Preserve local-part case; only the domain is case-insensitive.
    value = unicodedata.normalize("NFC", value.strip())
    local, separator, domain = value.rpartition("@")
    return local + separator + domain.lower() if separator else value


def normalize_address(value):
    # TODO(decision): Address equivalence needs policy; do not infer abbreviations.
    return normalize_text(value)


def payload_fingerprint(payload):
    """Hash an already normalized, complete submission envelope deterministically.

    Callers must include assignment, category, explanation and evidence identity,
    not just the proposed value. List order and numeric types remain significant.
    """
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
