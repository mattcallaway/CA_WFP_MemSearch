import re
from django.db import transaction
from django.db.models import Q
from roster.models import (
    ContributorEntity, Person, Organization, ContributionCluster, 
    ContributionClusterAssignment, MergeDecision, AuditEvent, Contribution
)

# Configuration list for organization keywords
ORGANIZATION_KEYWORDS = [
    r"\bUNION\b", r"\bPAC\b", r"\bCOMMITTEE\b", r"\bLLC\b", r"\bINC\b", 
    r"\bCORP\b", r"\bASSOCIATION\b", r"\bTRUST\b", r"\bLOCAL\b", r"\bCLUB\b", 
    r"\bCOUNCIL\b", r"\bPARTY\b", r"\bFUND\b", r"\bCAMPAIGN\b", r"\bFRIENDS OF\b", 
    r"\bTO ELECT\b", r"\bELECT\b", r"\bCO\b", r"\bFOUNDATION\b", r"\bBOARD\b"
]

JOINT_KEYWORDS = [
    r"\bAND\b", r"&", r"\bOR\b", r"/"
]

PREFIXES = [
    r"\bMR\b", r"\bMRS\b", r"\bMS\b", r"\bDR\b", r"\bPROF\b", r"\bHON\b"
]

SUFFIXES = [
    r"\bJR\b", r"\bSR\b", r"\bIII\b", r"\bIV\b", r"\bII\b", r"\bMD\b", r"\bPHD\b", r"\bDDS\b"
]

def clean_token(token):
    return re.sub(r"[^\w\s]", "", token).strip()

def detect_entity_type(name_str):
    name_upper = name_str.upper()
    
    # Check joint keywords
    for keyword in JOINT_KEYWORDS:
        if re.search(keyword, name_upper):
            return 'JOINT'
            
    # Check organization keywords
    for keyword in ORGANIZATION_KEYWORDS:
        if re.search(keyword, name_upper):
            return 'ORGANIZATION'
            
    return 'INDIVIDUAL'

def extract_and_strip_prefix_suffix(text):
    tokens = text.split()
    prefix = ""
    suffix = ""
    
    # Strip prefixes from the beginning
    while tokens:
        matched = False
        clean_token = re.sub(r"\.", "", tokens[0]).strip()
        for p_pat in PREFIXES:
            if re.match(p_pat, clean_token):
                prefix = clean_token
                tokens.pop(0)
                matched = True
                break
        if not matched:
            break
            
    # Strip suffixes from the end
    while tokens:
        matched = False
        clean_token = re.sub(r"\.", "", tokens[-1]).strip()
        for s_pat in SUFFIXES:
            if re.match(s_pat, clean_token):
                suffix = clean_token
                tokens.pop()
                matched = True
                break
        if not matched:
            break
            
    return " ".join(tokens), prefix, suffix

def normalize_name(name_str):
    """
    Standardizes casing, strips prefixes, suffixes, and handles Last-Name-First formats.
    Returns:
        dict with keys:
            normalized_full_name (str): Standardized name e.g. "JOHN DOE" or "TEAMSTERS LOCAL 396"
            first_name (str): Parsed first name (only for individuals)
            middle_name (str): Parsed middle name/initial
            last_name (str): Parsed last name
            suffix (str): Parsed suffix (JR, SR, etc.)
    """
    original_name = name_str.strip()
    if not original_name:
        return {
            "normalized_full_name": "",
            "first_name": "",
            "middle_name": "",
            "last_name": "",
            "suffix": ""
        }

    # Detect entity type
    entity_type = detect_entity_type(original_name)
    name_upper = original_name.upper()

    if entity_type in ['ORGANIZATION', 'JOINT']:
        # For organizations, clean extra spaces but preserve structure
        cleaned_org_name = re.sub(r"\s+", " ", name_upper).strip()
        return {
            "normalized_full_name": cleaned_org_name,
            "first_name": "",
            "middle_name": "",
            "last_name": "",
            "suffix": ""
        }

    # Clean double spaces and punctuation except comma
    name_upper = re.sub(r"[^\w\s\-\',]", "", name_upper)

    # Handle individual name parsing
    # Handle comma/Last-Name-First (e.g. "DOE, JOHN A.")
    if "," in name_upper:
        parts = name_upper.split(",", 1)
        last_part, l_prefix, l_suffix = extract_and_strip_prefix_suffix(parts[0].strip())
        first_middle_part, f_prefix, f_suffix = extract_and_strip_prefix_suffix(parts[1].strip())
        # Combine as "FIRST MIDDLE LAST"
        name_combined = f"{first_middle_part} {last_part}"
        parsed_prefix = f_prefix or l_prefix
        parsed_suffix = f_suffix or l_suffix
    else:
        name_combined, parsed_prefix, parsed_suffix = extract_and_strip_prefix_suffix(name_upper)

    tokens = [t.strip() for t in name_combined.split() if t.strip()]

    first_name = ""
    middle_name = ""
    last_name = ""

    if len(tokens) == 1:
        last_name = tokens[0]
    elif len(tokens) == 2:
        first_name = tokens[0]
        last_name = tokens[1]
    elif len(tokens) >= 3:
        first_name = tokens[0]
        # Middle token(s) are grouped as middle_name, last is last_name
        middle_name = " ".join(tokens[1:-1])
        last_name = tokens[-1]

    # Rebuild normalized full name
    name_parts = [first_name, middle_name, last_name]
    normalized_full_name = " ".join([p for p in name_parts if p]).strip()

    return {
        "normalized_full_name": normalized_full_name,
        "first_name": first_name,
        "middle_name": middle_name,
        "last_name": last_name,
        "suffix": parsed_suffix
    }

