from django.db import models
from django.contrib.auth.models import User

class ImportMappingProfile(models.Model):
    name = models.models.CharField(max_length=255) if False else models.CharField(max_length=255)
    mapping_rules = models.JSONField(help_text="JSON mapping rules from CSV headers to normalized fields")
    source_type = models.CharField(max_length=50, default='SOS_CONTRIBUTION')
    version = models.CharField(max_length=50, default='1.0')
    owner = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.name} (v{self.version})"

class ImportBatch(models.Model):
    STATUS_CHOICES = [
        ('PENDING', 'Pending'),
        ('VALIDATING', 'Validating'),
        ('COMPLETED', 'Completed'),
        ('FAILED', 'Failed'),
        ('ROLLED_BACK', 'Rolled Back'),
        ('RESTORING', 'Restoring'),
    ]

    file_name = models.CharField(max_length=255)
    file_hash = models.CharField(max_length=64)
    file_type = models.CharField(max_length=50)
    import_date = models.DateTimeField(auto_now_add=True)
    imported_by = models.CharField(max_length=150)
    row_count = models.IntegerField(default=0)
    successful_rows = models.IntegerField(default=0)
    failed_rows = models.IntegerField(default=0)
    duplicate_rows = models.IntegerField(default=0)
    mapping_profile = models.ForeignKey(ImportMappingProfile, on_delete=models.SET_NULL, null=True, blank=True)
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='PENDING', db_index=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["file_hash"],
                condition=models.Q(status="COMPLETED"),
                name="unique_completed_batch_file_hash"
            )
        ]

    def __str__(self):
        return f"{self.file_name} ({self.status} - {self.import_date.strftime('%Y-%m-%d')})"

class RawContribution(models.Model):
    STATUS_CHOICES = [
        ('UNPROCESSED', 'Unprocessed'),
        ('ACCEPTED', 'Accepted'),
        ('EXACT_DUPLICATE', 'Exact Duplicate'),
        ('POSSIBLE_DUPLICATE', 'Possible Duplicate'),
        ('POSSIBLE_AMENDMENT', 'Possible Amendment'),
        ('VALIDATION_WARNING', 'Validation Warning'),
        ('VALIDATION_FAILURE', 'Validation Failure'),
        ('MANUALLY_EXCLUDED', 'Manually Excluded'),
    ]

    import_batch = models.ForeignKey(ImportBatch, on_delete=models.CASCADE, related_name='raw_contributions')
    row_number = models.IntegerField()
    original_values = models.JSONField(help_text="Raw values dict parsed from the CSV row")
    raw_row_hash = models.CharField(max_length=64, db_index=True)
    validation_status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='UNPROCESSED')
    validation_errors = models.TextField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=['import_batch', 'validation_status'], name='idx_raw_batch_status'),
        ]

    def __str__(self):
        return f"Batch {self.import_batch_id} Row {self.row_number} ({self.validation_status})"

class ContributorEntity(models.Model):
    ENTITY_TYPE_CHOICES = [
        ('INDIVIDUAL', 'Individual'),
        ('ORGANIZATION', 'Organization'),
        ('JOINT', 'Joint / Ambiguous Contributor'),
        ('UNKNOWN', 'Unknown'),
    ]

    VERIFICATION_STATUS_CHOICES = [
        ('UNVERIFIED', 'Unverified'),
        ('VERIFIED', 'Verified'),
    ]

    VERIFICATION_METHOD_CHOICES = [
        ('NONE', 'None'),
        ('ADMIN_REVIEW', 'Administrative Review'),
        ('EXTERNAL_IDENTITY_MATCH', 'External Identity Match'),
        ('LEGACY_REVIEWED', 'Legacy Reviewed'),
    ]

    entity_type = models.CharField(max_length=50, choices=ENTITY_TYPE_CHOICES, default='INDIVIDUAL')
    display_name = models.CharField(max_length=255)
    is_verified = models.BooleanField(default=False)
    verification_status = models.CharField(max_length=50, choices=VERIFICATION_STATUS_CHOICES, default='UNVERIFIED')
    verification_method = models.CharField(max_length=50, choices=VERIFICATION_METHOD_CHOICES, default='NONE')
    verified_at = models.DateTimeField(null=True, blank=True)
    verified_by = models.CharField(max_length=150, null=True, blank=True)
    verification_evidence = models.JSONField(default=dict, blank=True)
    verification_explanation = models.TextField(blank=True)
    data_quality_flags = models.JSONField(default=list, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(verification_status='UNVERIFIED') | models.Q(verification_method='NONE'),
                name='check_unverified_method_none'
            ),
            models.CheckConstraint(
                condition=~models.Q(verification_status='VERIFIED') | ~models.Q(verification_method='NONE'),
                name='check_verified_method_not_none'
            ),
            models.CheckConstraint(
                condition=~models.Q(verification_status='VERIFIED') | models.Q(verified_at__isnull=False),
                name='check_verified_has_timestamp'
            ),
            models.CheckConstraint(
                condition=~models.Q(verification_method='ADMIN_REVIEW') | models.Q(verified_by__isnull=False),
                name='check_admin_verified_has_actor'
            ),
            models.CheckConstraint(
                condition=(
                    (models.Q(is_verified=True) & models.Q(verification_status='VERIFIED')) |
                    (models.Q(is_verified=False) & models.Q(verification_status='UNVERIFIED'))
                ),
                name='check_is_verified_sync'
            ),
        ]
        permissions = [
            ("view_sensitive_roster", "Can view sensitive roster information"),
            ("import_contributions", "Can upload and import contribution files"),
            ("override_duplicate_file", "Can override duplicate file import blocks"),
            ("rollback_import", "Can roll back imports"),
            ("restore_import", "Can restore rolled-back imports"),
            ("manage_identity", "Can merge, split, and correct contributor types"),
            ("override_membership", "Can manually override membership status"),
            ("view_audit", "Can view audit provenance records"),
            ("export_sensitive_data", "Can export sensitive roster data"),
            ("purge_data", "Can permanently purge data from CLI"),
            ("view_geography_reference", "Can view reference geography maps and lists"),
            ("import_geography_reference", "Can import geographic reference datasets"),
            ("manage_geography_reference", "Can manage and activate geographic references"),
            ("rollback_geography_import", "Can roll back and restore geographic imports"),
            ("resolve_geography_ambiguity", "Can resolve ambiguous or conflicting contributor locations"),
            ("view_chapter_definitions", "Can view chapter definitions"),
            ("manage_chapter_definitions", "Can manage chapter definitions"),
            ("preview_chapter_rules", "Can run previews of chapter rules"),
            ("activate_chapter_rules", "Can activate chapter rule sets"),
            ("evaluate_chapter_rules", "Can run apply evaluations for chapters"),
            ("manage_chapter_overrides", "Can manage manual chapter overrides"),
            ("view_chapter_assignments", "Can view contributor chapter assignments"),
        ]

    def save(self, *args, **kwargs):
        # Keep is_verified boolean aligned with verification_status
        self.is_verified = (self.verification_status == 'VERIFIED')
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.display_name} ({self.entity_type} - Verified: {self.is_verified})"

class Person(models.Model):
    contributor_entity = models.OneToOneField(ContributorEntity, on_delete=models.CASCADE, related_name='person_profile')
    first_name = models.CharField(max_length=100, blank=True)
    middle_name = models.CharField(max_length=100, blank=True)
    last_name = models.CharField(max_length=100, blank=True, db_index=True)
    suffix = models.CharField(max_length=50, blank=True)

    def __str__(self):
        return f"Person: {self.first_name} {self.last_name}"

class Organization(models.Model):
    contributor_entity = models.OneToOneField(ContributorEntity, on_delete=models.CASCADE, related_name='organization_profile')
    legal_name = models.CharField(max_length=255)
    committee_id = models.CharField(max_length=100, blank=True)

    def __str__(self):
        return f"Org: {self.legal_name}"

