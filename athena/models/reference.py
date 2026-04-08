"""Quebec court reference data — greffes and juridictions.

Lookup tables are embedded in-memory for instant parsing without
requiring Firestore. The seed script (scripts/seed_reference_data.py)
can still populate Firestore for future admin UI use.
"""

from typing import Optional


# ── In-memory greffe table ────────────────────────────────────────────
# Key: 3-digit greffe number → dict with palais_de_justice,
# district_judiciaire, point_de_service.

_GREFFES: dict[str, dict] = {
    "640": {"palais_de_justice": "Akulivik", "district_judiciaire": "Abitibi", "point_de_service": True},
    "160": {"palais_de_justice": "Alma", "district_judiciaire": "Alma", "point_de_service": False},
    "605": {"palais_de_justice": "Amos", "district_judiciaire": "Abitibi", "point_de_service": False},
    "120": {"palais_de_justice": "Amqui", "district_judiciaire": "Rimouski", "point_de_service": False},
    "635": {"palais_de_justice": "Aupaluk", "district_judiciaire": "Abitibi", "point_de_service": True},
    "655": {"palais_de_justice": "Baie-Comeau", "district_judiciaire": "Baie-Comeau", "point_de_service": False},
    "652": {"palais_de_justice": "Blanc-Sablon", "district_judiciaire": "Mingan", "point_de_service": True},
    "555": {"palais_de_justice": "Campbell's Bay", "district_judiciaire": "Pontiac", "point_de_service": False},
    "145": {"palais_de_justice": "Carleton-sur-Mer", "district_judiciaire": "Bonaventure", "point_de_service": False},
    "170": {"palais_de_justice": "Chibougamau", "district_judiciaire": "Abitibi", "point_de_service": False},
    "150": {"palais_de_justice": "Saguenay (Chicoutimi)", "district_judiciaire": "Chicoutimi", "point_de_service": False},
    "614": {"palais_de_justice": "Chisasibi", "district_judiciaire": "Abitibi", "point_de_service": True},
    "455": {"palais_de_justice": "Cowansville", "district_judiciaire": "Bedford", "point_de_service": False},
    "175": {"palais_de_justice": "Dolbeau-Mistassini", "district_judiciaire": "Roberval", "point_de_service": False},
    "405": {"palais_de_justice": "Drummondville", "district_judiciaire": "Drummond", "point_de_service": False},
    "665": {"palais_de_justice": "Forestville", "district_judiciaire": "Baie-Comeau", "point_de_service": False},
    "140": {"palais_de_justice": "Gaspé", "district_judiciaire": "Gaspé", "point_de_service": False},
    "460": {"palais_de_justice": "Granby", "district_judiciaire": "Bedford", "point_de_service": False},
    "115": {"palais_de_justice": "Havre-Aubert", "district_judiciaire": "Gaspé", "point_de_service": False},
    "550": {"palais_de_justice": "Gatineau", "district_judiciaire": "Gatineau", "point_de_service": False},
    "705": {"palais_de_justice": "Joliette", "district_judiciaire": "Joliette", "point_de_service": False},
    "480": {"palais_de_justice": "Lac-Mégantic", "district_judiciaire": "Mégantic", "point_de_service": False},
    "240": {"palais_de_justice": "La Malbaie", "district_judiciaire": "Charlevoix", "point_de_service": False},
    "620": {"palais_de_justice": "La Sarre", "district_judiciaire": "Abitibi", "point_de_service": False},
    "425": {"palais_de_justice": "La Tuque", "district_judiciaire": "Saint-Maurice", "point_de_service": False},
    "540": {"palais_de_justice": "Laval", "district_judiciaire": "Laval", "point_de_service": False},
    "505": {"palais_de_justice": "Longueuil", "district_judiciaire": "Longueuil", "point_de_service": False},
    "565": {"palais_de_justice": "Maniwaki", "district_judiciaire": "Labelle", "point_de_service": False},
    "125": {"palais_de_justice": "Matane", "district_judiciaire": "Rimouski", "point_de_service": False},
    "560": {"palais_de_justice": "Mont-Laurier", "district_judiciaire": "Labelle", "point_de_service": False},
    "300": {"palais_de_justice": "Montmagny", "district_judiciaire": "Montmagny", "point_de_service": False},
    "500": {"palais_de_justice": "Montréal", "district_judiciaire": "Montréal", "point_de_service": False},
    "525": {"palais_de_justice": "Montréal - Chambre de la jeunesse", "district_judiciaire": "Montréal", "point_de_service": False},
    "105": {"palais_de_justice": "New Carlisle", "district_judiciaire": "Bonaventure", "point_de_service": False},
    "110": {"palais_de_justice": "Percé", "district_judiciaire": "Gaspé", "point_de_service": False},
    "200": {"palais_de_justice": "Québec", "district_judiciaire": "Québec", "point_de_service": False},
    "100": {"palais_de_justice": "Rimouski", "district_judiciaire": "Rimouski", "point_de_service": False},
    "250": {"palais_de_justice": "Rivière-du-Loup", "district_judiciaire": "Kamouraska", "point_de_service": False},
    "155": {"palais_de_justice": "Roberval", "district_judiciaire": "Roberval", "point_de_service": False},
    "600": {"palais_de_justice": "Rouyn-Noranda", "district_judiciaire": "Rouyn-Noranda", "point_de_service": False},
    "750": {"palais_de_justice": "Saint-Hyacinthe", "district_judiciaire": "Saint-Hyacinthe", "point_de_service": False},
    "755": {"palais_de_justice": "Saint-Jean-sur-Richelieu", "district_judiciaire": "Iberville", "point_de_service": False},
    "700": {"palais_de_justice": "Saint-Jérôme", "district_judiciaire": "Terrebonne", "point_de_service": False},
    "350": {"palais_de_justice": "Saint-Joseph-de-Beauce", "district_judiciaire": "Beauce", "point_de_service": False},
    "715": {"palais_de_justice": "Sainte-Agathe-des-Monts", "district_judiciaire": "Terrebonne", "point_de_service": False},
    "130": {"palais_de_justice": "Sainte-Anne-des-Monts", "district_judiciaire": "Gaspé", "point_de_service": False},
    "760": {"palais_de_justice": "Salaberry-de-Valleyfield", "district_judiciaire": "Beauharnois", "point_de_service": False},
    "650": {"palais_de_justice": "Sept-Îles", "district_judiciaire": "Mingan", "point_de_service": False},
    "410": {"palais_de_justice": "Shawinigan", "district_judiciaire": "Saint-Maurice", "point_de_service": False},
    "450": {"palais_de_justice": "Sherbrooke", "district_judiciaire": "Saint-François", "point_de_service": False},
    "765": {"palais_de_justice": "Sorel-Tracy", "district_judiciaire": "Richelieu", "point_de_service": False},
    "235": {"palais_de_justice": "Thetford Mines", "district_judiciaire": "Frontenac", "point_de_service": False},
    "400": {"palais_de_justice": "Trois-Rivières", "district_judiciaire": "Trois-Rivières", "point_de_service": False},
    "615": {"palais_de_justice": "Val d'Or", "district_judiciaire": "Abitibi", "point_de_service": False},
    "415": {"palais_de_justice": "Victoriaville", "district_judiciaire": "Arthabaska", "point_de_service": False},
    "610": {"palais_de_justice": "Ville-Marie", "district_judiciaire": "Témiscamingue", "point_de_service": False},
}

