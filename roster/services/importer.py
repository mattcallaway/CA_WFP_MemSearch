import csv
import hashlib
import decimal
import re
from datetime import datetime
from django.db import transaction
from django.utils.dateparse import parse_date as django_parse_date
from django.contrib.auth.models import User
from roster.models import (
    ImportBatch, ImportMappingProfile, RawContribution, Contribution, 
    ContributionClusterAssignment, ContributionCluster, Location, 
    AuditEvent, FieldAssertion
)
from roster.services.resolver import resolve_and_cluster_contribution

def compute_file_hash(file_path):
    sha256 = hashlib.sha256()
    with open(file_path, 'rb') as f:
        while chunk := f.read(8192):
            sha256.update(chunk)
    return sha256.hexdigest()

def compute_row_hash(row_dict):
    # Standardize row dict values to compute a unique string representation
    sorted_items = sorted((str(k).strip(), str(v).strip()) for k, v in row_dict.items())
    row_str = "||".join(f"{k}:{v}" for k, v in sorted_items)
    return hashlib.sha256(row_str.encode('utf-8')).hexdigest()

def parse_date(date_str):
    if not date_str or not str(date_str).strip():
        return None
    cleaned = str(date_str).strip()
    
    # Try Django's standard ISO format parser
    try:
        dt = django_parse_date(cleaned)
        if dt:
            return dt
    except ValueError:
        pass
        
    # Try other common formats
    formats = [
        '%Y-%m-%d', '%m/%d/%Y', '%m/%d/%y', '%Y/%m/%d',
        '%d-%m-%Y', '%Y-%b-%d', '%d %b %Y'
    ]
    for fmt in formats:
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    return None

def parse_decimal(amount_str):
    if not amount_str or not str(amount_str).strip():
        return None
    cleaned = str(amount_str).strip().replace('$', '').replace(',', '')
    try:
        return decimal.Decimal(cleaned)
    except (decimal.InvalidOperation, ValueError):
        return None

import csv
import hashlib
import decimal
import re
import os
from datetime import datetime
from django.db import transaction
from django.utils.dateparse import parse_date as django_parse_date
from django.contrib.auth.models import User
from roster.models import (
    ImportBatch, ImportMappingProfile, RawContribution, Contribution, 
    ContributionClusterAssignment, ContributionCluster, Location, 
    AuditEvent, FieldAssertion, ImportAttempt, ContributorEntity, Person, Organization
)

def compute_file_hash(file_path):
    sha256 = hashlib.sha256()
    with open(file_path, 'rb') as f:
        while chunk := f.read(8192):
            sha256.update(chunk)
    return sha256.hexdigest()

def compute_row_hash(row_dict):
    # Standardize row dict values to compute a unique string representation
    sorted_items = sorted((str(k).strip(), str(v).strip()) for k, v in row_dict.items())
    row_str = "||".join(f"{k}:{v}" for k, v in sorted_items)
    return hashlib.sha256(row_str.encode('utf-8')).hexdigest()

def parse_date(date_str):
    if not date_str or not str(date_str).strip():
        return None
    cleaned = str(date_str).strip()
    
    # Try Django's standard ISO format parser
    try:
        dt = django_parse_date(cleaned)
        if dt:
            return dt
    except ValueError:
        pass
        
    # Try other common formats
    formats = [
        '%Y-%m-%d', '%m/%d/%Y', '%m/%d/%y', '%Y/%m/%d',
        '%d-%m-%Y', '%Y-%b-%d', '%d %b %Y'
    ]
    for fmt in formats:
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    return None

def parse_decimal(amount_str):
    if not amount_str or not str(amount_str).strip():
        return None
    cleaned = str(amount_str).strip().replace('$', '').replace(',', '')
    try:
        return decimal.Decimal(cleaned)
    except (decimal.InvalidOperation, ValueError):
        return None