class ContributionCluster(models.Model):
    CONFIDENCE_CHOICES = [
        ('HIGH', 'High'),
        ('MEDIUM', 'Medium'),
        ('LOW', 'Low'),
    ]

    contributor_entity = models.ForeignKey(ContributorEntity, on_delete=models.CASCADE, related_name='clusters')
    normalized_name = models.CharField(max_length=255, db_index=True)
    zip_code = models.CharField(max_length=20, blank=True, db_index=True)
    confidence_level = models.CharField(max_length=50, choices=CONFIDENCE_CHOICES, default='LOW')
    confidence_explanation = models.TextField(blank=True)

    class Meta:
        indexes = [
            models.Index(fields=['normalized_name', 'zip_code'], name='idx_cluster_name_zip'),
        ]

    def __str__(self):
        return f"Cluster: {self.normalized_name} ({self.zip_code}) - {self.confidence_level}"

class Contribution(models.Model):
    TRANSACTION_TYPE_CHOICES = [
        ('CONTRIBUTION', 'Positive Contribution'),
        ('REFUND', 'Refund'),
        ('REVERSAL', 'Processor Reversal'),
        ('ADJUSTMENT', 'Filing Adjustment'),
    ]

    STATUS_CHOICES = [
        ('ACTIVE', 'Active'),
        ('SUPERSEDED', 'Superseded'),
        ('MANUALLY_EXCLUDED', 'Manually Excluded'),
    ]

    raw_contribution = models.OneToOneField(RawContribution, on_delete=models.CASCADE, related_name='normalized_contribution')
    transaction_number = models.CharField(max_length=100, blank=True, db_index=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    transaction_type = models.CharField(max_length=50, choices=TRANSACTION_TYPE_CHOICES, default='CONTRIBUTION')
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='ACTIVE')
    transaction_date = models.DateField(db_index=True)
    filed_date = models.DateField(null=True, blank=True)
    raw_address = models.TextField(blank=True)
    employer = models.CharField(max_length=255, blank=True)
    occupation = models.CharField(max_length=255, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=['transaction_number', 'transaction_date'], name='idx_contrib_num_date'),
        ]

    def __str__(self):
        return f"{self.transaction_type} {self.transaction_number} - {self.amount} ({self.status})"

class ContributionClusterAssignment(models.Model):
    contribution = models.ForeignKey(Contribution, on_delete=models.CASCADE, related_name='assignments')
    contribution_cluster = models.ForeignKey(ContributionCluster, on_delete=models.CASCADE, related_name='assignments')
    assigned_at = models.DateTimeField(auto_now_add=True)
    assigned_by = models.CharField(max_length=150, default='AUTOMATED_RESOLVER')
    is_active = models.BooleanField(default=True, db_index=True)
    notes = models.TextField(blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["contribution"],
                condition=models.Q(is_active=True),
                name="unique_active_cluster_assignment_per_contribution",
            )
        ]
        indexes = [
            models.Index(fields=['contribution_cluster', 'is_active'], name='idx_assign_cluster_active'),
        ]

    def __str__(self):
        active_str = "Active" if self.is_active else "Inactive"
        return f"Assign: Txn {self.contribution_id} to Cluster {self.contribution_cluster_id} ({active_str})"

class ProfilePatternAssessment(models.Model):
    PATTERN_CHOICES = [
        ('POSSIBLE_RECURRING', 'Possible Recurring Pattern'),
        ('SINGLE_VISIBLE_CONTRIBUTION', 'Single Visible Contribution'),
        ('PREVIOUSLY_RECURRING', 'Previously Recurring Pattern'),
        ('INSUFFICIENT_HISTORY', 'Insufficient History'),
        ('AMBIGUOUS_IDENTITY_CLUSTER', 'Ambiguous Identity Cluster'),
    ]

    contribution_cluster = models.OneToOneField(ContributionCluster, on_delete=models.CASCADE, related_name='pattern_assessment')
    calculated_pattern = models.CharField(max_length=100, choices=PATTERN_CHOICES)
    pattern_explanation = models.TextField(blank=True)
    calculated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Pattern: {self.calculated_pattern} on Cluster {self.contribution_cluster_id}"

class MembershipRuleVersion(models.Model):
    name = models.CharField(max_length=100)
    monthly_interval_min = models.IntegerField(default=20)
    monthly_interval_max = models.IntegerField(default=40)
    active_grace_period = models.IntegerField(default=60)
    min_recurring_payments = models.IntegerField(default=2)
    allowed_amount_variance = models.DecimalField(max_digits=5, decimal_places=2, default=0.00)
    skip_payment_allowed = models.BooleanField(default=True)
    refund_behavior = models.CharField(max_length=100, default='EXCLUDE_TIMELINE')
    coverage_requirements = models.CharField(max_length=100, default='CONFIRMED_OR_CONTINUOUS')
    effective_date = models.DateField()
    created_by = models.CharField(max_length=150)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.name} (Grace: {self.active_grace_period}d)"

class MembershipAssessment(models.Model):
    STATUS_CHOICES = [
        ('ACTIVE', 'Active Member'),
        ('PROVISIONAL', 'New or Provisional Member'),
        ('LIKELY', 'Likely Member'),
        ('PREVIOUSLY_RECURRING', 'Previously Recurring'),
        ('LAPSED', 'Lapsed Member'),
        ('ONE_TIME', 'One-Time Donor'),
        ('UNKNOWN', 'Unknown'),
        ('INSUFFICIENT_HISTORY', 'Insufficient History'),
        ('DATASET_TOO_STALE', 'Dataset Too Stale'),
    ]

    RECURRENCE_PATTERN_CHOICES = [
        ('RECURRING_PATTERN', 'Recurring Pattern'),
        ('PREVIOUSLY_RECURRING_PATTERN', 'Previously Recurring Pattern'),
        ('IRREGULAR_PATTERN', 'Irregular Pattern'),
        ('INSUFFICIENT_HISTORY', 'Insufficient History'),
        ('NO_RECURRING_PATTERN', 'No Recurring Pattern'),
    ]

    AUTHORITY_CHOICES = [
        ('AUTHORITATIVE', 'Authoritative'),
        ('PROVISIONAL', 'Provisional'),
        ('INELIGIBLE', 'Ineligible'),
    ]

    contributor_entity = models.ForeignKey(ContributorEntity, on_delete=models.CASCADE, related_name='membership_assessments')
    calculated_status = models.CharField(max_length=100, choices=STATUS_CHOICES)
    recurrence_pattern_status = models.CharField(max_length=50, choices=RECURRENCE_PATTERN_CHOICES, default='INSUFFICIENT_HISTORY')
    membership_authority = models.CharField(max_length=50, choices=AUTHORITY_CHOICES, default='PROVISIONAL')
    identity_verified_at_assessment = models.BooleanField(default=False)
    is_current = models.BooleanField(default=True, db_index=True)
    superseded_by = models.ForeignKey('self', null=True, blank=True, on_delete=models.SET_NULL, related_name='supersedes')
    recurring_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    payment_interval = models.CharField(max_length=50, blank=True)
    rule_version = models.ForeignKey(MembershipRuleVersion, on_delete=models.PROTECT)
    calculation_date = models.DateTimeField(auto_now=True)
    manual_override = models.BooleanField(default=False)
    explanation = models.TextField(blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["contributor_entity"],
                condition=models.Q(is_current=True),
                name="unique_current_assessment_per_entity"
            )
        ]

    def __str__(self):
        return f"Assessment: Entity {self.contributor_entity_id} - {self.calculated_status} ({self.membership_authority})"

