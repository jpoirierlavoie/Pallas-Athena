"""Quebec court reference data — court locations, greffes and juridictions.

Lookup tables are embedded in-memory for instant parsing without
requiring Firestore. The seed script (scripts/seed_reference_data.py)
can still populate Firestore for future admin UI use.
"""

from typing import Optional


# ── In-memory court-location (palais de justice) table ────────────────
# Key: stable ASCII slug → dict with the MJQ-published civic address.
#
# WHY A SEPARATE TABLE FROM _GREFFES: a location is a *building*; a
# greffe is a *registry* that sits in one. The relationship is neither
# 1:1 nor total — several greffes have no fixed building (itinerant
# circuit court), and Kuujjuaq is a published courthouse that no greffe
# number in _GREFFES currently names. Keying addresses by greffe number
# would have to either drop or invent those. Greffes point here via
# their "palais_key"; None means "no published address" (see below).
#
# `location_type` mirrors the MJQ's own split between a full palais de
# justice and a point de service de justice. NOTE it is NOT the same
# notion as the greffe-level `point_de_service` flag, which marks
# itinerant circuit greffes (614/635/640/652) — the two disagree on the
# eight MJQ points de service by design; don't conflate them.
#
# Address fields mirror the `parties` address convention (street holds
# civic number + name, unit separate, full province/country names) so a
# resolved address drops straight into the existing address shape.
#
# Source: MJQ, « Trouver un palais de justice » — extracted 2026-07-15.
# 43 palais de justice + 8 points de service de justice. Verify the
# official listing before any signification or filing; addresses move.

