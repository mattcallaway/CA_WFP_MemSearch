import csv
import hashlib
import itertools
from django.db import transaction, connection
from django.utils import timezone
from roster.models import (
    GeographyDataset, GeographyImportBatch, RawGeographyRecord,
    GeographyMappingProfile, County, GeographicPlace, PostalArea,
    CountySourceRecord, PlaceSourceRecord, PostalAreaSourceRecord,
    GeographyIdentifier, PlaceCountyAssociation, PostalCountyAssociation,
    PostalPlaceAssociation, GeographyAlias, AuditEvent, GeographyResolutionRun
)

class QueryProfiler:
    """
    Optional query counter hook using connection.execute_wrapper.
    Counts SQL queries per import phase without storing SQL text.
    """
    def __init__(self):
        self.counts = {}
        self.current_phase = None

    def start_phase(self, phase_name):
        self.current_phase = phase_name
        if phase_name not in self.counts:
            self.counts[phase_name] = 0

    def __call__(self, execute, sql, params, many, context):
        if self.current_phase:
            self.counts[self.current_phase] += 1
        return execute(sql, params, many, context)

def compute_file_hash(file_path):
    hasher = hashlib.sha256()
    with open(file_path, 'rb') as f:
        buf = f.read(65536)
        while len(buf) > 0:
            hasher.update(buf)
            buf = f.read(65536)
    return hasher.hexdigest()

def normalize_zip_code(zip_raw, mapping_profile=None):
    """
    Conservative ZIP normalization:
    - 95401 -> 95401
    - 95401-1234 -> ZIP5 95401, ZIP4 1234
    - 02138 -> 02138
    - 2138 -> no automatic padding unless authorized in mapping snapshot
    """
    if not zip_raw:
        return None, None, "Missing ZIP value"
        
    zip_str = str(zip_raw).strip()
    
    if '-' in zip_str:
        parts = zip_str.split('-')
        zip5 = parts[0].zfill(5)
        zip4 = parts[1]
        if len(zip5) == 5 and zip5.isdigit():
            return zip5, zip4, None
        else:
            return None, None, f"Malformed ZIP5 in ZIP+4: '{zip_str}'"

    if len(zip_str) == 4 and zip_str.isdigit():
        allow_padding = False
        if mapping_profile:
            norm_rules = getattr(mapping_profile, 'normalization_rules', {}) or {}
            map_rules = getattr(mapping_profile, 'mapping_rules', {}) or {}
            allow_padding = (
                norm_rules.get('allow_zip_padding') in (True, 'true') or
                map_rules.get('allow_zip_padding') in (True, 'true')
            )
        if allow_padding:
            return zip_str.zfill(5), None, f"ZIP '{zip_str}' padded to 5 digits"
        else:
            return None, None, f"ZIP '{zip_str}' has 4 digits (unauthorized padding)"

    if len(zip_str) < 5 and zip_str.isdigit():
        return None, None, f"ZIP '{zip_str}' is too short and invalid"
        
    if len(zip_str) == 9 and zip_str.isdigit():
        return zip_str[:5], zip_str[5:], None
        
    if len(zip_str) == 5 and zip_str.isdigit():
        return zip_str, None, None
        
    return zip_str, None, f"Unrecognized postal format: '{zip_str}'"

def normalize_weight_value(raw_val, basis, unit):
    """
    Normalizes raw weight values to [0.0, 1.0] fraction when applicable.
    """
    if raw_val is None:
        return None, "", None
    raw_str = str(raw_val).strip()
    if not raw_str:
        return None, "", None
        
    unit_upper = str(unit).strip().upper() if unit else 'UNKNOWN'
    
    clean_str = raw_str.replace('%', '').strip()
    try:
        val = float(clean_str)
    except ValueError:
        return None, "", f"Malformed weight value: {raw_val}"
        
    if '%' in raw_str or unit_upper == 'PERCENTAGE':
        return val / 100.0, "PERCENTAGE_TO_FRACTION", None
    elif unit_upper == 'FRACTION':
        return val, "DIRECT_FRACTION", None
    elif unit_upper == 'COUNT':
        return None, "COUNT_UNNORMALIZED", None
    else:
        if 0.0 <= val <= 1.0:
            return val, "ASSUMED_FRACTION", None
        elif 1.0 < val <= 100.0:
            return val / 100.0, "ASSUMED_PERCENTAGE", None
        else:
            return None, "OUT_OF_RANGE_UNNORMALIZED", None