class AmendmentRelationship(models.Model):
    """Tracks amendment relationships between contributions.
    
    Conservative disposition policy:
    - PENDING: Original operative, replacement is evidence only
    - ACCEPTED: Replacement operative, original is historical provenance
    - REJECTED: Original operative, replacement excluded
    """
    RELATIONSHIP_CHOICES = [
        ('AMENDMENT', 'Amendment'),
        ('CORRECTION', 'Correction'),
        ('SUPERSESSION', 'Supersession'),
    ]
    REVIEW_STATUS_CHOICES = [
        ('PENDING', 'Pending Review'),
        ('ACCEPTED', 'Accepted'),
        ('REJECTED', 'Rejected'),
    ]
    DISPOSITION_CHOICES = [
        ('ORIGINAL', 'Original is operative'),
        ('REPLACEMENT', 'Replacement is operative'),
        ('NEITHER', 'Neither is operative'),
    ]

    original_contribution = models.ForeignKey(
        'Contribution', on_delete=models.PROTECT,
        related_name='amendment_as_original',
        help_text='The original contribution being amended',
    )
    replacement_contribution = models.ForeignKey(
        'Contribution', on_delete=models.PROTECT,
        related_name='amendment_as_replacement',
        help_text='The replacement contribution',
    )
    relationship_type = models.CharField(
        max_length=50, choices=RELATIONSHIP_CHOICES,
    )
    review_status = models.CharField(
        max_length=50, choices=REVIEW_STATUS_CHOICES, default='PENDING',
    )
    operative_contribution = models.ForeignKey(
        'Contribution', on_delete=models.PROTECT,
        related_name='amendment_operative_for',
        null=True, blank=True,
        help_text='Which contribution is currently operative for totals',
    )
    financial_disposition = models.CharField(
        max_length=50, choices=DISPOSITION_CHOICES, default='ORIGINAL',
    )
    recurrence_disposition = models.CharField(
        max_length=50, choices=DISPOSITION_CHOICES, default='ORIGINAL',
    )
    reviewed_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    explanation = models.TextField(blank=True)
    audit_event = models.ForeignKey(
        'AuditEvent', on_delete=models.SET_NULL, null=True, blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(original_contribution=models.F('replacement_contribution')),
                name='amendment_not_self_referential',
            ),
            models.UniqueConstraint(
                fields=['original_contribution', 'replacement_contribution'],
                name='unique_amendment_pair',
            ),
        ]

    def __str__(self):
        return (
            f"Amendment: Contribution {self.original_contribution_id} → "
            f"{self.replacement_contribution_id} ({self.review_status})"
        )

class Location(models.Model):
    PRECISION_CHOICES = [
        ('STREET', 'Full Street Address'),
        ('CITY_ZIP', 'City and ZIP Locality'),
        ('ZIP_ONLY', 'ZIP Only'),
        ('INFERRED_COUNTY', 'County Inferred'),
        ('VOTER_CONFIRMED', 'Voter Confirmed Address'),
        ('MANUAL', 'Manually Assigned Geography'),
    ]

    CONFIDENCE_CHOICES = [
        ('HIGH', 'High'),
        ('MEDIUM', 'Medium'),
        ('LOW', 'Low'),
    ]

    STATUS_CHOICES = [
        ('CURRENT', 'Current'),
        ('HISTORICAL', 'Historical'),
    ]

    contributor_profile = models.ForeignKey(ContributionCluster, on_delete=models.CASCADE, related_name='locations', null=True, blank=True)
    street_address = models.TextField(blank=True, null=True)
    city = models.CharField(max_length=100)
    state = models.CharField(max_length=50)
    zip = models.CharField(max_length=20)
    county = models.CharField(max_length=100, blank=True, null=True)
    precision_level = models.CharField(max_length=50, choices=PRECISION_CHOICES)
    confidence = models.CharField(max_length=50, choices=CONFIDENCE_CHOICES)
    effective_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='CURRENT')
    is_observed = models.BooleanField(default=True)
    is_inferred = models.BooleanField(default=False)
    is_manual = models.BooleanField(default=False)

    # Geography Stage 2A Cache Fields
    matched_place = models.ForeignKey('GeographicPlace', on_delete=models.SET_NULL, null=True, blank=True, related_name='locations')
    matched_postal_area = models.ForeignKey('PostalArea', on_delete=models.SET_NULL, null=True, blank=True, related_name='locations')
    matched_county = models.ForeignKey('County', on_delete=models.SET_NULL, null=True, blank=True, related_name='locations')
    geography_dataset = models.ForeignKey('GeographyDataset', on_delete=models.SET_NULL, null=True, blank=True, related_name='locations')
    match_method = models.CharField(max_length=50, blank=True, null=True)
    geo_confidence = models.CharField(max_length=50, blank=True, null=True)
    geo_ambiguity_status = models.CharField(max_length=50, default='UNRESOLVED')
    geo_explanation = models.TextField(blank=True, null=True)

    def __str__(self):
        return f"Loc: {self.city}, {self.state} {self.zip} ({self.precision_level})"

class ContactPoint(models.Model):
    TYPE_CHOICES = [
        ('EMAIL', 'Email Address'),
        ('PHONE', 'Phone Number'),
    ]

    STATUS_CHOICES = [
        ('CURRENT', 'Current'),
        ('HISTORICAL', 'Historical'),
    ]

    contributor_profile = models.ForeignKey(ContributionCluster, on_delete=models.CASCADE, related_name='contact_points')
    type = models.CharField(max_length=50, choices=TYPE_CHOICES)
    value = models.CharField(max_length=255)
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='CURRENT')

    def __str__(self):
        return f"Contact: {self.type} - {self.value}"

class FieldAssertion(models.Model):
    CONFIDENCE_CHOICES = [
        ('HIGH', 'High'),
        ('MEDIUM', 'Medium'),
        ('LOW', 'Low'),
    ]

    STATUS_CHOICES = [
        ('ACCEPTED', 'Accepted'),
        ('REJECTED', 'Rejected'),
        ('SUPERSEDED', 'Superseded'),
        ('PREFERRED', 'Preferred'),
    ]

    field_name = models.CharField(max_length=100)
    value = models.TextField()
    confidence = models.CharField(max_length=50, choices=CONFIDENCE_CHOICES, default='MEDIUM')
    effective_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='PREFERRED')

    def __str__(self):
        return f"Assertion: {self.field_name}={self.value} ({self.status})"

class SourceRecordLink(models.Model):
    target_model_name = models.CharField(max_length=100)
    target_record_id = models.IntegerField()
    source_model_name = models.CharField(max_length=100)
    source_record_id = models.IntegerField()

    def __str__(self):
        return f"Link: {self.source_model_name}#{self.source_record_id} -> {self.target_model_name}#{self.target_record_id}"

class MergeDecision(models.Model):
    source_cluster = models.ForeignKey(ContributionCluster, on_delete=models.CASCADE, related_name='merged_out_decisions')
    target_cluster = models.ForeignKey(ContributionCluster, on_delete=models.CASCADE, related_name='merged_in_decisions')
    merge_date = models.DateTimeField(auto_now_add=True)
    merged_by = models.CharField(max_length=150)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        active_str = "Active" if self.is_active else "Inactive"
        return f"Merge: Cluster {self.source_cluster_id} into {self.target_cluster_id} ({active_str})"

class MatchDecision(models.Model):
    DECISION_CHOICES = [
        ('MATCHED', 'Matched'),
        ('REJECTED', 'Rejected'),
    ]

    contribution_cluster = models.ForeignKey(ContributionCluster, on_delete=models.CASCADE, related_name='match_decisions')
    voter_record_id = models.IntegerField()
    decision = models.CharField(max_length=50, choices=DECISION_CHOICES, default='MATCHED')
    confidence = models.CharField(max_length=50)
    notes = models.TextField(blank=True)

    def __str__(self):
        return f"Match: Cluster {self.contribution_cluster_id} to Voter {self.voter_record_id} ({self.decision})"

class AuditEvent(models.Model):
    event_type = models.CharField(max_length=100)
    description = models.TextField()
    actor = models.CharField(max_length=150)
    timestamp = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"[{self.timestamp.strftime('%Y-%m-%d %H:%M')}] {self.event_type} by {self.actor}"

class DatasetCoverageMetadata(models.Model):
    STATUS_CHOICES = [
        ('UNKNOWN', 'Unknown'),
        ('PARTIAL', 'Partial'),
        ('APPARENTLY_CONTINUOUS', 'Apparently Continuous'),
        ('CONFIRMED_COMPLETE', 'Confirmed Complete'),
        ('STALE', 'Stale'),
        ('MIXED_COVERAGE', 'Mixed Coverage'),
    ]

    coverage_start_date = models.DateField()
    coverage_end_date = models.DateField()
    coverage_complete_through = models.DateField()
    coverage_scope = models.CharField(max_length=255, default='SOS Statewide')
    coverage_status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='UNKNOWN')
    continuity_status = models.CharField(max_length=100, blank=True)
    source_obtained_date = models.DateField()
    coverage_confirmed_by = models.CharField(max_length=150, blank=True)
    coverage_confirmation_date = models.DateField(null=True, blank=True)
    coverage_notes = models.TextField(blank=True)

    def __str__(self):
        return f"Coverage: {self.coverage_start_date} to {self.coverage_end_date} ({self.coverage_status})"

