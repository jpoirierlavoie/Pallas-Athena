# PATCH: Add Folder Organization to Document Storage (Phase 9)

Read SPEC.md and CLAUDE.md for full project context. This patch adds hierarchical folder organization to the existing Phase 9 document storage system. Documents and folders belong to a dossier. Folders can be nested.

## Architecture Approach

Folders are **Firestore metadata only**. They do not exist in Firebase Storage — files remain stored at their existing flat paths in Storage. The folder hierarchy is modeled in Firestore via a `folders` subcollection with a `parent_folder_id` field for nesting. Each existing document gains an optional `folder_id` field that places it inside a folder (null = dossier root).

This means:
- Creating a folder = creating a Firestore document (no Storage operation)
- Moving a file into a folder = updating the file's `folder_id` in Firestore (no Storage operation)
- Deleting a folder = reassigning or deleting its contents (no Storage path changes)
- The Storage path of a file NEVER changes after upload — folders are a purely logical/UI concept

## Step 1 — Add Firestore Schema: `folders/{folderId}`

Create a new subcollection under dossiers. Add to `models/folder.py`:

```python
# Firestore path: users/{userId}/dossiers/{dossierId}/folders/{folderId}
{
    "id": "uuid-v4",
    "dossier_id": "uuid-ref",
    "name": "Pièces du demandeur",           # Folder display name
    "parent_folder_id": None | "uuid-ref",   # None = root of dossier, uuid = nested inside another folder
    "order": 0,                               # Display order among siblings (optional, for manual sorting)
    "created_at": datetime,
    "updated_at": datetime
}
```

## Step 2 — Model Layer: `models/folder.py`

Create a new model file with these functions:

```python
def create_folder(dossier_id: str, name: str, parent_folder_id: str | None = None) -> dict
    # Validate: name is non-empty, max 100 chars, no path separators (/ \)
    # Validate: if parent_folder_id is provided, it must exist and belong to the same dossier
    # Validate: no duplicate folder name within the same parent (case-insensitive)
    # Generate UUID, set created_at/updated_at, write to Firestore
    # Return the created folder dict

def get_folder(dossier_id: str, folder_id: str) -> dict | None
    # Return folder document or None

def list_folders(dossier_id: str, parent_folder_id: str | None = None) -> list[dict]
    # Return all folders where parent_folder_id matches the argument
    # If parent_folder_id is None, return root-level folders
    # Order by name (alphabetical)

def rename_folder(dossier_id: str, folder_id: str, new_name: str) -> dict
    # Validate: same rules as create (non-empty, no dupes in same parent)
    # Update name and updated_at

def move_folder(dossier_id: str, folder_id: str, new_parent_folder_id: str | None) -> dict
    # Validate: new_parent_folder_id must exist (or be None for root)
    # Validate: CRITICAL — prevent circular references. The new parent must not be
    #   the folder itself, nor any descendant of the folder. Walk up the parent chain
    #   from new_parent_folder_id to root and verify folder_id is never encountered.
    # Validate: no duplicate name in the new parent location
    # Update parent_folder_id and updated_at

def delete_folder(dossier_id: str, folder_id: str, recursive: bool = False) -> bool
    # If recursive=False:
    #   Check if folder has any child folders or documents
    #   If yes, raise an error: "Le dossier n'est pas vide."
    #   If no, delete the folder document
    # If recursive=True:
    #   Move all contained documents to the parent folder (or root if no parent)
    #   Recursively delete all child folders (moving their documents up too)
    #   Delete this folder
    # Never delete actual files from Storage — only reorganize Firestore metadata

def get_folder_breadcrumb(dossier_id: str, folder_id: str | None) -> list[dict]
    # Walk up the parent chain from folder_id to root
    # Return a list of {id, name} dicts from root to current folder
    # Used for breadcrumb navigation in the UI
    # If folder_id is None, return empty list (we're at root)
    # Guard against infinite loops (max depth 20)

def get_folder_tree(dossier_id: str) -> list[dict]
    # Fetch ALL folders for this dossier in one query
    # Build a nested tree structure in memory:
    #   Each node: {id, name, parent_folder_id, children: []}
    # Return the root-level nodes with children nested recursively
    # Used for the folder tree sidebar and move-to-folder modal
```

## Step 3 — Modify Existing Document Schema

Add `folder_id` to the existing document schema. Update `models/document.py`:

```python
# Add to existing document schema:
"folder_id": None | "uuid-ref",   # None = dossier root level
```

Update these existing functions:

```python
def upload_document(dossier_id, file_stream, filename, metadata):
    # Add: accept optional folder_id in metadata
    # Add: if folder_id provided, validate it exists in this dossier
    # Storage path does NOT change — folder_id is metadata only

def list_documents(dossier_id, folder_id=None, category=None, search=None):
    # MODIFY: add folder_id filter parameter
    # When folder_id is explicitly passed (even as None), filter documents to that folder
    # When folder_id is a specific ID, return only documents in that folder
    # When folder_id is None, return only documents at dossier root level
    # When search is active, search across ALL folders (ignore folder_id filter)
    #   and include the folder name/path in results for context
```

Add new function:

```python
def move_document(dossier_id: str, document_id: str, target_folder_id: str | None) -> dict:
    # Validate: document exists and belongs to this dossier
    # Validate: if target_folder_id is not None, it must exist in this dossier
    # Update folder_id and updated_at in Firestore
    # Do NOT move the file in Storage — this is metadata only

def move_documents_bulk(dossier_id: str, document_ids: list[str], target_folder_id: str | None) -> int:
    # Batch version of move_document
    # Return count of successfully moved documents
    # Use a Firestore batch write for atomicity
```

## Step 4 — Update Routes: `routes/documents.py`

Modify the existing document routes and add folder routes:

```python
# === Folder Routes (add these) ===

@bp.route('/dossiers/<dossier_id>/folders', methods=['POST'])
    # Create a new folder
    # Accept: name, parent_folder_id (optional)
    # Return: HTMX partial refreshing the file browser, or JSON for modal

@bp.route('/dossiers/<dossier_id>/folders/<folder_id>/rename', methods=['POST'])
    # Rename a folder
    # Accept: new_name
    # Return: HTMX partial updating the folder name in place

@bp.route('/dossiers/<dossier_id>/folders/<folder_id>/move', methods=['POST'])
    # Move a folder to a new parent
    # Accept: new_parent_folder_id (None for root)
    # Return: HTMX partial refreshing the file browser

@bp.route('/dossiers/<dossier_id>/folders/<folder_id>', methods=['DELETE'])
    # Delete a folder
    # Accept: recursive (boolean, default false)
    # Return: HTMX partial refreshing the file browser

# === Modified Document Routes ===

@bp.route('/dossiers/<dossier_id>/documents')
    # MODIFY: accept ?folder_id= query param (None or UUID)
    # Pass folder_id to list_documents() and list_folders()
    # Return both folders and documents for the current location
    # Include breadcrumb navigation

@bp.route('/dossiers/<dossier_id>/documents/upload', methods=['POST'])
    # MODIFY: accept folder_id in form data
    # Pass to upload_document()

@bp.route('/dossiers/<dossier_id>/documents/<document_id>/move', methods=['POST'])
    # NEW: Move a document to a different folder
    # Accept: target_folder_id
    # Return: HTMX partial (toast + refresh)

@bp.route('/dossiers/<dossier_id>/documents/move-bulk', methods=['POST'])
    # NEW: Move multiple documents at once
    # Accept: document_ids[], target_folder_id
    # Return: HTMX partial (toast + refresh)
```

## Step 5 — Update Templates

### Modify `templates/documents/browser.html`

The document browser is the core UI change. It must now function as a file browser with folder navigation.

**Layout:**
- **Breadcrumb bar** at top: "Documents > Pièces > Expert Tremblay" — each segment is a clickable link. Root is "Documents". Use HTMX `hx-get` on each breadcrumb segment to navigate without full reload.
- **Toolbar row** below breadcrumb:
  - "Nouveau dossier" button (folder icon + text) — opens inline form or small modal to enter folder name
  - "Téléverser" button (existing upload button)
  - When items are selected (checkbox mode): "Déplacer" button, "Supprimer" button
  - Search input (existing — when active, searches across all folders with folder path shown in results)
  - Category filter (existing)
  - Sort dropdown (existing)
- **Content area:**
  - Folders displayed FIRST, as a group, before documents
  - Each folder row: folder icon (📁 or SVG), folder name, item count ("3 fichiers"), date modified. Tap to navigate into folder.
  - Each document row: file type icon, display_name, category badge, file_size, upload date. Tap to view/download. (Existing design)
  - Both folders and documents have a checkbox on the left for multi-select
  - Empty folder state: "Ce dossier est vide."
  - Empty root state (no folders, no documents): "Aucun document pour le moment. Téléversez votre premier fichier ou créez un dossier."