_PALAIS: dict[str, dict] = {
    "alma": {"name": "Alma", "location_type": "palais", "street": "725, rue Harvey Ouest", "unit": "", "city": "Alma", "province": "Québec", "postal_code": "G8B 1P5", "country": "Canada", "mailing_address": ""},
    "amos": {"name": "Amos", "location_type": "palais", "street": "891, 3e Rue Ouest", "unit": "", "city": "Amos", "province": "Québec", "postal_code": "J9T 2T4", "country": "Canada", "mailing_address": ""},
    "baie-comeau": {"name": "Baie-Comeau", "location_type": "palais", "street": "71, avenue Mance", "unit": "", "city": "Baie-Comeau", "province": "Québec", "postal_code": "G4Z 1N2", "country": "Canada", "mailing_address": ""},
    "campbells-bay": {"name": "Campbell's Bay", "location_type": "palais", "street": "30, rue John", "unit": "", "city": "Campbell's Bay", "province": "Québec", "postal_code": "J0X 1K0", "country": "Canada", "mailing_address": ""},
    "chibougamau": {"name": "Chibougamau", "location_type": "palais", "street": "860, 3e Rue", "unit": "", "city": "Chibougamau", "province": "Québec", "postal_code": "G8P 1P9", "country": "Canada", "mailing_address": ""},
    # The Chicoutimi courthouse sits in the city of Saguenay.
    "chicoutimi": {"name": "Chicoutimi", "location_type": "palais", "street": "227, rue Racine Est", "unit": "1er étage", "city": "Saguenay", "province": "Québec", "postal_code": "G7H 7B4", "country": "Canada", "mailing_address": ""},
    "cowansville": {"name": "Cowansville", "location_type": "palais", "street": "920, rue Principale", "unit": "", "city": "Cowansville", "province": "Québec", "postal_code": "J2K 0E3", "country": "Canada", "mailing_address": ""},
    "drummondville": {"name": "Drummondville", "location_type": "palais", "street": "1680, boulevard Saint-Joseph", "unit": "", "city": "Drummondville", "province": "Québec", "postal_code": "J2C 2G3", "country": "Canada", "mailing_address": ""},
    "gatineau": {"name": "Gatineau", "location_type": "palais", "street": "17, rue Laurier", "unit": "", "city": "Gatineau", "province": "Québec", "postal_code": "J8X 4C1", "country": "Canada", "mailing_address": ""},
    "granby": {"name": "Granby", "location_type": "palais", "street": "77, rue Principale", "unit": "bureau 1.32", "city": "Granby", "province": "Québec", "postal_code": "J2G 9B3", "country": "Canada", "mailing_address": ""},
    # The Havre-Aubert courthouse sits in Les Îles-de-la-Madeleine.
    "havre-aubert": {"name": "Havre-Aubert", "location_type": "palais", "street": "405, chemin d'En-Haut", "unit": "bureau 102", "city": "Les Îles-de-la-Madeleine", "province": "Québec", "postal_code": "G4T 9A7", "country": "Canada", "mailing_address": ""},
    "joliette": {"name": "Joliette", "location_type": "palais", "street": "200, rue Saint-Marc", "unit": "", "city": "Joliette", "province": "Québec", "postal_code": "J6E 8C2", "country": "Canada", "mailing_address": ""},
    # Published by the MJQ, but no greffe number in _GREFFES names it —
    # left unreferenced rather than guessed onto a Nunavik circuit greffe.
    "kuujjuaq": {"name": "Kuujjuaq", "location_type": "palais", "street": "151, rue Siuralikuut", "unit": "", "city": "Kuujjuaq", "province": "Québec", "postal_code": "J0M 1C0", "country": "Canada", "mailing_address": ""},
    "la-malbaie": {"name": "La Malbaie", "location_type": "palais", "street": "30, chemin de la Vallée", "unit": "", "city": "La Malbaie", "province": "Québec", "postal_code": "G5A 1A3", "country": "Canada", "mailing_address": ""},
    "la-tuque": {"name": "La Tuque", "location_type": "palais", "street": "290, rue Saint-Joseph", "unit": "", "city": "La Tuque", "province": "Québec", "postal_code": "G9X 3Z8", "country": "Canada", "mailing_address": ""},
    "lac-megantic": {"name": "Lac-Mégantic", "location_type": "palais", "street": "5527, rue Frontenac", "unit": "bureau 316", "city": "Lac-Mégantic", "province": "Québec", "postal_code": "G6B 1H6", "country": "Canada", "mailing_address": ""},
    "laval": {"name": "Laval", "location_type": "palais", "street": "2800, boulevard Saint-Martin Ouest", "unit": "", "city": "Laval", "province": "Québec", "postal_code": "H7T 2S9", "country": "Canada", "mailing_address": ""},
    "longueuil": {"name": "Longueuil", "location_type": "palais", "street": "1111, boulevard Jacques-Cartier Est", "unit": "", "city": "Longueuil", "province": "Québec", "postal_code": "J4M 2J6", "country": "Canada", "mailing_address": ""},
    "maniwaki": {"name": "Maniwaki", "location_type": "palais", "street": "266, rue Notre-Dame", "unit": "1er étage", "city": "Maniwaki", "province": "Québec", "postal_code": "J9E 2J8", "country": "Canada", "mailing_address": ""},
    "mont-laurier": {"name": "Mont-Laurier", "location_type": "palais", "street": "645, rue de la Madone", "unit": "", "city": "Mont-Laurier", "province": "Québec", "postal_code": "J9L 1T1", "country": "Canada", "mailing_address": ""},
    "montmagny": {"name": "Montmagny", "location_type": "palais", "street": "110, avenue Jacques-Cartier", "unit": "", "city": "Montmagny", "province": "Québec", "postal_code": "G5V 0G5", "country": "Canada", "mailing_address": ""},
    "montreal": {"name": "Montréal", "location_type": "palais", "street": "1, rue Notre-Dame Est", "unit": "", "city": "Montréal", "province": "Québec", "postal_code": "H2Y 1B6", "country": "Canada", "mailing_address": ""},
    "new-carlisle": {"name": "New Carlisle", "location_type": "palais", "street": "87, boulevard Gérard-D.-Lévesque", "unit": "local 103", "city": "New Carlisle", "province": "Québec", "postal_code": "G0C 1Z0", "country": "Canada", "mailing_address": ""},
    "perce": {"name": "Percé", "location_type": "palais", "street": "124, route 132", "unit": "", "city": "Percé", "province": "Québec", "postal_code": "G0C 2L0", "country": "Canada", "mailing_address": "Case postale 188, Percé (Québec) G0C 2L0"},
    "quebec": {"name": "Québec", "location_type": "palais", "street": "300, boulevard Jean-Lesage", "unit": "", "city": "Québec", "province": "Québec", "postal_code": "G1K 8K6", "country": "Canada", "mailing_address": ""},
    "rimouski": {"name": "Rimouski", "location_type": "palais", "street": "183, avenue de la Cathédrale", "unit": "", "city": "Rimouski", "province": "Québec", "postal_code": "G5L 5J1", "country": "Canada", "mailing_address": ""},
    "riviere-du-loup": {"name": "Rivière-du-Loup", "location_type": "palais", "street": "33, rue De la Cour", "unit": "", "city": "Rivière-du-Loup", "province": "Québec", "postal_code": "G5R 1J1", "country": "Canada", "mailing_address": ""},
    "roberval": {"name": "Roberval", "location_type": "palais", "street": "750, boulevard Saint-Joseph", "unit": "", "city": "Roberval", "province": "Québec", "postal_code": "G8H 2L5", "country": "Canada", "mailing_address": ""},
    "rouyn-noranda": {"name": "Rouyn-Noranda", "location_type": "palais", "street": "2, avenue du Palais", "unit": "", "city": "Rouyn-Noranda", "province": "Québec", "postal_code": "J9X 2N9", "country": "Canada", "mailing_address": ""},
    "saint-hyacinthe": {"name": "Saint-Hyacinthe", "location_type": "palais", "street": "3800, avenue Cusson", "unit": "", "city": "Saint-Hyacinthe", "province": "Québec", "postal_code": "J2S 8V6", "country": "Canada", "mailing_address": ""},
    "saint-jean-sur-richelieu": {"name": "Saint-Jean-sur-Richelieu", "location_type": "palais", "street": "109, rue Saint-Charles", "unit": "", "city": "Saint-Jean-sur-Richelieu", "province": "Québec", "postal_code": "J3B 2C2", "country": "Canada", "mailing_address": ""},
    "saint-jerome": {"name": "Saint-Jérôme", "location_type": "palais", "street": "25, rue de Martigny Ouest", "unit": "", "city": "Saint-Jérôme", "province": "Québec", "postal_code": "J7Y 4Z1", "country": "Canada", "mailing_address": ""},
    "saint-joseph-de-beauce": {"name": "Saint-Joseph-de-Beauce", "location_type": "palais", "street": "795, avenue du Palais", "unit": "", "city": "Saint-Joseph-de-Beauce", "province": "Québec", "postal_code": "G0S 2V0", "country": "Canada", "mailing_address": ""},
    "salaberry-de-valleyfield": {"name": "Salaberry-de-Valleyfield", "location_type": "palais", "street": "74, rue Académie", "unit": "", "city": "Salaberry-de-Valleyfield", "province": "Québec", "postal_code": "J6T 0B8", "country": "Canada", "mailing_address": ""},
    "sept-iles": {"name": "Sept-Îles", "location_type": "palais", "street": "425, boulevard Laure", "unit": "", "city": "Sept-Îles", "province": "Québec", "postal_code": "G4R 1X6", "country": "Canada", "mailing_address": ""},
    "shawinigan": {"name": "Shawinigan", "location_type": "palais", "street": "212, 6e rue de la Pointe", "unit": "", "city": "Shawinigan", "province": "Québec", "postal_code": "G9N 8B6", "country": "Canada", "mailing_address": ""},
    "sherbrooke": {"name": "Sherbrooke", "location_type": "palais", "street": "375, rue King Ouest", "unit": "", "city": "Sherbrooke", "province": "Québec", "postal_code": "J1H 6B9", "country": "Canada", "mailing_address": ""},
    "sorel-tracy": {"name": "Sorel-Tracy", "location_type": "palais", "street": "46, rue Charlotte", "unit": "", "city": "Sorel-Tracy", "province": "Québec", "postal_code": "J3P 6N5", "country": "Canada", "mailing_address": ""},
    "thetford-mines": {"name": "Thetford Mines", "location_type": "palais", "street": "693, rue Saint-Alphonse Nord", "unit": "bureau 1.23", "city": "Thetford Mines", "province": "Québec", "postal_code": "G6G 3X3", "country": "Canada", "mailing_address": ""},
    "trois-rivieres": {"name": "Trois-Rivières", "location_type": "palais", "street": "850, rue Hart", "unit": "", "city": "Trois-Rivières", "province": "Québec", "postal_code": "G9A 1T9", "country": "Canada", "mailing_address": ""},
    "val-dor": {"name": "Val-d'Or", "location_type": "palais", "street": "900, 7e Rue", "unit": "", "city": "Val-d'Or", "province": "Québec", "postal_code": "J9P 3P8", "country": "Canada", "mailing_address": ""},
    "victoriaville": {"name": "Victoriaville", "location_type": "palais", "street": "800, boulevard Bois-Francs Sud", "unit": "", "city": "Victoriaville", "province": "Québec", "postal_code": "G6P 5W5", "country": "Canada", "mailing_address": ""},
    "ville-marie": {"name": "Ville-Marie", "location_type": "palais", "street": "8, rue Saint-Gabriel Nord", "unit": "", "city": "Ville-Marie", "province": "Québec", "postal_code": "J9V 1Z9", "country": "Canada", "mailing_address": ""},
    # ── Points de service de justice ──────────────────────────────────
    "amqui": {"name": "Amqui", "location_type": "point_de_service", "street": "29, boulevard Saint-Benoît Ouest", "unit": "", "city": "Amqui", "province": "Québec", "postal_code": "G5J 2E4", "country": "Canada", "mailing_address": ""},
    "carleton-sur-mer": {"name": "Carleton-sur-Mer", "location_type": "point_de_service", "street": "17, rue Lacroix", "unit": "", "city": "Carleton-sur-Mer", "province": "Québec", "postal_code": "G0C 1J0", "country": "Canada", "mailing_address": ""},
    "dolbeau-mistassini": {"name": "Dolbeau-Mistassini", "location_type": "point_de_service", "street": "1420, boulevard Wallberg", "unit": "1er étage", "city": "Dolbeau-Mistassini", "province": "Québec", "postal_code": "G8L 1H4", "country": "Canada", "mailing_address": ""},
    "forestville": {"name": "Forestville", "location_type": "point_de_service", "street": "134, route 138 Est", "unit": "", "city": "Forestville", "province": "Québec", "postal_code": "G0T 1E0", "country": "Canada", "mailing_address": "Case postale 400, Forestville (Québec) G0T 1E0"},
    "gaspe": {"name": "Gaspé", "location_type": "point_de_service", "street": "11, rue de la Cathédrale", "unit": "bureau 101", "city": "Gaspé", "province": "Québec", "postal_code": "G4X 2V9", "country": "Canada", "mailing_address": ""},
    "la-sarre": {"name": "La Sarre", "location_type": "point_de_service", "street": "651, 2e Rue Est", "unit": "", "city": "La Sarre", "province": "Québec", "postal_code": "J9Z 2Y9", "country": "Canada", "mailing_address": ""},
    "matane": {"name": "Matane", "location_type": "point_de_service", "street": "382, avenue Saint-Jérôme", "unit": "", "city": "Matane", "province": "Québec", "postal_code": "G4W 3B3", "country": "Canada", "mailing_address": ""},
    "sainte-anne-des-monts": {"name": "Sainte-Anne-des-Monts", "location_type": "point_de_service", "street": "10-B, boulevard Sainte-Anne Ouest", "unit": "", "city": "Sainte-Anne-des-Monts", "province": "Québec", "postal_code": "G4V 1P3", "country": "Canada", "mailing_address": ""},
}


