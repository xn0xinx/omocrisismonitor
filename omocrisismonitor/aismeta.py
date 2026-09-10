"""Static AIS reference data: MMSI → flag (ITU Maritime Identification Digits)
and AIS ship-type code → (label, category). Pure data, no I/O.
"""
from __future__ import annotations

# ITU MID (first 3 digits of MMSI) → ISO-3166 alpha-2. Abridged to the flags
# that actually show up at sea; unknown → "".  Source: ITU Table of MIDs.
_MID: dict[str, str] = {
    "201": "AL", "202": "AD", "203": "AT", "204": "PT", "205": "BE", "206": "BY",
    "207": "BG", "208": "VA", "209": "CY", "210": "CY", "211": "DE", "212": "CY",
    "213": "GE", "214": "MD", "215": "MT", "216": "AM", "218": "DE", "219": "DK",
    "220": "DK", "224": "ES", "225": "ES", "226": "FR", "227": "FR", "228": "FR",
    "229": "MT", "230": "FI", "231": "FO", "232": "GB", "233": "GB", "234": "GB",
    "235": "GB", "236": "GI", "237": "GR", "238": "HR", "239": "GR", "240": "GR",
    "241": "GR", "242": "MA", "243": "HU", "244": "NL", "245": "NL", "246": "NL",
    "247": "IT", "248": "MT", "249": "MT", "250": "IE", "251": "IS", "252": "LI",
    "253": "LU", "254": "MC", "255": "PT", "256": "MT", "257": "NO", "258": "NO",
    "259": "NO", "261": "PL", "262": "ME", "263": "PT", "264": "RO", "265": "SE",
    "266": "SE", "267": "SK", "268": "SM", "269": "CH", "270": "CZ", "271": "TR",
    "272": "UA", "273": "RU", "274": "MK", "275": "LV", "276": "EE", "277": "LT",
    "278": "SI", "279": "RS",
    "301": "AI", "303": "US", "304": "AG", "305": "AG", "306": "CW", "307": "AW",
    "308": "BS", "309": "BS", "310": "BM", "311": "BS", "312": "BZ", "314": "BB",
    "316": "CA", "319": "KY", "321": "CR", "323": "CU", "325": "DM", "327": "DO",
    "329": "GP", "330": "GD", "331": "GL", "332": "GT", "334": "HN", "336": "HT",
    "338": "US", "339": "JM", "341": "KN", "343": "LC", "345": "MX", "347": "MQ",
    "348": "MS", "350": "NI", "351": "PA", "352": "PA", "353": "PA", "354": "PA",
    "355": "PA", "356": "PA", "357": "PA", "358": "PR", "359": "SV", "361": "PM",
    "362": "TT", "364": "TC", "366": "US", "367": "US", "368": "US", "369": "US",
    "370": "PA", "371": "PA", "372": "PA", "373": "PA", "374": "PA", "375": "VC",
    "376": "VC", "377": "VC", "378": "VG", "379": "VI",
    "401": "AF", "403": "SA", "405": "BD", "408": "BH", "410": "BT", "412": "CN",
    "413": "CN", "414": "CN", "416": "TW", "417": "LK", "419": "IN", "422": "IR",
    "423": "AZ", "425": "IQ", "428": "IL", "431": "JP", "432": "JP", "434": "TM",
    "436": "KZ", "437": "UZ", "438": "JO", "440": "KR", "441": "KR", "443": "PS",
    "445": "KP", "447": "KW", "450": "LB", "451": "KG", "453": "MO", "455": "MV",
    "457": "MN", "459": "NP", "461": "OM", "463": "PK", "466": "QA", "468": "SY",
    "470": "AE", "471": "AE", "472": "TJ", "473": "YE", "475": "YE", "477": "HK",
    "478": "BA",
    "501": "FR", "503": "AU", "506": "MM", "508": "BN", "510": "FM", "511": "PW",
    "512": "NZ", "514": "KH", "515": "KH", "516": "CX", "518": "CK", "520": "FJ",
    "523": "CC", "525": "ID", "529": "KI", "531": "LA", "533": "MY", "536": "MP",
    "538": "MH", "540": "NC", "542": "NU", "544": "NR", "546": "PF", "548": "PH",
    "553": "PG", "555": "PN", "557": "SB", "559": "AS", "561": "WS", "563": "SG",
    "564": "SG", "565": "SG", "566": "SG", "567": "TH", "570": "TO", "572": "TV",
    "574": "VN", "576": "VU", "577": "VU", "578": "WF",
    "601": "ZA", "603": "AO", "605": "DZ", "607": "TF", "608": "SH", "609": "BI",
    "610": "BJ", "611": "BW", "612": "CF", "613": "CM", "615": "CG", "616": "KM",
    "617": "CV", "618": "TF", "619": "CI", "620": "KM", "621": "DJ", "622": "EG",
    "624": "ET", "625": "ER", "626": "GA", "627": "GH", "629": "GM", "630": "GW",
    "631": "GQ", "632": "GN", "633": "BF", "634": "KE", "635": "TF", "636": "LR",
    "637": "LR", "638": "SS", "642": "LY", "644": "LS", "645": "MU", "647": "MG",
    "649": "ML", "650": "MZ", "654": "MR", "655": "MW", "656": "NE", "657": "NG",
    "659": "NA", "660": "RE", "661": "RW", "662": "SD", "663": "SN", "664": "SC",
    "665": "SH", "666": "SO", "667": "SL", "668": "ST", "669": "SZ", "670": "TD",
    "671": "TG", "672": "TN", "674": "TZ", "675": "UG", "676": "CD", "677": "TZ",
    "678": "ZM", "679": "ZW",
    "701": "AR", "710": "BR", "720": "BO", "725": "CL", "730": "CO", "735": "EC",
    "740": "FK", "745": "GF", "750": "GY", "755": "PY", "760": "PE", "765": "SR",
    "770": "UY", "775": "VE",
}