**Folder interactions:**
- **Tap/click folder** → navigates into it (HTMX swap of the content area, updates breadcrumb, updates URL query param ?folder_id=)
- **Long-press or right-click folder** (desktop) → context menu: Renommer, Déplacer, Supprimer
- **Long-press folder** (mobile) → action sheet: Renommer, Déplacer, Supprimer

**Create folder inline:**
- Clicking "Nouveau dossier" inserts a new row at the top of the folder list with an editable text input (folder name), pre-focused
- Press Enter or tap checkmark to save (HTMX POST)
- Press Escape or tap X to cancel
- This pattern is similar to how Google Drive handles "New folder"

**Move-to-folder modal:**
When the user selects documents/folders and clicks "Déplacer", show a modal with:
- A tree view of all folders in the dossier (use get_folder_tree())
- The dossier root as the top-level option ("Racine du dossier")
- Current location highlighted/disabled (can't move to where you already are)
- "Déplacer ici" button at the bottom
- HTMX POST on confirm, refresh browser on success

**Drag-and-drop (desktop only, nice-to-have):**
If feasible with HTMX/Alpine.js, allow dragging documents onto folder rows to move them. This is a progressive enhancement — the move modal is the primary mechanism. Use HTML5 Drag and Drop API with Alpine.js state management. Do NOT implement this if it adds significant complexity — the modal is sufficient.

### Modify `templates/documents/viewer.html`

Add to the document viewer/detail page:
- Show the folder path: "Dans: Pièces > Expert Tremblay" (clickable, navigates back to that folder)
- "Déplacer" button alongside existing "Télécharger" and "Supprimer" buttons

### Modify upload flow

When uploading files:
- If currently browsing inside a folder, the uploaded file is automatically placed in that folder (folder_id passed as hidden field)
- The upload form should show the current folder context: "Téléverser dans: Pièces > Expert Tremblay"
- If uploading from dossier root, folder_id is None

## Step 6 — Update the Firestore Collection Map in SPEC.md

The collection map should now include:
```
users/{userId}/
├── ...
├── dossiers/{dossierId}
│   └── folders/{folderId}        # NEW — folder metadata
├── documents/{documentId}        # Now includes folder_id field
├── ...
```

## Validation Constraints

These MUST be enforced in the model layer:
1. Folder names: max 100 characters, no / or \ characters, no leading/trailing whitespace
2. No duplicate folder names within the same parent (case-insensitive comparison)
3. Maximum nesting depth: 5 levels (root → L1 → L2 → L3 → L4 → L5). Enforce in create_folder and move_folder.
4. Circular reference prevention in move_folder: walk the parent chain to verify the target is not a descendant
5. Non-empty folder deletion without recursive flag must be rejected with a clear French error message
6. Folder creation, rename, and move should update the parent folder's updated_at (if it has a parent)

## Testing Checklist

After implementation, verify:
- [ ] Can create a folder at dossier root level
- [ ] Can create a nested folder inside another folder
- [ ] Maximum nesting depth (5 levels) is enforced
- [ ] Duplicate folder names in the same parent are rejected
- [ ] Can rename a folder
- [ ] Can navigate into and out of folders via breadcrumb and clicking
- [ ] Breadcrumb displays correct path and each segment is clickable
- [ ] Documents uploaded while inside a folder are assigned to that folder
- [ ] Documents at root level are unaffected (folder_id remains None)
- [ ] Can move a document into a folder via the move modal
- [ ] Can move multiple documents at once (bulk move)
- [ ] Can move a document back to root
- [ ] Can move a folder to a different parent
- [ ] Circular folder moves are prevented
- [ ] Deleting an empty folder works
- [ ] Deleting a non-empty folder without recursive flag shows error
- [ ] Deleting a folder with recursive=True reassigns contents to parent and deletes folder
- [ ] Search searches across all folders and shows folder context in results
- [ ] URL query param ?folder_id= correctly restores folder navigation on page refresh
- [ ] Mobile layout: folder rows have adequate touch targets, breadcrumb scrolls horizontally if deep
- [ ] Existing documents without folder_id continue to display at root level (backward compatible)
- [ ] The existing KYC document linking (Phase 2 client compliance) is unaffected
