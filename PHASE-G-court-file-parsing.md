# PHASE G — Court File Number Parsing & Judicial Metadata

Read CLAUDE.md for project context. This phase adds a `court_file_number` field to dossiers. When the user enters a Quebec court file number, the application auto-determines the judicial district, tribunal, competence, and courthouse from reference data stored in Firestore.

## Context

A Quebec court file number follows the format `NNN-NN-NNNNNN-NN` (e.g., `500-05-123456-241`):

- **Positions 1–3** (before the first dash): the 3-digit **greffe number**, which identifies the courthouse and judicial district.
- **Positions 5–6** (after the first dash): the 2-digit **jurisdiction number** (zero-padded), which identifies the tribunal and its competence (subject-matter jurisdiction).
- The remaining segments (sequence number, year/check digits) are not parsed.

If the court file number begins with letters (e.g., `TAL-123456-789`), it is an administrative tribunal file — no parsing is performed and the auto-population fields remain empty.

## Step 1 — Firestore Reference Collections

Create two top-level Firestore collections for reference data. These are **not** nested under `users/{userId}` — they are shared application-level reference data readable by the authenticated user.

### Collection: `ref_greffes`

Each document ID is the 3-digit greffe number (string, e.g., `"500"`).

```
ref_greffes/{greffe_number}
├── greffe_number: "500"               # String, 3 digits
├── palais_de_justice: "Montréal"      # String
├── district_judiciaire: "Montréal"    # String
├── point_de_service: false            # Boolean — true if itinerant (marked * in source)
└── updated_at: Timestamp
```

### Collection: `ref_juridictions`

Each document ID is the 2-digit jurisdiction number (string, e.g., `"05"`).

```
ref_juridictions/{juridiction_number}
├── juridiction_number: "05"                    # String, 2 digits zero-padded
├── tribunal: "Cour supérieure"                 # String
├── competence: "Division générale"             # String
└── updated_at: Timestamp
```

**Note on `greffe_type`:** The jurisdiction numbers are partitioned across three greffe types (GC = civil, GP = criminal/penal, GI = provincial statutory). This field is informational — the parsing logic does not depend on it, but it's useful for display and filtering.

## Step 2 — Seed Script

Create `scripts/seed_reference_data.py` to populate both collections from the data below.

### Greffe Data

```python
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
```

**Note:** Some greffe numbers map to multiple locations (e.g., `614` maps to Chisasibi, Eastmain, Mistissini, Nemiscau, Oujé-Bougoumou, Waskaganish, Waswanipi, Wemindji, Whapmagoostui — all itinerant points of service). For these, store one document with the primary location name. The `point_de_service` flag indicates it's itinerant. Similarly, `640` covers Akulivik, Inukjuak, Ivujivik, Kuujjuaraapik, Puvirnituq, Salluit, Umiujaq; and `652` covers Blanc-Sablon, Fermont, Havre-Saint-Pierre, Kawawachikamach, La Romaine, Natashquan, Port-Cartier, Saint-Augustin, Schefferville.

For shared greffe numbers, add an `other_locations` array field listing the additional locations:

```python
# Example for greffe 614:
{
    "greffe_number": "614",
    "palais_de_justice": "Chisasibi",
    "district_judiciaire": "Abitibi",
    "point_de_service": True,
    "other_locations": [
        "Eastmain", "Mistissini", "Nemiscau",
        "Oujé-Bougoumou", "Waskaganish", "Waswanipi",
        "Wemindji", "Whapmagoostui"
    ],
    "updated_at": ...
}
```

### Juridiction Data

```python
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
    ("46", "—", "Appels divers", "GC"),
    ("53", "Cour du Québec", "Tribunal des droits de la personne", "GC"),
    ("80", "Cour du Québec", "Appel en matière administrative", "GC"),
    ("01", "Cour supérieure", "Matières criminelles", "GP"),
    ("27", "Cour du Québec", "Chambre criminelle et pénale", "GP"),
    ("72", "—", "Infractions statutaires fédérales", "GP"),
    ("73", "—", "Dossiers G.R.C.", "GP"),
    ("61", "Cour du Québec", "Infractions statutaires provinciales", "GI"),
]
```

### Script Implementation

```python
"""Seed Firestore with Quebec court reference data.

Usage:
    python scripts/seed_reference_data.py

Requires GOOGLE_APPLICATION_CREDENTIALS or running within App Engine context.
"""

import firebase_admin
from firebase_admin import credentials, firestore
from datetime import datetime, timezone


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
        # Add other_locations for shared greffe numbers
        if greffe_number in SHARED_GREFFES:
            data["other_locations"] = SHARED_GREFFES[greffe_number]
        batch.set(doc_ref, data, merge=True)
        count += 1

        # Firestore batch limit is 500
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


if __name__ == "__main__":
    firebase_admin.initialize_app()
    db = firestore.client()
    seed_greffes(db)
    seed_juridictions(db)
    print("Reference data seeding complete.")
```