class ImportAttempt(models.Model):
    import_batch = models.ForeignKey(ImportBatch, on_delete=models.CASCADE, related_name='attempts')
    attempted_at = models.DateTimeField(auto_now_add=True)
    attempted_by = models.CharField(max_length=150)
    action = models.CharField(max_length=50) # 'INITIAL', 'REPROCESS_OVERRIDE'
    notes = models.TextField(null=True, blank=True)

    def __str__(self):
        return f"Attempt {self.id} on Batch {self.import_batch_id} - {self.action}"

class GeographyDataset(models.Model):
    STATUS_CHOICES = [
        ('PENDING', 'Pending'),
        ('ACTIVE', 'Active'),
        ('SUPERSEDED', 'Superseded'),
        ('FAILED', 'Failed'),
        ('ROLLED_BACK', 'Rolled Back'),
    ]

    name = models.CharField(max_length=255)
    dataset_type = models.CharField(max_length=50)
    source_organization = models.CharField(max_length=255, blank=True)
    source_description = models.TextField(blank=True)
    version = models.CharField(max_length=50)
    effective_date = models.DateField(null=True, blank=True)
    obtained_date = models.DateField(null=True, blank=True)
    file_name = models.CharField(max_length=255)
    file_hash = models.CharField(max_length=64)
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='PENDING', db_index=True)
    authority_level = models.IntegerField(default=100)
    resolver_priority = models.IntegerField(default=100)
    notes = models.TextField(blank=True)
    imported_by = models.CharField(max_length=150)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.name} (v{self.version} - {self.status})"

class GeographyMappingProfile(models.Model):
    name = models.CharField(max_length=255)
    source_type = models.CharField(max_length=50)
    version = models.CharField(max_length=50, default='1.0')
    mapping_rules = models.JSONField(help_text="Header mappings")
    normalization_rules = models.JSONField(default=dict, blank=True)
    validation_rules = models.JSONField(default=dict, blank=True)
    owner = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.name} (v{self.version})"

class GeographyImportBatch(models.Model):
    STATUS_CHOICES = [
        ('PENDING', 'Pending'),
        ('VALIDATING', 'Validating'),
        ('COMPLETED', 'Completed'),
        ('FAILED', 'Failed'),
        ('ROLLED_BACK', 'Rolled Back'),
    ]

    dataset = models.ForeignKey(GeographyDataset, on_delete=models.CASCADE, related_name='import_batches')
    file_name = models.CharField(max_length=255)
    file_hash = models.CharField(max_length=64, unique=True)
    import_type = models.CharField(max_length=50)
    mapping_profile_version = models.CharField(max_length=50, blank=True)
    row_count = models.IntegerField(default=0)
    successful_rows = models.IntegerField(default=0)
    warning_rows = models.IntegerField(default=0)
    failed_rows = models.IntegerField(default=0)
    duplicate_rows = models.IntegerField(default=0)
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='PENDING', db_index=True)
    actor = models.CharField(max_length=150)
    started_time = models.DateTimeField(auto_now_add=True)
    completed_time = models.DateTimeField(null=True, blank=True)
    rollback_state = models.CharField(max_length=50, default='ACTIVE')
    error_summary = models.TextField(null=True, blank=True)

    def __str__(self):
        return f"GeoBatch: {self.file_name} ({self.status})"

class RawGeographyRecord(models.Model):
    import_batch = models.ForeignKey(GeographyImportBatch, on_delete=models.CASCADE, related_name='raw_records')
    row_number = models.IntegerField()
    original_values = models.JSONField()
    raw_row_hash = models.CharField(max_length=64, db_index=True)
    validation_status = models.CharField(max_length=50, default='UNPROCESSED')
    validation_errors = models.TextField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=['import_batch', 'validation_status'], name='idx_raw_geo_batch_status'),
        ]

    def __str__(self):
        return f"RawGeo: Batch {self.import_batch_id} Row {self.row_number}"

class County(models.Model):
    state_code = models.CharField(max_length=2, default='CA')
    normalized_name = models.CharField(max_length=100, db_index=True)
    display_name = models.CharField(max_length=100)
    is_active = models.BooleanField(default=True, db_index=True)

    class Meta:
        verbose_name_plural = "counties"

    def __str__(self):
        return f"{self.display_name}, {self.state_code}"

class GeographicPlace(models.Model):
    state_code = models.CharField(max_length=2, default='CA')
    canonical_name = models.CharField(max_length=100)
    normalized_name = models.CharField(max_length=100, db_index=True)
    general_category = models.CharField(max_length=50)
    is_active = models.BooleanField(default=True, db_index=True)

    def __str__(self):
        return f"{self.canonical_name} ({self.general_category}), {self.state_code}"

class PostalArea(models.Model):
    postal_code = models.CharField(max_length=20, db_index=True)
    postal_area_type = models.CharField(max_length=50)
    is_active = models.BooleanField(default=True, db_index=True)

    def __str__(self):
        return f"{self.postal_code} ({self.postal_area_type})"

class CountySourceRecord(models.Model):
    county = models.ForeignKey(County, on_delete=models.CASCADE, related_name='source_records')
    dataset = models.ForeignKey(GeographyDataset, on_delete=models.CASCADE)
    import_batch = models.ForeignKey(GeographyImportBatch, on_delete=models.CASCADE)
    raw_record = models.ForeignKey(RawGeographyRecord, on_delete=models.SET_NULL, null=True, blank=True)
    source_id = models.CharField(max_length=100, blank=True)
    source_name = models.CharField(max_length=100)
    state_fips = models.CharField(max_length=2, blank=True)
    county_fips_component = models.CharField(max_length=3, blank=True)
    county_geoid = models.CharField(max_length=5, blank=True)
    ansi_code = models.CharField(max_length=50, blank=True)
    gnis_id = models.CharField(max_length=50, blank=True)
    effective_date = models.DateField(null=True, blank=True)
    expiration_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=50, default='ACTIVE')

    def __str__(self):
        return f"CountySource: {self.source_name} in Dataset {self.dataset_id}"

class PlaceSourceRecord(models.Model):
    place = models.ForeignKey(GeographicPlace, on_delete=models.CASCADE, related_name='source_records')
    dataset = models.ForeignKey(GeographyDataset, on_delete=models.CASCADE)
    import_batch = models.ForeignKey(GeographyImportBatch, on_delete=models.CASCADE)
    raw_record = models.ForeignKey(RawGeographyRecord, on_delete=models.SET_NULL, null=True, blank=True)
    source_id = models.CharField(max_length=100, blank=True)
    source_name = models.CharField(max_length=100)
    place_type = models.CharField(max_length=100, blank=True)
    state_fips = models.CharField(max_length=2, blank=True)
    place_fips_component = models.CharField(max_length=5, blank=True)
    place_geoid = models.CharField(max_length=7, blank=True)
    ansi_code = models.CharField(max_length=50, blank=True)
    gnis_id = models.CharField(max_length=50, blank=True)
    effective_date = models.DateField(null=True, blank=True)
    expiration_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=50, default='ACTIVE')

    def __str__(self):
        return f"PlaceSource: {self.source_name} in Dataset {self.dataset_id}"

class PostalAreaSourceRecord(models.Model):
    postal_area = models.ForeignKey(PostalArea, on_delete=models.CASCADE, related_name='source_records')
    dataset = models.ForeignKey(GeographyDataset, on_delete=models.CASCADE)
    import_batch = models.ForeignKey(GeographyImportBatch, on_delete=models.CASCADE)
    raw_record = models.ForeignKey(RawGeographyRecord, on_delete=models.SET_NULL, null=True, blank=True)
    source_code = models.CharField(max_length=20)
    source_type = models.CharField(max_length=50)
    display_name = models.CharField(max_length=100, blank=True)
    effective_date = models.DateField(null=True, blank=True)
    expiration_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=50, default='ACTIVE')

    def __str__(self):
        return f"PostalSource: {self.source_code} in Dataset {self.dataset_id}"