# ── In-memory greffe table ────────────────────────────────────────────
# Key: 3-digit greffe number → dict with palais_de_justice,
# district_judiciaire, point_de_service, palais_key.
#
# `palais_key` indexes _PALAIS, or is None where the MJQ publishes no
# civic address for that greffe: the four itinerant circuit greffes
# (614/635/640/652, which sit wherever the court travels) plus 525 and
# 715, absent from the July 2026 extraction. None means "unknown", never
# "no address exists" — resolve before relying on it for a filing.

_GREFFES: dict[str, dict] = {
    "640": {"palais_de_justice": "Akulivik", "district_judiciaire": "Abitibi", "point_de_service": True, "palais_key": None},
    "160": {"palais_de_justice": "Alma", "district_judiciaire": "Alma", "point_de_service": False, "palais_key": "alma"},
    "605": {"palais_de_justice": "Amos", "district_judiciaire": "Abitibi", "point_de_service": False, "palais_key": "amos"},
    "120": {"palais_de_justice": "Amqui", "district_judiciaire": "Rimouski", "point_de_service": False, "palais_key": "amqui"},
    "635": {"palais_de_justice": "Aupaluk", "district_judiciaire": "Abitibi", "point_de_service": True, "palais_key": None},
    "655": {"palais_de_justice": "Baie-Comeau", "district_judiciaire": "Baie-Comeau", "point_de_service": False, "palais_key": "baie-comeau"},
    "652": {"palais_de_justice": "Blanc-Sablon", "district_judiciaire": "Mingan", "point_de_service": True, "palais_key": None},
    "555": {"palais_de_justice": "Campbell's Bay", "district_judiciaire": "Pontiac", "point_de_service": False, "palais_key": "campbells-bay"},
    "145": {"palais_de_justice": "Carleton-sur-Mer", "district_judiciaire": "Bonaventure", "point_de_service": False, "palais_key": "carleton-sur-mer"},
    "170": {"palais_de_justice": "Chibougamau", "district_judiciaire": "Abitibi", "point_de_service": False, "palais_key": "chibougamau"},
    "150": {"palais_de_justice": "Saguenay (Chicoutimi)", "district_judiciaire": "Chicoutimi", "point_de_service": False, "palais_key": "chicoutimi"},
    "614": {"palais_de_justice": "Chisasibi", "district_judiciaire": "Abitibi", "point_de_service": True, "palais_key": None},
    "455": {"palais_de_justice": "Cowansville", "district_judiciaire": "Bedford", "point_de_service": False, "palais_key": "cowansville"},
    "175": {"palais_de_justice": "Dolbeau-Mistassini", "district_judiciaire": "Roberval", "point_de_service": False, "palais_key": "dolbeau-mistassini"},
    "405": {"palais_de_justice": "Drummondville", "district_judiciaire": "Drummond", "point_de_service": False, "palais_key": "drummondville"},
    "665": {"palais_de_justice": "Forestville", "district_judiciaire": "Baie-Comeau", "point_de_service": False, "palais_key": "forestville"},
    "140": {"palais_de_justice": "Gaspé", "district_judiciaire": "Gaspé", "point_de_service": False, "palais_key": "gaspe"},
    "460": {"palais_de_justice": "Granby", "district_judiciaire": "Bedford", "point_de_service": False, "palais_key": "granby"},
    "115": {"palais_de_justice": "Havre-Aubert", "district_judiciaire": "Gaspé", "point_de_service": False, "palais_key": "havre-aubert"},
    "550": {"palais_de_justice": "Gatineau", "district_judiciaire": "Gatineau", "point_de_service": False, "palais_key": "gatineau"},
    "705": {"palais_de_justice": "Joliette", "district_judiciaire": "Joliette", "point_de_service": False, "palais_key": "joliette"},
    "480": {"palais_de_justice": "Lac-Mégantic", "district_judiciaire": "Mégantic", "point_de_service": False, "palais_key": "lac-megantic"},
    "240": {"palais_de_justice": "La Malbaie", "district_judiciaire": "Charlevoix", "point_de_service": False, "palais_key": "la-malbaie"},
    "620": {"palais_de_justice": "La Sarre", "district_judiciaire": "Abitibi", "point_de_service": False, "palais_key": "la-sarre"},
    "425": {"palais_de_justice": "La Tuque", "district_judiciaire": "Saint-Maurice", "point_de_service": False, "palais_key": "la-tuque"},
    "540": {"palais_de_justice": "Laval", "district_judiciaire": "Laval", "point_de_service": False, "palais_key": "laval"},
    "505": {"palais_de_justice": "Longueuil", "district_judiciaire": "Longueuil", "point_de_service": False, "palais_key": "longueuil"},
    "565": {"palais_de_justice": "Maniwaki", "district_judiciaire": "Labelle", "point_de_service": False, "palais_key": "maniwaki"},
    "125": {"palais_de_justice": "Matane", "district_judiciaire": "Rimouski", "point_de_service": False, "palais_key": "matane"},
    "560": {"palais_de_justice": "Mont-Laurier", "district_judiciaire": "Labelle", "point_de_service": False, "palais_key": "mont-laurier"},
    "300": {"palais_de_justice": "Montmagny", "district_judiciaire": "Montmagny", "point_de_service": False, "palais_key": "montmagny"},
    "500": {"palais_de_justice": "Montréal", "district_judiciaire": "Montréal", "point_de_service": False, "palais_key": "montreal"},
    "525": {"palais_de_justice": "Montréal - Chambre de la jeunesse", "district_judiciaire": "Montréal", "point_de_service": False, "palais_key": None},
    "105": {"palais_de_justice": "New Carlisle", "district_judiciaire": "Bonaventure", "point_de_service": False, "palais_key": "new-carlisle"},
    "110": {"palais_de_justice": "Percé", "district_judiciaire": "Gaspé", "point_de_service": False, "palais_key": "perce"},
    "200": {"palais_de_justice": "Québec", "district_judiciaire": "Québec", "point_de_service": False, "palais_key": "quebec"},
    "100": {"palais_de_justice": "Rimouski", "district_judiciaire": "Rimouski", "point_de_service": False, "palais_key": "rimouski"},
    "250": {"palais_de_justice": "Rivière-du-Loup", "district_judiciaire": "Kamouraska", "point_de_service": False, "palais_key": "riviere-du-loup"},
    "155": {"palais_de_justice": "Roberval", "district_judiciaire": "Roberval", "point_de_service": False, "palais_key": "roberval"},
    "600": {"palais_de_justice": "Rouyn-Noranda", "district_judiciaire": "Rouyn-Noranda", "point_de_service": False, "palais_key": "rouyn-noranda"},
    "750": {"palais_de_justice": "Saint-Hyacinthe", "district_judiciaire": "Saint-Hyacinthe", "point_de_service": False, "palais_key": "saint-hyacinthe"},
    "755": {"palais_de_justice": "Saint-Jean-sur-Richelieu", "district_judiciaire": "Iberville", "point_de_service": False, "palais_key": "saint-jean-sur-richelieu"},
    "700": {"palais_de_justice": "Saint-Jérôme", "district_judiciaire": "Terrebonne", "point_de_service": False, "palais_key": "saint-jerome"},
    "350": {"palais_de_justice": "Saint-Joseph-de-Beauce", "district_judiciaire": "Beauce", "point_de_service": False, "palais_key": "saint-joseph-de-beauce"},
    "715": {"palais_de_justice": "Sainte-Agathe-des-Monts", "district_judiciaire": "Terrebonne", "point_de_service": False, "palais_key": None},
    "130": {"palais_de_justice": "Sainte-Anne-des-Monts", "district_judiciaire": "Gaspé", "point_de_service": False, "palais_key": "sainte-anne-des-monts"},
    "760": {"palais_de_justice": "Salaberry-de-Valleyfield", "district_judiciaire": "Beauharnois", "point_de_service": False, "palais_key": "salaberry-de-valleyfield"},
    "650": {"palais_de_justice": "Sept-Îles", "district_judiciaire": "Mingan", "point_de_service": False, "palais_key": "sept-iles"},
    "410": {"palais_de_justice": "Shawinigan", "district_judiciaire": "Saint-Maurice", "point_de_service": False, "palais_key": "shawinigan"},
    "450": {"palais_de_justice": "Sherbrooke", "district_judiciaire": "Saint-François", "point_de_service": False, "palais_key": "sherbrooke"},
    "765": {"palais_de_justice": "Sorel-Tracy", "district_judiciaire": "Richelieu", "point_de_service": False, "palais_key": "sorel-tracy"},
    "235": {"palais_de_justice": "Thetford Mines", "district_judiciaire": "Frontenac", "point_de_service": False, "palais_key": "thetford-mines"},
    "400": {"palais_de_justice": "Trois-Rivières", "district_judiciaire": "Trois-Rivières", "point_de_service": False, "palais_key": "trois-rivieres"},
    "615": {"palais_de_justice": "Val d'Or", "district_judiciaire": "Abitibi", "point_de_service": False, "palais_key": "val-dor"},
    "415": {"palais_de_justice": "Victoriaville", "district_judiciaire": "Arthabaska", "point_de_service": False, "palais_key": "victoriaville"},
    "610": {"palais_de_justice": "Ville-Marie", "district_judiciaire": "Témiscamingue", "point_de_service": False, "palais_key": "ville-marie"},
}

