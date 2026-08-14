from auspex.extraction.json_response import load_model_json


def test_invalid_unicode_escape_is_preserved_as_text():
    parsed = load_model_json('{"evidence_excerpt":"path \\\\users and text"}')
    assert parsed["evidence_excerpt"] == "path \\users and text"


def test_valid_unicode_escape_is_unchanged():
    parsed = load_model_json(r'{"evidence_excerpt":"valid \u2014 dash"}')
    assert parsed["evidence_excerpt"] == "valid — dash"