def has_conflict(cluster, first_name, middle_name, last_name, suffix, employer, occupation, assignment_cache=None):
    """
    Checks if a contribution conflicts with an existing cluster.
    """
    # Use explicit cache if provided, otherwise fallback to DB query
    if assignment_cache is not None:
        key = None
        cache_map = assignment_cache.assignments_by_cluster if hasattr(assignment_cache, 'assignments_by_cluster') else assignment_cache
        
        if getattr(cluster, 'id', None) is not None and cluster.id in cache_map:
            key = cluster.id
        elif getattr(cluster, '_temp_id', None) is not None and cluster._temp_id in cache_map:
            key = cluster._temp_id
        elif cluster in cache_map:
            key = cluster
            
        if key is not None:
            assignments = [a for a in cache_map[key] if a.is_active]
        else:
            assignments = []
    else:
        assignments = ContributionClusterAssignment.objects.filter(
            contribution_cluster=cluster,
            is_active=True
        ).select_related('contribution', 'contribution__raw_contribution')
    
    for assign in assignments:
        c = assign.contribution
        
        # Retrieve name column name from cache or fallback
        if assignment_cache and hasattr(assignment_cache, 'name_col'):
            name_col = assignment_cache.name_col
        else:
            batch = c.raw_contribution.import_batch
            rules = batch.mapping_profile.mapping_rules if (batch and batch.mapping_profile) else {}
            name_col = rules.get('NAME OF CONTRIBUTOR', 'NAME OF CONTRIBUTOR')
        
        # Name components conflict checks (if both values are non-empty, they must match)
        c_parsed = normalize_name(c.raw_contribution.original_values.get(name_col, ''))
        
        # Suffix conflict
        if suffix and c_parsed['suffix'] and suffix != c_parsed['suffix']:
            return True
            
        # Middle name conflict (e.g. "A" vs "B")
        if middle_name and c_parsed['middle_name']:
            # Strip initials for comparison if one is just an initial
            m1 = middle_name.replace(".", "").strip()
            m2 = c_parsed['middle_name'].replace(".", "").strip()
            if m1[0] != m2[0]: # Conflict if first letters differ
                return True
                
        # Employer conflict
        if employer and c.employer:
            emp1 = employer.strip().upper()
            emp2 = c.employer.strip().upper()
            if emp1 != emp2:
                # Basic check: one must not be a direct conflict. If totally different, flag it.
                # Avoid matching e.g. "ACME CORP" vs "STARK INDUSTRIES"
                return True
                
        # Occupation conflict
        if occupation and c.occupation:
            occ1 = occupation.strip().upper()
            occ2 = c.occupation.strip().upper()
            if occ1 != occ2:
                return True
                
    return False