def flag_for_mmsi(mmsi: int | str | None) -> str:
    if not mmsi:
        return ""
    s = str(mmsi)
    # strip leading service prefixes: 00 (coast station), 98/99 (aux craft), 111 (SAR)
    if s.startswith(("00", "98", "99")):
        s = s[2:]
    if s.startswith("111"):
        s = s[3:]
    return _MID.get(s[:3], "")


# AIS ship & cargo type (0-99). (label, category) — category drives map colour.
def type_info(code: int | None) -> tuple[str, str]:
    if code is None:
        return ("Unknown", "other")
    c = int(code)
    if c == 30:
        return ("Fishing", "fishing")
    if c in (31, 32, 52):
        return ("Tug / Tow", "special")
    if c == 33:
        return ("Dredger", "special")
    if c == 34:
        return ("Dive vessel", "special")
    if c == 35:
        return ("Military", "special")
    if c == 36:
        return ("Sailing", "pleasure")
    if c == 37:
        return ("Pleasure craft", "pleasure")
    if 40 <= c <= 49:
        return ("High-speed craft", "passenger")
    if c == 50:
        return ("Pilot", "special")
    if c == 51:
        return ("Search & rescue", "special")
    if c == 53:
        return ("Port tender", "special")
    if c == 55:
        return ("Law enforcement", "special")
    if c == 58:
        return ("Medical transport", "special")
    if 60 <= c <= 69:
        return ("Passenger", "passenger")
    if 70 <= c <= 79:
        return ("Cargo", "cargo")
    if 80 <= c <= 89:
        return ("Tanker", "tanker")
    if 90 <= c <= 99:
        return ("Other", "other")
    return (f"Type {c}", "other")


NAV_STATUS = {
    0: "Under way (engine)", 1: "At anchor", 2: "Not under command",
    3: "Restricted manoeuvrability", 4: "Constrained by draught", 5: "Moored",
    6: "Aground", 7: "Fishing", 8: "Under way (sailing)", 11: "Towing astern",
    12: "Pushing ahead", 14: "AIS-SART", 15: "Undefined",
}
