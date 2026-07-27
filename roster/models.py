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
    file_hash = models.CharField(max_length=64, unique=True)
    file_type = models.CharField(max_length=50)
    import_date = models.DateTimeField(auto_now_add=True)
    imported_by = models.CharField(max_length=150)
    row_count = models.IntegerField(default=0)
    successful_rows = models.IntegerField(default=0)
    failed_rows = models.IntegerField(default=0)
    duplicate_rows = models.IntegerField(default=0)
    mapping_profile = models.ForeignKey(ImportMappingProfile, on_delete=models.SET_NULL, null=True, blank=True)
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='PENDING', db_index=True)

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

    entity_type = models.CharField(max_length=50, choices=ENTITY_TYPE_CHOICES, default='INDIVIDUAL')
    display_name = models.CharField(max_length=255)
    is_verified = models.BooleanField(default=False)
    data_quality_flags = models.JSONField(default=list, blank=True)

    class Meta:
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
        ]

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

    contributor_entity = models.ForeignKey(ContributorEntity, on_delete=models.CASCADE, related_name='membership_assessments')
    calculated_status = models.CharField(max_length=100, choices=STATUS_CHOICES)
    recurring_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    payment_interval = models.CharField(max_length=50, blank=True)
    rule_version = models.ForeignKey(MembershipRuleVersion, on_delete=models.PROTECT)
    calculation_date = models.DateTimeField(auto_now=True)
    manual_override = models.BooleanField(default=False)
    explanation = models.TextField(blank=True)

    def __str__(self):
        return f"Assessment: Entity {self.contributor_entity_id} - {self.calculated_status}"

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
