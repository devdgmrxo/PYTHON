from app.matching.scorer import (
    MAX_EFFECTIVE_SCORE,
    classify_location,
    find_keywords,
    score_job,
)


def test_no_false_positives():
    assert find_keywords("digital marketing", ["git"]) == []
    assert find_keywords("we use javascript", ["java"]) == []
    assert find_keywords("reactive programming", ["react"]) == []
    assert find_keywords("github and git", ["git"]) == ["git"]
    assert find_keywords("java developer", ["java"]) == ["java"]
    assert find_keywords("react and react-native", ["react"]) == ["react"]


def test_special_keywords():
    text = "Experience with C++, C# and .NET, plus Node.js and ASP.NET"
    assert find_keywords(text, ["c++", "c#", ".net", "node.js"]) == ["c++", "c#", ".net", "node.js"]
    assert find_keywords("we love c++ here", ["c"]) == []
    assert find_keywords("we love c# here", ["c"]) == []
    assert find_keywords("language c is old", ["c"]) == ["c"]
    assert find_keywords("dotnetcore network", [".net"]) == []


def test_html_is_cleaned_before_scoring():
    assert find_keywords("<p>Python&nbsp;&amp; <b>SQL</b></p>", ["python", "sql"]) == ["python", "sql"]
    assert find_keywords("&lt;p&gt;Experiência em Python&lt;/p&gt;", ["python"]) == ["python"]
    assert find_keywords("<script>var react=1</script>Vaga", ["react"]) == []


def test_accent_and_case_normalization():
    assert find_keywords("Experiência com PYTHON", ["Python"]) == ["Python"]
    assert find_keywords("Estagio", ["estágio"]) == ["estágio"]


def test_location_unknown_remote_without_countries():
    assert classify_location("Remote", "Great team", True).status == "unknown"
    assert classify_location("Worldwide", None, True).status == "unknown"
    assert classify_location(None, None, True).status == "unknown"


def test_location_allowed_brazil():
    assert classify_location("Brazil", None, True).status == "allowed_brazil"
    assert classify_location("Remoto - Brasil", None, True).status == "allowed_brazil"
    assert classify_location("São Paulo, SP", None, False).status == "allowed_brazil"
    assert classify_location("Remote", "Open to candidates in Brazil", True).status == "allowed_brazil"


def test_location_restricted():
    for text in ("USA only", "US only", "EU only", "UK only", "U.S. only", "Remote - US", "Remote (USA)"):
        assert classify_location(text, None, True).status == "restricted", text
    assert classify_location("Remote", "Must be authorized to work in the US.", True).status == "restricted"
    assert classify_location("Remote", "Must reside in the United States", True).status == "restricted"
    assert classify_location("Remote", "Candidates must be located in Canada", True).status == "restricted"


def test_location_conflict_goes_to_human_review():
    res = classify_location("Brazil", "Must be authorized to work in the US", True)
    assert res.status == "unknown"


def test_latam_is_not_treated_as_restriction():
    assert classify_location("Latin America only", None, True).status == "unknown"


def test_max_effective_score_is_90():
    result = score_job(
        "Desenvolvedor Júnior Python",
        "Python, SQL, Git, React e JavaScript. Vaga aberta para o Brazil.",
        "Brazil",
        True,
        ["python", "sql", "git", "react", "javascript"],
    )
    assert result.components["education"] == 0
    assert result.total == 90 == MAX_EFFECTIVE_SCORE
    assert any("formacao" in r for r in result.reasons)


def test_senior_scores_low_and_reasons_are_listed():
    senior = score_job("Senior Backend Engineer", "10+ years", "Brazil", True, ["python"])
    junior = score_job("Estágio em Desenvolvimento", "", "Brazil", True, ["python"])
    assert senior.components["seniority"] == 0
    assert junior.components["seniority"] == 25
    assert junior.total > senior.total
    assert len(junior.reasons) == 5


def test_non_dev_role_gets_zero_role_points():
    res = score_job("Estágio em Marketing", "campanhas", "Brazil", True, ["python"])
    assert res.components["role"] == 0