class GeographyIdentifier(models.Model):
    SCHEME_CHOICES = [
        ('STATE_FIPS', 'State FIPS'),
        ('COUNTY_FIPS_COMPONENT', 'County FIPS Component'),
        ('COUNTY_GEOID', 'County GEOID'),
        ('PLACE_FIPS_COMPONENT', 'Place FIPS Component'),
        ('PLACE_GEOID', 'Place GEOID'),
        ('ANSI', 'ANSI'),
        ('GNIS', 'GNIS'),
        ('USPS_ZIP', 'USPS ZIP'),
        ('CENSUS_ZCTA', 'Census ZCTA'),
        ('SOURCE_SPECIFIC', 'Source Specific'),
    ]

    county_target = models.ForeignKey(County, on_delete=models.CASCADE, null=True, blank=True, related_name='identifiers')
    place_target = models.ForeignKey(GeographicPlace, on_delete=models.CASCADE, null=True, blank=True, related_name='identifiers')
    postal_target = models.ForeignKey(PostalArea, on_delete=models.CASCADE, null=True, blank=True, related_name='identifiers')
    scheme = models.CharField(max_length=50, choices=SCHEME_CHOICES)
    component_designation = models.CharField(max_length=50, blank=True)
    value = models.CharField(max_length=100, db_index=True)
    issuing_authority = models.CharField(max_length=100, blank=True)
    dataset = models.ForeignKey(GeographyDataset, on_delete=models.CASCADE)
    import_batch = models.ForeignKey(GeographyImportBatch, on_delete=models.CASCADE)
    effective_date = models.DateField(null=True, blank=True)
    expiration_date = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True, db_index=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    (models.Q(county_target__isnull=False) & models.Q(place_target__isnull=True) & models.Q(postal_target__isnull=True)) |
                    (models.Q(county_target__isnull=True) & models.Q(place_target__isnull=False) & models.Q(postal_target__isnull=True)) |
                    (models.Q(county_target__isnull=True) & models.Q(place_target__isnull=True) & models.Q(postal_target__isnull=False))
                ),
                name="exactly_one_target_identifier"
            )
        ]

    def __str__(self):
        return f"Identifier: {self.scheme}={self.value}"

class PlaceCountyAssociation(models.Model):
    BASIS_CHOICES = [
        ('POPULATION', 'Population'),
        ('RESIDENTIAL_ADDRESS', 'Residential Address'),
        ('TOTAL_ADDRESS', 'Total Address'),
        ('HOUSING_UNIT', 'Housing Unit'),
        ('LAND_AREA', 'Land Area'),
        ('SOURCE_DEFINED', 'Source Defined'),
        ('UNKNOWN', 'Unknown'),
    ]

    place = models.ForeignKey(GeographicPlace, on_delete=models.CASCADE, related_name='county_associations')
    county = models.ForeignKey(County, on_delete=models.CASCADE, related_name='place_associations')
    relationship_type = models.CharField(max_length=50)
    confidence = models.CharField(max_length=50)
    effective_date = models.DateField(null=True, blank=True)
    expiration_date = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True, db_index=True)
    dataset = models.ForeignKey(GeographyDataset, on_delete=models.CASCADE)
    import_batch = models.ForeignKey(GeographyImportBatch, on_delete=models.CASCADE)
    raw_record = models.ForeignKey(RawGeographyRecord, on_delete=models.SET_NULL, null=True, blank=True)
    weight_value = models.DecimalField(max_digits=5, decimal_places=4, null=True, blank=True)
    normalized_weight_value = models.DecimalField(max_digits=5, decimal_places=4, null=True, blank=True)
    weight_basis = models.CharField(max_length=50, choices=BASIS_CHOICES, default='UNKNOWN')
    weight_unit = models.CharField(max_length=50, blank=True)
    raw_weight_value = models.CharField(max_length=100, blank=True)
    normalization_rule = models.CharField(max_length=255, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['place', 'county', 'dataset'],
                condition=models.Q(is_active=True),
                name='unique_active_place_county_association'
            ),
            models.CheckConstraint(
                condition=models.Q(normalized_weight_value__isnull=True) | (models.Q(normalized_weight_value__gte=0.0) & models.Q(normalized_weight_value__lte=1.0)),
                name='range_place_county_normalized_weight'
            )
        ]

    def __str__(self):
        return f"PlaceCounty: {self.place.canonical_name} -> {self.county.display_name}"

class PostalCountyAssociation(models.Model):
    BASIS_CHOICES = [
        ('POPULATION', 'Population'),
        ('RESIDENTIAL_ADDRESS', 'Residential Address'),
        ('TOTAL_ADDRESS', 'Total Address'),
        ('HOUSING_UNIT', 'Housing Unit'),
        ('LAND_AREA', 'Land Area'),
        ('SOURCE_DEFINED', 'Source Defined'),
        ('UNKNOWN', 'Unknown'),
    ]

    postal_area = models.ForeignKey(PostalArea, on_delete=models.CASCADE, related_name='county_associations')
    county = models.ForeignKey(County, on_delete=models.CASCADE, related_name='postal_associations')
    relationship_type = models.CharField(max_length=50)
    confidence = models.CharField(max_length=50)
    effective_date = models.DateField(null=True, blank=True)
    expiration_date = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True, db_index=True)
    dataset = models.ForeignKey(GeographyDataset, on_delete=models.CASCADE)
    import_batch = models.ForeignKey(GeographyImportBatch, on_delete=models.CASCADE)
    raw_record = models.ForeignKey(RawGeographyRecord, on_delete=models.SET_NULL, null=True, blank=True)
    weight_value = models.DecimalField(max_digits=5, decimal_places=4, null=True, blank=True)
    normalized_weight_value = models.DecimalField(max_digits=5, decimal_places=4, null=True, blank=True)
    weight_basis = models.CharField(max_length=50, choices=BASIS_CHOICES, default='UNKNOWN')
    weight_unit = models.CharField(max_length=50, blank=True)
    raw_weight_value = models.CharField(max_length=100, blank=True)
    normalization_rule = models.CharField(max_length=255, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['postal_area', 'county', 'dataset'],
                condition=models.Q(is_active=True),
                name='unique_active_postal_county_association'
            ),
            models.CheckConstraint(
                condition=models.Q(normalized_weight_value__isnull=True) | (models.Q(normalized_weight_value__gte=0.0) & models.Q(normalized_weight_value__lte=1.0)),
                name='range_postal_county_normalized_weight'
            )
        ]

    def __str__(self):
        return f"PostalCounty: {self.postal_area.postal_code} -> {self.county.display_name}"

class PostalPlaceAssociation(models.Model):
    BASIS_CHOICES = [
        ('POPULATION', 'Population'),
        ('RESIDENTIAL_ADDRESS', 'Residential Address'),
        ('TOTAL_ADDRESS', 'Total Address'),
        ('HOUSING_UNIT', 'Housing Unit'),
        ('LAND_AREA', 'Land Area'),
        ('SOURCE_DEFINED', 'Source Defined'),
        ('UNKNOWN', 'Unknown'),
    ]

    postal_area = models.ForeignKey(PostalArea, on_delete=models.CASCADE, related_name='place_associations')
    place = models.ForeignKey(GeographicPlace, on_delete=models.CASCADE, related_name='postal_associations')
    relationship_type = models.CharField(max_length=50)
    confidence = models.CharField(max_length=50)
    effective_date = models.DateField(null=True, blank=True)
    expiration_date = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True, db_index=True)
    dataset = models.ForeignKey(GeographyDataset, on_delete=models.CASCADE)
    import_batch = models.ForeignKey(GeographyImportBatch, on_delete=models.CASCADE)
    raw_record = models.ForeignKey(RawGeographyRecord, on_delete=models.SET_NULL, null=True, blank=True)
    weight_value = models.DecimalField(max_digits=5, decimal_places=4, null=True, blank=True)
    normalized_weight_value = models.DecimalField(max_digits=5, decimal_places=4, null=True, blank=True)
    weight_basis = models.CharField(max_length=50, choices=BASIS_CHOICES, default='UNKNOWN')
    weight_unit = models.CharField(max_length=50, blank=True)
    raw_weight_value = models.CharField(max_length=100, blank=True)
    normalization_rule = models.CharField(max_length=255, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['postal_area', 'place', 'dataset'],
                condition=models.Q(is_active=True),
                name='unique_active_postal_place_association'
            ),
            models.CheckConstraint(
                condition=models.Q(normalized_weight_value__isnull=True) | (models.Q(normalized_weight_value__gte=0.0) & models.Q(normalized_weight_value__lte=1.0)),
                name='range_postal_place_normalized_weight'
            )
        ]

    def __str__(self):
        return f"PostalPlace: {self.postal_area.postal_code} -> {self.place.canonical_name}"