# ── In-memory juridiction table ───────────────────────────────────────
# Key: 2-digit juridiction number → dict with tribunal, competence,
# greffe_type.

_JURIDICTIONS: dict[str, dict] = {
    "02": {"tribunal": "Cour du Québec", "competence": "Chambre civile", "greffe_type": "GC"},
    "04": {"tribunal": "Cour supérieure", "competence": "Séparation et autres requêtes", "greffe_type": "GC"},
    "05": {"tribunal": "Cour supérieure", "competence": "Division générale", "greffe_type": "GC"},
    "06": {"tribunal": "Cour supérieure", "competence": "Recours collectifs", "greffe_type": "GC"},
    "07": {"tribunal": "Cour du Québec", "competence": "Tribunal des professions", "greffe_type": "GC"},
    "09": {"tribunal": "Cour d'appel", "competence": "Affaires civiles", "greffe_type": "GC"},
    "10": {"tribunal": "Cour d'appel", "competence": "Affaires pénales", "greffe_type": "GC"},
    "11": {"tribunal": "Cour supérieure", "competence": "Division des faillites", "greffe_type": "GC"},
    "12": {"tribunal": "Cour supérieure", "competence": "Division des divorces", "greffe_type": "GC"},
    "13": {"tribunal": "Cour supérieure", "competence": "Mariages civils", "greffe_type": "GC"},
    "14": {"tribunal": "Cour supérieure", "competence": "Procédures non contentieuses", "greffe_type": "GC"},
    "17": {"tribunal": "Cour supérieure", "competence": "Voie allégée", "greffe_type": "GC"},
    "18": {"tribunal": "Cour supérieure", "competence": "Shérif", "greffe_type": "GC"},
    "19": {"tribunal": "Cour du Québec", "competence": "Chambre civile, divers", "greffe_type": "GC"},
    "22": {"tribunal": "Cour du Québec", "competence": "Voie allégée", "greffe_type": "GC"},
    "32": {"tribunal": "Cour du Québec", "competence": "Division des petites créances", "greffe_type": "GC"},
    "34": {"tribunal": "Cour du Québec", "competence": "Chambre civile, expropriation", "greffe_type": "GC"},
    "36": {"tribunal": "Cour supérieure", "competence": "Procès de novo", "greffe_type": "GC"},
    "38": {"tribunal": "Cour du Québec", "competence": "Chambre criminelle et pénale (divers)", "greffe_type": "GC"},
    "46": {"tribunal": "\u2014", "competence": "Appels divers", "greffe_type": "GC"},
    "53": {"tribunal": "Cour du Québec", "competence": "Tribunal des droits de la personne", "greffe_type": "GC"},
    "80": {"tribunal": "Cour du Québec", "competence": "Appel en matière administrative", "greffe_type": "GC"},
    "01": {"tribunal": "Cour supérieure", "competence": "Matières criminelles", "greffe_type": "GP"},
    "27": {"tribunal": "Cour du Québec", "competence": "Chambre criminelle et pénale", "greffe_type": "GP"},
    "72": {"tribunal": "\u2014", "competence": "Infractions statutaires fédérales", "greffe_type": "GP"},
    "73": {"tribunal": "\u2014", "competence": "Dossiers G.R.C.", "greffe_type": "GP"},
    "61": {"tribunal": "Cour du Québec", "competence": "Infractions statutaires provinciales", "greffe_type": "GI"},
}


