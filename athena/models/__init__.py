"""Firestore data-access layer — shared client singleton."""

from google.cloud import firestore

# Module-level singleton; initialised once on first import.
# In App Engine the project ID is inferred from the environment.
db: firestore.Client = firestore.Client()