class GeographyAlias(models.Model):
    SCHEME_CHOICES = [
        ('OFFICIAL_ALTERNATE', 'Official Alternate'),
        ('COMMON_NAME', 'Common Name'),
        ('HISTORICAL_NAME', 'Historical Name'),
        ('POSTAL_CITY', 'Postal City Label'),
        ('ABBREVIATION', 'Abbreviation'),
        ('SOURCE_SPECIFIC', 'Source Specific'),
    ]

    alias_type = models.CharField(max_length=50, choices=SCHEME_CHOICES)
    original_alias = models.CharField(max_length=255)
    normalized_alias = models.CharField(max_length=255, db_index=True)
    county_target = models.ForeignKey(County, on_delete=models.CASCADE, null=True, blank=True, related_name='aliases')
    place_target = models.ForeignKey(GeographicPlace, on_delete=models.CASCADE, null=True, blank=True, related_name='aliases')
    postal_target = models.ForeignKey(PostalArea, on_delete=models.CASCADE, null=True, blank=True, related_name='aliases')
    dataset = models.ForeignKey(GeographyDataset, on_delete=models.CASCADE)
    import_batch = models.ForeignKey(GeographyImportBatch, on_delete=models.CASCADE)
    raw_record = models.ForeignKey(RawGeographyRecord, on_delete=models.SET_NULL, null=True, blank=True)
    source_description = models.CharField(max_length=255, blank=True)
    effective_date = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True, db_index=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    (models.Q(county_target__isnull=False) & models.Q(place_target__isnull=True) & models.Q(postal_target__isnull=True)) |
                    (models.Q(county_target__isnull=True) & models.Q(place_target__isnull=False) & models.Q(postal_target__isnull=True)) |
                    (models.Q(county_target__isnull=True) & models.Q(place_target__isnull=True) & models.Q(postal_target__isnull=False))
                ),
                name="exactly_one_target_alias"
            )
        ]

    def __str__(self):
        return f"Alias: {self.original_alias} -> Type {self.alias_type}"

class GeographyResolutionRun(models.Model):
    TRIGGER_CHOICES = [
        ('POST_CONTRIBUTION_IMPORT', 'Post Contribution Import'),
        ('DATASET_ACTIVATION', 'Dataset Activation'),
        ('MANUAL_BULK_RESOLUTION', 'Manual Bulk Resolution'),
        ('SINGLE_LOCATION_REVIEW', 'Single Location Review'),
    ]

    trigger_type = models.CharField(max_length=50, choices=TRIGGER_CHOICES)
    resolver_version = models.CharField(max_length=50, default='1.0')
    scope = models.CharField(max_length=255, blank=True)
    actor = models.CharField(max_length=150)
    status = models.CharField(max_length=50, default='PENDING', db_index=True)
    dataset = models.ForeignKey(GeographyDataset, on_delete=models.SET_NULL, null=True, blank=True, related_name='resolution_runs')
    locations_considered = models.IntegerField(default=0)
    resolved_count = models.IntegerField(default=0)
    ambiguous_count = models.IntegerField(default=0)
    conflict_count = models.IntegerField(default=0)
    unmatched_count = models.IntegerField(default=0)
    started_time = models.DateTimeField(auto_now_add=True)
    completed_time = models.DateTimeField(null=True, blank=True)
    error_summary = models.TextField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['dataset', 'scope'],
                condition=models.Q(status='PENDING'),
                name='unique_pending_run_per_dataset_and_scope'
            )
        ]

    def __str__(self):
        return f"ResolutionRun {self.id} ({self.trigger_type} - {self.status})"

class LocationGeographyResolution(models.Model):
    STATUS_CHOICES = [
        ('CURRENT', 'Current'),
        ('SUPERSEDED', 'Superseded'),
        ('REVOKED', 'Revoked'),
    ]

    METHOD_CHOICES = [
        ('UNRESOLVED', 'Unresolved'),
        ('EXACT_PLACE_ZIP_MATCH', 'Exact Place and ZIP Match'),
        ('EXACT_ALIAS_ZIP_MATCH', 'Exact Alias and ZIP Match'),
        ('UNIQUE_ZIP_INFERENCE', 'Unique ZIP Inference'),
        ('AMBIGUOUS_PLACE', 'Ambiguous Place'),
        ('AMBIGUOUS_ALIAS', 'Ambiguous Alias'),
        ('AMBIGUOUS_ZIP', 'Ambiguous ZIP'),
        ('CONFLICTING_SOURCE_VALUES', 'Conflicting Source Values'),
        ('MANUALLY_RESOLVED', 'Manually Resolved'),
        ('NO_REFERENCE_MATCH', 'No Reference Match'),
    ]

    location = models.ForeignKey(Location, on_delete=models.CASCADE, related_name='resolutions')
    resolution_run = models.ForeignKey(GeographyResolutionRun, on_delete=models.CASCADE, related_name='resolutions')
    observed_city = models.CharField(max_length=100)
    observed_state = models.CharField(max_length=50)
    observed_zip = models.CharField(max_length=20)
    matched_canonical_county = models.ForeignKey(County, on_delete=models.SET_NULL, null=True, blank=True)
    matched_canonical_place = models.ForeignKey(GeographicPlace, on_delete=models.SET_NULL, null=True, blank=True)
    matched_postal_area = models.ForeignKey(PostalArea, on_delete=models.SET_NULL, null=True, blank=True)
    match_method = models.CharField(max_length=50, choices=METHOD_CHOICES)
    confidence = models.CharField(max_length=50)
    explanation = models.TextField(blank=True)
    origin = models.CharField(max_length=50, default='AUTOMATIC')
    actor = models.CharField(max_length=150, blank=True)
    created_time = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='CURRENT', db_index=True)
    superseded_resolution = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True, related_name='supersedes')

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['location'],
                condition=models.Q(status='CURRENT'),
                name='unique_current_resolution_per_location'
            )
        ]

    def __str__(self):
        return f"Resolution for Loc {self.location_id} ({self.match_method} - {self.status})"

class GeographyResolutionCandidate(models.Model):
    STATUS_CHOICES = [
        ('ACCEPTED', 'Accepted'),
        ('REJECTED', 'Rejected'),
        ('PENDING', 'Pending'),
    ]

    location_resolution = models.ForeignKey(LocationGeographyResolution, on_delete=models.CASCADE, related_name='candidates')
    candidate_county = models.ForeignKey(County, on_delete=models.CASCADE, null=True, blank=True)
    candidate_place = models.ForeignKey(GeographicPlace, on_delete=models.CASCADE, null=True, blank=True)
    candidate_postal_area = models.ForeignKey(PostalArea, on_delete=models.CASCADE, null=True, blank=True)
    supporting_rule = models.CharField(max_length=255, blank=True)
    dataset = models.ForeignKey(GeographyDataset, on_delete=models.CASCADE)
    confidence = models.CharField(max_length=50)
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='PENDING')
    explanation = models.TextField(blank=True)

    def __str__(self):
        return f"Candidate for Res {self.location_resolution_id}"


class Chapter(models.Model):
    STATUS_CHOICES = [
        ('DRAFT', 'Draft'),
        ('ACTIVE', 'Active'),
        ('INACTIVE', 'Inactive'),
        ('ARCHIVED', 'Archived'),
    ]

    name = models.CharField(max_length=255, unique=True)
    slug = models.SlugField(max_length=255, unique=True)
    short_name = models.CharField(max_length=50, blank=True)
    description = models.TextField(blank=True)
    state_code = models.CharField(max_length=2, default='CA')
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='DRAFT')
    effective_date = models.DateField(null=True, blank=True)
    retired_date = models.DateField(null=True, blank=True)
    created_by = models.CharField(max_length=150)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    current_evaluation_run = models.ForeignKey(
        'ChapterEvaluationRun', null=True, blank=True, on_delete=models.SET_NULL, related_name='+'
    )

    def __str__(self):
        return f"{self.name} ({self.status})"