# ── In-memory juridiction table ───────────────────────────────────────
# Key: 2-digit juridiction number → dict with tribunal, competence,
# greffe_type.

_JURIDICTIONS: dict[str, dict] = {
    "02": {"tribunal": "Cour du Québec", "competence": "Chambre civile", "greffe_type": "GC"},
    "04": {"tribunal": "Cour supérieure", "competence": "Chambre familiale", "greffe_type": "GC"},
    "05": {"tribunal": "Cour supérieure", "competence": "Division générale", "greffe_type": "GC"},
    "06": {"tribunal": "Cour supérieure", "competence": "Recours collectifs", "greffe_type": "GC"},
    "07": {"tribunal": "Cour du Québec", "competence": "Tribunal des professions", "greffe_type": "GC"},
    "09": {"tribunal": "Cour d'appel", "competence": "Chambre civile", "greffe_type": "GC"},
    "10": {"tribunal": "Cour d'appel", "competence": "Chambre criminelle et pénale", "greffe_type": "GC"},
    "11": {"tribunal": "Cour supérieure", "competence": "Chambre commerciale", "greffe_type": "GC"},
    "12": {"tribunal": "Cour supérieure", "competence": "Chambre familiale", "greffe_type": "GC"},
    "13": {"tribunal": "Cour supérieure", "competence": "Chambre familiale", "greffe_type": "GC"},
    "14": {"tribunal": "Cour supérieure", "competence": "Procédures non contentieuses", "greffe_type": "GC"},
    "17": {"tribunal": "Cour supérieure", "competence": "Chambre civile", "greffe_type": "GC"},
    "18": {"tribunal": "Cour supérieure", "competence": "Shérif", "greffe_type": "GC"},
    "19": {"tribunal": "Cour du Québec", "competence": "Chambre civile", "greffe_type": "GC"},
    "22": {"tribunal": "Cour du Québec", "competence": "Chambre civile", "greffe_type": "GC"},
    "32": {"tribunal": "Cour du Québec", "competence": "Division des petites créances", "greffe_type": "GC"},
    "34": {"tribunal": "Cour du Québec", "competence": "Chambre civile", "greffe_type": "GC"},
    "36": {"tribunal": "Cour supérieure", "competence": "Procès de novo", "greffe_type": "GC"},
    "38": {"tribunal": "Cour du Québec", "competence": "Chambre criminelle et pénale", "greffe_type": "GC"},
    "46": {"tribunal": "\u2014", "competence": "Appels divers", "greffe_type": "GC"},
    "53": {"tribunal": "Cour du Québec", "competence": "Tribunal des droits de la personne", "greffe_type": "GC"},
    "80": {"tribunal": "Cour du Québec", "competence": "Appel en matière administrative", "greffe_type": "GC"},
    "01": {"tribunal": "Cour supérieure", "competence": "Chambre criminelle", "greffe_type": "GP"},
    "27": {"tribunal": "Cour du Québec", "competence": "Chambre criminelle et pénale", "greffe_type": "GP"},
    "72": {"tribunal": "\u2014", "competence": "Infractions statutaires fédérales", "greffe_type": "GP"},
    "73": {"tribunal": "\u2014", "competence": "Dossiers G.R.C.", "greffe_type": "GP"},
    "61": {"tribunal": "Cour du Québec", "competence": "Chambre criminelle et pénale", "greffe_type": "GI"},
}


