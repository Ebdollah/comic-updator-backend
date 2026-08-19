import firebase_admin
from firebase_admin import credentials, firestore
from typing import Optional
import json
from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)


class FirebaseService:
    """Firebase service for database operations."""
    
    def __init__(self):
        self._app: Optional[firebase_admin.App] = None
        self._db: Optional[firestore.Client] = None
    
    def initialize(self) -> None:
        """Initialize Firebase Admin SDK."""
        try:
            if self._app is None:
                # Create credentials from environment variables
                cred_dict = {
                    "type": "service_account",
                    "project_id": settings.firebase_project_id,
                    "private_key_id": settings.firebase_private_key_id,
                    "private_key": settings.firebase_private_key.replace('\\n', '\n') if settings.firebase_private_key else None,
                    "client_email": settings.firebase_client_email,
                    "client_id": settings.firebase_client_id,
                    "auth_uri": settings.firebase_auth_uri,
                    "token_uri": settings.firebase_token_uri,
                    "auth_provider_x509_cert_url": settings.firebase_auth_provider_x509_cert_url,
                    "client_x509_cert_url": settings.firebase_client_x509_cert_url,
                }
                
                # Filter out None values
                cred_dict = {k: v for k, v in cred_dict.items() if v is not None}
                
                if not cred_dict.get("project_id"):
                    logger.warning("Firebase not configured, running without database")
                    return
                
                cred = credentials.Certificate(cred_dict)
                self._app = firebase_admin.initialize_app(cred)
                self._db = firestore.client()
                logger.info("Firebase initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize Firebase: {e}")
            raise
    
    @property
    def db(self) -> Optional[firestore.Client]:
        """Get Firestore database client."""
        if self._db is None:
            self.initialize()
        return self._db
    
    def get_collection(self, collection_name: str) -> Optional[firestore.CollectionReference]:
        """Get a Firestore collection reference."""
        if self._db is None:
            return None
        return self._db.collection(collection_name)

    def _require_collection(self, collection_name: str):
        """Get a collection reference or raise if Firebase is not configured."""
        col = self.get_collection(collection_name)
        if col is None:
            raise RuntimeError("Firestore is not initialized")
        return col

    def document_exists(self, collection_name: str, doc_id: str) -> bool:
        """Check whether a document already exists."""
        return self._require_collection(collection_name).document(doc_id).get().exists

    def existing_ids(self, collection_name: str) -> set:
        """Return the ids of every document in a collection.

        Cheaper than one exists() call per item when checking many at once.
        """
        col = self.get_collection(collection_name)
        if col is None:
            return set()
        return {doc.id for doc in col.select([]).stream()}

    def existing_values(self, collection_name: str, field: str) -> set:
        """Collect one field across a whole collection, e.g. every stored name."""
        col = self.get_collection(collection_name)
        if col is None:
            return set()
        values = set()
        for doc in col.select([field]).stream():
            value = (doc.to_dict() or {}).get(field)
            if value:
                values.add(value)
        return values

    def set_document(self, collection_name: str, doc_id: str, data: dict, merge: bool = False) -> None:
        """Create or overwrite a document."""
        self._require_collection(collection_name).document(doc_id).set(data, merge=merge)

    def get_document(self, collection_name: str, doc_id: str) -> Optional[dict]:
        """Fetch a single document, or None if it does not exist."""
        snap = self._require_collection(collection_name).document(doc_id).get()
        return snap.to_dict() if snap.exists else None

    def list_documents(self, collection_name: str, limit: int = 100) -> list:
        """List documents in a collection, newest writes first where ordering allows."""
        col = self.get_collection(collection_name)
        if col is None:
            return []
        return [{**doc.to_dict(), "id": doc.id} for doc in col.limit(limit).stream()]


# Global Firebase service instance
firebase_service = FirebaseService()