def check_corroboration(cluster, first_name, middle_name, last_name, suffix, employer, occupation, assignment_cache=None):
    """
    Returns True if there is positive corroborating evidence matching the cluster.
    (e.g., matching non-empty middle name, suffix, or employer/occupation).
    Absence of a conflict is NOT treated as positive corroboration.
    """
    # Use explicit cache if provided, otherwise fallback to DB query
    if assignment_cache is not None:
        key = None
        cache_map = assignment_cache.assignments_by_cluster if hasattr(assignment_cache, 'assignments_by_cluster') else assignment_cache
        
        if getattr(cluster, 'id', None) is not None and cluster.id in cache_map:
            key = cluster.id
        elif getattr(cluster, '_temp_id', None) is not None and cluster._temp_id in cache_map:
            key = cluster._temp_id
        elif cluster in cache_map:
            key = cluster
            
        if key is not None:
            assignments = [a for a in cache_map[key] if a.is_active]
        else:
            assignments = []
    else:
        assignments = list(ContributionClusterAssignment.objects.filter(
            contribution_cluster=cluster,
            is_active=True
        ).select_related('contribution', 'contribution__raw_contribution'))
    
    if not assignments:
        return False
        
    has_corrob = False
    
    for assign in assignments:
        c = assign.contribution
        
        # Retrieve name column name from cache or fallback
        if assignment_cache and hasattr(assignment_cache, 'name_col'):
            name_col = assignment_cache.name_col
        else:
            batch = c.raw_contribution.import_batch
            rules = batch.mapping_profile.mapping_rules if (batch and batch.mapping_profile) else {}
            name_col = rules.get('NAME OF CONTRIBUTOR', 'NAME OF CONTRIBUTOR')
            
        c_parsed = normalize_name(c.raw_contribution.original_values.get(name_col, ''))
        
        # Non-empty matching suffix
        if suffix and c_parsed['suffix'] and suffix == c_parsed['suffix']:
            has_corrob = True
            
        # Non-empty matching middle name/initial
        if middle_name and c_parsed['middle_name']:
            m1 = middle_name.replace(".", "").strip().upper()
            m2 = c_parsed['middle_name'].replace(".", "").strip().upper()
            if m1 == m2 or (len(m1) == 1 and m1[0] == m2[0]) or (len(m2) == 1 and m1[0] == m2[0]):
                has_corrob = True
                
        # Non-empty matching employer
        if employer and c.employer:
            if employer.strip().upper() == c.employer.strip().upper():
                has_corrob = True
                
        # Non-empty matching occupation
        if occupation and c.occupation:
            if occupation.strip().upper() == c.occupation.strip().upper():
                has_corrob = True
                
    return has_corrob

@transaction.atomic
def resolve_and_cluster_contribution(contribution, actor="SYSTEM"):
    """
    Associates a Contribution with a ContributionCluster and ContributorEntity.
    Handles Individuals, Organizations, Joint and Unknown types.
    """
    raw_name = contribution.raw_contribution.original_values.get('NAME OF CONTRIBUTOR', '')
    parsed = normalize_name(raw_name)
    normalized_full_name = parsed['normalized_full_name']
    
    zip_code = contribution.raw_contribution.original_values.get('ZIP', '').strip()
    # Format ZIP (ensure no truncation of leading zeros)
    if zip_code and len(zip_code) < 5 and zip_code.isdigit():
        zip_code = zip_code.zfill(5)
        
    entity_type = detect_entity_type(raw_name)
    
    # Check if an manual override exists on the raw contribution
    is_org = (entity_type == 'ORGANIZATION')
    is_joint = (entity_type == 'JOINT')
    
    # Default fallback display name
    display_name = normalized_full_name if normalized_full_name else raw_name.upper()
    
    # 1. Non-Individuals (Organizations, Joint names) are NEVER auto-grouped.
    if is_org or is_joint:
        ent_type = 'ORGANIZATION' if is_org else 'JOINT'
        entity = ContributorEntity.objects.create(
            entity_type=ent_type,
            display_name=display_name,
            is_verified=False
        )
        if is_org:
            Organization.objects.create(
                contributor_entity=entity,
                legal_name=display_name,
                committee_id=contribution.raw_contribution.original_values.get('ID NUMBER', '')
            )
        cluster = ContributionCluster.objects.create(
            contributor_entity=entity,
            normalized_name=normalized_full_name,
            zip_code=zip_code,
            confidence_level='LOW',
            confidence_explanation="Non-individual entities are isolated to single clusters by default."
        )
        ContributionClusterAssignment.objects.create(
            contribution=contribution,
            contribution_cluster=cluster,
            assigned_by=actor,
            is_active=True
        )
        return cluster

    # 2. Individuals
    # Find active non-conflicting candidate clusters
    candidates = ContributionCluster.objects.filter(
        normalized_name=normalized_full_name,
        zip_code=zip_code,
        contributor_entity__entity_type='INDIVIDUAL'
    )
    
    matched_cluster = None
    for cand in candidates:
        if not has_conflict(
            cand, 
            parsed['first_name'], parsed['middle_name'], parsed['last_name'], parsed['suffix'], 
            contribution.employer, contribution.occupation
        ):
            # Require positive corroborating evidence to auto-group
            corroborated = check_corroboration(
                cand,
                parsed['first_name'], parsed['middle_name'], parsed['last_name'], parsed['suffix'], 
                contribution.employer, contribution.occupation
            )
            if corroborated:
                matched_cluster = cand
                break
            
    if matched_cluster:
        # Check corroboration
        corroborated = check_corroboration(
            matched_cluster,
            parsed['first_name'], parsed['middle_name'], parsed['last_name'], parsed['suffix'], 
            contribution.employer, contribution.occupation
        )
        
        # Update confidence if corroborated
        if corroborated and matched_cluster.confidence_level == 'LOW':
            matched_cluster.confidence_level = 'MEDIUM'
            matched_cluster.confidence_explanation = "Grouped based on matching Name, ZIP, and corroborated employer/occupation/middle name."
            matched_cluster.save()
            
        ContributionClusterAssignment.objects.create(
            contribution=contribution,
            contribution_cluster=matched_cluster,
            assigned_by=actor,
            is_active=True
        )
        return matched_cluster
    else:
        # Create a new entity and cluster
        entity = ContributorEntity.objects.create(
            entity_type='INDIVIDUAL',
            display_name=display_name,
            is_verified=False
        )
        Person.objects.create(
            contributor_entity=entity,
            first_name=parsed['first_name'],
            middle_name=parsed['middle_name'],
            last_name=parsed['last_name'],
            suffix=parsed['suffix']
        )
        cluster = ContributionCluster.objects.create(
            contributor_entity=entity,
            normalized_name=normalized_full_name,
            zip_code=zip_code,
            confidence_level='LOW',
            confidence_explanation="Initial low-confidence cluster based on single contribution."
        )
        ContributionClusterAssignment.objects.create(
            contribution=contribution,
            contribution_cluster=cluster,
            assigned_by=actor,
            is_active=True
        )
        return cluster