# ── Lookup helpers ────────────────────────────────────────────────────


def get_greffe(greffe_number: str) -> Optional[dict]:
    """Look up a greffe by its 3-digit number (in-memory)."""
    return _GREFFES.get(greffe_number)


def get_juridiction(juridiction_number: str) -> Optional[dict]:
    """Look up a juridiction by its 2-digit number (in-memory)."""
    return _JURIDICTIONS.get(juridiction_number)


def list_greffes() -> list[dict]:
    """Return all greffes, sorted by palais_de_justice name."""
    items = [
        {"greffe_number": k, **v} for k, v in _GREFFES.items()
    ]
    items.sort(key=lambda g: g["palais_de_justice"])
    return items


def list_juridictions() -> list[dict]:
    """Return all juridictions, sorted by juridiction_number."""
    items = [
        {"juridiction_number": k, **v} for k, v in _JURIDICTIONS.items()
    ]
    items.sort(key=lambda j: j["juridiction_number"])
    return items


def parse_court_file_number(court_file_number: str) -> dict:
    """Parse a Quebec court file number and return resolved metadata.

    Args:
        court_file_number: e.g., "500-05-123456-241"

    Returns:
        {
            "greffe_number": "500" or None,
            "juridiction_number": "05" or None,
            "greffe": { ... } or None,
            "juridiction": { ... } or None,
            "is_administrative": False,
            "parse_error": None or "error message"
        }
    """
    result = {
        "greffe_number": None,
        "juridiction_number": None,
        "greffe": None,
        "juridiction": None,
        "is_administrative": False,
        "parse_error": None,
    }

    if not court_file_number or not court_file_number.strip():
        result["parse_error"] = "Numéro de dossier judiciaire requis."
        return result

    cleaned = court_file_number.strip()

    # Check for administrative tribunal prefix (starts with letters)
    if cleaned[0].isalpha():
        result["is_administrative"] = True
        return result

    # Parse: expect NNN-NN-...
    parts = cleaned.split("-")
    if len(parts) < 2:
        result["parse_error"] = (
            "Format invalide. Attendu : NNN-NN-NNNNNN-NN "
            "(ex. : 500-05-123456-241)"
        )
        return result

    greffe_str = parts[0]
    juridiction_str = parts[1]

    # Validate greffe: exactly 3 digits
    if len(greffe_str) != 3 or not greffe_str.isdigit():
        result["parse_error"] = (
            f"Le numéro de greffe « {greffe_str} » doit être composé "
            "de 3 chiffres."
        )
        return result

    # Validate juridiction: exactly 2 digits
    if len(juridiction_str) != 2 or not juridiction_str.isdigit():
        result["parse_error"] = (
            f"Le numéro de juridiction « {juridiction_str} » doit être "
            "composé de 2 chiffres."
        )
        return result

    result["greffe_number"] = greffe_str
    result["juridiction_number"] = juridiction_str

    # Look up in-memory
    result["greffe"] = get_greffe(greffe_str)
    result["juridiction"] = get_juridiction(juridiction_str)

    if not result["greffe"]:
        result["parse_error"] = (
            f"Greffe « {greffe_str} » introuvable dans les données "
            "de référence."
        )

    if not result["juridiction"]:
        existing_error = result["parse_error"] or ""
        sep = " " if existing_error else ""
        result["parse_error"] = (
            f"{existing_error}{sep}Juridiction « {juridiction_str} » "
            "introuvable dans les données de référence."
        )

    return result