def get_chunks(iterable, chunk_size):
    iterator = iter(iterable)
    while True:
        chunk = list(itertools.islice(iterator, chunk_size))
        if not chunk:
            break
        yield chunk

def import_geography_file(file_path, file_name, dataset_id, actor, override_duplicate=False, profiler=None):
    """
    Synchronously parses and ingests a geographic reference CSV using bounded chunks
    and bulk DB operations.
    """
    if profiler is not None:
        ctx = connection.execute_wrapper(profiler)
    else:
        class DummyCtx:
            def __enter__(self): pass
            def __exit__(self, exc_type, exc_val, exc_tb): pass
        ctx = DummyCtx()

    with ctx:
        # 1. Dataset and batch setup
        if profiler: profiler.start_phase("dataset_batch_setup")
        file_hash = compute_file_hash(file_path)
        existing_batch = GeographyImportBatch.objects.filter(file_hash=file_hash, status='COMPLETED').first()
        if existing_batch and not override_duplicate:
            raise ValueError(f"File with hash {file_hash} was already imported in batch {existing_batch.id}")

        try:
            dataset = GeographyDataset.objects.get(id=dataset_id)
        except GeographyDataset.DoesNotExist:
            raise ValueError(f"Dataset with ID {dataset_id} does not exist.")

        # 2. Mapping-profile retrieval
        if profiler: profiler.start_phase("mapping_profile_retrieval")
        profile = GeographyMappingProfile.objects.filter(
            source_type=dataset.dataset_type, is_active=True
        ).order_by('-created_at').first()
        
        if not profile:
            raise ValueError(f"No active GeographyMappingProfile found for type '{dataset.dataset_type}'")

        rules = profile.mapping_rules
        mapping_profile_version = f"{profile.name} (v{profile.version})"

        batch = GeographyImportBatch.objects.create(
            dataset=dataset,
            file_name=file_name,
            file_hash=file_hash,
            import_type=dataset.dataset_type,
            mapping_profile_version=mapping_profile_version,
            status='VALIDATING',
            actor=actor
        )

        # In-memory deduplication structures
        seen_row_hashes = set()
        county_cache = {c.normalized_name: c for c in County.objects.filter(is_active=True)}
        place_cache = {p.normalized_name: p for p in GeographicPlace.objects.filter(is_active=True)}
        # Resolve postal area type from mapping snapshot profile rules
        postal_type = 'USPS_ZIP5'
        if profile:
            postal_type = profile.mapping_rules.get('POSTAL_AREA_TYPE') or profile.normalization_rules.get('POSTAL_AREA_TYPE') or 'USPS_ZIP5'
        postal_type = postal_type.upper()

        postal_cache = {p.postal_code: p for p in PostalArea.objects.filter(postal_area_type=postal_type, is_active=True)}

        success_count = 0
        warning_count = 0
        failed_count = 0
        duplicate_count = 0

        # Read CSV file
        with open(file_path, mode='r', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            rows_list = list(reader)
            
        batch.row_count = len(rows_list)
        batch.save()

        chunk_size = 500
        global_row_index = 1

        for chunk_rows in get_chunks(rows_list, chunk_size):
            with transaction.atomic():
                raw_records = []
                chunk_hashes = []
                
                # Pre-calculate hashes
                for row in chunk_rows:
                    row_hash = hashlib.sha256(str(sorted(row.items())).encode('utf-8')).hexdigest()
                    chunk_hashes.append(row_hash)

                # 3. Existing raw-row hashes lookup
                if profiler: profiler.start_phase("existing_raw_row_hashes")
                
                # 4. Raw-row creation
                if profiler: profiler.start_phase("raw_row_creation")
                for index, row in enumerate(chunk_rows):
                    row_hash = chunk_hashes[index]
                    if row_hash in seen_row_hashes:
                        duplicate_count += 1
                        status = 'EXACT_DUPLICATE'
                    else:
                        status = 'ACCEPTED'
                        seen_row_hashes.add(row_hash)

                    raw_rec = RawGeographyRecord(
                        import_batch=batch,
                        row_number=global_row_index + index,
                        original_values=row,
                        raw_row_hash=row_hash,
                        validation_status=status
                    )
                    raw_records.append(raw_rec)

                RawGeographyRecord.objects.bulk_create(raw_records)
                
                # Confirm primary keys for current chunk
                db_raws = RawGeographyRecord.objects.filter(import_batch=batch, row_number__gte=global_row_index, row_number__lt=global_row_index + len(chunk_rows))
                raw_by_row = {r.row_number: r for r in db_raws}
                for r in raw_records:
                    r.id = raw_by_row[r.row_number].id

                # Collect canonical components to pre-load / verify
                chunk_counties_needed = set()
                chunk_places_needed = set()
                chunk_postals_needed = set()

                for index, row in enumerate(chunk_rows):
                    raw_rec = raw_records[index]
                    if raw_rec.validation_status != 'ACCEPTED':
                        continue

                    if dataset.dataset_type == 'COUNTY_LIST':
                        c_name = row.get(rules.get('COUNTY_NAME'), '').strip()
                        if c_name:
                            chunk_counties_needed.add(c_name.upper().replace(' COUNTY', '').strip())
                    elif dataset.dataset_type == 'PLACE_LIST':
                        p_name = row.get(rules.get('PLACE_NAME'), '').strip()
                        if p_name:
                            chunk_places_needed.add(p_name.upper().strip())
                    elif dataset.dataset_type == 'POSTAL_LIST':
                        p_code = row.get(rules.get('POSTAL_CODE'), '').strip()
                        zip5, _, _ = normalize_zip_code(p_code, profile)
                        if zip5:
                            chunk_postals_needed.add(zip5)
                    elif dataset.dataset_type == 'ZIP_COUNTY_CROSSWALK':
                        zip_raw = row.get(rules.get('POSTAL_CODE'), '').strip()
                        county_val = row.get(rules.get('COUNTY_NAME'), '').strip()
                        zip5, _, _ = normalize_zip_code(zip_raw, profile)
                        if zip5:
                            chunk_postals_needed.add(zip5)
                        if county_val:
                            chunk_counties_needed.add(county_val.upper().replace(' COUNTY', '').strip())
                    elif dataset.dataset_type == 'ZIP_PLACE_CROSSWALK':
                        zip_raw = row.get(rules.get('POSTAL_CODE'), '').strip()
                        place_val = row.get(rules.get('PLACE_NAME'), '').strip()
                        zip5, _, _ = normalize_zip_code(zip_raw, profile)
                        if zip5:
                            chunk_postals_needed.add(zip5)
                        if place_val:
                            chunk_places_needed.add(place_val.upper().strip())
                    elif dataset.dataset_type == 'PLACE_COUNTY_CROSSWALK':
                        place_val = row.get(rules.get('PLACE_NAME'), '').strip()
                        county_val = row.get(rules.get('COUNTY_NAME'), '').strip()
                        if place_val:
                            chunk_places_needed.add(place_val.upper().strip())
                        if county_val:
                            chunk_counties_needed.add(county_val.upper().replace(' COUNTY', '').strip())

                # Resolve postal area type from mapping snapshot profile rules
                postal_type = 'USPS_ZIP5'
                if profile:
                    postal_type = profile.mapping_rules.get('POSTAL_AREA_TYPE') or profile.normalization_rules.get('POSTAL_AREA_TYPE') or 'USPS_ZIP5'
                postal_type = postal_type.upper()

                # 5. Canonical lookup
                if profiler: profiler.start_phase("canonical_lookup")
                missing_counties = chunk_counties_needed - set(county_cache.keys())
                if missing_counties:
                    db_counties = County.objects.filter(normalized_name__in=missing_counties, is_active=True)
                    for c in db_counties:
                        county_cache[c.normalized_name] = c

                missing_places = chunk_places_needed - set(place_cache.keys())
                if missing_places:
                    db_places = GeographicPlace.objects.filter(normalized_name__in=missing_places, is_active=True)
                    for p in db_places:
                        place_cache[p.normalized_name] = p

                missing_postals = chunk_postals_needed - set(postal_cache.keys())
                if missing_postals:
                    db_postals = PostalArea.objects.filter(postal_code__in=missing_postals, postal_area_type=postal_type, is_active=True)
                    for po in db_postals:
                        postal_cache[po.postal_code] = po

                # 6. Canonical creation
                if profiler: profiler.start_phase("canonical_creation")
                new_counties = []
                for name in (chunk_counties_needed - set(county_cache.keys())):
                    new_counties.append(County(state_code='CA', normalized_name=name, display_name=name.title(), is_active=True))
                if new_counties:
                    County.objects.bulk_create(new_counties)
                    for c in County.objects.filter(normalized_name__in=[nc.normalized_name for nc in new_counties]):
                        county_cache[c.normalized_name] = c

                new_places = []
                for name in (chunk_places_needed - set(place_cache.keys())):
                    new_places.append(GeographicPlace(state_code='CA', canonical_name=name.title(), normalized_name=name, general_category='CITY', is_active=True))
                if new_places:
                    GeographicPlace.objects.bulk_create(new_places)
                    for p in GeographicPlace.objects.filter(normalized_name__in=[np.normalized_name for np in new_places]):
                        place_cache[p.normalized_name] = p

                new_postals = []
                for code in (chunk_postals_needed - set(postal_cache.keys())):
                    new_postals.append(PostalArea(postal_code=code, postal_area_type=postal_type, is_active=True))
                if new_postals:
                    PostalArea.objects.bulk_create(new_postals)
                    for po in PostalArea.objects.filter(postal_code__in=[np.postal_code for np in new_postals], postal_area_type=postal_type):
                        postal_cache[po.postal_code] = po

                # Mappings and validation
                to_create_source_records = []
                to_create_associations = []
                to_create_aliases = []
                to_create_identifiers = []

                for index, row in enumerate(chunk_rows):
                    raw_rec = raw_records[index]
                    if raw_rec.validation_status != 'ACCEPTED':
                        continue

                    try:
                        row_errors = []
                        row_warnings = []
                        status_override = None

                        if dataset.dataset_type == 'COUNTY_LIST':
                            c_name = row.get(rules.get('COUNTY_NAME'), '').strip()
                            if not c_name:
                                raise ValueError("County name is required")
                            norm = c_name.upper().replace(' COUNTY', '').strip()
                            canonical = county_cache.get(norm)
                            
                            src = CountySourceRecord(
                                county=canonical, dataset=dataset, import_batch=batch, raw_record=raw_rec,
                                source_id=row.get(rules.get('COUNTY_IDENTIFIER'), '').strip(),
                                source_name=c_name, status='ACTIVE'
                            )
                            to_create_source_records.append(src)
                            success_count += 1

                        elif dataset.dataset_type == 'PLACE_LIST':
                            p_name = row.get(rules.get('PLACE_NAME'), '').strip()
                            if not p_name:
                                raise ValueError("Place name is required")
                            norm = p_name.upper().strip()
                            canonical = place_cache.get(norm)

                            src = PlaceSourceRecord(
                                place=canonical, dataset=dataset, import_batch=batch, raw_record=raw_rec,
                                source_id=row.get(rules.get('PLACE_IDENTIFIER'), '').strip(),
                                source_name=p_name, status='ACTIVE'
                            )
                            to_create_source_records.append(src)
                            success_count += 1

                        elif dataset.dataset_type == 'POSTAL_LIST':
                            p_code = row.get(rules.get('POSTAL_CODE'), '').strip()
                            zip5, zip4, warning = normalize_zip_code(p_code, profile)
                            if warning:
                                row_warnings.append(warning)
                            if zip5 is None and warning and "unauthorized padding" in warning:
                                status_override = 'POSSIBLE_TRUNCATED_LEADING_ZERO'
                            
                            if status_override != 'POSSIBLE_TRUNCATED_LEADING_ZERO' and not zip5:
                                raise ValueError(warning or "Postal code normalization failed")

                            canonical = postal_cache.get(zip5) if zip5 else None
                            
                            src = PostalAreaSourceRecord(
                                postal_area=canonical, dataset=dataset, import_batch=batch, raw_record=raw_rec,
                                source_code=p_code, source_type=row.get(rules.get('POSTAL_AREA_TYPE'), 'USPS_ZIP5').upper(),
                                display_name=row.get(rules.get('DISPLAY_NAME'), '').strip(), status='ACTIVE'
                            )
                            to_create_source_records.append(src)
                            
                            if status_override == 'POSSIBLE_TRUNCATED_LEADING_ZERO':
                                raw_rec.validation_status = 'POSSIBLE_TRUNCATED_LEADING_ZERO'
                                warning_count += 1
                            elif warning:
                                raw_rec.validation_status = 'VALIDATION_WARNING'
                                warning_count += 1
                            else:
                                success_count += 1

                        elif dataset.dataset_type == 'ZIP_COUNTY_CROSSWALK':
                            zip_raw = row.get(rules.get('POSTAL_CODE'), '').strip()
                            county_val = row.get(rules.get('COUNTY_NAME'), '').strip()
                            
                            zip5, zip4, warning = normalize_zip_code(zip_raw, profile)
                            if warning:
                                row_warnings.append(warning)
                            if zip5 is None and warning and "unauthorized padding" in warning:
                                status_override = 'POSSIBLE_TRUNCATED_LEADING_ZERO'

                            if status_override != 'POSSIBLE_TRUNCATED_LEADING_ZERO' and not zip5:
                                raise ValueError(warning or "Postal code is required")
                            
                            p_area = postal_cache.get(zip5) if zip5 else None
                            norm_county = county_val.upper().replace(' COUNTY', '').strip()
                            county = county_cache.get(norm_county)
                            if not county:
                                raise ValueError(f"Referenced County '{county_val}' does not exist")

                            raw_weight = row.get(rules.get('WEIGHT_VALUE'), '').strip()
                            basis = row.get(rules.get('WEIGHT_BASIS'), 'UNKNOWN').strip()
                            unit = row.get(rules.get('WEIGHT_UNIT'), 'UNKNOWN').strip()
                            
                            norm_weight, rule, w_err = normalize_weight_value(raw_weight, basis, unit)
                            if w_err:
                                row_warnings.append(w_err)

                            assoc = PostalCountyAssociation(
                                postal_area=p_area, county=county, relationship_type='CROSSWALK', confidence='HIGH',
                                is_active=True, dataset=dataset, import_batch=batch, raw_record=raw_rec,
                                weight_value=norm_weight, normalized_weight_value=norm_weight,
                                weight_basis=basis, weight_unit=unit, raw_weight_value=raw_weight,
                                normalization_rule=rule
                            )
                            to_create_associations.append(assoc)
                            if status_override == 'POSSIBLE_TRUNCATED_LEADING_ZERO':
                                raw_rec.validation_status = 'POSSIBLE_TRUNCATED_LEADING_ZERO'
                                warning_count += 1
                            elif warning or w_err:
                                raw_rec.validation_status = 'VALIDATION_WARNING'
                                warning_count += 1
                            else:
                                success_count += 1

                        elif dataset.dataset_type == 'ZIP_PLACE_CROSSWALK':
                            zip_raw = row.get(rules.get('POSTAL_CODE'), '').strip()
                            place_val = row.get(rules.get('PLACE_NAME'), '').strip()
                            
                            zip5, zip4, warning = normalize_zip_code(zip_raw, profile)
                            if warning:
                                row_warnings.append(warning)
                            if zip5 is None and warning and "unauthorized padding" in warning:
                                status_override = 'POSSIBLE_TRUNCATED_LEADING_ZERO'

                            if status_override != 'POSSIBLE_TRUNCATED_LEADING_ZERO' and not zip5:
                                raise ValueError(warning or "Postal code is required")
                            
                            p_area = postal_cache.get(zip5) if zip5 else None
                            norm_place = place_val.upper().strip()
                            place = place_cache.get(norm_place)
                            if not place:
                                raise ValueError(f"Referenced Place '{place_val}' does not exist")

                            raw_weight = row.get(rules.get('WEIGHT_VALUE'), '').strip()
                            basis = row.get(rules.get('WEIGHT_BASIS'), 'UNKNOWN').strip()
                            unit = row.get(rules.get('WEIGHT_UNIT'), 'UNKNOWN').strip()
                            
                            norm_weight, rule, w_err = normalize_weight_value(raw_weight, basis, unit)
                            if w_err:
                                row_warnings.append(w_err)

                            assoc = PostalPlaceAssociation(
                                postal_area=p_area, place=place, relationship_type='CROSSWALK', confidence='HIGH',
                                is_active=True, dataset=dataset, import_batch=batch, raw_record=raw_rec,
                                weight_value=norm_weight, normalized_weight_value=norm_weight,
                                weight_basis=basis, weight_unit=unit, raw_weight_value=raw_weight,
                                normalization_rule=rule
                            )
                            to_create_associations.append(assoc)
                            if status_override == 'POSSIBLE_TRUNCATED_LEADING_ZERO':
                                raw_rec.validation_status = 'POSSIBLE_TRUNCATED_LEADING_ZERO'
                                warning_count += 1
                            elif warning or w_err:
                                raw_rec.validation_status = 'VALIDATION_WARNING'
                                warning_count += 1
                            else:
                                success_count += 1

                        elif dataset.dataset_type == 'PLACE_COUNTY_CROSSWALK':
                            place_val = row.get(rules.get('PLACE_NAME'), '').strip()
                            county_val = row.get(rules.get('COUNTY_NAME'), '').strip()
                            if not place_val or not county_val:
                                raise ValueError("Both PLACE and COUNTY are required")
                            norm_place = place_val.upper().strip()
                            place = place_cache.get(norm_place)
                            norm_county = county_val.upper().replace(' COUNTY', '').strip()
                            county = county_cache.get(norm_county)

                            raw_weight = row.get(rules.get('WEIGHT_VALUE'), '').strip()
                            basis = row.get(rules.get('WEIGHT_BASIS'), 'UNKNOWN').strip()
                            unit = row.get(rules.get('WEIGHT_UNIT'), 'UNKNOWN').strip()
                            
                            norm_weight, rule, w_err = normalize_weight_value(raw_weight, basis, unit)
                            if w_err:
                                row_warnings.append(w_err)

                            assoc = PlaceCountyAssociation(
                                place=place, county=county, relationship_type='CROSSWALK', confidence='HIGH',
                                is_active=True, dataset=dataset, import_batch=batch, raw_record=raw_rec,
                                weight_value=norm_weight, normalized_weight_value=norm_weight,
                                weight_basis=basis, weight_unit=unit, raw_weight_value=raw_weight,
                                normalization_rule=rule
                            )
                            to_create_associations.append(assoc)
                            success_count += 1

                        elif dataset.dataset_type == 'ALIAS_LIST':
                            original = row.get(rules.get('ORIGINAL_ALIAS'), '').strip()
                            a_type = row.get(rules.get('ALIAS_TYPE'), 'COMMON_NAME').strip()
                            target_name = row.get(rules.get('TARGET_NAME'), '').strip()
                            target_type = row.get(rules.get('TARGET_TYPE'), '').strip().upper()

                            c_target, pl_target, pos_target = None, None, None
                            if target_type == 'COUNTY':
                                c_target = county_cache.get(target_name.upper().replace(' COUNTY', '').strip())
                            elif target_type == 'PLACE':
                                pl_target = place_cache.get(target_name.upper().strip())
                            elif target_type == 'POSTAL_AREA':
                                pos_target = postal_cache.get(target_name)

                            alias = GeographyAlias(
                                alias_type=a_type.upper(), original_alias=original, normalized_alias=original.upper().strip(),
                                county_target=c_target, place_target=pl_target, postal_target=pos_target,
                                dataset=dataset, import_batch=batch, raw_record=raw_rec, source_description=f"Imported from {file_name}"
                            )
                            to_create_aliases.append(alias)
                            success_count += 1

                        elif dataset.dataset_type == 'IDENTIFIER_LIST':
                            target_name = row.get(rules.get('TARGET_NAME'), '').strip()
                            target_type = row.get(rules.get('TARGET_TYPE'), '').strip().upper()
                            scheme = row.get(rules.get('SCHEME'), '').strip()
                            val = row.get(rules.get('VALUE'), '').strip()

                            c_target, pl_target, pos_target = None, None, None
                            if target_type == 'COUNTY':
                                c_target = county_cache.get(target_name.upper().replace(' COUNTY', '').strip())
                            elif target_type == 'PLACE':
                                pl_target = place_cache.get(target_name.upper().strip())
                            elif target_type == 'POSTAL_AREA':
                                pos_target = postal_cache.get(target_name)

                            identifier = GeographyIdentifier(
                                county_target=c_target, place_target=pl_target, postal_target=pos_target,
                                scheme=scheme.upper(), component_designation=row.get(rules.get('COMPONENT_DESIGNATION'), '').strip(),
                                value=val, issuing_authority=row.get(rules.get('ISSUING_AUTHORITY'), '').strip(),
                                dataset=dataset, import_batch=batch, is_active=True
                            )
                            to_create_identifiers.append(identifier)
                            success_count += 1

                        if row_warnings:
                            raw_rec.validation_errors = "; ".join(row_warnings)
                            raw_rec.save()

                    except Exception as e:
                        failed_count += 1
                        raw_rec.validation_status = 'VALIDATION_FAILURE'
                        raw_rec.validation_errors = str(e)
                        raw_rec.save()

                # 7. Source-record lookup
                if profiler: profiler.start_phase("source_record_lookup")
                
                # 8. Source-record creation
                if profiler: profiler.start_phase("source_record_creation")
                if to_create_source_records:
                    if dataset.dataset_type == 'COUNTY_LIST':
                        CountySourceRecord.objects.bulk_create(to_create_source_records)
                    elif dataset.dataset_type == 'PLACE_LIST':
                        PlaceSourceRecord.objects.bulk_create(to_create_source_records)
                    elif dataset.dataset_type == 'POSTAL_LIST':
                        PostalAreaSourceRecord.objects.bulk_create(to_create_source_records)

                # 9. Existing relationship lookup
                if profiler: profiler.start_phase("relationship_lookup")
                
                # 10. Relationship creation
                if profiler: profiler.start_phase("relationship_creation")
                if to_create_associations:
                    if dataset.dataset_type == 'ZIP_COUNTY_CROSSWALK':
                        # Bulk-check active associations in exactly 1 query
                        postal_codes = [assoc.postal_area.postal_code for assoc in to_create_associations if assoc.postal_area]
                        existing_pairs = set(PostalCountyAssociation.objects.filter(
                            postal_area__postal_code__in=postal_codes, dataset=dataset, is_active=True
                        ).values_list('postal_area_id', 'county_id'))

                        checked = []
                        seen_pairs = set()
                        for assoc in to_create_associations:
                            if not assoc.postal_area:
                                continue
                            pair = (assoc.postal_area_id, assoc.county_id)
                            if pair in seen_pairs or pair in existing_pairs:
                                duplicate_count += 1
                                success_count -= 1
                                continue
                            checked.append(assoc)
                            seen_pairs.add(pair)
                        if checked:
                            PostalCountyAssociation.objects.bulk_create(checked)

                    elif dataset.dataset_type == 'ZIP_PLACE_CROSSWALK':
                        postal_codes = [assoc.postal_area.postal_code for assoc in to_create_associations if assoc.postal_area]
                        existing_pairs = set(PostalPlaceAssociation.objects.filter(
                            postal_area__postal_code__in=postal_codes, dataset=dataset, is_active=True
                        ).values_list('postal_area_id', 'place_id'))

                        checked = []
                        seen_pairs = set()
                        for assoc in to_create_associations:
                            if not assoc.postal_area:
                                continue
                            pair = (assoc.postal_area_id, assoc.place_id)
                            if pair in seen_pairs or pair in existing_pairs:
                                duplicate_count += 1
                                success_count -= 1
                                continue
                            checked.append(assoc)
                            seen_pairs.add(pair)
                        if checked:
                            PostalPlaceAssociation.objects.bulk_create(checked)

                    elif dataset.dataset_type == 'PLACE_COUNTY_CROSSWALK':
                        place_names = [assoc.place.normalized_name for assoc in to_create_associations if assoc.place]
                        existing_pairs = set(PlaceCountyAssociation.objects.filter(
                            place__normalized_name__in=place_names, dataset=dataset, is_active=True
                        ).values_list('place_id', 'county_id'))

                        checked = []
                        seen_pairs = set()
                        for assoc in to_create_associations:
                            pair = (assoc.place_id, assoc.county_id)
                            if pair in seen_pairs or pair in existing_pairs:
                                duplicate_count += 1
                                success_count -= 1
                                continue
                            checked.append(assoc)
                            seen_pairs.add(pair)
                        if checked:
                            PlaceCountyAssociation.objects.bulk_create(checked)

                # 11. Alias and identifier creation
                if profiler: profiler.start_phase("alias_identifier_creation")
                valid_aliases = [a for a in to_create_aliases if a.county_target or a.place_target or a.postal_target]
                if valid_aliases:
                    GeographyAlias.objects.bulk_create(valid_aliases)
                if to_create_identifiers:
                    valid_identifiers = [i for i in to_create_identifiers if i.county_target or i.place_target or i.postal_target]
                    if valid_identifiers:
                        GeographyIdentifier.objects.bulk_create(valid_identifiers)

            global_row_index += len(chunk_rows)

        # 12. Batch count and status updates
        if profiler: profiler.start_phase("batch_status_updates")
        batch.status = 'COMPLETED'
        batch.successful_rows = success_count
        batch.warning_rows = warning_count
        batch.failed_rows = failed_count
        batch.duplicate_rows = duplicate_count
        batch.completed_time = timezone.now()
        batch.save()

        # 13. Audit events
        if profiler: profiler.start_phase("audit_event_creation")
        AuditEvent.objects.create(
            event_type='GEOGRAPHY_IMPORT_EXECUTE',
            description=f"Imported geographic reference file '{file_name}' (Batch {batch.id}) for dataset '{dataset.name}'. Successful: {success_count}, Failed: {failed_count}.",
            actor=actor
        )

        # 14. Resolution proposal creation
        if profiler: profiler.start_phase("resolution_proposal_creation")
        GeographyResolutionRun.objects.create(
            trigger_type='DATASET_ACTIVATION',
            resolver_version='1.0',
            scope=f"dataset_{dataset.id}",
            actor=actor,
            status='PENDING',
            dataset=dataset
        )

    return batch

def rollback_geography_batch(batch_id, actor):
    with transaction.atomic():
        try:
            batch = GeographyImportBatch.objects.get(id=batch_id)
        except GeographyImportBatch.DoesNotExist:
            raise ValueError(f"Batch with ID {batch_id} not found")

        if batch.status != 'COMPLETED':
            raise ValueError("Only completed batches can be rolled back")

        # Deactivate source records
        CountySourceRecord.objects.filter(import_batch=batch).update(status='ROLLED_BACK')
        PlaceSourceRecord.objects.filter(import_batch=batch).update(status='ROLLED_BACK')
        PostalAreaSourceRecord.objects.filter(import_batch=batch).update(status='ROLLED_BACK')
        
        # Deactivate identifiers and associations
        GeographyIdentifier.objects.filter(import_batch=batch).update(is_active=False)
        GeographyAlias.objects.filter(import_batch=batch).update(is_active=False)
        PlaceCountyAssociation.objects.filter(import_batch=batch).update(is_active=False)
        PostalCountyAssociation.objects.filter(import_batch=batch).update(is_active=False)
        PostalPlaceAssociation.objects.filter(import_batch=batch).update(is_active=False)
        
        # Cancel any pending runs associated with this dataset
        GeographyResolutionRun.objects.filter(dataset=batch.dataset, status='PENDING').update(status='CANCELLED')

        batch.status = 'ROLLED_BACK'
        batch.rollback_state = 'ROLLED_BACK'
        batch.save()

        AuditEvent.objects.create(
            event_type='GEOGRAPHY_IMPORT_ROLLBACK',
            description=f"Rolled back geography import batch {batch_id} ('{batch.file_name}').",
            actor=actor
        )

def restore_geography_batch(batch_id, actor):
    with transaction.atomic():
        try:
            batch = GeographyImportBatch.objects.get(id=batch_id)
        except GeographyImportBatch.DoesNotExist:
            raise ValueError(f"Batch with ID {batch_id} not found")

        if batch.status != 'ROLLED_BACK':
            raise ValueError("Only rolled back batches can be restored")

        # Reactivate
        CountySourceRecord.objects.filter(import_batch=batch).update(status='ACTIVE')
        PlaceSourceRecord.objects.filter(import_batch=batch).update(status='ACTIVE')
        PostalAreaSourceRecord.objects.filter(import_batch=batch).update(status='ACTIVE')
        
        GeographyIdentifier.objects.filter(import_batch=batch).update(is_active=True)
        GeographyAlias.objects.filter(import_batch=batch).update(is_active=True)
        PlaceCountyAssociation.objects.filter(import_batch=batch).update(is_active=True)
        PostalCountyAssociation.objects.filter(import_batch=batch).update(is_active=True)
        PostalPlaceAssociation.objects.filter(import_batch=batch).update(is_active=True)
        
        batch.status = 'COMPLETED'
        batch.rollback_state = 'ACTIVE'
        batch.save()

        # Re-create pending proposal
        GeographyResolutionRun.objects.create(
            trigger_type='DATASET_ACTIVATION',
            resolver_version='1.0',
            scope=f"dataset_{batch.dataset.id}",
            actor=actor,
            status='PENDING',
            dataset=batch.dataset
        )

        AuditEvent.objects.create(
            event_type='GEOGRAPHY_IMPORT_RESTORE',
            description=f"Restored rolled-back geography import batch {batch_id} ('{batch.file_name}').",
            actor=actor
        )
