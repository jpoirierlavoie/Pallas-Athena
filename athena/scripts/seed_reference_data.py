"""Seed Firestore with Quebec court reference data.

Usage:
    python scripts/seed_reference_data.py

Requires GOOGLE_APPLICATION_CREDENTIALS or running within App Engine context.
"""

import firebase_admin
from firebase_admin import firestore
from datetime import datetime, timezone


GREFFES = [
    ("640", "Akulivik", "Abitibi", True),
    ("160", "Alma", "Alma", False),
    ("605", "Amos", "Abitibi", False),
    ("120", "Amqui", "Rimouski", False),
    ("635", "Aupaluk", "Abitibi", True),
    ("655", "Baie-Comeau", "Baie-Comeau", False),
    ("652", "Blanc-Sablon", "Mingan", True),
    ("555", "Campbell's Bay", "Pontiac", False),
    ("145", "Carleton-sur-Mer", "Bonaventure", False),
    ("170", "Chibougamau", "Abitibi", False),
    ("150", "Saguenay (Chicoutimi)", "Chicoutimi", False),
    ("614", "Chisasibi", "Abitibi", True),
    ("455", "Cowansville", "Bedford", False),
    ("175", "Dolbeau-Mistassini", "Roberval", False),
    ("405", "Drummondville", "Drummond", False),
    ("665", "Forestville", "Baie-Comeau", False),
    ("140", "Gaspé", "Gaspé", False),
    ("460", "Granby", "Bedford", False),
    ("115", "Havre-Aubert", "Gaspé", False),
    ("550", "Gatineau", "Gatineau", False),
    ("705", "Joliette", "Joliette", False),
    ("480", "Lac-Mégantic", "Mégantic", False),
    ("240", "La Malbaie", "Charlevoix", False),
    ("620", "La Sarre", "Abitibi", False),
    ("425", "La Tuque", "Saint-Maurice", False),
    ("540", "Laval", "Laval", False),
    ("505", "Longueuil", "Longueuil", False),
    ("565", "Maniwaki", "Labelle", False),
    ("125", "Matane", "Rimouski", False),
    ("560", "Mont-Laurier", "Labelle", False),
    ("300", "Montmagny", "Montmagny", False),
    ("500", "Montréal", "Montréal", False),
    ("525", "Montréal - Chambre de la jeunesse", "Montréal", False),
    ("105", "New Carlisle", "Bonaventure", False),
    ("110", "Percé", "Gaspé", False),
    ("200", "Québec", "Québec", False),
    ("100", "Rimouski", "Rimouski", False),
    ("250", "Rivière-du-Loup", "Kamouraska", False),
    ("155", "Roberval", "Roberval", False),
    ("600", "Rouyn-Noranda", "Rouyn-Noranda", False),
    ("750", "Saint-Hyacinthe", "Saint-Hyacinthe", False),
    ("755", "Saint-Jean-sur-Richelieu", "Iberville", False),
    ("700", "Saint-Jérôme", "Terrebonne", False),
    ("350", "Saint-Joseph-de-Beauce", "Beauce", False),
    ("715", "Sainte-Agathe-des-Monts", "Terrebonne", False),
    ("130", "Sainte-Anne-des-Monts", "Gaspé", False),
    ("760", "Salaberry-de-Valleyfield", "Beauharnois", False),
    ("650", "Sept-Îles", "Mingan", False),
    ("410", "Shawinigan", "Saint-Maurice", False),
    ("450", "Sherbrooke", "Saint-François", False),
    ("765", "Sorel-Tracy", "Richelieu", False),
    ("235", "Thetford Mines", "Frontenac", False),
    ("400", "Trois-Rivières", "Trois-Rivières", False),
    ("615", "Val d'Or", "Abitibi", False),
    ("415", "Victoriaville", "Arthabaska", False),
    ("610", "Ville-Marie", "Témiscamingue", False),
]