# ── In-memory forum table (non-judicial forums) ───────────────────────
# The forums a dossier can be before that the court-file-number parser does
# NOT handle: Quebec administrative tribunals and the federal courts. The
# parser resolves greffe/juridiction codes for the three Quebec judicial
# courts (Cour du Quebec / superieure / d'appel) - everything here is chosen
# from a list instead, and its file number is stored verbatim, unparsed.
#
# Key: stable ASCII slug -> {name, abbr, category}. `name` is what lands in the
# dossier's `tribunal` field (so the detail card, gabarits and MCP need no
# change). `category` groups the picker and drives `is_administrative_tribunal`
# (True only for "administratif" - a federal court is not one).
#
# Sources: the 16 Quebec administrative tribunals listed by the Conseil de la
# justice administrative du Quebec (cjaq.qc.ca), plus the four federal courts
# (Loi sur les Cours federales; Cour canadienne de l'impot; Cour supreme du
# Canada). Verified 2026-07-16. Tribunaux specialises attached to the Cour du
# Quebec (Tribunal des droits de la personne, Tribunal des professions) are
# deliberately absent - they run through the judicial stream and the parser
# already covers them via juridiction codes 53 / 07.

ADMINISTRATIF = "administratif"   # Quebec administrative tribunal
FEDERAL = "federal"               # Federal court