class ChapterRuleSet(models.Model):
    STATUS_CHOICES = [
        ('DRAFT', 'Draft'),
        ('VALIDATING', 'Validating'),
        ('ACTIVE', 'Active'),
        ('SUPERSEDED', 'Superseded'),
        ('RETIRED', 'Retired'),
        ('INVALID', 'Invalid'),
    ]

    chapter = models.ForeignKey(Chapter, on_delete=models.CASCADE, related_name='rule_sets')
    version = models.IntegerField(default=1)
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='DRAFT')
    description = models.TextField(blank=True)
    include_match_mode = models.CharField(max_length=50, default='ANY')
    created_by = models.CharField(max_length=150)
    created_at = models.DateTimeField(auto_now_add=True)
    activated_by = models.CharField(max_length=150, null=True, blank=True)
    activated_at = models.DateTimeField(null=True, blank=True)
    superseded_at = models.DateTimeField(null=True, blank=True)
    validation_summary = models.TextField(blank=True, null=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["chapter", "version"],
                name="unique_ruleset_version_per_chapter"
            ),
            models.UniqueConstraint(
                fields=["chapter"],
                condition=models.Q(status="ACTIVE"),
                name="unique_active_ruleset_per_chapter"
            )
        ]

    def __str__(self):
        return f"{self.chapter.name} Ruleset v{self.version} ({self.status})"


class ChapterRule(models.Model):
    EFFECT_CHOICES = [
        ('INCLUDE', 'Include'),
        ('EXCLUDE', 'Exclude'),
    ]

    TARGET_CHOICES = [
        ('COUNTY', 'County'),
        ('PLACE', 'Place'),
        ('POSTAL_AREA', 'Postal Area'),
    ]

    rule_set = models.ForeignKey(ChapterRuleSet, on_delete=models.CASCADE, related_name='rules')
    effect = models.CharField(max_length=50, choices=EFFECT_CHOICES)
    target_type = models.CharField(max_length=50, choices=TARGET_CHOICES)
    county = models.ForeignKey(County, null=True, blank=True, on_delete=models.CASCADE)
    place = models.ForeignKey(GeographicPlace, null=True, blank=True, on_delete=models.CASCADE)
    postal_area = models.ForeignKey(PostalArea, null=True, blank=True, on_delete=models.CASCADE)
    description = models.TextField(blank=True)
    display_order = models.IntegerField(default=10)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    (models.Q(county__isnull=False) & models.Q(place__isnull=True) & models.Q(postal_area__isnull=True)) |
                    (models.Q(county__isnull=True) & models.Q(place__isnull=False) & models.Q(postal_area__isnull=True)) |
                    (models.Q(county__isnull=True) & models.Q(place__isnull=True) & models.Q(postal_area__isnull=False))
                ),
                name="check_target_mutually_exclusive"
            ),
            models.CheckConstraint(
                condition=~models.Q(target_type="COUNTY") | (models.Q(county__isnull=False) & models.Q(place__isnull=True) & models.Q(postal_area__isnull=True)),
                name="check_target_type_county"
            ),
            models.CheckConstraint(
                condition=~models.Q(target_type="PLACE") | (models.Q(county__isnull=True) & models.Q(place__isnull=False) & models.Q(postal_area__isnull=True)),
                name="check_target_type_place"
            ),
            models.CheckConstraint(
                condition=~models.Q(target_type="POSTAL_AREA") | (models.Q(county__isnull=True) & models.Q(place__isnull=True) & models.Q(postal_area__isnull=False)),
                name="check_target_type_postal"
            ),
            models.UniqueConstraint(
                fields=["rule_set", "effect", "county"],
                condition=models.Q(is_active=True, target_type="COUNTY"),
                name="unique_active_county_rule"
            ),
            models.UniqueConstraint(
                fields=["rule_set", "effect", "place"],
                condition=models.Q(is_active=True, target_type="PLACE"),
                name="unique_active_place_rule"
            ),
            models.UniqueConstraint(
                fields=["rule_set", "effect", "postal_area"],
                condition=models.Q(is_active=True, target_type="POSTAL_AREA"),
                name="unique_active_postal_rule"
            )
        ]

    def __str__(self):
        target = self.county or self.place or self.postal_area
        return f"{self.effect} {self.target_type}: {target} (Ruleset v{self.rule_set.version})"


class ChapterEntityOverride(models.Model):
    OVERRIDE_CHOICES = [
        ('INCLUDE', 'Include'),
        ('EXCLUDE', 'Exclude'),
    ]

    STATUS_CHOICES = [
        ('ACTIVE', 'Active'),
        ('EXPIRED', 'Expired'),
        ('SUPERSEDED', 'Superseded'),
        ('REVOKED', 'Revoked'),
    ]

    chapter = models.ForeignKey(Chapter, on_delete=models.CASCADE, related_name='overrides')
    contributor_entity = models.ForeignKey(ContributorEntity, on_delete=models.CASCADE, related_name='chapter_overrides')
    override_type = models.CharField(max_length=50, choices=OVERRIDE_CHOICES)
    reason = models.TextField()
    effective_date = models.DateField()
    expiration_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='ACTIVE')
    created_by = models.CharField(max_length=150)
    created_at = models.DateTimeField(auto_now_add=True)
    superseded_by = models.ForeignKey('self', null=True, blank=True, on_delete=models.SET_NULL, related_name='supersedes')

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(expiration_date__isnull=True) | models.Q(expiration_date__gte=models.F('effective_date')),
                name="check_override_expiration"
            ),
            models.UniqueConstraint(
                fields=["chapter", "contributor_entity"],
                condition=models.Q(status="ACTIVE"),
                name="unique_active_override_per_chapter_and_entity"
            )
        ]

    def __str__(self):
        return f"{self.override_type} Override for Entity {self.contributor_entity_id} on {self.chapter.name} ({self.status})"


class ChapterEvaluationRun(models.Model):
    MODE_CHOICES = [
        ('PREVIEW', 'Preview'),
        ('APPLY', 'Apply'),
    ]

    TRIGGER_CHOICES = [
        ('RULE_SET_ACTIVATION', 'Rule Set Activation'),
        ('MANUAL_FULL_EVALUATION', 'Manual Full Evaluation'),
        ('GEOGRAPHY_RESOLUTION_UPDATE', 'Geography Resolution Update'),
        ('ENTITY_REEVALUATION', 'Entity Re-evaluation'),
        ('OVERRIDE_CHANGE', 'Override Change'),
    ]

    STATUS_CHOICES = [
        ('PENDING', 'Pending'),
        ('RUNNING', 'Running'),
        ('COMPLETED', 'Completed'),
        ('FAILED', 'Failed'),
        ('CANCELLED', 'Cancelled'),
        ('SUPERSEDED', 'Superseded'),
    ]

    chapter = models.ForeignKey(Chapter, on_delete=models.CASCADE, related_name='evaluation_runs')
    rule_set = models.ForeignKey(ChapterRuleSet, on_delete=models.CASCADE, related_name='evaluation_runs')
    run_mode = models.CharField(max_length=50, choices=MODE_CHOICES)
    trigger_type = models.CharField(max_length=50, choices=TRIGGER_CHOICES)
    geography_dataset_snapshot = models.JSONField()
    resolver_version = models.CharField(max_length=50)
    evaluation_engine_version = models.CharField(max_length=50)
    membership_snapshot_date = models.DateField()
    scope = models.CharField(max_length=255)
    actor = models.CharField(max_length=150)
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='PENDING')
    entities_considered = models.IntegerField(default=0)
    included_count = models.IntegerField(default=0)
    excluded_count = models.IntegerField(default=0)
    ambiguous_count = models.IntegerField(default=0)
    unresolved_count = models.IntegerField(default=0)
    overlap_count = models.IntegerField(default=0)
    error_count = models.IntegerField(default=0)
    started_time = models.DateTimeField(null=True, blank=True)
    completed_time = models.DateTimeField(null=True, blank=True)
    error_summary = models.TextField(blank=True, null=True)
    retry_of = models.ForeignKey('self', null=True, blank=True, on_delete=models.SET_NULL, related_name='retries')

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["chapter", "rule_set", "run_mode", "scope"],
                condition=models.Q(status="PENDING"),
                name="unique_pending_run_proposal"
            )
        ]

    def __str__(self):
        return f"Run {self.id} for {self.chapter.name} ({self.run_mode} - {self.status})"


