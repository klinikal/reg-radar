from collector.language import detect_language


def test_detects_english():
    text = "The European Securities and Markets Authority published new guidance on crypto assets."
    assert detect_language(text) == "en"


def test_detects_german():
    text = "Die Bundesanstalt für Finanzdienstleistungsaufsicht hat eine Warnung herausgegeben."
    assert detect_language(text) == "de"


def test_detects_french():
    text = "L'Autorité des marchés financiers publie un avertissement concernant les actifs numériques."
    assert detect_language(text) == "fr"


def test_empty_string_returns_fallback():
    assert detect_language("") == "unknown"


def test_very_short_text_returns_fallback():
    assert detect_language("ok") == "unknown"


def test_custom_fallback():
    assert detect_language("", fallback="xx") == "xx"