_FORUMS: dict[str, dict] = {
    # Tribunaux administratifs du Quebec
    "taq": {"name": "Tribunal administratif du Québec", "abbr": "TAQ", "category": ADMINISTRATIF},
    "tat": {"name": "Tribunal administratif du travail", "abbr": "TAT", "category": ADMINISTRATIF},
    "tal": {"name": "Tribunal administratif du logement", "abbr": "TAL", "category": ADMINISTRATIF},
    "tamf": {"name": "Tribunal administratif des marchés financiers", "abbr": "TAMF", "category": ADMINISTRATIF},
    "tadp": {"name": "Tribunal administratif de déontologie policière", "abbr": "TADP", "category": ADMINISTRATIF},
    "cai": {"name": "Commission d'accès à l'information", "abbr": "CAI", "category": ADMINISTRATIF},
    "cfp": {"name": "Commission de la fonction publique", "abbr": "CFP", "category": ADMINISTRATIF},
    "cptaq": {"name": "Commission de protection du territoire agricole du Québec", "abbr": "CPTAQ", "category": ADMINISTRATIF},
    "ctq": {"name": "Commission des transports du Québec", "abbr": "CTQ", "category": ADMINISTRATIF},
    "cmq": {"name": "Commission municipale du Québec", "abbr": "CMQ", "category": ADMINISTRATIF},
    "cqlc": {"name": "Commission québécoise des libérations conditionnelles", "abbr": "CQLC", "category": ADMINISTRATIF},
    "bpcd": {"name": "Bureau des présidents des conseils de discipline", "abbr": "BPCD", "category": ADMINISTRATIF},
    "re": {"name": "Régie de l'énergie", "abbr": "RE", "category": ADMINISTRATIF},
    "racj": {"name": "Régie des alcools, des courses et des jeux", "abbr": "RACJ", "category": ADMINISTRATIF},
    "rmaaq": {"name": "Régie des marchés agricoles et alimentaires du Québec", "abbr": "RMAAQ", "category": ADMINISTRATIF},
    "rbq": {"name": "Régie du bâtiment du Québec", "abbr": "RBQ", "category": ADMINISTRATIF},
    # Cours et tribunaux federaux
    "cour_federale": {"name": "Cour fédérale", "abbr": "C.F.", "category": FEDERAL},
    "cour_appel_federale": {"name": "Cour d'appel fédérale", "abbr": "C.A.F.", "category": FEDERAL},
    "cour_canadienne_impot": {"name": "Cour canadienne de l'impôt", "abbr": "C.C.I.", "category": FEDERAL},
    "cour_supreme_canada": {"name": "Cour suprême du Canada", "abbr": "C.S.C.", "category": FEDERAL},
}