**Firestore Security Rules:** add read access for the authenticated user:

```
match /ref_greffes/{doc} {
  allow read: if request.auth != null;
  allow write: if false;  // Admin only via script or admin UI
}
match /ref_juridictions/{doc} {
  allow read: if request.auth != null;
  allow write: if false;
}
```

## Step 3 — Reference Data Model

Create `models/reference.py`:

```python
"""Quebec court reference data — greffes and juridictions.

Read-only model for querying Firestore reference collections.
Data is seeded by scripts/seed_reference_data.py.
"""

from typing import Optional
from models import db

REF_GREFFES = "ref_greffes"
REF_JURIDICTIONS = "ref_juridictions"


def get_greffe(greffe_number: str) -> Optional[dict]:
    """Look up a greffe by its 3-digit number."""
    doc = db.collection(REF_GREFFES).document(greffe_number).get()
    if doc.exists:
        return doc.to_dict()
    return None


def get_juridiction(juridiction_number: str) -> Optional[dict]:
    """Look up a juridiction by its 2-digit number."""
    doc = db.collection(REF_JURIDICTIONS).document(juridiction_number).get()
    if doc.exists:
        return doc.to_dict()
    return None


def list_greffes() -> list[dict]:
    """Return all greffes, sorted by palais_de_justice name."""
    docs = db.collection(REF_GREFFES).order_by("palais_de_justice").stream()
    return [d.to_dict() for d in docs]


def list_juridictions() -> list[dict]:
    """Return all juridictions, sorted by juridiction_number."""
    docs = db.collection(REF_JURIDICTIONS).order_by("juridiction_number").stream()
    return [d.to_dict() for d in docs]


def parse_court_file_number(court_file_number: str) -> dict:
    """Parse a Quebec court file number and return resolved metadata.

    Args:
        court_file_number: e.g., "500-05-123456-241"

    Returns:
        {
            "greffe_number": "500" or None,
            "juridiction_number": "05" or None,
            "greffe": { ... } or None,          # full greffe document
            "juridiction": { ... } or None,      # full juridiction document
            "is_administrative": False,           # True if starts with letters
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

    # Look up in Firestore
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
```

## Step 4 — Add Court File Number to Dossier Schema

In `models/dossier.py`, add the following fields to `_default_doc`:

```python
def _default_doc() -> dict:
    return {
        ...
        # Existing fields unchanged

        # NEW: Court file number and parsed judicial metadata
        "court_file_number": "",             # Raw input, e.g., "500-05-123456-241"
        "district_judiciaire": "",           # Auto-populated from greffe
        "tribunal": "",                      # Auto-populated from juridiction
        "competence": "",                    # Auto-populated from juridiction
        "palais_de_justice": "",             # Auto-populated from greffe
        "greffe_number": "",                 # Parsed 3-digit greffe code
        "juridiction_number": "",            # Parsed 2-digit juridiction code
        "is_administrative_tribunal": False, # True if letters prefix detected
        ...
    }
```

These fields are stored on the dossier document for display and search. They are **auto-populated** on create/update when the `court_file_number` changes, but the user can override them manually (in case of exceptions or edge cases).

## Step 5 — API Route for Court File Number Parsing

Create a lightweight JSON endpoint that the frontend calls via HTMX or Alpine.js when the user types or pastes a court file number.

In `routes/dossiers.py`, add:

```python
@dossiers_bp.route("/parse-court-file", methods=["POST"])
@login_required
def parse_court_file():
    """Parse a court file number and return judicial metadata as JSON.

    Called via HTMX/Alpine from the dossier form when the court file
    number field loses focus or is submitted.
    """
    court_file_number = request.form.get("court_file_number", "").strip()

    from models.reference import parse_court_file_number
    result = parse_court_file_number(court_file_number)

    return jsonify({
        "district_judiciaire": (
            result["greffe"]["district_judiciaire"]
            if result.get("greffe") else ""
        ),
        "tribunal": (
            result["juridiction"]["tribunal"]
            if result.get("juridiction") else ""
        ),
        "competence": (
            result["juridiction"]["competence"]
            if result.get("juridiction") else ""
        ),
        "palais_de_justice": (
            result["greffe"]["palais_de_justice"]
            if result.get("greffe") else ""
        ),
        "greffe_number": result.get("greffe_number", ""),
        "juridiction_number": result.get("juridiction_number", ""),
        "is_administrative": result.get("is_administrative", False),
        "parse_error": result.get("parse_error"),
    })
```

## Step 6 — Dossier Form Updates

### Add Fields to the Dossier Form Template

