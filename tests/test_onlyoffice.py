"""Unit tests for the ONLYOFFICE core logic + scoped service tokens (offline)."""


from convert_search_ai import crypto, onlyoffice as oo


# ------------------------------ type mapping --------------------------------
def test_document_type_for_office_formats():
    assert oo.document_type_for("report.docx") == ("word", "docx")
    assert oo.document_type_for("budget.XLSX") == ("cell", "xlsx")  # case-insensitive
    assert oo.document_type_for("deck.pptx") == ("slide", "pptx")
    assert oo.document_type_for("notes.odt") == ("word", "odt")


def test_non_editable_types_return_none():
    for n in ("photo.png", "model.ifc", "archive.zip", "noext", "data.pdf"):
        assert oo.document_type_for(n) is None
        assert oo.is_editable(n) is False
    assert oo.is_editable("a.docx") is True


# ------------------------------ document key --------------------------------
def test_document_key_changes_with_version_and_is_charset_safe():
    import re
    k1 = oo.document_key("file-uuid-1", "20260101_000000.000")
    k2 = oo.document_key("file-uuid-1", "20260102_000000.000")
    assert k1 != k2                     # new version → new key → editor reloads
    assert k1 == oo.document_key("file-uuid-1", "20260101_000000.000")  # stable
    assert len(k1) <= 128 and re.fullmatch(r"[0-9A-Za-z_-]+", k1)


# ------------------------------ save decision -------------------------------
def test_should_save_only_on_save_statuses():
    assert oo.should_save(2) is True and oo.should_save(6) is True
    for s in (1, 3, 4, 7, 0):
        assert oo.should_save(s) is False


# --------------------------- ONLYOFFICE JWT seam ----------------------------
def test_onlyoffice_jwt_roundtrip_and_rejects_tamper():
    secret = "docserver-shared-secret"
    tok = oo.sign_onlyoffice_jwt(secret, {"status": 2, "key": "abc"})
    assert oo.verify_onlyoffice_jwt(secret, tok) == {"status": 2, "key": "abc"}
    assert oo.verify_onlyoffice_jwt("wrong-secret", tok) is None
    assert oo.verify_onlyoffice_jwt(secret, tok + "x") is None


def test_build_editor_config_signs_when_secret_present():
    cfg = oo.build_editor_config(
        doc_type="word", file_type="docx", title="Report.docx", key="k1",
        document_url="https://csai/download?token=d", callback_url="https://csai/callback?token=c",
        user_id="alice", user_name="Alice", jwt_secret="s3cr3t")
    assert cfg["documentType"] == "word"
    assert cfg["document"]["url"].endswith("token=d")
    assert cfg["editorConfig"]["callbackUrl"].endswith("token=c")
    assert cfg["editorConfig"]["user"] == {"id": "alice", "name": "Alice"}
    # autosave + an active (forcesave) Save button so edits write a version on
    # demand and on close, not just silently.
    assert cfg["editorConfig"]["customization"] == {"autosave": True, "forcesave": True}
    # signed: token verifies and its payload matches the (unsigned) config
    claims = oo.verify_onlyoffice_jwt("s3cr3t", cfg["token"])
    assert claims["document"]["key"] == "k1"


def test_build_editor_config_unsigned_without_secret():
    cfg = oo.build_editor_config(
        doc_type="cell", file_type="xlsx", title="b.xlsx", key="k", document_url="u",
        callback_url="c", user_id="u1", user_name="", jwt_secret="")
    assert "token" not in cfg
    assert cfg["editorConfig"]["user"]["name"] == "u1"  # falls back to id


def test_parse_callback():
    assert oo.parse_callback({"status": "2", "url": "https://ds/edited.docx", "key": "k"}) == {
        "status": 2, "url": "https://ds/edited.docx", "key": "k"}
    assert oo.parse_callback({}) == {"status": 0, "url": "", "key": ""}


# --------------------------- scoped service tokens --------------------------
def test_scoped_token_roundtrip_carries_binding():
    secret = "signing-secret"
    tok = crypto.sign_scoped_token(secret, purpose="oo-download", ttl=3600,
                                   file_uid="f1", user="alice", tenant="acme",
                                   roles=["users"])
    claims = crypto.verify_scoped_token(secret, tok, purpose="oo-download")
    assert claims is not None
    assert claims["file_uid"] == "f1" and claims["user"] == "alice"
    assert claims["tenant"] == "acme" and claims["roles"] == ["users"]


def test_scoped_token_purpose_is_pinned():
    secret = "signing-secret"
    tok = crypto.sign_scoped_token(secret, purpose="oo-download", ttl=3600, file_uid="f1")
    # a download token can't be replayed as a callback token
    assert crypto.verify_scoped_token(secret, tok, purpose="oo-callback") is None
    assert crypto.verify_scoped_token(secret, tok, purpose="oo-download") is not None


def test_scoped_token_rejects_wrong_secret_and_none():
    tok = crypto.sign_scoped_token("a", purpose="p", ttl=60, x=1)
    assert crypto.verify_scoped_token("b", tok, purpose="p") is None
    assert crypto.sign_scoped_token("", purpose="p", ttl=60) is None
    assert crypto.verify_scoped_token("a", "", purpose="p") is None