FORUM_CATEGORY_LABELS: dict[str, str] = {
    ADMINISTRATIF: "Tribunaux administratifs du Québec",
    FEDERAL: "Cours et tribunaux fédéraux",
}


# ── Lookup helpers ────────────────────────────────────────────────────


def get_greffe(greffe_number: str) -> Optional[dict]:
    """Look up a greffe by its 3-digit number (in-memory)."""
    return _GREFFES.get(greffe_number)


def get_juridiction(juridiction_number: str) -> Optional[dict]:
    """Look up a juridiction by its 2-digit number (in-memory)."""
    return _JURIDICTIONS.get(juridiction_number)


def get_palais(palais_key: str) -> Optional[dict]:
    """Look up a court location by its slug, with the slug attached.

    Returns a copy — callers may mutate it without corrupting the table.
    """
    palais = _PALAIS.get(palais_key)
    if not palais:
        return None
    return {"palais_key": palais_key, **palais}


def get_greffe_address(greffe_number: str) -> Optional[dict]:
    """Resolve a greffe's court location, or None when it has no
    published civic address (itinerant circuit greffe, or a greffe the
    reference extraction does not cover).
    """
    greffe = _GREFFES.get(greffe_number)
    if not greffe:
        return None
    palais_key = greffe.get("palais_key")
    return get_palais(palais_key) if palais_key else None