class ChapterEvaluationLocationSelection(models.Model):
    STATUS_CHOICES = [
        ('SELECTED', 'Selected'),
        ('MULTIPLE_EQUIVALENT', 'Multiple Equivalent'),
        ('AMBIGUOUS_LOCATION', 'Ambiguous Location'),
        ('NO_CURRENT_LOCATION', 'No Current Location'),
        ('NO_CURRENT_RESOLVED_LOCATION', 'No Current Resolved Location'),
        ('INELIGIBLE_ENTITY_TYPE', 'Ineligible Entity Type'),
        ('MANUALLY_SELECTED', 'Manually Selected'),
    ]

    METHOD_CHOICES = [
        ('AUTOMATIC', 'Automatic'),
        ('MANUAL', 'Manual'),
    ]

    evaluation_run = models.ForeignKey(ChapterEvaluationRun, on_delete=models.CASCADE, related_name='location_selections')
    contributor_entity = models.ForeignKey(ContributorEntity, on_delete=models.CASCADE, related_name='chapter_location_selections')
    selected_location = models.ForeignKey(Location, null=True, blank=True, on_delete=models.SET_NULL)
    selected_resolution = models.ForeignKey(LocationGeographyResolution, null=True, blank=True, on_delete=models.SET_NULL)
    selection_status = models.CharField(max_length=50, choices=STATUS_CHOICES)
    selection_method = models.CharField(max_length=50, choices=METHOD_CHOICES, default='AUTOMATIC')
    explanation = models.TextField(blank=True)
    manual_selection = models.BooleanField(default=False)
    actor = models.CharField(max_length=150, null=True, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Location Selection for Entity {self.contributor_entity_id} in Run {self.evaluation_run_id} ({self.selection_status})"


class ChapterEvaluationResult(models.Model):
    RESULT_CHOICES = [
        ('INCLUDED_BY_RULE', 'Included by Rule'),
        ('EXCLUDED_BY_RULE', 'Excluded by Rule'),
        ('MANUALLY_INCLUDED', 'Manually Included'),
        ('MANUALLY_EXCLUDED', 'Manually Excluded'),
        ('PROVISIONAL_GEOGRAPHIC_MATCH', 'Provisional Geographic Match'),
        ('AMBIGUOUS_GEOGRAPHY', 'Ambiguous Geography'),
        ('AMBIGUOUS_LOCATION', 'Ambiguous Location'),
        ('NO_CURRENT_LOCATION', 'No Current Location'),
        ('NO_CURRENT_RESOLVED_LOCATION', 'No Current Resolved Location'),
        ('NO_RULE_MATCH', 'No Rule Match'),
        ('INELIGIBLE_ENTITY_TYPE', 'Ineligible Entity Type'),
        ('ERROR', 'Error'),
    ]

    CONFIDENCE_CHOICES = [
        ('HIGH', 'High'),
        ('MEDIUM', 'Medium'),
        ('LOW', 'Low'),
    ]

    evaluation_run = models.ForeignKey(ChapterEvaluationRun, on_delete=models.CASCADE, related_name='results')
    chapter = models.ForeignKey(Chapter, on_delete=models.CASCADE, related_name='results')
    rule_set = models.ForeignKey(ChapterRuleSet, on_delete=models.CASCADE, related_name='results')
    contributor_entity = models.ForeignKey(ContributorEntity, on_delete=models.CASCADE, related_name='chapter_results')
    location_selection = models.ForeignKey(ChapterEvaluationLocationSelection, on_delete=models.CASCADE, related_name='results')
    selected_location = models.ForeignKey(Location, null=True, blank=True, on_delete=models.SET_NULL)
    selected_resolution = models.ForeignKey(LocationGeographyResolution, null=True, blank=True, on_delete=models.SET_NULL)
    result_status = models.CharField(max_length=50, choices=RESULT_CHOICES)
    confidence = models.CharField(max_length=50, choices=CONFIDENCE_CHOICES)
    explanation = models.TextField(blank=True)
    entity_type_snapshot = models.CharField(max_length=50)
    entity_verification_snapshot = models.BooleanField()
    membership_assessment = models.ForeignKey(MembershipAssessment, on_delete=models.PROTECT, related_name='chapter_results', null=True, blank=True)
    membership_status_snapshot = models.CharField(max_length=50)
    membership_rule_version_snapshot = models.CharField(max_length=50)
    membership_assessment_date_snapshot = models.DateField(null=True, blank=True)
    manual_override = models.ForeignKey(ChapterEntityOverride, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["evaluation_run", "contributor_entity"],
                name="unique_result_per_run_and_entity"
            )
        ]

    def __str__(self):
        return f"Result {self.id} for Entity {self.contributor_entity_id} in Run {self.evaluation_run_id} ({self.result_status})"


class ChapterRuleMatch(models.Model):
    OUTCOME_CHOICES = [
        ('MATCHED_INCLUDE', 'Matched Include'),
        ('MATCHED_EXCLUDE', 'Matched Exclude'),
        ('NOT_MATCHED', 'Not Matched'),
        ('NOT_EVALUATED', 'Not Evaluated'),
        ('AMBIGUOUS', 'Ambiguous'),
    ]

    evaluation_result = models.ForeignKey(ChapterEvaluationResult, on_delete=models.CASCADE, related_name='rule_matches')
    rule = models.ForeignKey(ChapterRule, on_delete=models.CASCADE, related_name='matches', null=True, blank=True)
    match_outcome = models.CharField(max_length=50, choices=OUTCOME_CHOICES)
    matched_county = models.ForeignKey(County, null=True, blank=True, on_delete=models.SET_NULL)
    matched_place = models.ForeignKey(GeographicPlace, null=True, blank=True, on_delete=models.SET_NULL)
    matched_postal_area = models.ForeignKey(PostalArea, null=True, blank=True, on_delete=models.SET_NULL)
    location_resolution = models.ForeignKey(LocationGeographyResolution, null=True, blank=True, on_delete=models.SET_NULL)
    explanation = models.TextField(blank=True)
    confidence = models.CharField(max_length=50)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    (models.Q(matched_county__isnull=True) & models.Q(matched_place__isnull=True) & models.Q(matched_postal_area__isnull=True)) |
                    (models.Q(matched_county__isnull=False) & models.Q(matched_place__isnull=True) & models.Q(matched_postal_area__isnull=True)) |
                    (models.Q(matched_county__isnull=True) & models.Q(matched_place__isnull=False) & models.Q(matched_postal_area__isnull=True)) |
                    (models.Q(matched_county__isnull=True) & models.Q(matched_place__isnull=True) & models.Q(matched_postal_area__isnull=False))
                ),
                name="check_match_target_mutually_exclusive"
            )
        ]

    def __str__(self):
        return f"Match {self.id} for Result {self.evaluation_result_id} ({self.match_outcome})"


class ChapterAssignment(models.Model):
    STATUS_CHOICES = [
        ('INCLUDED', 'Included'),
        ('PROVISIONALLY_INCLUDED', 'Provisionally Included'),
        ('EXCLUDED', 'Excluded'),
        ('AMBIGUOUS', 'Ambiguous'),
        ('UNRESOLVED', 'Unresolved'),
        ('NO_MATCH', 'No Match'),
        ('INELIGIBLE', 'Ineligible'),
    ]

    chapter = models.ForeignKey(Chapter, on_delete=models.CASCADE, related_name='assignments')
    evaluation_run = models.ForeignKey(ChapterEvaluationRun, on_delete=models.CASCADE, related_name='assignments')
    contributor_entity = models.ForeignKey(ContributorEntity, on_delete=models.CASCADE, related_name='chapter_assignments')
    evaluation_result = models.ForeignKey(ChapterEvaluationResult, on_delete=models.CASCADE, related_name='assignments')
    assignment_status = models.CharField(max_length=50, choices=STATUS_CHOICES)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["evaluation_run", "contributor_entity"],
                name="unique_assignment_per_run_and_entity"
            )
        ]

    def __str__(self):
        return f"Assignment for Entity {self.contributor_entity_id} in Run {self.evaluation_run_id} ({self.assignment_status})"
