import uuid
from dataclasses import dataclass, field

@dataclass
class IngestionCache:
    name_col: str = 'NAME OF CONTRIBUTOR'
    cluster_by_id: dict = field(default_factory=dict)
    contribution_by_id: dict = field(default_factory=dict)
    clusters_by_name_zip: dict = field(default_factory=dict)
    assignments_by_cluster: dict = field(default_factory=dict)  # Keyed by stable cluster key
    entity_by_id: dict = field(default_factory=dict)
    
    # Ingestion lookup caches
    existing_completed_hashes: set = field(default_factory=set)
    existing_txns: dict = field(default_factory=dict)

    def get_cluster_key(self, cluster):
        """
        Returns a stable hashable key for a cluster, using database ID if available
        or generating a temporary UUID for unsaved instances.
        """
        if getattr(cluster, 'id', None) is not None:
            return cluster.id
        if not hasattr(cluster, '_temp_id') or not cluster._temp_id:
            cluster._temp_id = f"temp_cluster_{uuid.uuid4()}"
        return cluster._temp_id

    def get_entity_key(self, entity):
        """
        Returns a stable hashable key for an entity, using database ID if available
        or generating a temporary UUID for unsaved instances.
        """
        if getattr(entity, 'id', None) is not None:
            return entity.id
        if not hasattr(entity, '_temp_id') or not entity._temp_id:
            entity._temp_id = f"temp_entity_{uuid.uuid4()}"
        return entity._temp_id