def format_palais_address(palais: dict, multiline: bool = False) -> str:
    """Render a court location as an address string, MJQ-style.

    Single line: "227, rue Racine Est, 1er étage, Saguenay (Québec) G7H 7B4"
    Multiline splits before the city, for a letter address block.
    """
    if not palais:
        return ""
    street_parts = [p for p in (palais.get("street"), palais.get("unit")) if p]
    locality = " ".join(
        p for p in (
            f"{palais.get('city', '')} ({palais.get('province', '')})".strip(),
            palais.get("postal_code", ""),
        ) if p.strip("() ")
    ).strip()
    street = ", ".join(street_parts)
    if not street:
        return locality
    if not locality:
        return street
    return f"{street}\n{locality}" if multiline else f"{street}, {locality}"


def list_greffes() -> list[dict]:
    """Return all greffes, sorted by palais_de_justice name."""
    items = [
        {"greffe_number": k, **v} for k, v in _GREFFES.items()
    ]
    items.sort(key=lambda g: g["palais_de_justice"])
    return items


def list_palais(location_type: Optional[str] = None) -> list[dict]:
    """Return all court locations, sorted by name.

    location_type filters to "palais" or "point_de_service" when given.
    """
    items = [
        {"palais_key": k, **v}
        for k, v in _PALAIS.items()
        if location_type is None or v["location_type"] == location_type
    ]
    items.sort(key=lambda p: p["name"])
    return items


def get_forum(forum_key: str) -> Optional[dict]:
    """Look up a non-judicial forum by slug, with the slug attached.

    Returns a copy (callers can't corrupt the shared table).
    """
    forum = _FORUMS.get(forum_key or "")
    if not forum:
        return None
    return {"forum_key": forum_key, **forum}


def forum_tribunal_name(forum_key: str) -> str:
    """The display name a forum contributes to the dossier's `tribunal` field."""
    forum = _FORUMS.get(forum_key or "")
    return forum["name"] if forum else ""


def list_forums(category: Optional[str] = None) -> list[dict]:
    """Return non-judicial forums (slug + fields), category-filtered, name-sorted.

    `category` is "administratif" or "federal"; None returns all.
    """
    items = [
        {"forum_key": k, **v}
        for k, v in _FORUMS.items()
        if category is None or v["category"] == category
    ]
    items.sort(key=lambda f: f["name"])
    return items


def forums_by_category() -> list[tuple[str, str, list[dict]]]:
    """Grouped forums for the form's optgroup picker.

    Returns [(category_key, category_label, [forum, ...]), ...] in display
    order (Québec administrative tribunals, then federal courts).
    """
    return [
        (cat, FORUM_CATEGORY_LABELS[cat], list_forums(cat))
        for cat in (ADMINISTRATIF, FEDERAL)
    ]


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