JURIDICTIONS = [
    ("02", "Cour du Québec", "Chambre civile", "GC"),
    ("04", "Cour supérieure", "Séparation et autres requêtes", "GC"),
    ("05", "Cour supérieure", "Division générale", "GC"),
    ("06", "Cour supérieure", "Recours collectifs", "GC"),
    ("07", "Cour du Québec", "Tribunal des professions", "GC"),
    ("09", "Cour d'appel", "Affaires civiles", "GC"),
    ("10", "Cour d'appel", "Affaires pénales", "GC"),
    ("11", "Cour supérieure", "Division des faillites", "GC"),
    ("12", "Cour supérieure", "Division des divorces", "GC"),
    ("13", "Cour supérieure", "Mariages civils", "GC"),
    ("14", "Cour supérieure", "Procédures non contentieuses", "GC"),
    ("17", "Cour supérieure", "Voie allégée", "GC"),
    ("18", "Cour supérieure", "Shérif", "GC"),
    ("19", "Cour du Québec", "Chambre civile, divers", "GC"),
    ("22", "Cour du Québec", "Voie allégée", "GC"),
    ("32", "Cour du Québec", "Division des petites créances", "GC"),
    ("34", "Cour du Québec", "Chambre civile, expropriation", "GC"),
    ("36", "Cour supérieure", "Procès de novo", "GC"),
    ("38", "Cour du Québec", "Chambre criminelle et pénale (divers)", "GC"),
    ("46", "\u2014", "Appels divers", "GC"),
    ("53", "Cour du Québec", "Tribunal des droits de la personne", "GC"),
    ("80", "Cour du Québec", "Appel en matière administrative", "GC"),
    ("01", "Cour supérieure", "Matières criminelles", "GP"),
    ("27", "Cour du Québec", "Chambre criminelle et pénale", "GP"),
    ("72", "\u2014", "Infractions statutaires fédérales", "GP"),
    ("73", "\u2014", "Dossiers G.R.C.", "GP"),
    ("61", "Cour du Québec", "Infractions statutaires provinciales", "GI"),
]

SHARED_GREFFES = {
    "614": [
        "Eastmain", "Mistissini", "Nemiscau",
        "Oujé-Bougoumou", "Waskaganish", "Waswanipi",
        "Wemindji", "Whapmagoostui",
    ],
    "640": [
        "Inukjuak", "Ivujivik", "Kuujjuaraapik",
        "Puvirnituq", "Salluit", "Umiujaq",
    ],
    "635": [
        "Kangiqsualujjuaq", "Kangiqsujuaq", "Kangirsuk",
        "Quaqtaq", "Tasiujaq",
    ],
    "652": [
        "Fermont", "Havre-Saint-Pierre", "Kawawachikamach",
        "La Romaine", "Natashquan", "Port-Cartier",
        "Saint-Augustin", "Schefferville",
    ],
}


def seed_greffes(db):
    """Write all greffe documents to ref_greffes collection."""
    batch = db.batch()
    count = 0

    for greffe_number, palais, district, itinerant in GREFFES:
        doc_ref = db.collection("ref_greffes").document(greffe_number)
        data = {
            "greffe_number": greffe_number,
            "palais_de_justice": palais,
            "district_judiciaire": district,
            "point_de_service": itinerant,
            "updated_at": datetime.now(timezone.utc),
        }
        if greffe_number in SHARED_GREFFES:
            data["other_locations"] = SHARED_GREFFES[greffe_number]
        batch.set(doc_ref, data, merge=True)
        count += 1

        if count % 400 == 0:
            batch.commit()
            batch = db.batch()

    batch.commit()
    print(f"Seeded {count} greffe documents.")


def seed_juridictions(db):
    """Write all juridiction documents to ref_juridictions collection."""
    batch = db.batch()
    count = 0

    for juridiction_number, tribunal, competence, greffe_type in JURIDICTIONS:
        doc_ref = db.collection("ref_juridictions").document(juridiction_number)
        data = {
            "juridiction_number": juridiction_number,
            "tribunal": tribunal,
            "competence": competence,
            "greffe_type": greffe_type,
            "updated_at": datetime.now(timezone.utc),
        }
        batch.set(doc_ref, data, merge=True)
        count += 1

    batch.commit()
    print(f"Seeded {count} juridiction documents.")


if __name__ == "__main__":
    firebase_admin.initialize_app()
    db = firestore.client()
    seed_greffes(db)
    seed_juridictions(db)
    print("Reference data seeding complete.")