In `templates/dossiers/form.html`, add the court file number field and the auto-populated metadata fields.

```jinja2
{# ── Court File Number ────────────────────────────────────── #}
<div class="space-y-4">
  <div>
    <label for="court_file_number" class="block text-sm font-medium text-gray-700 mb-1">
      Numéro de dossier judiciaire
    </label>
    <input type="text"
           id="court_file_number"
           name="court_file_number"
           value="{{ dossier.court_file_number if dossier else '' }}"
           placeholder="500-05-123456-241"
           class="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm
                  focus:ring-2 focus:ring-indigo-500 focus:border-indigo-500"
           x-on:blur="parseCourtFile()"
           autocomplete="off">
    <p id="court-file-error" class="text-xs text-red-500 mt-1 hidden"></p>
  </div>

  {# Auto-populated fields — visible after parsing #}
  <div id="judicial-metadata" class="grid grid-cols-1 sm:grid-cols-2 gap-3"
       x-show="judicialMetadata.district_judiciaire || judicialMetadata.tribunal"
       x-cloak>

    <div>
      <label class="block text-xs font-medium text-gray-500 mb-0.5">District judiciaire</label>
      <input type="text" name="district_judiciaire"
             x-model="judicialMetadata.district_judiciaire"
             class="w-full px-3 py-2 border border-gray-200 rounded-lg text-sm bg-gray-50">
    </div>

    <div>
      <label class="block text-xs font-medium text-gray-500 mb-0.5">Palais de justice</label>
      <input type="text" name="palais_de_justice"
             x-model="judicialMetadata.palais_de_justice"
             class="w-full px-3 py-2 border border-gray-200 rounded-lg text-sm bg-gray-50">
    </div>

    <div>
      <label class="block text-xs font-medium text-gray-500 mb-0.5">Tribunal</label>
      <input type="text" name="tribunal"
             x-model="judicialMetadata.tribunal"
             class="w-full px-3 py-2 border border-gray-200 rounded-lg text-sm bg-gray-50">
    </div>

    <div>
      <label class="block text-xs font-medium text-gray-500 mb-0.5">Compétence</label>
      <input type="text" name="competence"
             x-model="judicialMetadata.competence"
             class="w-full px-3 py-2 border border-gray-200 rounded-lg text-sm bg-gray-50">
    </div>
  </div>

  {# Administrative tribunal indicator #}
  <div x-show="judicialMetadata.is_administrative" x-cloak
       class="p-3 bg-amber-50 border border-amber-200 rounded-xl text-sm text-amber-700">
    Dossier devant un tribunal administratif — aucune juridiction déterminée automatiquement.
  </div>
</div>
```

### Hidden Fields for Parsed Codes

```jinja2
<input type="hidden" name="greffe_number" x-model="judicialMetadata.greffe_number">
<input type="hidden" name="juridiction_number" x-model="judicialMetadata.juridiction_number">
<input type="hidden" name="is_administrative_tribunal"
       :value="judicialMetadata.is_administrative ? 'true' : 'false'">
```

### Alpine.js Logic

Add to the `x-data` object of the dossier form:

```javascript
judicialMetadata: {
    district_judiciaire: '{{ dossier.district_judiciaire if dossier else "" }}',
    tribunal: '{{ dossier.tribunal if dossier else "" }}',
    competence: '{{ dossier.competence if dossier else "" }}',
    palais_de_justice: '{{ dossier.palais_de_justice if dossier else "" }}',
    greffe_number: '{{ dossier.greffe_number if dossier else "" }}',
    juridiction_number: '{{ dossier.juridiction_number if dossier else "" }}',
    is_administrative: {{ 'true' if dossier and dossier.is_administrative_tribunal else 'false' }},
},

async parseCourtFile() {
    const input = document.getElementById('court_file_number').value.trim();
    const errorEl = document.getElementById('court-file-error');

    if (!input) {
        this.judicialMetadata = {
            district_judiciaire: '', tribunal: '', competence: '',
            palais_de_justice: '', greffe_number: '', juridiction_number: '',
            is_administrative: false,
        };
        errorEl.classList.add('hidden');
        return;
    }

    const formData = new FormData();
    formData.append('court_file_number', input);

    try {
        const resp = await fetch('{{ url_for("dossiers.parse_court_file") }}', {
            method: 'POST',
            headers: { 'X-CSRFToken': '{{ csrf_token() }}' },
            body: formData,
        });
        const data = await resp.json();

        this.judicialMetadata = {
            district_judiciaire: data.district_judiciaire || '',
            tribunal: data.tribunal || '',
            competence: data.competence || '',
            palais_de_justice: data.palais_de_justice || '',
            greffe_number: data.greffe_number || '',
            juridiction_number: data.juridiction_number || '',
            is_administrative: data.is_administrative || false,
        };

        if (data.parse_error) {
            errorEl.textContent = data.parse_error;
            errorEl.classList.remove('hidden');
        } else {
            errorEl.classList.add('hidden');
        }
    } catch (err) {
        console.error('Court file parsing failed:', err);
    }
},
```

