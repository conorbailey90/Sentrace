"""Shared name processing utilities for sanctions parsers."""

import re

# Common company suffixes to strip (lowercase, no punctuation)
COMPANY_SUFFIXES = {
    "limited", "ltd", "llc", "inc", "corp",
    "co", "plc", "gmbh", "sa", "ag", "bv", "nv", "pty",
    "lp", "llp", "sarl", "spa", "kg", "oy", "ab", "as", 'pjsc', 'ojsc', 'jsc', 'srl', 'sas', 
    'kft', 'eeig', 'eurl', 'sro', 'sl', 'ltda'
}


def strip_company_suffix(name):
    """
    Remove common company suffixes from the end of an organisation name.
    Returns (stripped_name, was_changed).
    Only strips suffixes — not words in the middle of the name.
    """
    if not name:
        return name, False

    # Normalise: split into tokens, removing trailing punctuation from each
    tokens = name.split()
    if not tokens:
        return name, False

    original_token_count = len(tokens)

    # Strip suffix tokens from the end, repeatedly
    while tokens:
        # Clean the last token: remove trailing/surrounding punctuation
        last_clean = re.sub(r"[^\w]", "", tokens[-1]).lower()
        if last_clean in COMPANY_SUFFIXES:
            tokens.pop()
        else:
            break

    # Also strip a trailing comma from the new last token (e.g. "Acme,")
    if tokens:
        tokens[-1] = tokens[-1].rstrip(",")

    stripped = " ".join(tokens).strip()

    # Only return as changed if we actually removed something meaningful
    if stripped and stripped.lower() != name.lower() and len(tokens) < original_token_count:
        return stripped, True

    return name, False

