from app.collectors.common import make_text_key, normalize_text, normalize_url


def key(url):
    return normalize_url(url)[0]


def test_with_and_without_www_and_protocol():
    expected = "empresa.com/vagas/123"
    assert key("https://www.empresa.com/vagas/123") == expected
    assert key("http://empresa.com/vagas/123/") == expected
    assert key("empresa.com/vagas/123") == expected
    assert key("www.empresa.com/vagas/123") == expected
    assert key("//empresa.com/vagas/123") == expected


def test_tracking_params_removed():
    url = "https://empresa.com/vagas/123?utm_source=x&utm_medium=y&ref=a&source=b&fbclid=1&gclid=2"
    assert key(url) == "empresa.com/vagas/123"


def test_identifier_params_preserved():
    for param in ("gh_jid", "currentJobId", "jobId", "job", "jid", "id"):
        k = key(f"https://empresa.com/careers?{param}=77&utm_source=z")
        assert k == f"empresa.com/careers?{param.lower()}=77"


def test_two_different_gh_jid_give_different_keys():
    a = key("https://empresa.com/careers?gh_jid=1001")
    b = key("https://empresa.com/careers?gh_jid=1002")
    assert a != b


def test_generic_id_flag():
    assert normalize_url("https://empresa.com/vagas?id=123") == ("empresa.com/vagas?id=123", True)
    assert normalize_url("https://empresa.com/vagas?gh_jid=5&id=1")[1] is False
    assert normalize_url("https://empresa.com/vagas/123?id=1")[1] is False  # caminho identifica
    assert normalize_url("https://empresa.com/vagas/123")[1] is False


def test_missing_or_useless_urls_give_none():
    assert normalize_url(None) == (None, False)
    assert normalize_url("") == (None, False)
    assert normalize_url("   ") == (None, False)
    assert normalize_url("https://empresa.com") == (None, False)
    assert normalize_url("https://empresa.com/?utm_source=x") == (None, False)


def test_param_order_does_not_matter():
    assert key("https://e.com/v?jid=1&id=2") == key("https://e.com/v?id=2&jid=1")


def test_text_key_normalization():
    assert make_text_key("Empresa Ltda.", "Estágio  em Desenvolvimento") == make_text_key(
        "empresa ltda", "estagio em desenvolvimento"
    )
    assert make_text_key("Acme", "") is None
    assert normalize_text("<b>Olá&nbsp;Mundo</b>") == "ola mundo"