## Step 7 — Save Parsed Metadata on Dossier Create/Update

In `routes/dossiers.py`, update `_form_data()` to include the new fields:

```python
def _form_data() -> dict:
    f = request.form
    return {
        ...
        # Existing fields unchanged
        "court_file_number": f.get("court_file_number", "").strip(),
        "district_judiciaire": f.get("district_judiciaire", "").strip(),
        "tribunal": f.get("tribunal", "").strip(),
        "competence": f.get("competence", "").strip(),
        "palais_de_justice": f.get("palais_de_justice", "").strip(),
        "greffe_number": f.get("greffe_number", "").strip(),
        "juridiction_number": f.get("juridiction_number", "").strip(),
        "is_administrative_tribunal": f.get("is_administrative_tribunal") == "true",
    }
```

## Step 8 — Display Judicial Metadata on Dossier Detail

In `templates/dossiers/detail.html`, add a section showing the parsed judicial information:

```jinja2
{% if dossier.court_file_number %}
<div class="bg-white rounded-xl border border-gray-200 p-5">
  <h2 class="text-sm font-semibold text-gray-900 mb-3">Dossier judiciaire</h2>
  <dl class="grid grid-cols-1 sm:grid-cols-2 gap-x-6 gap-y-2 text-sm">
    <div>
      <dt class="text-gray-500">Numéro</dt>
      <dd class="text-gray-900 font-medium">{{ dossier.court_file_number }}</dd>
    </div>
    {% if dossier.district_judiciaire %}
    <div>
      <dt class="text-gray-500">District judiciaire</dt>
      <dd class="text-gray-900">{{ dossier.district_judiciaire }}</dd>
    </div>
    {% endif %}
    {% if dossier.palais_de_justice %}
    <div>
      <dt class="text-gray-500">Palais de justice</dt>
      <dd class="text-gray-900">{{ dossier.palais_de_justice }}</dd>
    </div>
    {% endif %}
    {% if dossier.tribunal %}
    <div>
      <dt class="text-gray-500">Tribunal</dt>
      <dd class="text-gray-900">{{ dossier.tribunal }}</dd>
    </div>
    {% endif %}
    {% if dossier.competence %}
    <div>
      <dt class="text-gray-500">Compétence</dt>
      <dd class="text-gray-900">{{ dossier.competence }}</dd>
    </div>
    {% endif %}
    {% if dossier.is_administrative_tribunal %}
    <div class="sm:col-span-2">
      <dd class="text-amber-600 text-xs">Tribunal administratif</dd>
    </div>
    {% endif %}
  </dl>
</div>
{% endif %}
```

## Step 9 — Admin Reference Data Management (Future)

This step is deferred but the architecture supports it. A future admin page at `/admin/reference-data` would:

- List all greffes with inline editing
- List all juridictions with inline editing
- Add new entries
- Delete obsolete entries (e.g., juridiction 15, 16 which are historical)

The Firestore security rules would need to be updated to allow writes from the authenticated admin user. For now, the seed script is the only write path.

## Testing Checklist

- [ ] Seed script runs without errors and populates both collections
- [ ] `ref_greffes` contains all greffe documents with correct data
- [ ] `ref_juridictions` contains all juridiction documents with correct data
- [ ] `parse_court_file_number("500-05-123456-241")` returns Montréal, Cour supérieure, Division générale
- [ ] `parse_court_file_number("200-32-654321-199")` returns Québec, Cour du Québec, Division des petites créances
- [ ] `parse_court_file_number("TAL-123456")` returns `is_administrative=True`, no greffe/juridiction
- [ ] `parse_court_file_number("999-05-123456-241")` returns parse error for unknown greffe
- [ ] `parse_court_file_number("500-99-123456-241")` returns parse error for unknown juridiction
- [ ] `parse_court_file_number("")` returns parse error
- [ ] `parse_court_file_number("50005123456")` returns parse error (no dashes)
- [ ] Dossier form: entering a court file number and tabbing out auto-populates the 4 fields
- [ ] Dossier form: auto-populated fields are editable (user can override)
- [ ] Dossier form: administrative tribunal file shows amber notice, no auto-population
- [ ] Dossier create: all judicial metadata fields saved to Firestore
- [ ] Dossier update: changing the court file number re-parses and updates metadata
- [ ] Dossier detail: judicial metadata section displays correctly
- [ ] Dossier detail: section hidden when no court file number is set
- [ ] Existing dossiers without court_file_number are unaffected (no regression)
