import os
for k in ("SLACK_BOT_TOKEN","SLACK_SIGNING_SECRET","PROMO_SHEET_ID","PL_SHEET_ID","SELLOUT_SHEET_ID"):
    os.environ.setdefault(k,"x")
os.environ.setdefault("GOOGLE_SERVICE_ACCOUNT_JSON","{}")
from app.regions import normalize_region, register_aliases

def test_register_aliases_merges_without_clobbering_code_aliases():
    import app.regions as R
    snapshot = dict(R._ALIAS_INDEX)
    try:
        register_aliases({"dxb": "UAE", "kbh": "Denmark"})
        assert normalize_region("dxb") == "UAE"
        assert normalize_region("kbh") == "Denmark"
        assert normalize_region("dubai") == "UAE"
        assert normalize_region("zzz") == ""
        # no-clobber: registering an existing code alias to a different canonical is ignored
        register_aliases({"dubai": "KSA"})
        assert normalize_region("dubai") == "UAE"
    finally:
        R._ALIAS_INDEX.clear()
        R._ALIAS_INDEX.update(snapshot)

def test_main_normalization_parity():
    import app.regions as R
    snapshot = dict(R._ALIAS_INDEX)
    try:
        assert normalize_region("Germany") == "Germany"
        assert normalize_region("DE") == "Germany"
        assert normalize_region("u.a.e.") == "UAE"
        assert normalize_region("South Africa") == "ZA"
    finally:
        R._ALIAS_INDEX.clear()
        R._ALIAS_INDEX.update(snapshot)

def test_d2c_normalize_country_parity():
    import app.regions as R
    from app.lift_table_d2c import normalize_country
    snapshot = dict(R._ALIAS_INDEX)
    try:
        # Original 4 assertions
        assert normalize_country("uae") == "GCC"
        assert normalize_country("Germany") == "EU"
        assert normalize_country("australia") == "AU"
        assert normalize_country("zzz") == ""

        # Every D2C code reachable via a representative spelling
        assert normalize_country("india") == "IN"
        assert normalize_country("australia") == "AU"
        assert normalize_country("japan") == "JP"
        assert normalize_country("malaysia") == "MY"
        assert normalize_country("new zealand") == "NZ"
        assert normalize_country("singapore") == "SG"
        assert normalize_country("united states") == "US"
        assert normalize_country("united kingdom") == "UK"
        assert normalize_country("canada") == "CA"
        assert normalize_country("poland") == "PL"
        assert normalize_country("gcc") == "GCC"
        assert normalize_country("europe") == "EU"

        # Previously-regressed informal aliases
        assert normalize_country("gulf") == "GCC"
        assert normalize_country("oman") == "GCC"
        assert normalize_country("aus") == "AU"
        assert normalize_country("america") == "US"
        assert normalize_country("nippon") == "JP"
        assert normalize_country("eea") == "EU"
        assert normalize_country("newzealand") == "NZ"
    finally:
        R._ALIAS_INDEX.clear()
        R._ALIAS_INDEX.update(snapshot)
