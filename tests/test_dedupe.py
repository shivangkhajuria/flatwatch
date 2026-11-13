from flatwatch.config import EXAMPLE_CONFIG, load_config
from flatwatch.dedupe import find_original, fingerprint, is_duplicate, normalize, numbers_signature, phone_of

CRIT = load_config(EXAMPLE_CONFIG).criteria

TEMPLATE = ("🏡 {u} For Rent in Kudlu Gate– HSR Layout Very Near to HSR Layout 📞 Contact: 9000012345 "
            "💰 Rent: ₹{r}/month 🔒 Deposit: {d} 🤝 Brokerage: One month rent ✅ Amenities: Security Guard, CCTV, Lift")


def fp(t):
    return fingerprint(t, CRIT)


def test_exact_crosspost_is_duplicate():
    t = "2 bhk available partial furnished Sector 5 near ORR 35 K rent 3 months deposit call 9000012345 BROKERAGE APPLICABLE"
    assert is_duplicate(fp(t), fp(t))


def test_crosspost_with_small_edits_is_duplicate():
    a = ("Male flatmate needed in a 2BHK at HSR Sector 7, room with attached bathroom, rent 16k, deposit 40k, "
         "no brokerage, DM for details")
    b = ("Male flatmate needed in 2BHK at HSR Sector 7!! Room with attached bathroom. Rent 16k, deposit 40k. "
         "No brokerage. DM for more details 🙏")
    assert is_duplicate(fp(a), fp(b))


def test_same_broker_template_different_units_not_duplicate():
    a = TEMPLATE.format(u="2BHK", r="40k", d="2L")
    b = TEMPLATE.format(u="2BHK", r="30k", d="1L")
    c = TEMPLATE.format(u="1BHK", r="20k", d="60k")
    assert not is_duplicate(fp(a), fp(b))
    assert not is_duplicate(fp(a), fp(c))
    assert is_duplicate(fp(a), fp(TEMPLATE.format(u="2BHK", r="40k", d="2L")))


def test_phone_plus_locality_rule():
    a = "1BHK in HSR Sector 2 for 25k. Call 98450 12345"
    b = "Flat for rent HSR, 1bhk, rent 25k, ph +91-9845012345"
    assert is_duplicate(fp(a), fp(b))
    # same phone, same numbers but different locality -> not a dup by rule (b)
    c = "1BHK in Domlur for 25k. Call 9845012345"
    assert not is_duplicate(fp(a), fp(c))


def test_short_texts_not_fuzzy_matched():
    assert not is_duplicate(fp("1BHK available"), fp("1BHK available!"))


def test_helpers():
    assert phone_of("call +91 98450 12345 now") == "9845012345"
    assert phone_of("call 09845012345") == "9845012345"
    assert phone_of("rent 25000 deposit 100000") is None
    assert numbers_signature("Rent 25k, deposit 1.5 lakh, 1BHK, call 9845012345") == "1.5l,1bhk,25k"
    assert numbers_signature("Rent ₹40,000") == "40000"
    assert normalize("𝗠𝗔𝗟𝗘 𝗙𝗟𝗔𝗧𝗠𝗔𝗧𝗘 https://x.y/z!") == "male flatmate"


def test_find_original_returns_first_match():
    t = "Spacious 1bhk flat available for rent in prime location of HSR layout, rent 23k deposit 2 months"
    cands = [("facebook:1", fp("totally different post about a 3BHK in Whitefield for 45k rent")), ("facebook:2", fp(t))]
    assert find_original(fp(t), cands) == "facebook:2"
    assert find_original(fp("unrelated text that is long enough to be fuzzy matched, about a PG"), cands) is None
