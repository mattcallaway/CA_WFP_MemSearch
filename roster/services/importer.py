import csv
import hashlib
import decimal
import re
import os
from datetime import datetime, date
from django.db import transaction
from django.db.models import Q
from django.utils.dateparse import parse_date as django_parse_date
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied

from roster.models import (
    ImportBatch, ImportMappingProfile, RawContribution, Contribution, ContributorEntity,
    Person, Organization, ContributionClusterAssignment, ContributionCluster,
    Location, AuditEvent, FieldAssertion, ImportAttempt, MembershipAssessment
)
from roster.services.resolver import normalize_name, detect_entity_type, check_corroboration, has_conflict
from roster.services.cache import IngestionCache

def compute_file_hash(file_path):
    sha256 = hashlib.sha256()
    with open(file_path, 'rb') as f:
        while chunk := f.read(8192):
            sha256.update(chunk)
    return sha256.hexdigest()

def compute_row_hash(row_data):
    sorted_items = sorted([(str(k), str(v)) for k, v in row_data.items() if v is not None])
    row_string = "".join(f"{k}:{v}" for k, v in sorted_items)
    return hashlib.sha256(row_string.encode('utf-8')).hexdigest()

def parse_decimal(value):
    if not value:
        return None
    cleaned = value.replace('$', '').replace(',', '').strip()
    try:
        return decimal.Decimal(cleaned)
    except decimal.InvalidOperation:
        return None

def parse_date(value):
    if not value:
        return None
    cleaned = value.strip()
    # Support YYYY-MM-DD
    try:
        parsed = django_parse_date(cleaned)
        if parsed:
            return parsed
    except ValueError:
        pass
    # Support MM/DD/YYYY or M/D/YYYY
    for fmt in ('%m/%d/%Y', '%m/%d/%y', '%d/%m/%Y', '%Y/%m/%d'):
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    return None