@transaction.atomic
def import_csv_file(file_path, file_name, mapping_profile_id, actor="SYSTEM", override_duplicate=False):
    """
    Imports a CSV file containing contribution records.
    Applies column mappings, row validation, duplicate checks, and generates derived views.
    """
    # 1. Limit check (max 10MB size)
    file_size = os.path.getsize(file_path)
    if file_size > 10 * 1024 * 1024:
        raise ValueError("CSV file exceeds maximum size limit of 10MB.")

    mapping_profile = ImportMappingProfile.objects.get(id=mapping_profile_id)
    rules = mapping_profile.mapping_rules
    
    # Read and parse file
    rows = []
    with open(file_path, mode='r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(dict(row))
            
    # Max rows guard
    if len(rows) > 10000:
        raise ValueError("CSV exceeds maximum limit of 10,000 rows.")

    # Compute and verify file hash
    file_hash = compute_file_hash(file_path)
    
    # Preferred duplicate-file override design: reprocess the existing batch canonical record
    existing_batch = ImportBatch.objects.filter(file_hash=file_hash).first()
    if existing_batch:
        if not override_duplicate:
            raise ValueError("This file has already been imported successfully.")
        else:
            # 1. Roll back the existing completed batch calculations non-destructively
            rollback_batch(existing_batch.id, actor=actor)
            # 2. Deletes normalized contributions (cascades to assignments/locations)
            Contribution.objects.filter(raw_contribution__import_batch=existing_batch).delete()
            # 3. Reset existing raw contributions
            RawContribution.objects.filter(import_batch=existing_batch).update(
                validation_status='UNPROCESSED',
                validation_errors=None
            )
            batch = existing_batch
            batch.file_name = file_name
            batch.mapping_profile = mapping_profile
            batch.imported_by = actor
            batch.status = 'VALIDATING'
            batch.save()
            
            ImportAttempt.objects.create(
                import_batch=batch,
                attempted_by=actor,
                action='REPROCESS_OVERRIDE',
                notes=f"Overrode duplicate file. Reprocessing batch."
            )
    else:
        # Create a new batch
        batch = ImportBatch.objects.create(
            file_name=file_name,
            file_hash=file_hash,
            file_type="CSV",
            imported_by=actor,
            mapping_profile=mapping_profile,
            status='VALIDATING'
        )
        ImportAttempt.objects.create(
            import_batch=batch,
            attempted_by=actor,
            action='INITIAL',
            notes="Initial batch import."
        )
        
    batch.row_count = len(rows)
    batch.save()
    
    # Pre-parse mappings and calculate row hashes
    parsed_rows = []
    raw_row_hashes = []
    
    name_col = rules.get('NAME OF CONTRIBUTOR')
    amount_col = rules.get('AMOUNT')
    date_col = rules.get('TRANSACTION DATE')
    zip_col = rules.get('ZIP')
    txn_col = rules.get('TRANSACTION NUMBER')
    city_col = rules.get('CITY')
    state_col = rules.get('STATE')
    emp_col = rules.get('EMPLOYER')
    occ_col = rules.get('OCCUPATION')
    filed_date_col = rules.get('FILED DATE')
    street_col = rules.get('STREET ADDRESS')
    
    for index, row_data in enumerate(rows, start=1):
        row_hash = compute_row_hash(row_data)
        raw_row_hashes.append(row_hash)
        
        name_val = (row_data.get(name_col) or '').strip() if name_col else ''
        amount_val = (row_data.get(amount_col) or '').strip() if amount_col else ''
        date_val = (row_data.get(date_col) or '').strip() if date_col else ''
        zip_val = (row_data.get(zip_col) or '').strip() if zip_col else ''
        txn_num = (row_data.get(txn_col) or '').strip() if txn_col else ''
        city_val = (row_data.get(city_col) or '').strip() if city_col else ''
        state_val = (row_data.get(state_col) or '').strip() if state_col else ''
        emp_val = (row_data.get(emp_col) or '').strip() if emp_col else ''
        occ_val = (row_data.get(occ_col) or '').strip() if occ_col else ''
        filed_date_val = (row_data.get(filed_date_col) or '').strip() if filed_date_col else ''
        street_val = (row_data.get(street_col) or '').strip() if street_col else ''
        
        parsed_rows.append({
            'index': index,
            'row_data': row_data,
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
            'street_val': street_val,
        })
        
    # Chunked lookup for existing raw row hashes in database to prevent exceeding limits
    existing_completed_hashes = set()
    chunk_size = 500
    for i in range(0, len(raw_row_hashes), chunk_size):
        chunk = raw_row_hashes[i:i+chunk_size]
        existing_completed_hashes.update(
            RawContribution.objects.filter(
                raw_row_hash__in=chunk,
                import_batch__status='COMPLETED'
            ).values_list('raw_row_hash', flat=True)
        )
        
    # Prepare RawContribution row items
    raw_contrib_instances = []
    reused_raw_contribs = {}
    if existing_batch:
        reused_raw_contribs = {rc.row_number: rc for rc in RawContribution.objects.filter(import_batch=batch)}
        
    for p in parsed_rows:
        row_number = p['index']
        if row_number in reused_raw_contribs:
            rc = reused_raw_contribs[row_number]
            rc.validation_status = 'UNPROCESSED'
            rc.validation_errors = None
            raw_contrib_instances.append(rc)
        else:
            rc = RawContribution(
                import_batch=batch,
                row_number=row_number,
                original_values=p['row_data'],
                raw_row_hash=p['row_hash'],
                validation_status='UNPROCESSED'
            )
            raw_contrib_instances.append(rc)
            
    if reused_raw_contribs:
        RawContribution.objects.bulk_update(raw_contrib_instances, ['validation_status', 'validation_errors'])
        raw_contrib_map = {rc.row_number: rc for rc in raw_contrib_instances}
    else:
        created_raw_contribs = RawContribution.objects.bulk_create(raw_contrib_instances)
        raw_contrib_map = {rc.row_number: rc for rc in created_raw_contribs}

    # Preload existing transactions matching incoming transaction numbers
    incoming_txns = [p['txn_num'] for p in parsed_rows if p['txn_num']]
    existing_txns = {}
    if incoming_txns:
        for i in range(0, len(incoming_txns), chunk_size):
            chunk = incoming_txns[i:i+chunk_size]
            qs = Contribution.objects.filter(
                transaction_number__in=chunk,
                raw_contribution__import_batch__status='COMPLETED'
            ).select_related('raw_contribution')
            for c in qs:
                existing_txns.setdefault(c.transaction_number, []).append(c)

    # Collect candidate cluster search keys
    from roster.services.resolver import normalize_name, detect_entity_type, check_corroboration, has_conflict
    
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
            
    # Batch lookup candidate clusters
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
            
    # Cache candidate clusters by (normalized_name, zip_code)
    cluster_cache = {}
    for cluster in candidate_clusters:
        key = (cluster.normalized_name, cluster.zip_code)
        cluster_cache.setdefault(key, []).append(cluster)

    successful_count = 0
    failed_count = 0
    duplicate_count = 0
    
    seen_in_batch = set()
    
    # Store elements to bulk create
    contributions_to_create = []
    locations_to_create = []
    assignments_to_create = []
    
    parsed_contrib_data = {}
    affected_cluster_ids = set()
    affected_entity_ids = set()
    
    for p in parsed_rows:
        row_number = p['index']
        raw_contrib = raw_contrib_map[row_number]
        
        # Check exact row duplication
        if p['row_hash'] in existing_completed_hashes or p['row_hash'] in seen_in_batch:
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
            dups = existing_txns.get(p['txn_num'], [])
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
            # Check composites in DB
            db_composites = Contribution.objects.filter(
                amount=parsed_amount,
                transaction_date=parsed_date,
                raw_contribution__import_batch__status='COMPLETED'
            )
            for c in db_composites:
                c_name = normalize_name(c.raw_contribution.original_values.get(name_col, ''))['normalized_full_name']
                c_zip = c.raw_contribution.original_values.get(zip_col, '').strip()
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
            if p['txn_num'] and (p['txn_num'].upper().startswith('REF') or p['txn_num'] in existing_txns):
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
        
        contributions_to_create.append(contribution)
        parsed_contrib_data[row_number] = p
        successful_count += 1
        
    RawContribution.objects.bulk_update(raw_contrib_instances, ['validation_status', 'validation_errors'])
    created_contributions = Contribution.objects.bulk_create(contributions_to_create)
    
    # 4. Identity clustering and locations
    entities_to_create = []
    persons_to_create = []
    orgs_to_create = []
    clusters_to_create = []
    clusters_to_update = []
    
    for c in created_contributions:
        p = parsed_contrib_data[c.raw_contribution.row_number]
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
            cluster._prefetched_objects_cache = {'assignments': []}
            clusters_to_create.append(cluster)
        else:
            # Individual candidates check in-memory cache
            key = (p['norm_name'], p['zip_formatted'])
            candidates = cluster_cache.get(key, [])
            
            matched_cluster = None
            for cand in candidates:
                if not has_conflict(
                    cand,
                    normalize_name(p['name_val'])['first_name'],
                    normalize_name(p['name_val'])['middle_name'],
                    normalize_name(p['name_val'])['last_name'],
                    normalize_name(p['name_val'])['suffix'],
                    c.employer, c.occupation
                ):
                    corroborated = check_corroboration(
                        cand,
                        normalize_name(p['name_val'])['first_name'],
                        normalize_name(p['name_val'])['middle_name'],
                        normalize_name(p['name_val'])['last_name'],
                        normalize_name(p['name_val'])['suffix'],
                        c.employer, c.occupation
                    )
                    if corroborated:
                        matched_cluster = cand
                        break
                        
            if matched_cluster:
                if matched_cluster.confidence_level == 'LOW':
                    matched_cluster.confidence_level = 'MEDIUM'
                    matched_cluster.confidence_explanation = "Grouped based on matching Name, ZIP, and corroborated employer/occupation/middle name."
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
                cluster._prefetched_objects_cache = {'assignments': []}
                clusters_to_create.append(cluster)
                cluster_cache.setdefault(key, []).append(cluster)
                
        assignment = ContributionClusterAssignment(
            contribution=c,
            contribution_cluster=cluster,
            assigned_by=actor,
            is_active=True
        )
        assignments_to_create.append(assignment)
        
        # Append to the prefetch cache in memory
        if hasattr(cluster, '_prefetched_objects_cache') and 'assignments' in cluster._prefetched_objects_cache:
            cluster._prefetched_objects_cache['assignments'].append(assignment)
        
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
        locations_to_create.append(location)

    # 4.1 Perform bulk creations
    if entities_to_create:
        ContributorEntity.objects.bulk_create(entities_to_create)
        
    if orgs_to_create:
        for org in orgs_to_create:
            org.contributor_entity_id = org.contributor_entity.id
        Organization.objects.bulk_create(orgs_to_create)
        
    if persons_to_create:
        for person in persons_to_create:
            person.contributor_entity_id = person.contributor_entity.id
        Person.objects.bulk_create(persons_to_create)
        
    if clusters_to_create:
        for cluster in clusters_to_create:
            cluster.contributor_entity_id = cluster.contributor_entity.id
        ContributionCluster.objects.bulk_create(clusters_to_create)

    # Set foreign keys for assignments and locations, and collect affected IDs before bulk_create
    for assign in assignments_to_create:
        cluster = assign.contribution_cluster
        affected_cluster_ids.add(cluster.id)
        affected_entity_ids.add(cluster.contributor_entity_id)
        assign.contribution_cluster_id = cluster.id
        
    for loc in locations_to_create:
        loc.contributor_profile_id = loc.contributor_profile.id

    ContributionClusterAssignment.objects.bulk_create(assignments_to_create)
    Location.objects.bulk_create(locations_to_create)
    
    if clusters_to_update:
        ContributionCluster.objects.bulk_update(clusters_to_update, ['confidence_level', 'confidence_explanation'])
    
    # Mark batch complete
    batch.successful_rows = successful_count
    batch.failed_rows = failed_count
    batch.duplicate_rows = duplicate_count
    batch.status = 'COMPLETED'
    batch.save()
    
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
    Marks the batch as ROLLED_BACK, which automatically excludes its contributions
    from aggregates (since queries filter out rolled back batch contributions).
    Recalculates affected profile and entity metrics.
    """
    batch = ImportBatch.objects.get(id=batch_id)
    if batch.status == 'ROLLED_BACK':
        return
        
    batch.status = 'ROLLED_BACK'
    batch.save()
    
    # Deactivate assignments created by this batch
    assignments = ContributionClusterAssignment.objects.filter(
        contribution__raw_contribution__import_batch=batch
    )
    for assign in assignments:
        assign.is_active = False
        assign.save()
        
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
    
    # Reactivate assignments created by this batch (unless they were overridden/deleted manually)
    assignments = ContributionClusterAssignment.objects.filter(
        contribution__raw_contribution__import_batch=batch
    )
    for assign in assignments:
        assign.is_active = True
        assign.save()
        
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
    Exceptional administrative action to permanently delete all data from a batch.
    This is only runnable from CLI/tests.
    """
    batch = ImportBatch.objects.get(id=batch_id)
    
    # We delete normalized contributions, which cascades to assignments, locations, and raw contributions
    Contribution.objects.filter(raw_contribution__import_batch=batch).delete()
    RawContribution.objects.filter(import_batch=batch).delete()
    
    # Clean up empty clusters/entities
    empty_clusters = ContributionCluster.objects.filter(assignments__isnull=True)
    for cluster in empty_clusters:
        entity = cluster.contributor_entity
        cluster.delete()
        if entity.clusters.count() == 0:
            entity.delete()
            
    batch.delete()
    
    AuditEvent.objects.create(
        event_type="PURGE_BATCH",
        description=f"Permanently purged batch {batch_id}.",
        actor=actor
    )