@transaction.atomic
def merge_clusters(source_cluster_id, target_cluster_id, actor="SYSTEM"):
    """
    Merges source cluster into target cluster.
    Deactivates active assignments on source cluster and maps them to target cluster.
    """
    source_cluster = ContributionCluster.objects.get(id=source_cluster_id)
    target_cluster = ContributionCluster.objects.get(id=target_cluster_id)
    
    # 1. Create Merge Decision
    merge_dec = MergeDecision.objects.create(
        source_cluster=source_cluster,
        target_cluster=target_cluster,
        merged_by=actor,
        is_active=True
    )
    
    # 2. Re-assign contributions
    active_assignments = ContributionClusterAssignment.objects.filter(
        contribution_cluster=source_cluster,
        is_active=True
    )
    
    for assign in active_assignments:
        assign.is_active = False
        assign.save()
        
        ContributionClusterAssignment.objects.create(
            contribution=assign.contribution,
            contribution_cluster=target_cluster,
            assigned_by=actor,
            is_active=True,
            notes=f"Merged from Cluster {source_cluster_id} via MergeDecision {merge_dec.id}"
        )
        
    # Log event
    AuditEvent.objects.create(
        event_type="MERGE_CLUSTERS",
        description=f"Merged Cluster {source_cluster_id} ({source_cluster.normalized_name}) into Cluster {target_cluster_id} ({target_cluster.normalized_name}).",
        actor=actor
    )
    
    # Deactivate source entity if no active contributions left
    # Recalculate will handle this
    return merge_dec

@transaction.atomic
def split_cluster(merge_decision_id, actor="SYSTEM"):
    """
    Undoes a previous MergeDecision by restoring the original assignments.
    """
    merge_dec = MergeDecision.objects.get(id=merge_decision_id)
    if not merge_dec.is_active:
        return
        
    merge_dec.is_active = False
    merge_dec.save()
    
    # Find all assignments created for the target cluster linking to contributions from this merge
    target_cluster = merge_dec.target_cluster
    source_cluster = merge_dec.source_cluster
    
    merged_assignments = ContributionClusterAssignment.objects.filter(
        contribution_cluster=target_cluster,
        is_active=True,
        notes__contains=f"MergeDecision {merge_dec.id}"
    )
    
    for assign in merged_assignments:
        assign.is_active = False
        assign.save()
        
        # Find the original assignment on the source cluster and re-activate it
        orig_assign = ContributionClusterAssignment.objects.filter(
            contribution=assign.contribution,
            contribution_cluster=source_cluster
        ).order_by('-assigned_at').first()
        
        if orig_assign:
            orig_assign.is_active = True
            orig_assign.save()
            
    # Log event
    AuditEvent.objects.create(
        event_type="SPLIT_CLUSTERS",
        description=f"Split Cluster {source_cluster.id} out of Cluster {target_cluster.id} (undid MergeDecision {merge_dec.id}).",
        actor=actor
    )