@transaction.atomic
def import_csv_file(file_path, file_name, mapping_profile_id, actor="SYSTEM", override_duplicate=False):
    """
    Imports a CSV file of SOS contributions.
    """
    profile = ImportMappingProfile.objects.get(id=mapping_profile_id)
    rules = profile.mapping_rules
    
    file_hash = compute_file_hash(file_path)
    
    # 1. Exact-file duplicate checking
    existing_completed_batch = ImportBatch.objects.filter(file_hash=file_hash, status='COMPLETED').first()
    if existing_completed_batch:
        if not override_duplicate:
            # Create failed attempt
            ImportAttempt.objects.create(
                import_batch=existing_completed_batch,
                attempted_by=actor,
                action="REPROCESS_FAILED",
                notes=f"Attempted to import duplicate file hash '{file_hash}' without authorization override."
            )
            raise ValueError("This file has already been imported successfully.")
        else:
            # Overwrite/Reprocess flow
            AuditEvent.objects.create(
                event_type="OVERRIDE_DUPLICATE",
                description=f"Authorized override for duplicate file '{file_name}' (hash: {file_hash}). Reprocessing batch {existing_completed_batch.id}.",
                actor=actor
            )
            # Delete old normalized data for this batch non-destructively
            Contribution.objects.filter(raw_contribution__import_batch=existing_completed_batch).delete()
            # Reset raw contribution validation statuses to unprocessed
            RawContribution.objects.filter(import_batch=existing_completed_batch).update(
                validation_status='UNPROCESSED',
                validation_errors=''
            )
            batch = existing_completed_batch
            batch.status = 'PENDING'
            batch.save()
            
            ImportAttempt.objects.create(
                import_batch=batch,
                attempted_by=actor,
                action="REPROCESS_START",
                notes=f"Reprocessing of batch {batch.id} initiated."
            )
    else:
        # Create a new batch
        batch = ImportBatch.objects.create(
            file_name=file_name,
            file_hash=file_hash,
            file_type="CSV",
            imported_by=actor,
            mapping_profile=profile,
            status='PENDING'
        )
        ImportAttempt.objects.create(
            import_batch=batch,
            attempted_by=actor,
            action="IMPORT_START",
            notes=f"Initial import of batch {batch.id} started."
        )

    # 2. Parse CSV
    parsed_rows = []
    raw_contrib_instances = []
    
    name_col = rules.get('NAME OF CONTRIBUTOR')
    amount_col = rules.get('AMOUNT')
    date_col = rules.get('TRANSACTION DATE')
    zip_col = rules.get('ZIP')
    txn_col = rules.get('TRANSACTION NUMBER') or rules.get('TRANSACTION ID')
    city_col = rules.get('CITY')
    state_col = rules.get('STATE')
    emp_col = rules.get('EMPLOYER')
    occ_col = rules.get('OCCUPATION')
    filed_date_col = rules.get('FILED DATE')
    street_col = rules.get('STREET ADDRESS')

    # Fetch existing raw contributions for this batch to avoid N+1 queries in the loop
    existing_raw_contribs = {rc.row_number: rc for rc in RawContribution.objects.filter(import_batch=batch)}

    # Read the file
    with open(file_path, mode='r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        for index, row in enumerate(reader, start=1):
            row_hash = compute_row_hash(row)
            
            name_val = (row.get(name_col) or '').strip() if name_col else ''
            amount_val = (row.get(amount_col) or '').strip() if amount_col else ''
            date_val = (row.get(date_col) or '').strip() if date_col else ''
            zip_val = (row.get(zip_col) or '').strip() if zip_col else ''
            txn_num = (row.get(txn_col) or '').strip() if txn_col else ''
            city_val = (row.get(city_col) or '').strip() if city_col else ''
            state_val = (row.get(state_col) or '').strip() if state_col else ''
            emp_val = (row.get(emp_col) or '').strip() if emp_col else ''
            occ_val = (row.get(occ_col) or '').strip() if occ_col else ''
            filed_date_val = (row.get(filed_date_col) or '').strip() if filed_date_col else ''
            street_val = (row.get(street_col) or '').strip() if street_col else ''
            
            parsed_rows.append({
                'index': index,
                'row_data': row,
                'row_hash': row_hash,
                'name_val': name_val,
                'amount_val': amount_val,
                'date_val': date_val,
                'zip_val': zip_val,
                'txn_num': txn_num,
                'city_val': city_val,
                'state_val': state_val,
                'emp_val': emp_val,
                'occ_val': occ_val,
                'filed_date_val': filed_date_val,
                'street_val': street_val
            })
            
            # Check if this raw contribution already exists for reprocessing from memory
            raw_contrib = existing_raw_contribs.get(index)
            if raw_contrib:
                raw_contrib.original_values = row
                raw_contrib.raw_row_hash = row_hash
                raw_contrib.validation_status = 'UNPROCESSED'
                raw_contrib.validation_errors = ''
            else:
                raw_contrib = RawContribution(
                    import_batch=batch,
                    row_number=index,
                    original_values=row,
                    raw_row_hash=row_hash,
                    validation_status='UNPROCESSED'
                )
            raw_contrib_instances.append(raw_contrib)

    # Bulk create raw contributions if not already in DB
    if not existing_completed_batch:
        RawContribution.objects.bulk_create(raw_contrib_instances, batch_size=500)
        
    raw_contrib_map = {rc.row_number: rc for rc in RawContribution.objects.filter(import_batch=batch)}

    # 3. Instantiate Ingestion Cache and pre-populate lookup lists
    ing_cache = IngestionCache(name_col=name_col)
    
    # Cache existing completed row hashes
    completed_batches = ImportBatch.objects.filter(status='COMPLETED').exclude(id=batch.id)
    completed_batch_ids = list(completed_batches.values_list('id', flat=True))
    
    # SQLite parameter chunking limit is 32766, chunk size 500 is extremely safe
    chunk_size = 500
    for i in range(0, len(completed_batch_ids), chunk_size):
        batch_chunk = completed_batch_ids[i:i+chunk_size]
        hashes = RawContribution.objects.filter(
            import_batch_id__in=batch_chunk,
            validation_status='ACCEPTED'
        ).values_list('raw_row_hash', flat=True)
        ing_cache.existing_completed_hashes.update(hashes)

    # Pre-fetch existing completed transactions with exact transaction numbers
    incoming_txns = [p['txn_num'] for p in parsed_rows if p['txn_num']]
    if incoming_txns:
        for i in range(0, len(incoming_txns), chunk_size):
            chunk = incoming_txns[i:i+chunk_size]
            qs = Contribution.objects.filter(
                transaction_number__in=chunk,
                raw_contribution__import_batch__status='COMPLETED'
            ).select_related('raw_contribution')
            for c in qs:
                ing_cache.existing_txns.setdefault(c.transaction_number, []).append(c)

    # Collect search keys for candidate clusters
    unique_names = set()
    unique_zips = set()
    for p in parsed_rows:
        zip_code = p['zip_val']
        if zip_code and len(zip_code) < 5 and zip_code.isdigit():
            zip_code = zip_code.zfill(5)
        p['zip_formatted'] = zip_code

        if p['name_val']:
            norm_name = normalize_name(p['name_val'])['normalized_full_name']
            p['norm_name'] = norm_name
            p['entity_type'] = detect_entity_type(p['name_val'])
            if p['entity_type'] == 'INDIVIDUAL' and norm_name:
                unique_names.add(norm_name)
                if zip_code:
                    unique_zips.add(zip_code)
        else:
            p['norm_name'] = ''
            p['entity_type'] = 'UNKNOWN'

    # Batch lookup candidate clusters from DB
    candidate_clusters = []
    if unique_names:
        unique_names_list = list(unique_names)
        for i in range(0, len(unique_names_list), chunk_size):
            name_chunk = unique_names_list[i:i+chunk_size]
            qs = ContributionCluster.objects.filter(
                normalized_name__in=name_chunk,
                contributor_entity__entity_type='INDIVIDUAL'
            ).select_related('contributor_entity').prefetch_related(
                'assignments__contribution__raw_contribution'
            )
            candidate_clusters.extend(qs)

    # Populate explicit cache mappings
    for cluster in candidate_clusters:
        key = (cluster.normalized_name, cluster.zip_code)
        ing_cache.clusters_by_name_zip.setdefault(key, []).append(cluster)
        ing_cache.cluster_by_id[cluster.id] = cluster
        ing_cache.entity_by_id[cluster.contributor_entity_id] = cluster.contributor_entity
        
        # Cache active assignments explicitly
        cluster_key = ing_cache.get_cluster_key(cluster)
        ing_cache.assignments_by_cluster[cluster_key] = list(cluster.assignments.all())

    # Pre-fetch composite duplicates for rows without transaction numbers
    # This replaces the per-row DB query that was an N+1 hazard
    no_txn_composites = {}  # (amount, date) -> list of (norm_name, zip, contribution)
    no_txn_rows = [p for p in parsed_rows if not p['txn_num']]
    if no_txn_rows:
        # Collect unique (amount, date) pairs
        composite_keys = set()
        for p in no_txn_rows:
            amt = parse_decimal(p['amount_val'])
            dt = parse_date(p['date_val'])
            if amt is not None and dt is not None:
                composite_keys.add((amt, dt))
        
        if composite_keys:
            # Batch-fetch candidates from completed batches
            composite_keys_list = list(composite_keys)
            for i in range(0, len(composite_keys_list), chunk_size):
                chunk = composite_keys_list[i:i+chunk_size]
                from django.db.models import Q
                q_filter = Q()
                for amt, dt in chunk:
                    q_filter |= Q(amount=amt, transaction_date=dt)
                candidates = Contribution.objects.filter(
                    q_filter,
                    raw_contribution__import_batch__status='COMPLETED'
                ).select_related('raw_contribution')
                for c in candidates:
                    key = (c.amount, c.transaction_date)
                    c_name = normalize_name(c.raw_contribution.original_values.get(name_col, ''))['normalized_full_name']
                    c_zip = c.raw_contribution.original_values.get(zip_col, '').strip()
                    no_txn_composites.setdefault(key, []).append((c_name, c_zip, c))

    successful_count = 0
    failed_count = 0
    duplicate_count = 0
    seen_in_batch = set()

    contributions_to_create = []
    locations_to_create = []
    assignments_to_create = []
    parsed_contrib_data = {}
    
    entities_to_create = []
    persons_to_create = []
    orgs_to_create = []
    clusters_to_create = []
    clusters_to_update = []

    # 4. Process CSV rows
    for p in parsed_rows:
        row_number = p['index']
        raw_contrib = raw_contrib_map[row_number]
        
        # Check exact duplicate rows
        if p['row_hash'] in ing_cache.existing_completed_hashes or p['row_hash'] in seen_in_batch:
            raw_contrib.validation_status = 'EXACT_DUPLICATE'
            raw_contrib.validation_errors = "Exact row hash already imported."
            duplicate_count += 1
            continue
            
        seen_in_batch.add(p['row_hash'])
        
        errors = []
        if not p['name_val']:
            errors.append("Missing name of contributor.")
            
        parsed_amount = parse_decimal(p['amount_val'])
        if parsed_amount is None:
            errors.append(f"Invalid amount value: '{p['amount_val']}'.")
            
        parsed_date = parse_date(p['date_val'])
        if not parsed_date:
            errors.append(f"Invalid date value: '{p['date_val']}'.")
            
        if errors:
            raw_contrib.validation_status = 'VALIDATION_FAILURE'
            raw_contrib.validation_errors = "; ".join(errors)
            failed_count += 1
            continue
            
        validation_status = 'ACCEPTED'
        warning_msg = None
        
        if p['txn_num']:
            dups = ing_cache.existing_txns.get(p['txn_num'], [])
            dups_in_batch = [c for c in contributions_to_create if c.transaction_number == p['txn_num']]
            all_dups = dups + dups_in_batch
            
            if all_dups:
                matching_dup = [d for d in all_dups if d.amount == parsed_amount and d.transaction_date == parsed_date]
                if matching_dup:
                    validation_status = 'POSSIBLE_DUPLICATE'
                    warning_msg = f"Transaction number '{p['txn_num']}' already exists with matching amount/date."
                else:
                    validation_status = 'POSSIBLE_AMENDMENT'
                    warning_msg = f"Transaction number '{p['txn_num']}' exists with different amount/date (potential amendment)."
        else:
            # Check composites from pre-fetched cache (no per-row DB query)
            composite_key = (parsed_amount, parsed_date)
            cached_composites = no_txn_composites.get(composite_key, [])
            for c_name, c_zip, c in cached_composites:
                if c_name == p['norm_name'] and c_zip == p['zip_val']:
                    validation_status = 'POSSIBLE_DUPLICATE'
                    warning_msg = "Possible duplicate based on identical Name, ZIP, Date, and Amount without a transaction number."
                    break
            
            # Check in-batch composites
            if validation_status == 'ACCEPTED':
                for c in contributions_to_create:
                    if c.amount == parsed_amount and c.transaction_date == parsed_date:
                        c_p = parsed_contrib_data[c.raw_contribution.row_number]
                        if c_p['norm_name'] == p['norm_name'] and c_p['zip_val'] == p['zip_val']:
                            validation_status = 'POSSIBLE_DUPLICATE'
                            warning_msg = "Possible duplicate based on identical Name, ZIP, Date, and Amount without a transaction number."
                            break
                            
        raw_contrib.validation_status = validation_status
        if warning_msg:
            raw_contrib.validation_errors = warning_msg
            
        txn_type = 'CONTRIBUTION'
        if parsed_amount < 0:
            name_upper = p['name_val'].upper()
            if p['txn_num'] and (p['txn_num'].upper().startswith('REF') or p['txn_num'] in ing_cache.existing_txns):
                txn_type = 'REFUND'
            elif p['txn_num'] and p['txn_num'].upper().startswith('REV'):
                txn_type = 'REVERSAL'
            elif "REFUND" in name_upper:
                txn_type = 'REFUND'
            elif "REVERSAL" in name_upper:
                txn_type = 'REVERSAL'
            else:
                txn_type = 'ADJUSTMENT'

        parsed_filed_date = parse_date(p['filed_date_val'])
        
        # Build raw address string
        raw_address = ", ".join([v for v in [p['street_val'], p['city_val'], p['state_val'], p['zip_formatted']] if v])
        
        contribution = Contribution(
            raw_contribution=raw_contrib,
            transaction_number=p['txn_num'],
            amount=parsed_amount,
            transaction_type=txn_type,
            status='ACTIVE',
            transaction_date=parsed_date,
            filed_date=parsed_filed_date,
            raw_address=raw_address,
            employer=p['emp_val'],
            occupation=p['occ_val']
        )
        contribution._raw_contrib_ref = raw_contrib
        
        contributions_to_create.append(contribution)
        parsed_contrib_data[row_number] = p
        successful_count += 1
        
    RawContribution.objects.bulk_update(raw_contrib_map.values(), ['validation_status', 'validation_errors'], batch_size=500)
    created_contributions = Contribution.objects.bulk_create(contributions_to_create, batch_size=500)
    
    # 5. Identity clustering and locations
    affected_clusters_list = []
    for c in created_contributions:
        raw_contrib = getattr(c, '_raw_contrib_ref', None) or c.raw_contribution
        p = parsed_contrib_data[raw_contrib.row_number]
        is_org = (p['entity_type'] == 'ORGANIZATION')
        is_joint = (p['entity_type'] == 'JOINT')
        display_name = p['norm_name'] if p['norm_name'] else p['name_val'].upper()
        
        if is_org or is_joint:
            ent_type = 'ORGANIZATION' if is_org else 'JOINT'
            entity = ContributorEntity(
                entity_type=ent_type,
                display_name=display_name,
                is_verified=False
            )
            entities_to_create.append(entity)
            if is_org:
                org = Organization(
                    contributor_entity=entity,
                    legal_name=display_name,
                    committee_id=p['row_data'].get(rules.get('ID NUMBER', ''), '')
                )
                orgs_to_create.append(org)
            cluster = ContributionCluster(
                contributor_entity=entity,
                normalized_name=p['norm_name'],
                zip_code=p['zip_formatted'],
                confidence_level='LOW',
                confidence_explanation="Non-individual entities are isolated to single clusters by default."
            )
            # Safe temporary key registration
            cluster_key = ing_cache.get_cluster_key(cluster)
            ing_cache.assignments_by_cluster[cluster_key] = []
            clusters_to_create.append(cluster)
        else:
            # Check candidate database clusters using explicit cache
            key = (p['norm_name'], p['zip_formatted'])
            candidates = ing_cache.clusters_by_name_zip.get(key, [])
            
            matched_cluster = None
            for cand in candidates:
                if not has_conflict(
                    cand,
                    normalize_name(p['name_val'])['first_name'],
                    normalize_name(p['name_val'])['middle_name'],
                    normalize_name(p['name_val'])['last_name'],
                    normalize_name(p['name_val'])['suffix'],
                    c.employer, c.occupation,
                    assignment_cache=ing_cache
                ):
                    corroborated = check_corroboration(
                        cand,
                        normalize_name(p['name_val'])['first_name'],
                        normalize_name(p['name_val'])['middle_name'],
                        normalize_name(p['name_val'])['last_name'],
                        normalize_name(p['name_val'])['suffix'],
                        c.employer, c.occupation,
                        assignment_cache=ing_cache
                    )
                    if corroborated:
                        matched_cluster = cand
                        break
                        
            if matched_cluster:
                if matched_cluster.confidence_level == 'LOW':
                    matched_cluster.confidence_level = 'MEDIUM'
                    matched_cluster.confidence_explanation = "Grouped based on matching Name, ZIP, and corroborated employer/occupation/middle name."
                    clusters_to_update.append(matched_cluster)
                elif matched_cluster.confidence_level == 'MEDIUM':
                    matched_cluster.confidence_level = 'HIGH'
                    matched_cluster.confidence_explanation = "Highly corroborated donor profile with multiple matching transactions."
                    clusters_to_update.append(matched_cluster)
                cluster = matched_cluster
            else:
                entity = ContributorEntity(
                    entity_type='INDIVIDUAL',
                    display_name=display_name,
                    is_verified=False
                )
                entities_to_create.append(entity)
                
                parsed_name = normalize_name(p['name_val'])
                person = Person(
                    contributor_entity=entity,
                    first_name=parsed_name['first_name'],
                    middle_name=parsed_name['middle_name'],
                    last_name=parsed_name['last_name'],
                    suffix=parsed_name['suffix']
                )
                persons_to_create.append(person)
                
                cluster = ContributionCluster(
                    contributor_entity=entity,
                    normalized_name=p['norm_name'],
                    zip_code=p['zip_formatted'],
                    confidence_level='LOW',
                    confidence_explanation="Initial low-confidence cluster based on single contribution."
                )
                # Safe temporary key registration
                cluster_key = ing_cache.get_cluster_key(cluster)
                ing_cache.assignments_by_cluster[cluster_key] = []
                clusters_to_create.append(cluster)
                ing_cache.clusters_by_name_zip.setdefault(key, []).append(cluster)
                
        assignment = ContributionClusterAssignment(
            contribution=c,
            contribution_cluster=cluster,
            assigned_by=actor,
            is_active=True
        )
        assignment._cluster_ref = cluster
        assignments_to_create.append(assignment)
        
        # Cache assignment explicitly in-memory using stable cluster key
        cluster_key = ing_cache.get_cluster_key(cluster)
        ing_cache.assignments_by_cluster[cluster_key].append(assignment)
        
        location = Location(
            contributor_profile=cluster,
            street_address=p['street_val'] if p['street_val'] else None,
            city=p['city_val'],
            state=p['state_val'],
            zip=p['zip_formatted'],
            precision_level='STREET' if p['street_val'] else 'CITY_ZIP',
            confidence='HIGH' if p['street_val'] else 'MEDIUM',
            effective_date=c.transaction_date,
            status='CURRENT',
            is_observed=True
        )
        location._cluster_ref = cluster
        locations_to_create.append(location)
        affected_clusters_list.append(cluster)

    # 5.1 Perform bulk creations
    if entities_to_create:
        ContributorEntity.objects.bulk_create(entities_to_create, batch_size=500)
        
    if orgs_to_create:
        for org in orgs_to_create:
            org.contributor_entity_id = org.contributor_entity.id
        Organization.objects.bulk_create(orgs_to_create, batch_size=500)
        
    if persons_to_create:
        for person in persons_to_create:
            person.contributor_entity_id = person.contributor_entity.id
        Person.objects.bulk_create(persons_to_create, batch_size=500)
        
    if clusters_to_create:
        for cluster in clusters_to_create:
            cluster.contributor_entity_id = cluster.contributor_entity.id
        ContributionCluster.objects.bulk_create(clusters_to_create, batch_size=500)

    # Map temporary reference keys to confirmed database primary keys
    for cluster in clusters_to_create:
        temp_id = getattr(cluster, '_temp_id', None)
        if temp_id and temp_id in ing_cache.assignments_by_cluster:
            ing_cache.assignments_by_cluster[cluster.id] = ing_cache.assignments_by_cluster[temp_id]
            del ing_cache.assignments_by_cluster[temp_id]

    # Set foreign keys for assignments and locations without triggering descriptor DB queries
    for assign in assignments_to_create:
        assign.contribution_cluster_id = assign._cluster_ref.id
        
    for loc in locations_to_create:
        loc.contributor_profile_id = loc._cluster_ref.id

    affected_cluster_ids = {cluster.id for cluster in affected_clusters_list if getattr(cluster, 'id', None)}
    affected_entity_ids = {cluster.contributor_entity_id for cluster in affected_clusters_list if getattr(cluster, 'contributor_entity_id', None)}

    ContributionClusterAssignment.objects.bulk_create(assignments_to_create, batch_size=500)
    Location.objects.bulk_create(locations_to_create, batch_size=500)
    
    if clusters_to_update:
        ContributionCluster.objects.bulk_update(clusters_to_update, ['confidence_level', 'confidence_explanation'], batch_size=500)
        
    # Mark batch complete
    batch.row_count = len(parsed_rows)
    batch.successful_rows = successful_count
    batch.failed_rows = failed_count
    batch.duplicate_rows = duplicate_count
    batch.status = 'COMPLETED'
    batch.save()
    
    # Save bulk SourceRecordLinks
    # Stage 1.1 Ingestion Hardening requirement: create links
    from roster.models import SourceRecordLink
    links_to_create = []
    for c in created_contributions:
        links_to_create.append(SourceRecordLink(
            source_model_name="RawContribution",
            source_record_id=c.raw_contribution_id,
            target_model_name="Contribution",
            target_record_id=c.id
        ))
    SourceRecordLink.objects.bulk_create(links_to_create, batch_size=500)
    
    AuditEvent.objects.create(
        event_type="IMPORT_BATCH",
        description=f"Imported batch '{file_name}'. Rows: {batch.row_count}, Success: {successful_count}, Failed: {failed_count}, Duplicates: {duplicate_count}.",
        actor=actor
    )
    
    # Recalculate assessments in bulk once per entity/cluster
    from roster.services.membership import evaluate_cluster_recurrence_bulk, evaluate_membership_for_entities, clear_membership_caches
    clear_membership_caches()
    evaluate_cluster_recurrence_bulk(list(affected_cluster_ids))
    evaluate_membership_for_entities(list(affected_entity_ids))
    clear_membership_caches()
    
    # Explicitly discard/clear cache references
    del ing_cache
    
    return batch


def recalculate_batch_entities(batch):
    from roster.services.membership import evaluate_cluster_recurrence_bulk, evaluate_membership_for_entities, clear_membership_caches
    clear_membership_caches()
    assignments = ContributionClusterAssignment.objects.filter(
        contribution__raw_contribution__import_batch=batch
    ).select_related('contribution_cluster')
    
    cluster_ids = list(assignments.values_list('contribution_cluster_id', flat=True).distinct())
    entity_ids = list(assignments.values_list('contribution_cluster__contributor_entity_id', flat=True).distinct())
    
    evaluate_cluster_recurrence_bulk(cluster_ids)
    evaluate_membership_for_entities(entity_ids)
    clear_membership_caches()

@transaction.atomic
def rollback_batch(batch_id, actor="SYSTEM"):
    """
    Rolls back an import batch non-destructively.
    Marks the batch as ROLLED_BACK, deactivates assignments, and updates metrics.
    """
    batch = ImportBatch.objects.get(id=batch_id)
    if batch.status == 'ROLLED_BACK':
        return
        
    batch.status = 'ROLLED_BACK'
    batch.save()
    
    # Deactivate assignments created by this batch
    ContributionClusterAssignment.objects.filter(
        contribution__raw_contribution__import_batch=batch
    ).update(is_active=False)
        
    # Log event
    AuditEvent.objects.create(
        event_type="ROLLBACK_BATCH",
        description=f"Rolled back import batch {batch_id} ('{batch.file_name}').",
        actor=actor
    )
    
    # Recalculate membership assessment on affected entities
    recalculate_batch_entities(batch)

@transaction.atomic
def restore_batch(batch_id, actor="SYSTEM"):
    """
    Restores a previously rolled back batch.
    Re-enables status to COMPLETED, reactivates assignments, and triggers assessments.
    """
    batch = ImportBatch.objects.get(id=batch_id)
    if batch.status != 'ROLLED_BACK':
        return
        
    batch.status = 'COMPLETED'
    batch.save()
    
    # Reactivate assignments created by this batch
    ContributionClusterAssignment.objects.filter(
        contribution__raw_contribution__import_batch=batch
    ).update(is_active=True)
        
    # Log event
    AuditEvent.objects.create(
        event_type="RESTORE_BATCH",
        description=f"Restored import batch {batch_id} ('{batch.file_name}').",
        actor=actor
    )
    
    # Recalculate membership assessment on affected entities
    recalculate_batch_entities(batch)

@transaction.atomic
def purge_batch(batch_id, actor="SYSTEM"):
    """
    Core service helper to execute a transaction-atomic purge of batch data.
    Only deletes clusters or entities when they are completely orphaned.
    """
    batch = ImportBatch.objects.get(id=batch_id)
    
    # Collect affected clusters/entities before deleting contribution records
    affected_clusters = list(ContributionCluster.objects.filter(
        assignments__contribution__raw_contribution__import_batch=batch
    ).distinct())
    
    affected_entities = list(ContributorEntity.objects.filter(
        clusters__in=affected_clusters
    ).distinct())
    
    # Delete batch data (normalized contributions, raw contributions, and active assignments)
    Contribution.objects.filter(raw_contribution__import_batch=batch).delete()
    RawContribution.objects.filter(import_batch=batch).delete()
    
    deleted_clusters = 0
    deleted_entities = 0
    
    # 1. Clean up orphaned clusters
    for cluster in affected_clusters:
        # Check surviving assignments (active or inactive lineage), merge decisions, and match decisions
        has_assignments = cluster.assignments.exists()
        
        # MergeDecision model check
        from roster.models import MergeDecision, MatchDecision
        has_merge_decisions = MergeDecision.objects.filter(Q(source_cluster=cluster) | Q(target_cluster=cluster)).exists()
        has_match_decisions = MatchDecision.objects.filter(contribution_cluster=cluster).exists()
        
        # If completely orphaned, delete the cluster
        if not (has_assignments or has_merge_decisions or has_match_decisions):
            cluster.delete()
            deleted_clusters += 1
            
    # 2. Clean up orphaned entities
    for entity in affected_entities:
        # An entity is orphaned if it has no clusters remaining, and no manual overrides/verification
        has_clusters = entity.clusters.exists()
        has_manual_overrides = MembershipAssessment.objects.filter(contributor_entity=entity, manual_override=True).exists()
        
        if not (has_clusters or has_manual_overrides or entity.is_verified):
            # Clean Person and Organization profile extensions
            Person.objects.filter(contributor_entity=entity).delete()
            Organization.objects.filter(contributor_entity=entity).delete()
            entity.delete()
            deleted_entities += 1
            
    # Record metadata before deletion
    file_name = batch.file_name
    file_hash = batch.file_hash
    batch_row_count = batch.row_count
    
    batch.delete()
    
    return {
        'file_name': file_name,
        'file_hash': file_hash,
        'raw_count': batch_row_count,
        'deleted_clusters': deleted_clusters,
        'deleted_entities': deleted_entities
    }
