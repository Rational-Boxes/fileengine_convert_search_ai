

# ── NUL bytes: Postgres refuses them, extraction produces them ───────────────

def test_pg_safe_text_drops_nuls_and_leaves_everything_else():
    from convert_search_ai.db import pg_safe_text
    assert pg_safe_text("a\x00b") == "ab"
    assert pg_safe_text("plain") == "plain"          # untouched, same object path
    assert pg_safe_text("") == ""
    assert pg_safe_text(None) is None
    # Only NUL. Other control characters are legal in a Postgres text column and
    # may be meaningful in extracted text (tabs, newlines, form feeds).
    assert pg_safe_text("a\tb\nc\x0cd") == "a\tb\nc\x0cd"


def test_extracted_text_with_a_nul_is_stored_rather_than_losing_the_document():
    # Measured on production: 52 files (PHP fixtures in assorted encodings) failed
    # their whole conversion on `DataError: PostgreSQL text fields cannot contain
    # NUL`, because extraction decodes unknown bytes with errors="replace" and a
    # mislabelled encoding yields NULs. Dropping them keeps the text; refusing the
    # document keeps nothing.
    from convert_search_ai.db import pg_safe_text
    extracted = "# Title\n\nsome text\x00with an embedded NUL\n"
    cleaned = pg_safe_text(extracted)
    assert "\x00" not in cleaned
    assert cleaned.startswith("# Title")
    assert "with an embedded NUL" in cleaned
