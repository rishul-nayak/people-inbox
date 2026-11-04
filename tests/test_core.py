"""Pure logic: name matching and cleaning Gemini's answer."""
from extract import normalise
from match import find, name_score, norm_linkedin, norm_name
from tests.fakes import cfg

CFG = cfg()


def test_norm_name_drops_titles_and_accents():
    assert norm_name("Dr. Néha  Sharma ji") == "neha sharma"


def test_name_scores():
    assert name_score("Neha Sharma", "neha sharma") == 1.0
    assert name_score("Sharma Neha", "Neha Sharma") == 0.98
    assert name_score("Neha", "Neha Sharma") == 0.8          # possible, never certain
    assert name_score("Neha Sharma", "Ankit Jain") < 0.5
    assert name_score("Neha Sharma", "Neha Verma") < 0.92    # same first name, different person


def test_find_same_by_linkedin_even_if_name_differs():
    rows = [{"id": "1", "name": "N. Sharma", "linkedin_url": "https://www.linkedin.com/in/nehasharma/", "email": "", "phone": "", "company": ""}]
    kind, hits = find({"name": "Neha", "linkedin_url": "linkedin.com/in/nehasharma?utm=x"}, rows)
    assert kind == "same" and hits[0]["id"] == "1"
    assert norm_linkedin("https://in.linkedin.com/in/ab-c/") == "ab-c"


def test_find_same_maybe_new():
    rows = [{"id": "1", "name": "Neha Sharma", "company": "Razorpay", "linkedin_url": "", "email": "", "phone": ""},
            {"id": "2", "name": "Ankit Jain", "company": "", "linkedin_url": "", "email": "", "phone": ""}]
    assert find({"name": "Neha Sharma"}, rows)[0] == "same"
    assert find({"name": "Neha"}, rows)[0] == "maybe"
    assert find({"name": "Neha Sharma", "company": "Zepto"}, rows)[0] == "maybe"   # company clash: ask
    assert find({"name": "Priya Nair"}, rows) == ("new", [])


def test_find_two_people_same_name_asks():
    rows = [{"id": "1", "name": "Rahul Gupta", "company": "", "linkedin_url": "", "email": "", "phone": ""},
            {"id": "2", "name": "Rahul Gupta", "company": "", "linkedin_url": "", "email": "", "phone": ""}]
    kind, hits = find({"name": "Rahul Gupta"}, rows)
    assert kind == "maybe" and len(hits) == 2


def test_normalise_cleans_gemini_output():
    raw = {"people": [{"name": "Rishul"}, {"name": "  Neha   Sharma "}, {"name": ""},
                      {"name": "Ankit", "world": "Made up", "email": "not-an-email", "linkedin_url": "x.com/a"}],
           "summary": "", "type": "Party", "date": "2099-01-01",
           "promises": [{"who": "me", "what": "send deck", "due": "Friday"}, {"who": "someone", "what": "x"}]}
    out = normalise(raw, CFG, "2026-11-04")
    assert [p["name"] for p in out["people"]] == ["Neha Sharma", "Ankit"]       # owner dropped
    assert out["people"][1]["world"] == "" and out["people"][1]["email"] == "" and out["people"][1]["linkedin_url"] == ""
    assert out["type"] == "Note" and out["summary"] == "Note"
    assert out["date"] == "2026-11-04"                                          # no future dates
    assert out["promises"] == [{"who": "me", "person": "", "what": "send deck", "due": ""}]


def test_notion_dropdown_options_have_no_commas():
    names = list(CFG["worlds"]) + list(CFG["tiers"]) + [CFG["new_person_tier"]]
    assert not [n for n in names if "," in n]
