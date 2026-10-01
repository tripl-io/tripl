-- Final PostgreSQL 18 schema from the complete pre-squash Alembic chain
-- at 19eea9a9 (head a1c3e5f7b9d2), restored into a fresh database.
-- Each section is executed as one statement by the baseline revision.
CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public;
-- tripl:statement
CREATE EXTENSION IF NOT EXISTS unaccent WITH SCHEMA public;
-- tripl:statement
CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;
-- tripl:statement
CREATE TYPE public.alert_delivery_status AS ENUM (
    'pending',
    'sent',
    'failed'
);
-- tripl:statement
CREATE TYPE public.alert_destination_type AS ENUM (
    'slack',
    'telegram',
    'webhook',
    'email',
    'jira',
    'linear',
    'demo_sink'
);
-- tripl:statement
CREATE TYPE public.alert_drift_type AS ENUM (
    'new_field',
    'missing_field',
    'type_changed',
    'enum_violation',
    'required_null_violation',
    'regex_violation',
    'range_violation',
    'distribution_shift',
    'missing',
    'volume_drop',
    'value_drift',
    'source_late',
    'source_overdue',
    'sunset_overdue',
    'successor_silent',
    'new_property',
    'missing_required',
    'type_change'
);
-- tripl:statement
CREATE TYPE public.alert_inbox_status AS ENUM (
    'open',
    'acknowledged',
    'resolved',
    'muted',
    'false_positive'
);
-- tripl:statement
CREATE TYPE public.alert_message_format AS ENUM (
    'plain',
    'slack_mrkdwn',
    'telegram_html',
    'telegram_markdownv2'
);
-- tripl:statement
CREATE TYPE public.alert_rule_filter_field AS ENUM (
    'event_type',
    'event',
    'direction',
    'metric'
);
-- tripl:statement
CREATE TYPE public.alert_rule_filter_operator AS ENUM (
    'eq',
    'ne',
    'in',
    'not_in'
);
-- tripl:statement
CREATE TYPE public.anomaly_direction AS ENUM (
    'spike',
    'drop'
);
-- tripl:statement
CREATE TYPE public.api_key_scope AS ENUM (
    'read',
    'write'
);
-- tripl:statement
CREATE TYPE public.chart_annotation_scope_type AS ENUM (
    'project_total',
    'event_type',
    'event',
    'metric'
);
-- tripl:statement
CREATE TYPE public.chart_annotation_source AS ENUM (
    'manual',
    'release',
    'api'
);
-- tripl:statement
CREATE TYPE public.data_source_db_type AS ENUM (
    'clickhouse',
    'postgres',
    'bigquery',
    'synthetic'
);
-- tripl:statement
CREATE TYPE public.data_source_test_status AS ENUM (
    'success',
    'failed'
);
-- tripl:statement
CREATE TYPE public.distribution_drift_band AS ENUM (
    'stable',
    'minor',
    'significant'
);
-- tripl:statement
CREATE TYPE public.event_comment_status AS ENUM (
    'open',
    'resolved',
    'snoozed'
);
-- tripl:statement
CREATE TYPE public.event_photo_kind AS ENUM (
    'photo',
    'figma'
);
-- tripl:statement
CREATE TYPE public.event_photo_storage_backend AS ENUM (
    'local',
    'gcs'
);
-- tripl:statement
CREATE TYPE public.event_status AS ENUM (
    'draft',
    'in_review',
    'ready_for_dev',
    'implemented',
    'live',
    'deprecated',
    'archived'
);
-- tripl:statement
CREATE TYPE public.field_definition_type AS ENUM (
    'string',
    'number',
    'boolean',
    'json',
    'enum',
    'url'
);
-- tripl:statement
CREATE TYPE public.merge_resolution_choice AS ENUM (
    'ours',
    'theirs'
);
-- tripl:statement
CREATE TYPE public.meta_field_type AS ENUM (
    'string',
    'url',
    'boolean',
    'enum',
    'date'
);
-- tripl:statement
CREATE TYPE public.metric_aggregation AS ENUM (
    'count',
    'sum',
    'avg',
    'min',
    'max',
    'count_distinct'
);
-- tripl:statement
CREATE TYPE public.metric_breakdown_anomaly_kind AS ENUM (
    'volume',
    'parity'
);
-- tripl:statement
CREATE TYPE public.metric_composition AS ENUM (
    'single',
    'ratio',
    'per_distinct_user'
);
-- tripl:statement
CREATE TYPE public.metric_kind AS ENUM (
    'sql',
    'event_composition',
    'fact'
);
-- tripl:statement
CREATE TYPE public.metric_scope_type AS ENUM (
    'project_total',
    'event_type',
    'event',
    'schema',
    'distribution',
    'release_regression',
    'metric',
    'variable_value_drift',
    'source_freshness',
    'lifecycle',
    'property_drift'
);
-- tripl:statement
CREATE TYPE public.metric_status AS ENUM (
    'draft',
    'active',
    'archived'
);
-- tripl:statement
CREATE TYPE public.organization_member_role AS ENUM (
    'owner',
    'admin',
    'member'
);
-- tripl:statement
CREATE TYPE public.organization_status AS ENUM (
    'active',
    'deleting',
    'suspended'
);
-- tripl:statement
CREATE TYPE public.plan_branch_kind AS ENUM (
    'main',
    'working'
);
-- tripl:statement
CREATE TYPE public.plan_branch_status AS ENUM (
    'draft',
    'ready_for_review',
    'changes_requested',
    'approved',
    'merged',
    'closed'
);
-- tripl:statement
CREATE TYPE public.plan_revision_kind AS ENUM (
    'snapshot',
    'branch_base',
    'merge'
);
-- tripl:statement
CREATE TYPE public.project_generation_status AS ENUM (
    'pending',
    'seeding',
    'ready',
    'failed'
);
-- tripl:statement
CREATE TYPE public.project_member_role AS ENUM (
    'none',
    'editor',
    'viewer'
);
-- tripl:statement
CREATE TYPE public.release_comparability_reason AS ENUM (
    'comparable',
    'no_baseline',
    'baseline_no_volume',
    'population_mismatch'
);
-- tripl:statement
CREATE TYPE public.release_regression_kind AS ENUM (
    'missing',
    'volume_drop'
);
-- tripl:statement
CREATE TYPE public.scan_interval AS ENUM (
    '15m',
    '1h',
    '6h',
    '1d',
    '1w'
);
-- tripl:statement
CREATE TYPE public.scan_job_status AS ENUM (
    'pending',
    'running',
    'completed',
    'failed',
    'cancelled'
);
-- tripl:statement
CREATE TYPE public.schema_drift_status AS ENUM (
    'open',
    'accepted',
    'snoozed',
    'false_positive'
);
-- tripl:statement
CREATE TYPE public.schema_drift_type AS ENUM (
    'new_field',
    'missing_field',
    'type_changed',
    'enum_violation',
    'required_null_violation',
    'regex_violation',
    'range_violation'
);
-- tripl:statement
CREATE TYPE public.sensitivity_level AS ENUM (
    'none',
    'pii',
    'phi',
    'financial',
    'secret'
);
-- tripl:statement
CREATE TYPE public.shadow_event_status AS ENUM (
    'new',
    'accepted',
    'dismissed'
);
-- tripl:statement
CREATE TYPE public.signal_expected_reason AS ENUM (
    'campaign',
    'release',
    'seasonality',
    'other'
);
-- tripl:statement
CREATE TYPE public.signal_triage_action AS ENUM (
    'acknowledged',
    'muted',
    'expected',
    'tracking_bug',
    'false_positive',
    'real_issue'
);
-- tripl:statement
CREATE TYPE public.variable_type AS ENUM (
    'string',
    'number',
    'boolean',
    'date',
    'datetime',
    'json',
    'string_array',
    'number_array'
);
-- tripl:statement
CREATE TYPE public.variable_value_kind AS ENUM (
    'low',
    'high'
);
-- tripl:statement
CREATE TEXT SEARCH DICTIONARY public.tripl_english_stem (
    TEMPLATE = pg_catalog.snowball,
    language = 'english' );
-- tripl:statement
CREATE TEXT SEARCH DICTIONARY public.tripl_russian_stem (
    TEMPLATE = pg_catalog.snowball,
    language = 'russian' );
-- tripl:statement
CREATE TEXT SEARCH CONFIGURATION public.tripl_search (
    PARSER = pg_catalog."default" );
-- tripl:statement
ALTER TEXT SEARCH CONFIGURATION public.tripl_search
    ADD MAPPING FOR asciiword WITH public.unaccent, public.tripl_english_stem;
-- tripl:statement
ALTER TEXT SEARCH CONFIGURATION public.tripl_search
    ADD MAPPING FOR word WITH public.unaccent, public.tripl_russian_stem;
-- tripl:statement
ALTER TEXT SEARCH CONFIGURATION public.tripl_search
    ADD MAPPING FOR numword WITH simple;
-- tripl:statement
ALTER TEXT SEARCH CONFIGURATION public.tripl_search
    ADD MAPPING FOR email WITH simple;
-- tripl:statement
ALTER TEXT SEARCH CONFIGURATION public.tripl_search
    ADD MAPPING FOR url WITH simple;
-- tripl:statement
ALTER TEXT SEARCH CONFIGURATION public.tripl_search
    ADD MAPPING FOR host WITH simple;
-- tripl:statement
ALTER TEXT SEARCH CONFIGURATION public.tripl_search
    ADD MAPPING FOR sfloat WITH simple;
-- tripl:statement
ALTER TEXT SEARCH CONFIGURATION public.tripl_search
    ADD MAPPING FOR version WITH simple;
-- tripl:statement
ALTER TEXT SEARCH CONFIGURATION public.tripl_search
    ADD MAPPING FOR hword_numpart WITH simple;
-- tripl:statement
ALTER TEXT SEARCH CONFIGURATION public.tripl_search
    ADD MAPPING FOR hword_part WITH public.unaccent, public.tripl_russian_stem;
-- tripl:statement
ALTER TEXT SEARCH CONFIGURATION public.tripl_search
    ADD MAPPING FOR hword_asciipart WITH public.unaccent, public.tripl_english_stem;
-- tripl:statement
ALTER TEXT SEARCH CONFIGURATION public.tripl_search
    ADD MAPPING FOR numhword WITH simple;
-- tripl:statement
ALTER TEXT SEARCH CONFIGURATION public.tripl_search
    ADD MAPPING FOR asciihword WITH public.unaccent, public.tripl_english_stem;
-- tripl:statement
ALTER TEXT SEARCH CONFIGURATION public.tripl_search
    ADD MAPPING FOR hword WITH public.unaccent, public.tripl_russian_stem;
-- tripl:statement
ALTER TEXT SEARCH CONFIGURATION public.tripl_search
    ADD MAPPING FOR url_path WITH simple;
-- tripl:statement
ALTER TEXT SEARCH CONFIGURATION public.tripl_search
    ADD MAPPING FOR file WITH simple;
-- tripl:statement
ALTER TEXT SEARCH CONFIGURATION public.tripl_search
    ADD MAPPING FOR "float" WITH simple;
-- tripl:statement
ALTER TEXT SEARCH CONFIGURATION public.tripl_search
    ADD MAPPING FOR "int" WITH simple;
-- tripl:statement
ALTER TEXT SEARCH CONFIGURATION public.tripl_search
    ADD MAPPING FOR uint WITH simple;
-- tripl:statement
CREATE TEXT SEARCH CONFIGURATION public.tripl_search_surface (
    PARSER = pg_catalog."default" );
-- tripl:statement
ALTER TEXT SEARCH CONFIGURATION public.tripl_search_surface
    ADD MAPPING FOR asciiword WITH public.unaccent, simple;
-- tripl:statement
ALTER TEXT SEARCH CONFIGURATION public.tripl_search_surface
    ADD MAPPING FOR word WITH public.unaccent, simple;
-- tripl:statement
ALTER TEXT SEARCH CONFIGURATION public.tripl_search_surface
    ADD MAPPING FOR numword WITH simple;
-- tripl:statement
ALTER TEXT SEARCH CONFIGURATION public.tripl_search_surface
    ADD MAPPING FOR email WITH simple;
-- tripl:statement
ALTER TEXT SEARCH CONFIGURATION public.tripl_search_surface
    ADD MAPPING FOR url WITH simple;
-- tripl:statement
ALTER TEXT SEARCH CONFIGURATION public.tripl_search_surface
    ADD MAPPING FOR host WITH simple;
-- tripl:statement
ALTER TEXT SEARCH CONFIGURATION public.tripl_search_surface
    ADD MAPPING FOR sfloat WITH simple;
-- tripl:statement
ALTER TEXT SEARCH CONFIGURATION public.tripl_search_surface
    ADD MAPPING FOR version WITH simple;
-- tripl:statement
ALTER TEXT SEARCH CONFIGURATION public.tripl_search_surface
    ADD MAPPING FOR hword_numpart WITH simple;
-- tripl:statement
ALTER TEXT SEARCH CONFIGURATION public.tripl_search_surface
    ADD MAPPING FOR hword_part WITH public.unaccent, simple;
-- tripl:statement
ALTER TEXT SEARCH CONFIGURATION public.tripl_search_surface
    ADD MAPPING FOR hword_asciipart WITH public.unaccent, simple;
-- tripl:statement
ALTER TEXT SEARCH CONFIGURATION public.tripl_search_surface
    ADD MAPPING FOR numhword WITH simple;
-- tripl:statement
ALTER TEXT SEARCH CONFIGURATION public.tripl_search_surface
    ADD MAPPING FOR asciihword WITH public.unaccent, simple;
-- tripl:statement
ALTER TEXT SEARCH CONFIGURATION public.tripl_search_surface
    ADD MAPPING FOR hword WITH public.unaccent, simple;
-- tripl:statement
ALTER TEXT SEARCH CONFIGURATION public.tripl_search_surface
    ADD MAPPING FOR url_path WITH simple;
-- tripl:statement
ALTER TEXT SEARCH CONFIGURATION public.tripl_search_surface
    ADD MAPPING FOR file WITH simple;
-- tripl:statement
ALTER TEXT SEARCH CONFIGURATION public.tripl_search_surface
    ADD MAPPING FOR "float" WITH simple;
-- tripl:statement
ALTER TEXT SEARCH CONFIGURATION public.tripl_search_surface
    ADD MAPPING FOR "int" WITH simple;
-- tripl:statement
ALTER TEXT SEARCH CONFIGURATION public.tripl_search_surface
    ADD MAPPING FOR uint WITH simple;
-- tripl:statement
CREATE TABLE public.alert_correlation_states (
    id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    project_id uuid NOT NULL,
    correlation_group_id uuid NOT NULL,
    status public.alert_inbox_status DEFAULT 'open'::public.alert_inbox_status NOT NULL,
    muted_until timestamp with time zone,
    note text,
    false_positive_count integer DEFAULT 0 NOT NULL,
    last_seen_at timestamp with time zone,
    acted_at timestamp with time zone,
    acted_by uuid
);
-- tripl:statement
CREATE TABLE public.alert_deliveries (
    project_id uuid NOT NULL,
    scan_config_id uuid NOT NULL,
    scan_job_id uuid,
    destination_id uuid NOT NULL,
    rule_id uuid NOT NULL,
    status public.alert_delivery_status DEFAULT 'pending'::public.alert_delivery_status NOT NULL,
    channel public.alert_destination_type NOT NULL,
    matched_count integer DEFAULT 0 NOT NULL,
    payload_snapshot json,
    error_message text,
    sent_at timestamp with time zone,
    id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    dispatch_attempts integer DEFAULT 0 NOT NULL,
    claimed_at timestamp with time zone
);
-- tripl:statement
CREATE TABLE public.alert_delivery_items (
    delivery_id uuid NOT NULL,
    scope_type public.metric_scope_type NOT NULL,
    scope_ref character varying(64) NOT NULL,
    scope_name character varying(255) NOT NULL,
    event_type_id uuid,
    event_id uuid,
    bucket timestamp with time zone NOT NULL,
    direction public.anomaly_direction NOT NULL,
    actual_count double precision NOT NULL,
    expected_count double precision NOT NULL,
    absolute_delta double precision NOT NULL,
    percent_delta double precision NOT NULL,
    details_path character varying(500),
    monitoring_path character varying(500),
    drift_field character varying(255),
    drift_type public.alert_drift_type,
    sample_value character varying(500),
    id uuid NOT NULL,
    correlation_group_id uuid,
    window_from timestamp with time zone
);
-- tripl:statement
CREATE TABLE public.alert_destinations (
    project_id uuid NOT NULL,
    type public.alert_destination_type NOT NULL,
    name character varying(255) NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    webhook_url_encrypted text,
    bot_token_encrypted text,
    chat_id character varying(255),
    id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    target_url_encrypted text,
    webhook_header_name character varying(255),
    webhook_header_value_encrypted text,
    email_recipients text,
    email_from_address character varying(255),
    email_subject_template character varying(500),
    jira_base_url character varying(255),
    jira_auth_email character varying(255),
    jira_api_token_encrypted text,
    jira_project_key character varying(64),
    jira_issue_type character varying(64),
    linear_api_key_encrypted text,
    linear_team_id character varying(64),
    linear_state_id character varying(64),
    linear_label_ids character varying(1024),
    delivery_schedule_cron character varying(120),
    last_flushed_at timestamp with time zone
);
-- tripl:statement
CREATE TABLE public.alert_owner_notifications (
    id uuid NOT NULL,
    project_id uuid NOT NULL,
    delivery_id uuid,
    correlation_group_id uuid,
    target_key character varying(512),
    user_id uuid,
    email character varying(320) NOT NULL,
    source character varying(16) DEFAULT 'rule'::character varying NOT NULL,
    status character varying(16) DEFAULT 'pending'::character varying NOT NULL,
    error text,
    triggered_by uuid,
    sent_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_alert_owner_notification_source CHECK (((source)::text = ANY ((ARRAY['rule'::character varying, 'manual'::character varying])::text[]))),
    CONSTRAINT ck_alert_owner_notification_status CHECK (((status)::text = ANY ((ARRAY['pending'::character varying, 'sent'::character varying, 'failed'::character varying, 'skipped'::character varying])::text[])))
);
-- tripl:statement
CREATE TABLE public.alert_pending_items (
    id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    project_id uuid NOT NULL,
    destination_id uuid NOT NULL,
    rule_id uuid NOT NULL,
    scan_config_id uuid,
    scan_job_id uuid,
    source_anomaly_id uuid,
    scope_type public.metric_scope_type NOT NULL,
    scope_ref character varying(64) NOT NULL,
    scope_name character varying(255) NOT NULL,
    event_type_id uuid,
    event_id uuid,
    bucket timestamp with time zone NOT NULL,
    direction public.anomaly_direction NOT NULL,
    actual_count double precision NOT NULL,
    expected_count double precision NOT NULL,
    drift_field character varying(255),
    drift_type public.alert_drift_type,
    sample_value character varying(500),
    window_from timestamp with time zone,
    correlation_group_id uuid NOT NULL,
    observation_count integer DEFAULT 1 NOT NULL
);
-- tripl:statement
CREATE TABLE public.alert_rule_filters (
    rule_id uuid NOT NULL,
    field public.alert_rule_filter_field NOT NULL,
    operator public.alert_rule_filter_operator NOT NULL,
    "values" json NOT NULL,
    "position" integer DEFAULT 0 NOT NULL,
    id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);
-- tripl:statement
CREATE TABLE public.alert_rule_states (
    rule_id uuid NOT NULL,
    scan_config_id uuid,
    scope_type public.metric_scope_type NOT NULL,
    scope_ref character varying(64) NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    opened_at timestamp with time zone,
    closed_at timestamp with time zone,
    last_anomaly_bucket timestamp with time zone,
    last_notified_at timestamp with time zone,
    last_notified_delivery_id uuid,
    id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);
-- tripl:statement
CREATE TABLE public.alert_rules (
    destination_id uuid NOT NULL,
    name character varying(255) NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    include_project_total boolean DEFAULT true NOT NULL,
    include_event_types boolean DEFAULT true NOT NULL,
    include_events boolean DEFAULT true NOT NULL,
    include_schema_drifts boolean DEFAULT false NOT NULL,
    notify_on_spike boolean DEFAULT true NOT NULL,
    notify_on_drop boolean DEFAULT true NOT NULL,
    min_percent_delta double precision DEFAULT '30'::double precision NOT NULL,
    min_absolute_delta double precision DEFAULT '0'::double precision NOT NULL,
    min_expected_count double precision DEFAULT '0'::double precision NOT NULL,
    cooldown_minutes integer DEFAULT 1440 NOT NULL,
    message_template text,
    items_template text,
    message_format public.alert_message_format DEFAULT 'plain'::public.alert_message_format NOT NULL,
    id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    include_distribution_drifts boolean DEFAULT false NOT NULL,
    ai_explanation_enabled boolean DEFAULT false NOT NULL,
    include_release_regressions boolean DEFAULT false NOT NULL,
    muted_until timestamp with time zone,
    include_metrics boolean DEFAULT false NOT NULL,
    include_variable_value_drifts boolean DEFAULT false NOT NULL,
    scan_config_id uuid,
    include_source_freshness boolean DEFAULT false NOT NULL,
    include_lifecycle boolean DEFAULT false NOT NULL,
    notify_owners boolean DEFAULT false NOT NULL,
    include_property_drifts boolean DEFAULT false NOT NULL
);
-- tripl:statement
CREATE TABLE public.anomaly_scope_overrides (
    id uuid NOT NULL,
    project_id uuid NOT NULL,
    scan_config_id uuid,
    scope_type public.metric_scope_type NOT NULL,
    scope_ref character varying(64) NOT NULL,
    scope_name character varying(255) DEFAULT ''::character varying NOT NULL,
    sigma_threshold double precision NOT NULL,
    min_expected_count integer NOT NULL,
    false_positive_count integer DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);
-- tripl:statement
CREATE TABLE public.api_keys (
    id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    user_id uuid NOT NULL,
    name character varying(100) NOT NULL,
    key_prefix character varying(20) NOT NULL,
    key_hash character varying(64) NOT NULL,
    scope public.api_key_scope NOT NULL,
    expires_at timestamp with time zone,
    revoked_at timestamp with time zone,
    last_used_at timestamp with time zone,
    project_id uuid,
    organization_id uuid NOT NULL,
    created_with_sso_org_id uuid
);
-- tripl:statement
CREATE TABLE public.app_settings (
    id uuid NOT NULL,
    key character varying(100) NOT NULL,
    value json NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    organization_id uuid
);
-- tripl:statement
CREATE TABLE public.audit_log (
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    user_id uuid,
    user_email character varying(320) DEFAULT ''::character varying NOT NULL,
    project_id uuid,
    project_slug character varying(255) DEFAULT ''::character varying NOT NULL,
    action character varying(64) NOT NULL,
    target_type character varying(32) NOT NULL,
    target_id uuid,
    target_name character varying(255) DEFAULT ''::character varying NOT NULL,
    payload json NOT NULL,
    id uuid NOT NULL,
    branch_id uuid,
    branch_name character varying(255) DEFAULT ''::character varying NOT NULL,
    organization_id uuid DEFAULT '00000000-0000-0000-0000-00000000d0f1'::uuid
);
-- tripl:statement
CREATE TABLE public.audit_webhook_outbox (
    id uuid NOT NULL,
    organization_id uuid NOT NULL,
    audit_log_id uuid NOT NULL,
    status character varying(16) DEFAULT 'pending'::character varying NOT NULL,
    attempts integer DEFAULT 0 NOT NULL,
    next_attempt_at timestamp with time zone DEFAULT now() NOT NULL,
    last_error character varying(255),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    sent_at timestamp with time zone
);
-- tripl:statement
CREATE TABLE public.chart_annotations (
    id uuid NOT NULL,
    project_id uuid NOT NULL,
    scope_type public.chart_annotation_scope_type,
    scope_ref character varying(120),
    bucket timestamp with time zone NOT NULL,
    label character varying(200) NOT NULL,
    description text,
    color character varying(20) DEFAULT '#ef4444'::character varying NOT NULL,
    created_by_user_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    source public.chart_annotation_source DEFAULT 'manual'::public.chart_annotation_source NOT NULL,
    url character varying(500)
);
-- tripl:statement
CREATE TABLE public.coverage_metrics (
    id uuid NOT NULL,
    scan_config_id uuid NOT NULL,
    bucket timestamp with time zone NOT NULL,
    total_count bigint NOT NULL,
    matched_count bigint NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);
-- tripl:statement
CREATE TABLE public.data_sources (
    name character varying(255) NOT NULL,
    db_type public.data_source_db_type NOT NULL,
    host character varying(500) NOT NULL,
    port integer NOT NULL,
    database_name character varying(255) NOT NULL,
    username character varying(255) NOT NULL,
    password_encrypted text NOT NULL,
    extra_params json,
    last_test_at timestamp with time zone,
    last_test_status public.data_source_test_status,
    last_test_message text,
    id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    timeout_seconds integer,
    json_path_discovery character varying(16),
    project_id uuid,
    organization_id uuid NOT NULL
);
-- tripl:statement
CREATE TABLE public.distribution_drifts (
    id uuid NOT NULL,
    scan_config_id uuid NOT NULL,
    event_type_id uuid,
    field_name character varying(255) NOT NULL,
    bucket timestamp with time zone NOT NULL,
    psi double precision NOT NULL,
    band public.distribution_drift_band NOT NULL,
    baseline_total integer NOT NULL,
    current_total integer NOT NULL,
    top_movers jsonb DEFAULT '[]'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);
-- tripl:statement
CREATE TABLE public.doc_files (
    id uuid NOT NULL,
    project_id uuid,
    organization_id uuid,
    path character varying(512) NOT NULL,
    path_key character varying(512) NOT NULL,
    content text NOT NULL,
    content_sha256 character varying(64) NOT NULL,
    size_bytes integer NOT NULL,
    title character varying(300) NOT NULL,
    description text NOT NULL,
    tags json NOT NULL,
    audience character varying(8) NOT NULL,
    revision integer NOT NULL,
    created_by uuid,
    updated_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    visibility character varying(16) DEFAULT 'level'::character varying NOT NULL,
    visibility_inherited boolean DEFAULT true NOT NULL,
    CONSTRAINT ck_doc_files_audience CHECK (((audience)::text = ANY ((ARRAY['human'::character varying, 'agent'::character varying, 'both'::character varying])::text[]))),
    CONSTRAINT ck_doc_files_one_scope CHECK (((project_id IS NULL) <> (organization_id IS NULL))),
    CONSTRAINT ck_doc_files_visibility CHECK (((visibility)::text = ANY ((ARRAY['private'::character varying, 'restricted'::character varying, 'level'::character varying])::text[])))
);
-- tripl:statement
CREATE TABLE public.doc_folder_settings (
    id uuid NOT NULL,
    project_id uuid,
    organization_id uuid,
    path character varying(512) NOT NULL,
    path_key character varying(512) NOT NULL,
    visibility character varying(16) NOT NULL,
    created_by uuid,
    updated_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_doc_folder_settings_one_scope CHECK (((project_id IS NULL) <> (organization_id IS NULL))),
    CONSTRAINT ck_doc_folder_settings_visibility CHECK (((visibility)::text = ANY ((ARRAY['private'::character varying, 'restricted'::character varying, 'level'::character varying])::text[])))
);
-- tripl:statement
CREATE TABLE public.doc_folder_shares (
    id uuid NOT NULL,
    folder_id uuid NOT NULL,
    user_id uuid,
    group_id uuid,
    permission character varying(8) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_doc_folder_shares_one_principal CHECK (((user_id IS NULL) <> (group_id IS NULL))),
    CONSTRAINT ck_doc_folder_shares_permission CHECK (((permission)::text = ANY ((ARRAY['view'::character varying, 'edit'::character varying])::text[])))
);
-- tripl:statement
CREATE TABLE public.doc_links (
    id uuid NOT NULL,
    doc_file_id uuid NOT NULL,
    kind character varying(16) NOT NULL,
    target character varying(500) NOT NULL,
    qualifier character varying(500),
    CONSTRAINT ck_doc_links_kind CHECK (((kind)::text = ANY ((ARRAY['event'::character varying, 'event_type'::character varying, 'field'::character varying, 'doc'::character varying, 'variable'::character varying, 'metric'::character varying, 'alert_rule'::character varying, 'branch'::character varying, 'scan'::character varying, 'data_source'::character varying, 'user'::character varying])::text[])))
);
-- tripl:statement
CREATE TABLE public.doc_revisions (
    id uuid NOT NULL,
    doc_file_id uuid NOT NULL,
    number integer NOT NULL,
    action character varying(16) NOT NULL,
    path character varying(512) NOT NULL,
    content text NOT NULL,
    content_sha256 character varying(64) NOT NULL,
    message character varying(500) NOT NULL,
    author_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    restored_from_number integer,
    CONSTRAINT ck_doc_revisions_action CHECK (((action)::text = ANY ((ARRAY['create'::character varying, 'update'::character varying, 'move'::character varying, 'restore'::character varying, 'import'::character varying])::text[])))
);
-- tripl:statement
CREATE TABLE public.doc_shares (
    id uuid NOT NULL,
    doc_file_id uuid NOT NULL,
    user_id uuid,
    group_id uuid,
    permission character varying(8) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_doc_shares_one_principal CHECK (((user_id IS NULL) <> (group_id IS NULL))),
    CONSTRAINT ck_doc_shares_permission CHECK (((permission)::text = ANY ((ARRAY['view'::character varying, 'edit'::character varying])::text[])))
);
-- tripl:statement
CREATE TABLE public.duplicate_dismissals (
    id uuid NOT NULL,
    project_id uuid NOT NULL,
    event_a_id uuid NOT NULL,
    event_b_id uuid NOT NULL,
    dismissed_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_duplicate_dismissal_ordered CHECK ((event_a_id < event_b_id))
);
-- tripl:statement
CREATE TABLE public.email_verification_tokens (
    id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    user_id uuid NOT NULL,
    token_hash character varying(64) NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    used_at timestamp with time zone
);
-- tripl:statement
CREATE TABLE public.event_changes (
    id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    event_id uuid NOT NULL,
    user_id uuid,
    field character varying(255) NOT NULL,
    old_value text,
    new_value text,
    source character varying(32)
);
-- tripl:statement
CREATE TABLE public.event_field_values (
    event_id uuid NOT NULL,
    field_definition_id uuid NOT NULL,
    value text NOT NULL,
    id uuid NOT NULL,
    is_authored boolean DEFAULT false NOT NULL
);
-- tripl:statement
CREATE TABLE public.event_meta_values (
    event_id uuid NOT NULL,
    meta_field_definition_id uuid NOT NULL,
    value text NOT NULL,
    id uuid NOT NULL
);
-- tripl:statement
CREATE TABLE public.event_metric_breakdowns (
    scan_config_id uuid NOT NULL,
    event_id uuid,
    event_type_id uuid,
    bucket timestamp with time zone NOT NULL,
    breakdown_column character varying(255) NOT NULL,
    breakdown_value character varying(500) NOT NULL,
    is_other boolean NOT NULL,
    count bigint NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    id uuid NOT NULL
);
-- tripl:statement
CREATE TABLE public.event_metrics (
    scan_config_id uuid NOT NULL,
    event_id uuid,
    event_type_id uuid,
    bucket timestamp with time zone NOT NULL,
    count bigint NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    id uuid NOT NULL
);
-- tripl:statement
CREATE TABLE public.event_photo_comments (
    id uuid NOT NULL,
    photo_id uuid,
    parent_id uuid,
    user_id uuid,
    body text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    event_id uuid,
    status public.event_comment_status DEFAULT 'open'::public.event_comment_status NOT NULL,
    resolution_note text,
    snoozed_until timestamp with time zone,
    resolved_at timestamp with time zone,
    resolved_by uuid,
    CONSTRAINT ck_event_photo_comment_one_anchor CHECK (((photo_id IS NULL) <> (event_id IS NULL)))
);
-- tripl:statement
CREATE TABLE public.event_photos (
    project_id uuid NOT NULL,
    event_id uuid NOT NULL,
    uploaded_by_user_id uuid,
    original_filename character varying(500) NOT NULL,
    content_type character varying(120) NOT NULL,
    size_bytes integer NOT NULL,
    storage_backend public.event_photo_storage_backend,
    storage_key character varying(500),
    sort_order integer DEFAULT 0 NOT NULL,
    id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    kind public.event_photo_kind DEFAULT 'photo'::public.event_photo_kind NOT NULL,
    external_url character varying(2000),
    storage_org_id uuid,
    storage_config_id uuid
);
-- tripl:statement
CREATE TABLE public.event_tags (
    event_id uuid NOT NULL,
    name character varying(100) NOT NULL,
    id uuid NOT NULL
);
-- tripl:statement
CREATE TABLE public.event_type_owners (
    id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    event_type_id uuid NOT NULL,
    user_id uuid NOT NULL,
    granted_by uuid
);
-- tripl:statement
CREATE TABLE public.event_type_relations (
    project_id uuid NOT NULL,
    source_event_type_id uuid NOT NULL,
    target_event_type_id uuid NOT NULL,
    source_field_id uuid NOT NULL,
    target_field_id uuid NOT NULL,
    relation_type character varying(50) NOT NULL,
    description text NOT NULL,
    id uuid NOT NULL,
    branch_id uuid NOT NULL,
    origin_id uuid
);
-- tripl:statement
CREATE TABLE public.event_types (
    project_id uuid NOT NULL,
    name character varying(100) NOT NULL,
    display_name character varying(255) NOT NULL,
    description text NOT NULL,
    color character varying(7) NOT NULL,
    "order" integer NOT NULL,
    id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    branch_id uuid NOT NULL
);
-- tripl:statement
CREATE TABLE public.events (
    project_id uuid NOT NULL,
    event_type_id uuid NOT NULL,
    name character varying(500) NOT NULL,
    description text NOT NULL,
    "order" integer DEFAULT 0 NOT NULL,
    last_seen_at timestamp with time zone,
    id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    metric_breakdown_columns json DEFAULT '[]'::json NOT NULL,
    branch_id uuid NOT NULL,
    source_name character varying(500),
    status public.event_status DEFAULT 'draft'::public.event_status NOT NULL,
    sunset_at timestamp with time zone,
    owner_id uuid,
    reviewed boolean DEFAULT false NOT NULL,
    title character varying(500) DEFAULT ''::character varying NOT NULL,
    superseded_by_event_id uuid,
    origin_id uuid,
    first_seen_at timestamp with time zone,
    required_presence_threshold double precision
);
-- tripl:statement
CREATE TABLE public.fact_tables (
    id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    project_id uuid NOT NULL,
    name character varying(255) NOT NULL,
    display_name character varying(255) NOT NULL,
    description text NOT NULL,
    color character varying(7) NOT NULL,
    "order" integer DEFAULT 0 NOT NULL,
    data_source_id uuid,
    sql text NOT NULL,
    timestamp_column character varying(255) NOT NULL,
    columns json DEFAULT '[]'::json NOT NULL,
    identifier_columns json DEFAULT '[]'::json NOT NULL,
    row_filters json DEFAULT '[]'::json NOT NULL
);
-- tripl:statement
CREATE TABLE public.field_definitions (
    event_type_id uuid NOT NULL,
    name character varying(100) NOT NULL,
    display_name character varying(255) NOT NULL,
    field_type public.field_definition_type NOT NULL,
    is_required boolean NOT NULL,
    enum_options json,
    description text NOT NULL,
    "order" integer NOT NULL,
    sensitivity public.sensitivity_level DEFAULT 'none'::public.sensitivity_level NOT NULL,
    id uuid NOT NULL,
    contract_required_max_null_rate double precision,
    contract_regex character varying(500),
    contract_min_value double precision,
    contract_max_value double precision,
    contract_max_bad_rate double precision DEFAULT '0'::double precision NOT NULL
);
-- tripl:statement
CREATE TABLE public.implementation_tickets (
    id uuid NOT NULL,
    project_id uuid NOT NULL,
    branch_id uuid NOT NULL,
    tracker_type character varying DEFAULT 'jira'::character varying NOT NULL,
    external_id character varying,
    external_key character varying,
    external_url character varying DEFAULT ''::character varying NOT NULL,
    status character varying DEFAULT 'open'::character varying NOT NULL,
    summary character varying DEFAULT ''::character varying NOT NULL,
    event_ids json DEFAULT '[]'::json NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    closed_at timestamp with time zone
);
-- tripl:statement
CREATE TABLE public.incident_summaries (
    id uuid NOT NULL,
    project_id uuid NOT NULL,
    correlation_group_id uuid NOT NULL,
    facts_hash character varying(64) NOT NULL,
    facts json NOT NULL,
    sentences json NOT NULL,
    cause_known boolean NOT NULL,
    model character varying(200),
    generated_by uuid,
    generated_at timestamp with time zone DEFAULT now() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);
-- tripl:statement
CREATE TABLE public.invitations (
    id uuid NOT NULL,
    email character varying(320) NOT NULL,
    token_hash character varying(64) NOT NULL,
    invited_by_user_id uuid,
    expires_at timestamp with time zone NOT NULL,
    used_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    organization_id uuid NOT NULL,
    org_role public.organization_member_role NOT NULL
);
-- tripl:statement
CREATE TABLE public.lifecycle_findings (
    id uuid NOT NULL,
    project_id uuid NOT NULL,
    event_id uuid NOT NULL,
    kind character varying(32) NOT NULL,
    related_event_id uuid,
    first_seen_at timestamp with time zone NOT NULL,
    last_seen_at timestamp with time zone NOT NULL,
    resolved_at timestamp with time zone,
    volume_24h bigint,
    successor_volume_7d bigint,
    CONSTRAINT ck_lifecycle_finding_kind CHECK (((kind)::text = ANY ((ARRAY['sunset_overdue'::character varying, 'successor_silent'::character varying])::text[])))
);
-- tripl:statement
CREATE TABLE public.meta_field_definitions (
    project_id uuid NOT NULL,
    name character varying(100) NOT NULL,
    display_name character varying(255) NOT NULL,
    field_type public.meta_field_type NOT NULL,
    is_required boolean NOT NULL,
    enum_options json,
    default_value text,
    link_template text,
    "order" integer NOT NULL,
    sensitivity public.sensitivity_level DEFAULT 'none'::public.sensitivity_level NOT NULL,
    id uuid NOT NULL,
    branch_id uuid NOT NULL,
    allow_multiple boolean DEFAULT false NOT NULL
);
-- tripl:statement
CREATE TABLE public.metric_anomalies (
    scan_config_id uuid,
    scope_type public.metric_scope_type NOT NULL,
    scope_ref character varying(64) NOT NULL,
    event_id uuid,
    event_type_id uuid,
    bucket timestamp with time zone NOT NULL,
    actual_count double precision NOT NULL,
    expected_count double precision NOT NULL,
    stddev double precision NOT NULL,
    z_score double precision NOT NULL,
    direction public.anomaly_direction NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    id uuid NOT NULL,
    effective_stddev double precision DEFAULT '0'::double precision NOT NULL,
    detector_kind character varying(16) DEFAULT 'phase'::character varying NOT NULL
);
-- tripl:statement
CREATE TABLE public.metric_anomaly_attributions (
    id uuid NOT NULL,
    anomaly_id uuid NOT NULL,
    delta double precision NOT NULL,
    columns json DEFAULT '[]'::json NOT NULL,
    release json,
    computed_at timestamp with time zone DEFAULT now() NOT NULL
);
-- tripl:statement
CREATE TABLE public.metric_baselines (
    id uuid NOT NULL,
    scan_config_id uuid NOT NULL,
    scope_type public.metric_scope_type NOT NULL,
    scope_ref character varying(64) NOT NULL,
    bucket timestamp with time zone NOT NULL,
    expected_count double precision NOT NULL,
    effective_stddev double precision NOT NULL
);
-- tripl:statement
CREATE TABLE public.metric_breakdown_anomalies (
    scan_config_id uuid NOT NULL,
    scope_type public.metric_scope_type NOT NULL,
    scope_ref character varying(64) NOT NULL,
    event_id uuid,
    event_type_id uuid,
    bucket timestamp with time zone NOT NULL,
    breakdown_column character varying(255) NOT NULL,
    breakdown_value character varying(500) NOT NULL,
    is_other boolean NOT NULL,
    actual_count double precision NOT NULL,
    expected_count double precision NOT NULL,
    stddev double precision NOT NULL,
    z_score double precision NOT NULL,
    direction public.anomaly_direction NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    id uuid NOT NULL,
    effective_stddev double precision DEFAULT '0'::double precision NOT NULL,
    detector_kind character varying(16) DEFAULT 'phase'::character varying NOT NULL,
    kind public.metric_breakdown_anomaly_kind DEFAULT 'volume'::public.metric_breakdown_anomaly_kind NOT NULL
);
-- tripl:statement
CREATE TABLE public.metric_definitions (
    id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    project_id uuid NOT NULL,
    name character varying(255) NOT NULL,
    display_name character varying(255) NOT NULL,
    description text NOT NULL,
    color character varying(7) NOT NULL,
    "order" integer DEFAULT 0 NOT NULL,
    unit character varying(50),
    status public.metric_status DEFAULT 'draft'::public.metric_status NOT NULL,
    owner_id uuid,
    reviewed boolean DEFAULT false NOT NULL,
    kind public.metric_kind NOT NULL,
    aggregation public.metric_aggregation,
    composition public.metric_composition,
    config json DEFAULT '{}'::json NOT NULL,
    breakdown_columns json DEFAULT '[]'::json NOT NULL,
    breakdown_values_limit integer,
    app_version_column character varying(255),
    platform_column character varying(255),
    data_source_id uuid,
    "interval" public.scan_interval,
    replay_chunk_interval public.scan_interval,
    numerator_event_id uuid,
    numerator_event_type_id uuid,
    denominator_event_id uuid,
    denominator_event_type_id uuid,
    anomaly_detection_enabled boolean DEFAULT true NOT NULL,
    last_collected_at timestamp with time zone,
    last_collection_status character varying(32),
    last_collection_error text,
    fact_table_id uuid,
    last_collection_window_to timestamp with time zone,
    last_collection_failed_at timestamp with time zone
);
-- tripl:statement
CREATE TABLE public.metric_value_breakdowns (
    id uuid NOT NULL,
    metric_definition_id uuid NOT NULL,
    scan_config_id uuid,
    bucket timestamp with time zone NOT NULL,
    breakdown_column character varying(255) NOT NULL,
    breakdown_value character varying(500) NOT NULL,
    is_other boolean DEFAULT false NOT NULL,
    value double precision NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);
-- tripl:statement
CREATE TABLE public.metric_values (
    id uuid NOT NULL,
    metric_definition_id uuid NOT NULL,
    scan_config_id uuid,
    bucket timestamp with time zone NOT NULL,
    value double precision NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);
-- tripl:statement
CREATE TABLE public.notifications (
    id uuid NOT NULL,
    user_id uuid NOT NULL,
    project_id uuid NOT NULL,
    kind character varying(32) NOT NULL,
    entity_type character varying(16) NOT NULL,
    entity_id uuid NOT NULL,
    title character varying(300) NOT NULL,
    body text DEFAULT ''::text NOT NULL,
    url character varying(1000) NOT NULL,
    actor_user_id uuid,
    read_at timestamp with time zone,
    emailed_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_notification_entity_type CHECK (((entity_type)::text = ANY ((ARRAY['event'::character varying, 'event_type'::character varying, 'metric'::character varying, 'branch'::character varying, 'doc'::character varying])::text[]))),
    CONSTRAINT ck_notification_kind CHECK (((kind)::text = ANY ((ARRAY['comment'::character varying, 'reply'::character varying, 'mention'::character varying, 'open_question'::character varying, 'signal'::character varying, 'branch_review_requested'::character varying, 'branch_approved'::character varying, 'branch_merged'::character varying, 'lifecycle'::character varying, 'property_drift'::character varying])::text[])))
);
-- tripl:statement
CREATE TABLE public.org_audit_webhooks (
    id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    organization_id uuid NOT NULL,
    url character varying(2048) NOT NULL,
    secret_encrypted text NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    last_success_at timestamp with time zone,
    last_error character varying(255),
    last_error_at timestamp with time zone
);
-- tripl:statement
CREATE TABLE public.org_scim_configs (
    id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    organization_id uuid NOT NULL,
    admin_group_id uuid
);
-- tripl:statement
CREATE TABLE public.org_scim_tokens (
    id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    organization_id uuid NOT NULL,
    token_hash character varying(64) NOT NULL,
    prefix character varying(32) NOT NULL,
    created_by uuid,
    last_used_at timestamp with time zone,
    revoked_at timestamp with time zone
);
-- tripl:statement
CREATE TABLE public.org_sso_configs (
    id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    organization_id uuid NOT NULL,
    issuer character varying(512),
    client_id character varying(255),
    client_secret_encrypted text NOT NULL,
    scopes character varying(512) DEFAULT 'openid email profile'::character varying NOT NULL,
    enabled boolean DEFAULT false NOT NULL,
    sso_required boolean DEFAULT false NOT NULL,
    protocol character varying(8) DEFAULT 'oidc'::character varying NOT NULL,
    saml_idp_entity_id character varying(512),
    saml_idp_sso_url character varying(2048),
    saml_idp_certs text,
    saml_name_id_format character varying(255) DEFAULT 'urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress'::character varying NOT NULL,
    saml_email_attribute character varying(255),
    CONSTRAINT ck_org_sso_configs_protocol CHECK (((protocol)::text = ANY ((ARRAY['oidc'::character varying, 'saml'::character varying])::text[])))
);
-- tripl:statement
CREATE TABLE public.org_sso_domains (
    id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    organization_id uuid NOT NULL,
    domain character varying(253) NOT NULL,
    verification_token character varying(64) NOT NULL,
    verified_at timestamp with time zone
);
-- tripl:statement
CREATE TABLE public.organization_group_members (
    id uuid NOT NULL,
    group_id uuid NOT NULL,
    user_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);
-- tripl:statement
CREATE TABLE public.organization_groups (
    id uuid NOT NULL,
    organization_id uuid NOT NULL,
    name character varying(255) NOT NULL,
    description character varying(2000) DEFAULT ''::character varying NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    managed_by_scim boolean DEFAULT false NOT NULL
);
-- tripl:statement
CREATE TABLE public.organization_members (
    id uuid NOT NULL,
    organization_id uuid NOT NULL,
    user_id uuid NOT NULL,
    role public.organization_member_role NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);
-- tripl:statement
CREATE TABLE public.organizations (
    id uuid NOT NULL,
    slug character varying(255) NOT NULL,
    name character varying(255) NOT NULL,
    default_project_role public.project_member_role DEFAULT 'none'::public.project_member_role NOT NULL,
    members_can_create_projects boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    status public.organization_status DEFAULT 'active'::public.organization_status NOT NULL,
    suspended_at timestamp with time zone,
    suspended_reason text,
    CONSTRAINT ck_organizations_default_project_role CHECK ((default_project_role = ANY (ARRAY['none'::public.project_member_role, 'viewer'::public.project_member_role, 'editor'::public.project_member_role])))
);
-- tripl:statement
CREATE TABLE public.password_reset_tokens (
    id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    user_id uuid NOT NULL,
    token_hash character varying(64) NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    used_at timestamp with time zone
);
-- tripl:statement
CREATE TABLE public.photo_storage_configs (
    id uuid NOT NULL,
    organization_id uuid NOT NULL,
    config_hash character varying(64) NOT NULL,
    backend character varying(20) NOT NULL,
    value json NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);
-- tripl:statement
CREATE TABLE public.plan_branch_approvals (
    id uuid NOT NULL,
    branch_id uuid NOT NULL,
    user_id uuid,
    approved_at timestamp with time zone DEFAULT now() NOT NULL,
    plan_hash character varying(64)
);
-- tripl:statement
CREATE TABLE public.plan_branch_comments (
    id uuid NOT NULL,
    branch_id uuid NOT NULL,
    parent_id uuid,
    user_id uuid,
    body text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);
-- tripl:statement
CREATE TABLE public.plan_branch_merge_resolutions (
    id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    branch_id uuid NOT NULL,
    entity_type character varying(40) NOT NULL,
    entity_name character varying(255) NOT NULL,
    field_name character varying(80) NOT NULL,
    choice public.merge_resolution_choice NOT NULL,
    custom_value text,
    resolved_by uuid
);
-- tripl:statement
CREATE TABLE public.plan_branch_reviewers (
    id uuid NOT NULL,
    branch_id uuid NOT NULL,
    user_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);
-- tripl:statement
CREATE TABLE public.plan_branches (
    id uuid NOT NULL,
    project_id uuid NOT NULL,
    name character varying(255) NOT NULL,
    kind public.plan_branch_kind DEFAULT 'working'::public.plan_branch_kind NOT NULL,
    status public.plan_branch_status DEFAULT 'draft'::public.plan_branch_status NOT NULL,
    description text DEFAULT ''::text NOT NULL,
    base_revision_id uuid,
    created_by uuid,
    merged_at timestamp with time zone,
    merged_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    origin_ids_complete boolean DEFAULT false NOT NULL
);
-- tripl:statement
CREATE TABLE public.plan_revisions (
    project_id uuid NOT NULL,
    created_by uuid,
    summary text DEFAULT ''::text NOT NULL,
    payload json NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    id uuid NOT NULL,
    kind public.plan_revision_kind DEFAULT 'snapshot'::public.plan_revision_kind NOT NULL,
    branch_id uuid
);
-- tripl:statement
CREATE TABLE public.platform_step_ins (
    id uuid NOT NULL,
    user_id uuid NOT NULL,
    organization_id uuid NOT NULL,
    reason text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    ended_at timestamp with time zone
);
-- tripl:statement
CREATE TABLE public.project_anomaly_settings (
    project_id uuid NOT NULL,
    anomaly_detection_enabled boolean NOT NULL,
    detect_project_total boolean NOT NULL,
    detect_event_types boolean NOT NULL,
    detect_events boolean NOT NULL,
    baseline_window_buckets integer NOT NULL,
    min_history_buckets integer NOT NULL,
    sigma_threshold double precision NOT NULL,
    min_expected_count integer NOT NULL,
    created_at timestamp without time zone DEFAULT now() NOT NULL,
    updated_at timestamp without time zone DEFAULT now() NOT NULL,
    id uuid NOT NULL,
    detect_metrics boolean DEFAULT true NOT NULL,
    recent_signal_window_hours integer DEFAULT 24 NOT NULL,
    anomaly_ingestion_settling_minutes integer DEFAULT 120 CONSTRAINT project_anomaly_settings_anomaly_ingestion_settling_mi_not_null NOT NULL
);
-- tripl:statement
CREATE TABLE public.project_branch_settings (
    id uuid NOT NULL,
    project_id uuid NOT NULL,
    min_approvals integer DEFAULT 1 NOT NULL,
    block_self_approval boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);
-- tripl:statement
CREATE TABLE public.project_health_snapshots (
    id uuid NOT NULL,
    project_id uuid NOT NULL,
    day date NOT NULL,
    score integer,
    scored_events integer NOT NULL,
    healthy_count integer NOT NULL,
    warning_count integer NOT NULL,
    unhealthy_count integer NOT NULL,
    component_averages json NOT NULL,
    worst_events json NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);
-- tripl:statement
CREATE TABLE public.project_members (
    id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    project_id uuid NOT NULL,
    user_id uuid NOT NULL,
    role public.project_member_role NOT NULL,
    added_by_user_id uuid
);
-- tripl:statement
CREATE TABLE public.project_tracker_configs (
    id uuid NOT NULL,
    project_id uuid NOT NULL,
    enabled boolean DEFAULT false NOT NULL,
    tracker_type character varying DEFAULT 'jira'::character varying NOT NULL,
    base_url character varying DEFAULT ''::character varying NOT NULL,
    project_key character varying DEFAULT ''::character varying NOT NULL,
    auth_email character varying DEFAULT ''::character varying NOT NULL,
    api_token_encrypted character varying DEFAULT ''::character varying NOT NULL,
    issue_type character varying DEFAULT 'Task'::character varying NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);
-- tripl:statement
CREATE TABLE public.projects (
    name character varying(255) NOT NULL,
    slug character varying(255) NOT NULL,
    description text NOT NULL,
    id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    is_demo boolean DEFAULT false NOT NULL,
    demo_recipe_version character varying(32),
    generation_status public.project_generation_status DEFAULT 'ready'::public.project_generation_status NOT NULL,
    generation_stage character varying(64),
    generation_error text,
    created_by_user_id uuid,
    demo_seeded_at timestamp with time zone,
    demo_last_tick_at timestamp with time zone,
    demo_last_accessed_at timestamp with time zone,
    app_version_keep_releases integer DEFAULT 5 NOT NULL,
    timezone character varying(64) DEFAULT 'UTC'::character varying NOT NULL,
    organization_id uuid NOT NULL,
    CONSTRAINT ck_projects_app_version_keep_releases_range CHECK (((app_version_keep_releases >= 1) AND (app_version_keep_releases <= 100)))
);
-- tripl:statement
CREATE TABLE public.property_drifts (
    project_id uuid NOT NULL,
    variable_id uuid NOT NULL,
    event_id uuid,
    scan_config_id uuid,
    kind character varying(32) NOT NULL,
    detail json DEFAULT '{}'::json NOT NULL,
    status public.schema_drift_status DEFAULT 'open'::public.schema_drift_status NOT NULL,
    resolution_note text,
    snoozed_until timestamp with time zone,
    resolved_at timestamp with time zone,
    resolved_by uuid,
    detected_at timestamp with time zone DEFAULT now() NOT NULL,
    id uuid NOT NULL
);
-- tripl:statement
CREATE TABLE public.release_comparabilities (
    scan_config_id uuid NOT NULL,
    scope_type public.metric_scope_type NOT NULL,
    app_version_column character varying(255) NOT NULL,
    version character varying(500),
    previous_version character varying(500),
    comparable boolean NOT NULL,
    reason public.release_comparability_reason NOT NULL,
    emerging_share double precision NOT NULL,
    max_emerging_share double precision NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    id uuid NOT NULL
);
-- tripl:statement
CREATE TABLE public.release_regressions (
    scan_config_id uuid NOT NULL,
    scope_type public.metric_scope_type NOT NULL,
    scope_ref character varying(64) NOT NULL,
    event_id uuid,
    event_type_id uuid,
    app_version_column character varying(255) NOT NULL,
    version character varying(500) NOT NULL,
    previous_version character varying(500) NOT NULL,
    kind public.release_regression_kind NOT NULL,
    observed_count bigint NOT NULL,
    expected_count double precision NOT NULL,
    ratio double precision NOT NULL,
    share_prev double precision NOT NULL,
    share_new double precision NOT NULL,
    release_share double precision NOT NULL,
    window_from timestamp with time zone NOT NULL,
    window_to timestamp with time zone NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    id uuid NOT NULL
);
-- tripl:statement
CREATE TABLE public.saml_assertion_ids (
    id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    organization_id uuid NOT NULL,
    assertion_id character varying(255) NOT NULL,
    expires_at timestamp with time zone NOT NULL
);
-- tripl:statement
CREATE TABLE public.scan_configs (
    data_source_id uuid NOT NULL,
    project_id uuid NOT NULL,
    event_type_id uuid,
    name character varying(255) NOT NULL,
    base_query text NOT NULL,
    event_type_column character varying(255),
    time_column character varying(255),
    event_name_format character varying(500),
    json_value_paths json DEFAULT '[]'::json NOT NULL,
    metric_breakdown_columns json DEFAULT '[]'::json NOT NULL,
    metric_breakdown_values_limit integer,
    cardinality_threshold integer NOT NULL,
    "interval" public.scan_interval,
    anomaly_detection_enabled boolean NOT NULL,
    detect_project_total boolean NOT NULL,
    detect_event_types boolean NOT NULL,
    detect_events boolean NOT NULL,
    baseline_window_buckets integer NOT NULL,
    min_history_buckets integer NOT NULL,
    sigma_threshold double precision NOT NULL,
    min_expected_count integer NOT NULL,
    id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    distribution_drift_fields json DEFAULT '[]'::json NOT NULL,
    replay_chunk_interval public.scan_interval,
    event_group_rules json DEFAULT '[]'::json NOT NULL,
    scan_row_limit integer,
    metrics_row_limit integer,
    scan_lookback_hours integer,
    app_version_column character varying(255),
    app_version_keep_releases integer,
    platform_column character varying(255),
    app_version_prerelease_pattern character varying(255),
    app_version_active_share_min double precision,
    last_event_at timestamp with time zone,
    last_collection_at timestamp with time zone,
    setup_preset character varying(32) DEFAULT 'custom'::character varying NOT NULL,
    event_name_column character varying(255),
    properties_column character varying(255),
    json_string_columns json DEFAULT '[]'::json NOT NULL
);
-- tripl:statement
CREATE TABLE public.scan_dry_run_jobs (
    id uuid NOT NULL,
    project_id uuid NOT NULL,
    scan_config_id uuid,
    data_source_id uuid,
    base_query text,
    event_type_id uuid,
    event_type_column character varying(255),
    time_column character varying(255),
    event_name_format character varying(500),
    event_group_rules json NOT NULL,
    json_value_paths json NOT NULL,
    cardinality_threshold integer NOT NULL,
    app_version_column character varying(255),
    platform_column character varying(255),
    scan_lookback_hours integer,
    sample_row_limit integer NOT NULL,
    status public.scan_job_status NOT NULL,
    started_at timestamp with time zone,
    completed_at timestamp with time zone,
    result_summary json,
    error_message text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    setup_preset character varying(32) DEFAULT 'custom'::character varying NOT NULL,
    event_name_column character varying(255),
    properties_column character varying(255),
    json_string_columns json DEFAULT '[]'::json NOT NULL
);
-- tripl:statement
CREATE TABLE public.scan_jobs (
    scan_config_id uuid NOT NULL,
    status public.scan_job_status NOT NULL,
    started_at timestamp with time zone,
    completed_at timestamp with time zone,
    result_summary json,
    error_message text,
    id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    celery_task_id text
);
-- tripl:statement
CREATE TABLE public.scan_preview_jobs (
    id uuid NOT NULL,
    project_id uuid NOT NULL,
    data_source_id uuid NOT NULL,
    base_query text NOT NULL,
    json_value_paths json NOT NULL,
    row_limit integer NOT NULL,
    status public.scan_job_status NOT NULL,
    started_at timestamp with time zone,
    completed_at timestamp with time zone,
    result_summary json,
    error_message text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    time_column character varying(255),
    scan_lookback_hours integer,
    include_json_paths boolean DEFAULT false NOT NULL,
    event_name_column character varying(255),
    properties_column character varying(255),
    json_string_columns json DEFAULT '[]'::json NOT NULL
);
-- tripl:statement
CREATE TABLE public.schema_drifts (
    event_type_id uuid NOT NULL,
    scan_config_id uuid,
    field_name character varying(255) NOT NULL,
    drift_type public.schema_drift_type NOT NULL,
    observed_type character varying(128),
    declared_type character varying(64),
    sample_value text,
    detected_at timestamp with time zone DEFAULT now() NOT NULL,
    id uuid NOT NULL,
    status public.schema_drift_status DEFAULT 'open'::public.schema_drift_status NOT NULL,
    resolution_note text,
    snoozed_until timestamp with time zone,
    resolved_at timestamp with time zone,
    resolved_by uuid
);
-- tripl:statement
CREATE TABLE public.scim_group_links (
    id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    organization_id uuid NOT NULL,
    group_id uuid NOT NULL,
    external_id character varying(255)
);
-- tripl:statement
CREATE TABLE public.scim_user_links (
    id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    organization_id uuid NOT NULL,
    user_id uuid NOT NULL,
    external_id character varying(255),
    given_name character varying(255),
    family_name character varying(255),
    active boolean DEFAULT true NOT NULL,
    removed_outside_scim boolean DEFAULT false NOT NULL
);
-- tripl:statement
CREATE TABLE public.search_documents (
    project_id uuid NOT NULL,
    branch_id uuid NOT NULL,
    entity_type character varying(32) NOT NULL,
    entity_id uuid NOT NULL,
    parent_event_id uuid,
    title character varying(500) NOT NULL,
    subtitle character varying(500) DEFAULT ''::character varying NOT NULL,
    body text DEFAULT ''::text NOT NULL,
    keywords text DEFAULT ''::text NOT NULL,
    route_path text NOT NULL,
    archived boolean DEFAULT false NOT NULL,
    text_vector tsvector,
    embedding public.vector(1536),
    content_hash character varying(64) NOT NULL,
    embedding_status character varying(16) DEFAULT 'disabled'::character varying NOT NULL,
    embedding_model character varying(128),
    id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    description text DEFAULT ''::text NOT NULL,
    builder_version integer DEFAULT 0 NOT NULL
);
-- tripl:statement
CREATE TABLE public.shadow_event_candidates (
    id uuid NOT NULL,
    project_id uuid NOT NULL,
    scan_config_id uuid NOT NULL,
    event_type_id uuid,
    event_name character varying(500) NOT NULL,
    observed_count bigint NOT NULL,
    first_seen_at timestamp with time zone NOT NULL,
    last_seen_at timestamp with time zone NOT NULL,
    status public.shadow_event_status NOT NULL,
    accepted_event_id uuid,
    resolved_by uuid,
    resolved_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    sample_properties json DEFAULT '[]'::json NOT NULL
);
-- tripl:statement
CREATE TABLE public.signal_triage (
    id uuid NOT NULL,
    project_id uuid NOT NULL,
    scan_config_id uuid,
    scope_type public.metric_scope_type NOT NULL,
    scope_ref character varying(64) NOT NULL,
    action public.signal_triage_action NOT NULL,
    bucket timestamp with time zone,
    muted_until timestamp with time zone,
    note text,
    annotation_id uuid,
    created_by_user_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    expected_reason public.signal_expected_reason,
    CONSTRAINT ck_signal_triage_bucket_matches_action CHECK (((action = 'muted'::public.signal_triage_action) = (bucket IS NULL)))
);
-- tripl:statement
CREATE TABLE public.sso_link_tickets (
    id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    ticket_hash character varying(64) NOT NULL,
    user_id uuid NOT NULL,
    organization_id uuid NOT NULL,
    issuer character varying(512) NOT NULL,
    subject character varying(255) NOT NULL,
    next_path character varying(2048) NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    used_at timestamp with time zone
);
-- tripl:statement
CREATE TABLE public.sso_login_states (
    id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    organization_id uuid NOT NULL,
    state_hash character varying(64) NOT NULL,
    nonce character varying(128) NOT NULL,
    code_verifier text NOT NULL,
    next_path character varying(2048) NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    used_at timestamp with time zone,
    request_id character varying(128)
);
-- tripl:statement
CREATE TABLE public.sso_membership_blocks (
    id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    organization_id uuid NOT NULL,
    user_id uuid NOT NULL
);
-- tripl:statement
CREATE TABLE public.subscriptions (
    id uuid NOT NULL,
    user_id uuid NOT NULL,
    project_id uuid NOT NULL,
    entity_type character varying(16) NOT NULL,
    entity_id uuid NOT NULL,
    reasons json DEFAULT '[]'::json NOT NULL,
    muted boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_subscription_entity_type CHECK (((entity_type)::text = ANY ((ARRAY['event'::character varying, 'event_type'::character varying, 'metric'::character varying, 'branch'::character varying])::text[])))
);
-- tripl:statement
CREATE TABLE public.user_notification_prefs (
    user_id uuid NOT NULL,
    email_mode character varying(16) DEFAULT 'daily'::character varying NOT NULL,
    mentions_email boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_user_notification_prefs_email_mode CHECK (((email_mode)::text = ANY ((ARRAY['off'::character varying, 'instant'::character varying, 'daily'::character varying, 'weekly'::character varying])::text[])))
);
-- tripl:statement
CREATE TABLE public.user_sessions (
    user_id uuid NOT NULL,
    session_token_hash character varying(64) NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    auth_method character varying(16) DEFAULT 'password'::character varying NOT NULL,
    sso_organization_id uuid,
    CONSTRAINT ck_user_sessions_auth_method CHECK (((auth_method)::text = ANY ((ARRAY['password'::character varying, 'sso'::character varying])::text[])))
);
-- tripl:statement
CREATE TABLE public.user_sso_identities (
    id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    user_id uuid NOT NULL,
    organization_id uuid NOT NULL,
    issuer character varying(512) NOT NULL,
    subject character varying(255) NOT NULL
);
-- tripl:statement
CREATE TABLE public.users (
    email character varying(320) NOT NULL,
    name character varying(255),
    password_hash text NOT NULL,
    id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    is_platform_admin boolean DEFAULT false NOT NULL,
    email_verified_at timestamp with time zone
);
-- tripl:statement
CREATE TABLE public.variable_event_value_overrides (
    project_id uuid NOT NULL,
    branch_id uuid NOT NULL,
    variable_id uuid NOT NULL,
    event_id uuid NOT NULL,
    "values" json,
    id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    required boolean DEFAULT false NOT NULL
);
-- tripl:statement
CREATE TABLE public.variable_value_drifts (
    project_id uuid NOT NULL,
    variable_id uuid NOT NULL,
    event_id uuid NOT NULL,
    scan_config_id uuid,
    observed_values json DEFAULT '[]'::json NOT NULL,
    status public.schema_drift_status DEFAULT 'open'::public.schema_drift_status NOT NULL,
    resolution_note text,
    snoozed_until timestamp with time zone,
    resolved_at timestamp with time zone,
    resolved_by uuid,
    detected_at timestamp with time zone DEFAULT now() NOT NULL,
    id uuid NOT NULL
);
-- tripl:statement
CREATE TABLE public.variable_values (
    project_id uuid NOT NULL,
    branch_id uuid NOT NULL,
    variable_id uuid NOT NULL,
    event_id uuid NOT NULL,
    field_definition_id uuid NOT NULL,
    source_column character varying(255) NOT NULL,
    value_kind public.variable_value_kind NOT NULL,
    observed_count integer NOT NULL,
    "values" json DEFAULT '[]'::json NOT NULL,
    id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    presence_rate double precision
);
-- tripl:statement
CREATE TABLE public.variables (
    project_id uuid NOT NULL,
    name character varying(100) NOT NULL,
    source_name character varying(100),
    variable_type public.variable_type NOT NULL,
    description text NOT NULL,
    id uuid NOT NULL,
    branch_id uuid NOT NULL,
    allowed_values json DEFAULT '[]'::json NOT NULL,
    bindings json DEFAULT '[]'::json NOT NULL,
    excluded_from_scans boolean DEFAULT false NOT NULL,
    json_schema json,
    type_checked_at timestamp with time zone
);
-- tripl:statement
ALTER TABLE ONLY public.alert_correlation_states
    ADD CONSTRAINT alert_correlation_states_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.alert_deliveries
    ADD CONSTRAINT alert_deliveries_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.alert_delivery_items
    ADD CONSTRAINT alert_delivery_items_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.alert_destinations
    ADD CONSTRAINT alert_destinations_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.alert_owner_notifications
    ADD CONSTRAINT alert_owner_notifications_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.alert_pending_items
    ADD CONSTRAINT alert_pending_items_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.alert_rule_filters
    ADD CONSTRAINT alert_rule_filters_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.alert_rule_states
    ADD CONSTRAINT alert_rule_states_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.alert_rules
    ADD CONSTRAINT alert_rules_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.anomaly_scope_overrides
    ADD CONSTRAINT anomaly_scope_overrides_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.api_keys
    ADD CONSTRAINT api_keys_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.app_settings
    ADD CONSTRAINT app_settings_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.audit_log
    ADD CONSTRAINT audit_log_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.audit_webhook_outbox
    ADD CONSTRAINT audit_webhook_outbox_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.chart_annotations
    ADD CONSTRAINT chart_annotations_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.coverage_metrics
    ADD CONSTRAINT coverage_metrics_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.data_sources
    ADD CONSTRAINT data_sources_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.distribution_drifts
    ADD CONSTRAINT distribution_drifts_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.doc_files
    ADD CONSTRAINT doc_files_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.doc_folder_settings
    ADD CONSTRAINT doc_folder_settings_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.doc_folder_shares
    ADD CONSTRAINT doc_folder_shares_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.doc_links
    ADD CONSTRAINT doc_links_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.doc_revisions
    ADD CONSTRAINT doc_revisions_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.doc_shares
    ADD CONSTRAINT doc_shares_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.duplicate_dismissals
    ADD CONSTRAINT duplicate_dismissals_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.email_verification_tokens
    ADD CONSTRAINT email_verification_tokens_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.event_changes
    ADD CONSTRAINT event_changes_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.event_field_values
    ADD CONSTRAINT event_field_values_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.event_meta_values
    ADD CONSTRAINT event_meta_values_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.event_metric_breakdowns
    ADD CONSTRAINT event_metric_breakdowns_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.event_metrics
    ADD CONSTRAINT event_metrics_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.event_photo_comments
    ADD CONSTRAINT event_photo_comments_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.event_photos
    ADD CONSTRAINT event_photos_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.event_tags
    ADD CONSTRAINT event_tags_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.event_type_owners
    ADD CONSTRAINT event_type_owners_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.event_type_relations
    ADD CONSTRAINT event_type_relations_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.event_types
    ADD CONSTRAINT event_types_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.events
    ADD CONSTRAINT events_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.fact_tables
    ADD CONSTRAINT fact_tables_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.field_definitions
    ADD CONSTRAINT field_definitions_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.implementation_tickets
    ADD CONSTRAINT implementation_tickets_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.incident_summaries
    ADD CONSTRAINT incident_summaries_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.invitations
    ADD CONSTRAINT invitations_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.lifecycle_findings
    ADD CONSTRAINT lifecycle_findings_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.meta_field_definitions
    ADD CONSTRAINT meta_field_definitions_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.metric_anomalies
    ADD CONSTRAINT metric_anomalies_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.metric_anomaly_attributions
    ADD CONSTRAINT metric_anomaly_attributions_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.metric_baselines
    ADD CONSTRAINT metric_baselines_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.metric_breakdown_anomalies
    ADD CONSTRAINT metric_breakdown_anomalies_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.metric_definitions
    ADD CONSTRAINT metric_definitions_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.metric_value_breakdowns
    ADD CONSTRAINT metric_value_breakdowns_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.metric_values
    ADD CONSTRAINT metric_values_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.notifications
    ADD CONSTRAINT notifications_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.org_audit_webhooks
    ADD CONSTRAINT org_audit_webhooks_organization_id_key UNIQUE (organization_id);
-- tripl:statement
ALTER TABLE ONLY public.org_audit_webhooks
    ADD CONSTRAINT org_audit_webhooks_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.org_scim_configs
    ADD CONSTRAINT org_scim_configs_organization_id_key UNIQUE (organization_id);
-- tripl:statement
ALTER TABLE ONLY public.org_scim_configs
    ADD CONSTRAINT org_scim_configs_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.org_scim_tokens
    ADD CONSTRAINT org_scim_tokens_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.org_sso_configs
    ADD CONSTRAINT org_sso_configs_organization_id_key UNIQUE (organization_id);
-- tripl:statement
ALTER TABLE ONLY public.org_sso_configs
    ADD CONSTRAINT org_sso_configs_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.org_sso_domains
    ADD CONSTRAINT org_sso_domains_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.organization_group_members
    ADD CONSTRAINT organization_group_members_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.organization_groups
    ADD CONSTRAINT organization_groups_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.organization_members
    ADD CONSTRAINT organization_members_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.organizations
    ADD CONSTRAINT organizations_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.password_reset_tokens
    ADD CONSTRAINT password_reset_tokens_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.photo_storage_configs
    ADD CONSTRAINT photo_storage_configs_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.plan_branch_approvals
    ADD CONSTRAINT plan_branch_approvals_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.plan_branch_comments
    ADD CONSTRAINT plan_branch_comments_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.plan_branch_merge_resolutions
    ADD CONSTRAINT plan_branch_merge_resolutions_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.plan_branch_reviewers
    ADD CONSTRAINT plan_branch_reviewers_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.plan_branches
    ADD CONSTRAINT plan_branches_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.plan_revisions
    ADD CONSTRAINT plan_revisions_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.platform_step_ins
    ADD CONSTRAINT platform_step_ins_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.project_anomaly_settings
    ADD CONSTRAINT project_anomaly_settings_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.project_branch_settings
    ADD CONSTRAINT project_branch_settings_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.project_health_snapshots
    ADD CONSTRAINT project_health_snapshots_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.project_members
    ADD CONSTRAINT project_members_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.project_tracker_configs
    ADD CONSTRAINT project_tracker_configs_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.projects
    ADD CONSTRAINT projects_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.property_drifts
    ADD CONSTRAINT property_drifts_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.release_comparabilities
    ADD CONSTRAINT release_comparabilities_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.release_regressions
    ADD CONSTRAINT release_regressions_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.saml_assertion_ids
    ADD CONSTRAINT saml_assertion_ids_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.scan_configs
    ADD CONSTRAINT scan_configs_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.scan_dry_run_jobs
    ADD CONSTRAINT scan_dry_run_jobs_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.scan_jobs
    ADD CONSTRAINT scan_jobs_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.scan_preview_jobs
    ADD CONSTRAINT scan_preview_jobs_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.schema_drifts
    ADD CONSTRAINT schema_drifts_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.scim_group_links
    ADD CONSTRAINT scim_group_links_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.scim_user_links
    ADD CONSTRAINT scim_user_links_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.search_documents
    ADD CONSTRAINT search_documents_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.shadow_event_candidates
    ADD CONSTRAINT shadow_event_candidates_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.signal_triage
    ADD CONSTRAINT signal_triage_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.sso_link_tickets
    ADD CONSTRAINT sso_link_tickets_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.sso_login_states
    ADD CONSTRAINT sso_login_states_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.sso_membership_blocks
    ADD CONSTRAINT sso_membership_blocks_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.subscriptions
    ADD CONSTRAINT subscriptions_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.alert_correlation_states
    ADD CONSTRAINT uq_alert_correlation_state_project_group UNIQUE (project_id, correlation_group_id);
-- tripl:statement
ALTER TABLE ONLY public.alert_owner_notifications
    ADD CONSTRAINT uq_alert_owner_notification_delivery_user UNIQUE (delivery_id, user_id);
-- tripl:statement
ALTER TABLE ONLY public.alert_pending_items
    ADD CONSTRAINT uq_alert_pending_item_scope UNIQUE (destination_id, rule_id, scan_config_id, scope_type, scope_ref, direction);
-- tripl:statement
ALTER TABLE ONLY public.alert_rule_states
    ADD CONSTRAINT uq_alert_rule_state_scope UNIQUE (rule_id, scan_config_id, scope_type, scope_ref);
-- tripl:statement
ALTER TABLE ONLY public.anomaly_scope_overrides
    ADD CONSTRAINT uq_anomaly_scope_override_scope UNIQUE (project_id, scan_config_id, scope_type, scope_ref);
-- tripl:statement
ALTER TABLE ONLY public.api_keys
    ADD CONSTRAINT uq_api_keys_key_hash UNIQUE (key_hash);
-- tripl:statement
ALTER TABLE ONLY public.coverage_metrics
    ADD CONSTRAINT uq_coverage_metric_config_bucket UNIQUE (scan_config_id, bucket);
-- tripl:statement
ALTER TABLE ONLY public.data_sources
    ADD CONSTRAINT uq_data_sources_organization_name UNIQUE (organization_id, name);
-- tripl:statement
ALTER TABLE ONLY public.doc_folder_shares
    ADD CONSTRAINT uq_doc_folder_shares_group UNIQUE (folder_id, group_id);
-- tripl:statement
ALTER TABLE ONLY public.doc_folder_shares
    ADD CONSTRAINT uq_doc_folder_shares_user UNIQUE (folder_id, user_id);
-- tripl:statement
ALTER TABLE ONLY public.doc_revisions
    ADD CONSTRAINT uq_doc_revisions_file_number UNIQUE (doc_file_id, number);
-- tripl:statement
ALTER TABLE ONLY public.doc_shares
    ADD CONSTRAINT uq_doc_shares_group UNIQUE (doc_file_id, group_id);
-- tripl:statement
ALTER TABLE ONLY public.doc_shares
    ADD CONSTRAINT uq_doc_shares_user UNIQUE (doc_file_id, user_id);
-- tripl:statement
ALTER TABLE ONLY public.duplicate_dismissals
    ADD CONSTRAINT uq_duplicate_dismissal UNIQUE (project_id, event_a_id, event_b_id);
-- tripl:statement
ALTER TABLE ONLY public.event_field_values
    ADD CONSTRAINT uq_event_field_value_event_field UNIQUE (event_id, field_definition_id);
-- tripl:statement
ALTER TABLE ONLY public.event_meta_values
    ADD CONSTRAINT uq_event_meta_value_event_meta_value UNIQUE (event_id, meta_field_definition_id, value);
-- tripl:statement
ALTER TABLE ONLY public.event_metric_breakdowns
    ADD CONSTRAINT uq_event_metric_breakdown_config_event_bucket_value UNIQUE (scan_config_id, event_id, bucket, breakdown_column, breakdown_value, is_other);
-- tripl:statement
ALTER TABLE ONLY public.event_metric_breakdowns
    ADD CONSTRAINT uq_event_metric_breakdown_config_type_bucket_value UNIQUE (scan_config_id, event_type_id, bucket, breakdown_column, breakdown_value, is_other);
-- tripl:statement
ALTER TABLE ONLY public.event_metrics
    ADD CONSTRAINT uq_event_metric_config_event_bucket UNIQUE (scan_config_id, event_id, bucket);
-- tripl:statement
ALTER TABLE ONLY public.event_metrics
    ADD CONSTRAINT uq_event_metric_config_type_bucket UNIQUE (scan_config_id, event_type_id, bucket);
-- tripl:statement
ALTER TABLE ONLY public.events
    ADD CONSTRAINT uq_event_scan_identity UNIQUE (event_type_id, source_name);
-- tripl:statement
ALTER TABLE ONLY public.event_tags
    ADD CONSTRAINT uq_event_tag UNIQUE (event_id, name);
-- tripl:statement
ALTER TABLE ONLY public.event_type_owners
    ADD CONSTRAINT uq_event_type_owner UNIQUE (event_type_id, user_id);
-- tripl:statement
ALTER TABLE ONLY public.event_types
    ADD CONSTRAINT uq_event_type_project_name UNIQUE (project_id, branch_id, name);
-- tripl:statement
ALTER TABLE ONLY public.fact_tables
    ADD CONSTRAINT uq_fact_table_project_name UNIQUE (project_id, name);
-- tripl:statement
ALTER TABLE ONLY public.field_definitions
    ADD CONSTRAINT uq_field_def_event_type_name UNIQUE (event_type_id, name);
-- tripl:statement
ALTER TABLE ONLY public.implementation_tickets
    ADD CONSTRAINT uq_implementation_ticket_branch UNIQUE (branch_id);
-- tripl:statement
ALTER TABLE ONLY public.incident_summaries
    ADD CONSTRAINT uq_incident_summary_project_group UNIQUE (project_id, correlation_group_id);
-- tripl:statement
ALTER TABLE ONLY public.lifecycle_findings
    ADD CONSTRAINT uq_lifecycle_finding_event_kind UNIQUE (event_id, kind);
-- tripl:statement
ALTER TABLE ONLY public.meta_field_definitions
    ADD CONSTRAINT uq_meta_field_def_project_name UNIQUE (project_id, branch_id, name);
-- tripl:statement
ALTER TABLE ONLY public.metric_anomaly_attributions
    ADD CONSTRAINT uq_metric_anomaly_attribution_anomaly UNIQUE (anomaly_id);
-- tripl:statement
ALTER TABLE ONLY public.metric_anomalies
    ADD CONSTRAINT uq_metric_anomaly_scope_bucket UNIQUE (scan_config_id, scope_type, scope_ref, bucket);
-- tripl:statement
ALTER TABLE ONLY public.metric_baselines
    ADD CONSTRAINT uq_metric_baseline_scope_bucket UNIQUE (scan_config_id, scope_type, scope_ref, bucket);
-- tripl:statement
ALTER TABLE ONLY public.metric_breakdown_anomalies
    ADD CONSTRAINT uq_metric_breakdown_anomaly_scope_bucket_value UNIQUE (scan_config_id, scope_type, scope_ref, breakdown_column, breakdown_value, is_other, bucket, kind);
-- tripl:statement
ALTER TABLE ONLY public.metric_definitions
    ADD CONSTRAINT uq_metric_def_project_name UNIQUE (project_id, name);
-- tripl:statement
ALTER TABLE ONLY public.metric_value_breakdowns
    ADD CONSTRAINT uq_metric_value_breakdown_def_config_bucket_value UNIQUE (metric_definition_id, scan_config_id, bucket, breakdown_column, breakdown_value, is_other);
-- tripl:statement
ALTER TABLE ONLY public.metric_values
    ADD CONSTRAINT uq_metric_value_def_config_bucket UNIQUE (metric_definition_id, scan_config_id, bucket);
-- tripl:statement
ALTER TABLE ONLY public.org_sso_domains
    ADD CONSTRAINT uq_org_sso_domains_org_domain UNIQUE (organization_id, domain);
-- tripl:statement
ALTER TABLE ONLY public.organization_group_members
    ADD CONSTRAINT uq_organization_group_member UNIQUE (group_id, user_id);
-- tripl:statement
ALTER TABLE ONLY public.organization_members
    ADD CONSTRAINT uq_organization_member UNIQUE (organization_id, user_id);
-- tripl:statement
ALTER TABLE ONLY public.photo_storage_configs
    ADD CONSTRAINT uq_photo_storage_configs_org_hash UNIQUE (organization_id, config_hash);
-- tripl:statement
ALTER TABLE ONLY public.plan_branch_approvals
    ADD CONSTRAINT uq_plan_branch_approval UNIQUE (branch_id, user_id);
-- tripl:statement
ALTER TABLE ONLY public.plan_branch_merge_resolutions
    ADD CONSTRAINT uq_plan_branch_merge_resolution UNIQUE (branch_id, entity_type, entity_name, field_name);
-- tripl:statement
ALTER TABLE ONLY public.plan_branches
    ADD CONSTRAINT uq_plan_branch_project_name UNIQUE (project_id, name);
-- tripl:statement
ALTER TABLE ONLY public.plan_branch_reviewers
    ADD CONSTRAINT uq_plan_branch_reviewer UNIQUE (branch_id, user_id);
-- tripl:statement
ALTER TABLE ONLY public.project_anomaly_settings
    ADD CONSTRAINT uq_project_anomaly_settings_project UNIQUE (project_id);
-- tripl:statement
ALTER TABLE ONLY public.project_branch_settings
    ADD CONSTRAINT uq_project_branch_settings_project UNIQUE (project_id);
-- tripl:statement
ALTER TABLE ONLY public.project_health_snapshots
    ADD CONSTRAINT uq_project_health_snapshot_day UNIQUE (project_id, day);
-- tripl:statement
ALTER TABLE ONLY public.project_members
    ADD CONSTRAINT uq_project_member UNIQUE (project_id, user_id);
-- tripl:statement
ALTER TABLE ONLY public.project_tracker_configs
    ADD CONSTRAINT uq_project_tracker_config_project UNIQUE (project_id);
-- tripl:statement
ALTER TABLE ONLY public.projects
    ADD CONSTRAINT uq_projects_id_organization UNIQUE (id, organization_id);
-- tripl:statement
ALTER TABLE ONLY public.projects
    ADD CONSTRAINT uq_projects_organization_slug UNIQUE (organization_id, slug);
-- tripl:statement
ALTER TABLE ONLY public.property_drifts
    ADD CONSTRAINT uq_property_drift UNIQUE (variable_id, event_id, kind);
-- tripl:statement
ALTER TABLE ONLY public.release_comparabilities
    ADD CONSTRAINT uq_release_comparability_scan_scope UNIQUE (scan_config_id, scope_type);
-- tripl:statement
ALTER TABLE ONLY public.release_regressions
    ADD CONSTRAINT uq_release_regression_scope_version UNIQUE (scan_config_id, scope_type, scope_ref, version);
-- tripl:statement
ALTER TABLE ONLY public.saml_assertion_ids
    ADD CONSTRAINT uq_saml_assertion_ids_org_assertion UNIQUE (organization_id, assertion_id);
-- tripl:statement
ALTER TABLE ONLY public.scan_configs
    ADD CONSTRAINT uq_scan_config_ds_name UNIQUE (data_source_id, name);
-- tripl:statement
ALTER TABLE ONLY public.schema_drifts
    ADD CONSTRAINT uq_schema_drift_event_type_field_kind UNIQUE (event_type_id, field_name, drift_type);
-- tripl:statement
ALTER TABLE ONLY public.scim_group_links
    ADD CONSTRAINT uq_scim_group_links_group UNIQUE (group_id);
-- tripl:statement
ALTER TABLE ONLY public.scim_user_links
    ADD CONSTRAINT uq_scim_user_links_org_user UNIQUE (organization_id, user_id);
-- tripl:statement
ALTER TABLE ONLY public.search_documents
    ADD CONSTRAINT uq_search_document_entity UNIQUE (project_id, branch_id, entity_type, entity_id);
-- tripl:statement
ALTER TABLE ONLY public.shadow_event_candidates
    ADD CONSTRAINT uq_shadow_candidate_config_name UNIQUE (scan_config_id, event_name);
-- tripl:statement
ALTER TABLE ONLY public.signal_triage
    ADD CONSTRAINT uq_signal_triage_signal UNIQUE (project_id, scan_config_id, scope_type, scope_ref, action, bucket);
-- tripl:statement
ALTER TABLE ONLY public.sso_membership_blocks
    ADD CONSTRAINT uq_sso_membership_blocks_org_user UNIQUE (organization_id, user_id);
-- tripl:statement
ALTER TABLE ONLY public.subscriptions
    ADD CONSTRAINT uq_subscription_user_entity UNIQUE (user_id, entity_type, entity_id);
-- tripl:statement
ALTER TABLE ONLY public.user_sso_identities
    ADD CONSTRAINT uq_user_sso_identities_subject UNIQUE (issuer, subject, organization_id);
-- tripl:statement
ALTER TABLE ONLY public.variable_event_value_overrides
    ADD CONSTRAINT uq_variable_event_value_override UNIQUE (variable_id, event_id);
-- tripl:statement
ALTER TABLE ONLY public.variables
    ADD CONSTRAINT uq_variable_project_name UNIQUE (project_id, branch_id, name);
-- tripl:statement
ALTER TABLE ONLY public.variables
    ADD CONSTRAINT uq_variable_project_source_name UNIQUE (project_id, branch_id, source_name);
-- tripl:statement
ALTER TABLE ONLY public.variable_values
    ADD CONSTRAINT uq_variable_value_context UNIQUE (variable_id, event_id, field_definition_id);
-- tripl:statement
ALTER TABLE ONLY public.variable_value_drifts
    ADD CONSTRAINT uq_variable_value_drift_context UNIQUE (variable_id, event_id);
-- tripl:statement
ALTER TABLE ONLY public.user_notification_prefs
    ADD CONSTRAINT user_notification_prefs_pkey PRIMARY KEY (user_id);
-- tripl:statement
ALTER TABLE ONLY public.user_sessions
    ADD CONSTRAINT user_sessions_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.user_sso_identities
    ADD CONSTRAINT user_sso_identities_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.variable_event_value_overrides
    ADD CONSTRAINT variable_event_value_overrides_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.variable_value_drifts
    ADD CONSTRAINT variable_value_drifts_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.variable_values
    ADD CONSTRAINT variable_values_pkey PRIMARY KEY (id);
-- tripl:statement
ALTER TABLE ONLY public.variables
    ADD CONSTRAINT variables_pkey PRIMARY KEY (id);
-- tripl:statement
CREATE INDEX ix_alert_correlation_state_project_status ON public.alert_correlation_states USING btree (project_id, status);
-- tripl:statement
CREATE INDEX ix_alert_delivery_destination_created ON public.alert_deliveries USING btree (destination_id, created_at);
-- tripl:statement
CREATE INDEX ix_alert_delivery_item_correlation_group ON public.alert_delivery_items USING btree (correlation_group_id);
-- tripl:statement
CREATE INDEX ix_alert_delivery_item_delivery ON public.alert_delivery_items USING btree (delivery_id);
-- tripl:statement
CREATE INDEX ix_alert_delivery_project_created ON public.alert_deliveries USING btree (project_id, created_at);
-- tripl:statement
CREATE INDEX ix_alert_delivery_rule_created ON public.alert_deliveries USING btree (rule_id, created_at);
-- tripl:statement
CREATE INDEX ix_alert_delivery_scan_created ON public.alert_deliveries USING btree (scan_config_id, created_at);
-- tripl:statement
CREATE INDEX ix_alert_destination_project ON public.alert_destinations USING btree (project_id);
-- tripl:statement
CREATE INDEX ix_alert_owner_notification_group ON public.alert_owner_notifications USING btree (correlation_group_id);
-- tripl:statement
CREATE INDEX ix_alert_owner_notification_project_created ON public.alert_owner_notifications USING btree (project_id, created_at);
-- tripl:statement
CREATE INDEX ix_alert_owner_notification_target_user ON public.alert_owner_notifications USING btree (project_id, target_key, user_id);
-- tripl:statement
CREATE INDEX ix_alert_pending_item_updated ON public.alert_pending_items USING btree (updated_at);
-- tripl:statement
CREATE INDEX ix_alert_rule_destination ON public.alert_rules USING btree (destination_id);
-- tripl:statement
CREATE INDEX ix_alert_rule_filter_rule ON public.alert_rule_filters USING btree (rule_id);
-- tripl:statement
CREATE INDEX ix_alert_rule_scan_config ON public.alert_rules USING btree (scan_config_id);
-- tripl:statement
CREATE INDEX ix_alert_rule_state_rule ON public.alert_rule_states USING btree (rule_id);
-- tripl:statement
CREATE INDEX ix_alert_rule_state_scan ON public.alert_rule_states USING btree (scan_config_id);
-- tripl:statement
CREATE INDEX ix_api_keys_key_prefix ON public.api_keys USING btree (key_prefix);
-- tripl:statement
CREATE INDEX ix_api_keys_organization_id ON public.api_keys USING btree (organization_id);
-- tripl:statement
CREATE INDEX ix_api_keys_project_id ON public.api_keys USING btree (project_id);
-- tripl:statement
CREATE INDEX ix_api_keys_user_id ON public.api_keys USING btree (user_id);
-- tripl:statement
CREATE INDEX ix_audit_log_branch ON public.audit_log USING btree (branch_id);
-- tripl:statement
CREATE INDEX ix_audit_log_created ON public.audit_log USING btree (created_at, id);
-- tripl:statement
CREATE INDEX ix_audit_log_organization_created ON public.audit_log USING btree (organization_id, created_at, id);
-- tripl:statement
CREATE INDEX ix_audit_log_project_created ON public.audit_log USING btree (project_id, created_at, id);
-- tripl:statement
CREATE INDEX ix_audit_log_project_slug_created ON public.audit_log USING btree (project_slug, created_at, id);
-- tripl:statement
CREATE INDEX ix_audit_webhook_outbox_audit_log ON public.audit_webhook_outbox USING btree (audit_log_id);
-- tripl:statement
CREATE INDEX ix_audit_webhook_outbox_org_created ON public.audit_webhook_outbox USING btree (organization_id, created_at);
-- tripl:statement
CREATE INDEX ix_audit_webhook_outbox_status_next_attempt ON public.audit_webhook_outbox USING btree (status, next_attempt_at);
-- tripl:statement
CREATE INDEX ix_chart_annotation_project_bucket ON public.chart_annotations USING btree (project_id, bucket);
-- tripl:statement
CREATE INDEX ix_chart_annotation_project_source_label ON public.chart_annotations USING btree (project_id, source, label);
-- tripl:statement
CREATE INDEX ix_chart_annotation_scope ON public.chart_annotations USING btree (project_id, scope_type, scope_ref);
-- tripl:statement
CREATE INDEX ix_data_sources_organization_id ON public.data_sources USING btree (organization_id);
-- tripl:statement
CREATE INDEX ix_data_sources_project_id ON public.data_sources USING btree (project_id);
-- tripl:statement
CREATE INDEX ix_distribution_drift_event_type ON public.distribution_drifts USING btree (event_type_id, bucket);
-- tripl:statement
CREATE INDEX ix_distribution_drift_scan_field_bucket ON public.distribution_drifts USING btree (scan_config_id, field_name, bucket);
-- tripl:statement
CREATE INDEX ix_doc_files_org ON public.doc_files USING btree (organization_id);
-- tripl:statement
CREATE INDEX ix_doc_folder_settings_org ON public.doc_folder_settings USING btree (organization_id);
-- tripl:statement
CREATE INDEX ix_doc_folder_shares_group_id ON public.doc_folder_shares USING btree (group_id);
-- tripl:statement
CREATE INDEX ix_doc_folder_shares_user_id ON public.doc_folder_shares USING btree (user_id);
-- tripl:statement
CREATE INDEX ix_doc_links_doc_file_id ON public.doc_links USING btree (doc_file_id);
-- tripl:statement
CREATE INDEX ix_doc_links_target ON public.doc_links USING btree (kind, target);
-- tripl:statement
CREATE INDEX ix_doc_revisions_doc_file_id ON public.doc_revisions USING btree (doc_file_id);
-- tripl:statement
CREATE INDEX ix_doc_shares_group_id ON public.doc_shares USING btree (group_id);
-- tripl:statement
CREATE INDEX ix_doc_shares_user_id ON public.doc_shares USING btree (user_id);
-- tripl:statement
CREATE INDEX ix_duplicate_dismissals_event_a_id ON public.duplicate_dismissals USING btree (event_a_id);
-- tripl:statement
CREATE INDEX ix_duplicate_dismissals_event_b_id ON public.duplicate_dismissals USING btree (event_b_id);
-- tripl:statement
CREATE INDEX ix_duplicate_dismissals_project_id ON public.duplicate_dismissals USING btree (project_id);
-- tripl:statement
CREATE INDEX ix_email_verification_tokens_expires_at ON public.email_verification_tokens USING btree (expires_at);
-- tripl:statement
CREATE UNIQUE INDEX ix_email_verification_tokens_token_hash ON public.email_verification_tokens USING btree (token_hash);
-- tripl:statement
CREATE INDEX ix_email_verification_tokens_user_id ON public.email_verification_tokens USING btree (user_id);
-- tripl:statement
CREATE INDEX ix_event_changes_event_id ON public.event_changes USING btree (event_id);
-- tripl:statement
CREATE INDEX ix_event_event_type ON public.events USING btree (event_type_id);
-- tripl:statement
CREATE INDEX ix_event_metric_breakdown_config_column_bucket ON public.event_metric_breakdowns USING btree (scan_config_id, breakdown_column, bucket);
-- tripl:statement
CREATE INDEX ix_event_metric_breakdown_event_bucket ON public.event_metric_breakdowns USING btree (event_id, breakdown_column, bucket);
-- tripl:statement
CREATE INDEX ix_event_metric_breakdown_type_bucket ON public.event_metric_breakdowns USING btree (event_type_id, breakdown_column, bucket);
-- tripl:statement
CREATE INDEX ix_event_metric_config_bucket ON public.event_metrics USING btree (scan_config_id, bucket);
-- tripl:statement
CREATE INDEX ix_event_metric_event_bucket ON public.event_metrics USING btree (event_id, bucket);
-- tripl:statement
CREATE INDEX ix_event_metric_type_bucket ON public.event_metrics USING btree (event_type_id, bucket);
-- tripl:statement
CREATE INDEX ix_event_photo_comment_event_status ON public.event_photo_comments USING btree (event_id, status);
-- tripl:statement
CREATE INDEX ix_event_photo_comment_photo_created ON public.event_photo_comments USING btree (photo_id, created_at);
-- tripl:statement
CREATE INDEX ix_event_photo_event_order ON public.event_photos USING btree (event_id, sort_order);
-- tripl:statement
CREATE INDEX ix_event_photos_storage_config_id ON public.event_photos USING btree (storage_config_id);
-- tripl:statement
CREATE INDEX ix_event_project_order ON public.events USING btree (project_id, "order");
-- tripl:statement
CREATE INDEX ix_event_tag_name ON public.event_tags USING btree (name);
-- tripl:statement
CREATE INDEX ix_event_type_owners_event_type_id ON public.event_type_owners USING btree (event_type_id);
-- tripl:statement
CREATE INDEX ix_event_type_relations_branch_id ON public.event_type_relations USING btree (branch_id);
-- tripl:statement
CREATE INDEX ix_event_type_relations_origin_id ON public.event_type_relations USING btree (origin_id);
-- tripl:statement
CREATE INDEX ix_event_types_branch_id ON public.event_types USING btree (branch_id);
-- tripl:statement
CREATE INDEX ix_events_branch_id ON public.events USING btree (branch_id);
-- tripl:statement
CREATE INDEX ix_events_description_trgm ON public.events USING gin (description public.gin_trgm_ops);
-- tripl:statement
CREATE INDEX ix_events_last_seen_at ON public.events USING btree (last_seen_at);
-- tripl:statement
CREATE INDEX ix_events_name_trgm ON public.events USING gin (name public.gin_trgm_ops);
-- tripl:statement
CREATE INDEX ix_events_origin_id ON public.events USING btree (origin_id);
-- tripl:statement
CREATE INDEX ix_events_owner_id ON public.events USING btree (owner_id);
-- tripl:statement
CREATE INDEX ix_events_project_branch_status ON public.events USING btree (project_id, branch_id, status);
-- tripl:statement
CREATE INDEX ix_events_source_name_trgm ON public.events USING gin (source_name public.gin_trgm_ops);
-- tripl:statement
CREATE INDEX ix_events_superseded_by_event_id ON public.events USING btree (superseded_by_event_id);
-- tripl:statement
CREATE INDEX ix_fact_table_project_order ON public.fact_tables USING btree (project_id, "order");
-- tripl:statement
CREATE INDEX ix_invitations_email ON public.invitations USING btree (email);
-- tripl:statement
CREATE INDEX ix_invitations_expires_at ON public.invitations USING btree (expires_at);
-- tripl:statement
CREATE INDEX ix_invitations_invited_by_user_id ON public.invitations USING btree (invited_by_user_id);
-- tripl:statement
CREATE INDEX ix_invitations_organization_id ON public.invitations USING btree (organization_id);
-- tripl:statement
CREATE UNIQUE INDEX ix_invitations_token_hash ON public.invitations USING btree (token_hash);
-- tripl:statement
CREATE INDEX ix_lifecycle_finding_project_open ON public.lifecycle_findings USING btree (project_id, resolved_at);
-- tripl:statement
CREATE INDEX ix_lifecycle_finding_related_event ON public.lifecycle_findings USING btree (related_event_id);
-- tripl:statement
CREATE INDEX ix_meta_field_definitions_branch_id ON public.meta_field_definitions USING btree (branch_id);
-- tripl:statement
CREATE INDEX ix_metric_anomaly_event_bucket ON public.metric_anomalies USING btree (event_id, bucket);
-- tripl:statement
CREATE INDEX ix_metric_anomaly_scope_ref_bucket ON public.metric_anomalies USING btree (scope_ref, bucket);
-- tripl:statement
CREATE INDEX ix_metric_anomaly_type_bucket ON public.metric_anomalies USING btree (event_type_id, bucket);
-- tripl:statement
CREATE INDEX ix_metric_breakdown_anomaly_event_bucket ON public.metric_breakdown_anomalies USING btree (event_id, breakdown_column, bucket);
-- tripl:statement
CREATE INDEX ix_metric_breakdown_anomaly_type_bucket ON public.metric_breakdown_anomalies USING btree (event_type_id, breakdown_column, bucket);
-- tripl:statement
CREATE INDEX ix_metric_def_project_order ON public.metric_definitions USING btree (project_id, "order");
-- tripl:statement
CREATE INDEX ix_metric_def_project_status ON public.metric_definitions USING btree (project_id, status);
-- tripl:statement
CREATE INDEX ix_metric_definitions_owner_id ON public.metric_definitions USING btree (owner_id);
-- tripl:statement
CREATE INDEX ix_metric_value_breakdown_def_bucket ON public.metric_value_breakdowns USING btree (metric_definition_id, bucket);
-- tripl:statement
CREATE INDEX ix_metric_value_def_bucket ON public.metric_values USING btree (metric_definition_id, bucket);
-- tripl:statement
CREATE INDEX ix_notification_entity_kind ON public.notifications USING btree (entity_type, entity_id, kind);
-- tripl:statement
CREATE INDEX ix_notification_throttle ON public.notifications USING btree (user_id, entity_type, entity_id, kind, created_at);
-- tripl:statement
CREATE INDEX ix_notification_user_read_created ON public.notifications USING btree (user_id, read_at, created_at);
-- tripl:statement
CREATE INDEX ix_notifications_project_id ON public.notifications USING btree (project_id);
-- tripl:statement
CREATE INDEX ix_org_scim_tokens_organization_id ON public.org_scim_tokens USING btree (organization_id);
-- tripl:statement
CREATE UNIQUE INDEX ix_org_scim_tokens_token_hash ON public.org_scim_tokens USING btree (token_hash);
-- tripl:statement
CREATE INDEX ix_org_sso_domains_organization_id ON public.org_sso_domains USING btree (organization_id);
-- tripl:statement
CREATE INDEX ix_organization_group_members_user_id ON public.organization_group_members USING btree (user_id);
-- tripl:statement
CREATE INDEX ix_organization_groups_organization_id ON public.organization_groups USING btree (organization_id);
-- tripl:statement
CREATE INDEX ix_organization_members_user_id ON public.organization_members USING btree (user_id);
-- tripl:statement
CREATE UNIQUE INDEX ix_organizations_slug ON public.organizations USING btree (slug);
-- tripl:statement
CREATE INDEX ix_password_reset_tokens_expires_at ON public.password_reset_tokens USING btree (expires_at);
-- tripl:statement
CREATE UNIQUE INDEX ix_password_reset_tokens_token_hash ON public.password_reset_tokens USING btree (token_hash);
-- tripl:statement
CREATE INDEX ix_password_reset_tokens_user_id ON public.password_reset_tokens USING btree (user_id);
-- tripl:statement
CREATE INDEX ix_photo_storage_configs_organization_id ON public.photo_storage_configs USING btree (organization_id);
-- tripl:statement
CREATE INDEX ix_plan_branch_approvals_branch_id ON public.plan_branch_approvals USING btree (branch_id);
-- tripl:statement
CREATE INDEX ix_plan_branch_comment_branch ON public.plan_branch_comments USING btree (branch_id);
-- tripl:statement
CREATE INDEX ix_plan_branch_merge_resolutions_branch_id ON public.plan_branch_merge_resolutions USING btree (branch_id);
-- tripl:statement
CREATE INDEX ix_plan_branch_project ON public.plan_branches USING btree (project_id);
-- tripl:statement
CREATE INDEX ix_plan_branch_reviewers_branch_id ON public.plan_branch_reviewers USING btree (branch_id);
-- tripl:statement
CREATE INDEX ix_plan_revisions_project_created ON public.plan_revisions USING btree (project_id, created_at);
-- tripl:statement
CREATE INDEX ix_platform_step_ins_organization_id ON public.platform_step_ins USING btree (organization_id);
-- tripl:statement
CREATE INDEX ix_platform_step_ins_user_org ON public.platform_step_ins USING btree (user_id, organization_id);
-- tripl:statement
CREATE INDEX ix_project_health_snapshot_project_day ON public.project_health_snapshots USING btree (project_id, day);
-- tripl:statement
CREATE INDEX ix_project_members_user_id ON public.project_members USING btree (user_id);
-- tripl:statement
CREATE INDEX ix_projects_organization_id ON public.projects USING btree (organization_id);
-- tripl:statement
CREATE INDEX ix_property_drifts_event_id ON public.property_drifts USING btree (event_id);
-- tripl:statement
CREATE INDEX ix_property_drifts_project_detected ON public.property_drifts USING btree (project_id, detected_at);
-- tripl:statement
CREATE INDEX ix_property_drifts_variable_id ON public.property_drifts USING btree (variable_id);
-- tripl:statement
CREATE INDEX ix_release_regression_event ON public.release_regressions USING btree (event_id);
-- tripl:statement
CREATE INDEX ix_release_regression_event_type ON public.release_regressions USING btree (event_type_id);
-- tripl:statement
CREATE INDEX ix_saml_assertion_ids_expires_at ON public.saml_assertion_ids USING btree (expires_at);
-- tripl:statement
CREATE INDEX ix_saml_assertion_ids_organization_id ON public.saml_assertion_ids USING btree (organization_id);
-- tripl:statement
CREATE INDEX ix_scan_config_project ON public.scan_configs USING btree (project_id);
-- tripl:statement
CREATE INDEX ix_scan_dry_run_job_project ON public.scan_dry_run_jobs USING btree (project_id);
-- tripl:statement
CREATE INDEX ix_scan_job_config_created ON public.scan_jobs USING btree (scan_config_id, created_at);
-- tripl:statement
CREATE INDEX ix_scan_preview_job_project ON public.scan_preview_jobs USING btree (project_id);
-- tripl:statement
CREATE INDEX ix_schema_drift_event_type_detected ON public.schema_drifts USING btree (event_type_id, detected_at);
-- tripl:statement
CREATE INDEX ix_scim_group_links_organization_id ON public.scim_group_links USING btree (organization_id);
-- tripl:statement
CREATE INDEX ix_scim_user_links_organization_id ON public.scim_user_links USING btree (organization_id);
-- tripl:statement
CREATE INDEX ix_scim_user_links_user_id ON public.scim_user_links USING btree (user_id);
-- tripl:statement
CREATE INDEX ix_search_documents_branch_id ON public.search_documents USING btree (branch_id);
-- tripl:statement
CREATE INDEX ix_search_documents_builder_version ON public.search_documents USING btree (builder_version);
-- tripl:statement
CREATE INDEX ix_search_documents_embedding_hnsw ON public.search_documents USING hnsw (embedding public.vector_cosine_ops) WHERE (embedding IS NOT NULL);
-- tripl:statement
CREATE INDEX ix_search_documents_keywords_trgm ON public.search_documents USING gin (keywords public.gin_trgm_ops);
-- tripl:statement
CREATE INDEX ix_search_documents_parent_event ON public.search_documents USING btree (parent_event_id);
-- tripl:statement
CREATE INDEX ix_search_documents_scope ON public.search_documents USING btree (project_id, branch_id, entity_type);
-- tripl:statement
CREATE INDEX ix_search_documents_text_vector ON public.search_documents USING gin (text_vector);
-- tripl:statement
CREATE INDEX ix_search_documents_title_trgm ON public.search_documents USING gin (title public.gin_trgm_ops);
-- tripl:statement
CREATE INDEX ix_shadow_candidate_project_status ON public.shadow_event_candidates USING btree (project_id, status);
-- tripl:statement
CREATE INDEX ix_signal_triage_project_id ON public.signal_triage USING btree (project_id);
-- tripl:statement
CREATE INDEX ix_sso_link_tickets_expires_at ON public.sso_link_tickets USING btree (expires_at);
-- tripl:statement
CREATE INDEX ix_sso_link_tickets_organization_id ON public.sso_link_tickets USING btree (organization_id);
-- tripl:statement
CREATE UNIQUE INDEX ix_sso_link_tickets_ticket_hash ON public.sso_link_tickets USING btree (ticket_hash);
-- tripl:statement
CREATE INDEX ix_sso_link_tickets_user_id ON public.sso_link_tickets USING btree (user_id);
-- tripl:statement
CREATE INDEX ix_sso_login_states_expires_at ON public.sso_login_states USING btree (expires_at);
-- tripl:statement
CREATE INDEX ix_sso_login_states_organization_id ON public.sso_login_states USING btree (organization_id);
-- tripl:statement
CREATE UNIQUE INDEX ix_sso_login_states_state_hash ON public.sso_login_states USING btree (state_hash);
-- tripl:statement
CREATE INDEX ix_sso_membership_blocks_organization_id ON public.sso_membership_blocks USING btree (organization_id);
-- tripl:statement
CREATE INDEX ix_sso_membership_blocks_user_id ON public.sso_membership_blocks USING btree (user_id);
-- tripl:statement
CREATE INDEX ix_subscription_entity ON public.subscriptions USING btree (entity_type, entity_id);
-- tripl:statement
CREATE INDEX ix_subscriptions_project_id ON public.subscriptions USING btree (project_id);
-- tripl:statement
CREATE INDEX ix_user_sessions_expires_at ON public.user_sessions USING btree (expires_at);
-- tripl:statement
CREATE UNIQUE INDEX ix_user_sessions_session_token_hash ON public.user_sessions USING btree (session_token_hash);
-- tripl:statement
CREATE INDEX ix_user_sessions_sso_organization_id ON public.user_sessions USING btree (sso_organization_id);
-- tripl:statement
CREATE INDEX ix_user_sessions_user_id ON public.user_sessions USING btree (user_id);
-- tripl:statement
CREATE INDEX ix_user_sso_identities_organization_id ON public.user_sso_identities USING btree (organization_id);
-- tripl:statement
CREATE INDEX ix_user_sso_identities_user_id ON public.user_sso_identities USING btree (user_id);
-- tripl:statement
CREATE UNIQUE INDEX ix_users_email ON public.users USING btree (email);
-- tripl:statement
CREATE INDEX ix_variable_event_value_overrides_branch_id ON public.variable_event_value_overrides USING btree (branch_id);
-- tripl:statement
CREATE INDEX ix_variable_event_value_overrides_event_id ON public.variable_event_value_overrides USING btree (event_id);
-- tripl:statement
CREATE INDEX ix_variable_event_value_overrides_project_branch ON public.variable_event_value_overrides USING btree (project_id, branch_id);
-- tripl:statement
CREATE INDEX ix_variable_event_value_overrides_variable_id ON public.variable_event_value_overrides USING btree (variable_id);
-- tripl:statement
CREATE INDEX ix_variable_value_drifts_event_id ON public.variable_value_drifts USING btree (event_id);
-- tripl:statement
CREATE INDEX ix_variable_value_drifts_project_detected ON public.variable_value_drifts USING btree (project_id, detected_at);
-- tripl:statement
CREATE INDEX ix_variable_value_drifts_variable_id ON public.variable_value_drifts USING btree (variable_id);
-- tripl:statement
CREATE INDEX ix_variable_values_branch_id ON public.variable_values USING btree (branch_id);
-- tripl:statement
CREATE INDEX ix_variable_values_event ON public.variable_values USING btree (event_id);
-- tripl:statement
CREATE INDEX ix_variable_values_project_branch ON public.variable_values USING btree (project_id, branch_id);
-- tripl:statement
CREATE INDEX ix_variables_branch_id ON public.variables USING btree (branch_id);
-- tripl:statement
CREATE UNIQUE INDEX uq_alert_pending_item_metric_scope ON public.alert_pending_items USING btree (destination_id, rule_id, scope_type, scope_ref, direction) WHERE (scan_config_id IS NULL);
-- tripl:statement
CREATE UNIQUE INDEX uq_alert_rule_state_metric_scope ON public.alert_rule_states USING btree (rule_id, scope_type, scope_ref) WHERE (scan_config_id IS NULL);
-- tripl:statement
CREATE UNIQUE INDEX uq_anomaly_scope_override_metric_scope ON public.anomaly_scope_overrides USING btree (project_id, scope_type, scope_ref) WHERE (scan_config_id IS NULL);
-- tripl:statement
CREATE UNIQUE INDEX uq_app_settings_operator_key ON public.app_settings USING btree (key) WHERE (organization_id IS NULL);
-- tripl:statement
CREATE UNIQUE INDEX uq_app_settings_organization_key ON public.app_settings USING btree (organization_id, key) WHERE (organization_id IS NOT NULL);
-- tripl:statement
CREATE UNIQUE INDEX uq_chart_annotation_release_label ON public.chart_annotations USING btree (project_id, label) WHERE (source = 'release'::public.chart_annotation_source);
-- tripl:statement
CREATE UNIQUE INDEX uq_doc_files_org_path ON public.doc_files USING btree (organization_id, path_key) WHERE (organization_id IS NOT NULL);
-- tripl:statement
CREATE UNIQUE INDEX uq_doc_files_project_path ON public.doc_files USING btree (project_id, path_key) WHERE (project_id IS NOT NULL);
-- tripl:statement
CREATE UNIQUE INDEX uq_doc_folder_settings_org_path ON public.doc_folder_settings USING btree (organization_id, path_key) WHERE (organization_id IS NOT NULL);
-- tripl:statement
CREATE UNIQUE INDEX uq_doc_folder_settings_project_path ON public.doc_folder_settings USING btree (project_id, path_key) WHERE (project_id IS NOT NULL);
-- tripl:statement
CREATE UNIQUE INDEX uq_metric_anomaly_metric_scope ON public.metric_anomalies USING btree (scope_type, scope_ref, bucket) WHERE (scan_config_id IS NULL);
-- tripl:statement
CREATE UNIQUE INDEX uq_metric_value_breakdown_catalog_bucket_value ON public.metric_value_breakdowns USING btree (metric_definition_id, bucket, breakdown_column, breakdown_value, is_other) WHERE (scan_config_id IS NULL);
-- tripl:statement
CREATE UNIQUE INDEX uq_metric_value_catalog_bucket ON public.metric_values USING btree (metric_definition_id, bucket) WHERE (scan_config_id IS NULL);
-- tripl:statement
CREATE UNIQUE INDEX uq_org_sso_domains_verified_domain ON public.org_sso_domains USING btree (domain) WHERE (verified_at IS NOT NULL);
-- tripl:statement
CREATE UNIQUE INDEX uq_organization_group_name_ci ON public.organization_groups USING btree (organization_id, lower((name)::text));
-- tripl:statement
CREATE UNIQUE INDEX uq_property_drift_variable ON public.property_drifts USING btree (variable_id, kind) WHERE (event_id IS NULL);
-- tripl:statement
CREATE UNIQUE INDEX uq_signal_triage_metric_mute ON public.signal_triage USING btree (project_id, scope_type, scope_ref) WHERE ((bucket IS NULL) AND (scan_config_id IS NULL));
-- tripl:statement
CREATE UNIQUE INDEX uq_signal_triage_metric_signal ON public.signal_triage USING btree (project_id, scope_type, scope_ref, action, bucket) WHERE (scan_config_id IS NULL);
-- tripl:statement
CREATE UNIQUE INDEX uq_signal_triage_scope_mute ON public.signal_triage USING btree (project_id, scan_config_id, scope_type, scope_ref) WHERE (bucket IS NULL);
-- tripl:statement
ALTER TABLE ONLY public.alert_correlation_states
    ADD CONSTRAINT alert_correlation_states_acted_by_fkey FOREIGN KEY (acted_by) REFERENCES public.users(id) ON DELETE SET NULL;
-- tripl:statement
ALTER TABLE ONLY public.alert_correlation_states
    ADD CONSTRAINT alert_correlation_states_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.alert_deliveries
    ADD CONSTRAINT alert_deliveries_destination_id_fkey FOREIGN KEY (destination_id) REFERENCES public.alert_destinations(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.alert_deliveries
    ADD CONSTRAINT alert_deliveries_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.alert_deliveries
    ADD CONSTRAINT alert_deliveries_rule_id_fkey FOREIGN KEY (rule_id) REFERENCES public.alert_rules(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.alert_deliveries
    ADD CONSTRAINT alert_deliveries_scan_config_id_fkey FOREIGN KEY (scan_config_id) REFERENCES public.scan_configs(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.alert_deliveries
    ADD CONSTRAINT alert_deliveries_scan_job_id_fkey FOREIGN KEY (scan_job_id) REFERENCES public.scan_jobs(id) ON DELETE SET NULL;
-- tripl:statement
ALTER TABLE ONLY public.alert_delivery_items
    ADD CONSTRAINT alert_delivery_items_delivery_id_fkey FOREIGN KEY (delivery_id) REFERENCES public.alert_deliveries(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.alert_delivery_items
    ADD CONSTRAINT alert_delivery_items_event_id_fkey FOREIGN KEY (event_id) REFERENCES public.events(id) ON DELETE SET NULL;
-- tripl:statement
ALTER TABLE ONLY public.alert_delivery_items
    ADD CONSTRAINT alert_delivery_items_event_type_id_fkey FOREIGN KEY (event_type_id) REFERENCES public.event_types(id) ON DELETE SET NULL;
-- tripl:statement
ALTER TABLE ONLY public.alert_destinations
    ADD CONSTRAINT alert_destinations_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.alert_owner_notifications
    ADD CONSTRAINT alert_owner_notifications_delivery_id_fkey FOREIGN KEY (delivery_id) REFERENCES public.alert_deliveries(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.alert_owner_notifications
    ADD CONSTRAINT alert_owner_notifications_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.alert_owner_notifications
    ADD CONSTRAINT alert_owner_notifications_triggered_by_fkey FOREIGN KEY (triggered_by) REFERENCES public.users(id) ON DELETE SET NULL;
-- tripl:statement
ALTER TABLE ONLY public.alert_owner_notifications
    ADD CONSTRAINT alert_owner_notifications_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE SET NULL;
-- tripl:statement
ALTER TABLE ONLY public.alert_pending_items
    ADD CONSTRAINT alert_pending_items_destination_id_fkey FOREIGN KEY (destination_id) REFERENCES public.alert_destinations(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.alert_pending_items
    ADD CONSTRAINT alert_pending_items_event_id_fkey FOREIGN KEY (event_id) REFERENCES public.events(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.alert_pending_items
    ADD CONSTRAINT alert_pending_items_event_type_id_fkey FOREIGN KEY (event_type_id) REFERENCES public.event_types(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.alert_pending_items
    ADD CONSTRAINT alert_pending_items_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.alert_pending_items
    ADD CONSTRAINT alert_pending_items_rule_id_fkey FOREIGN KEY (rule_id) REFERENCES public.alert_rules(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.alert_pending_items
    ADD CONSTRAINT alert_pending_items_scan_config_id_fkey FOREIGN KEY (scan_config_id) REFERENCES public.scan_configs(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.alert_pending_items
    ADD CONSTRAINT alert_pending_items_scan_job_id_fkey FOREIGN KEY (scan_job_id) REFERENCES public.scan_jobs(id) ON DELETE SET NULL;
-- tripl:statement
ALTER TABLE ONLY public.alert_rule_filters
    ADD CONSTRAINT alert_rule_filters_rule_id_fkey FOREIGN KEY (rule_id) REFERENCES public.alert_rules(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.alert_rule_states
    ADD CONSTRAINT alert_rule_states_last_notified_delivery_id_fkey FOREIGN KEY (last_notified_delivery_id) REFERENCES public.alert_deliveries(id) ON DELETE SET NULL;
-- tripl:statement
ALTER TABLE ONLY public.alert_rule_states
    ADD CONSTRAINT alert_rule_states_rule_id_fkey FOREIGN KEY (rule_id) REFERENCES public.alert_rules(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.alert_rule_states
    ADD CONSTRAINT alert_rule_states_scan_config_id_fkey FOREIGN KEY (scan_config_id) REFERENCES public.scan_configs(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.alert_rules
    ADD CONSTRAINT alert_rules_destination_id_fkey FOREIGN KEY (destination_id) REFERENCES public.alert_destinations(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.anomaly_scope_overrides
    ADD CONSTRAINT anomaly_scope_overrides_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.anomaly_scope_overrides
    ADD CONSTRAINT anomaly_scope_overrides_scan_config_id_fkey FOREIGN KEY (scan_config_id) REFERENCES public.scan_configs(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.api_keys
    ADD CONSTRAINT api_keys_organization_id_fkey FOREIGN KEY (organization_id) REFERENCES public.organizations(id) ON DELETE RESTRICT;
-- tripl:statement
ALTER TABLE ONLY public.api_keys
    ADD CONSTRAINT api_keys_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.app_settings
    ADD CONSTRAINT app_settings_organization_id_fkey FOREIGN KEY (organization_id) REFERENCES public.organizations(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.audit_log
    ADD CONSTRAINT audit_log_organization_id_fkey FOREIGN KEY (organization_id) REFERENCES public.organizations(id) ON DELETE SET NULL;
-- tripl:statement
ALTER TABLE ONLY public.audit_log
    ADD CONSTRAINT audit_log_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE SET NULL;
-- tripl:statement
ALTER TABLE ONLY public.audit_log
    ADD CONSTRAINT audit_log_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE SET NULL;
-- tripl:statement
ALTER TABLE ONLY public.audit_webhook_outbox
    ADD CONSTRAINT audit_webhook_outbox_audit_log_id_fkey FOREIGN KEY (audit_log_id) REFERENCES public.audit_log(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.audit_webhook_outbox
    ADD CONSTRAINT audit_webhook_outbox_organization_id_fkey FOREIGN KEY (organization_id) REFERENCES public.organizations(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.chart_annotations
    ADD CONSTRAINT chart_annotations_created_by_user_id_fkey FOREIGN KEY (created_by_user_id) REFERENCES public.users(id) ON DELETE SET NULL;
-- tripl:statement
ALTER TABLE ONLY public.chart_annotations
    ADD CONSTRAINT chart_annotations_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.coverage_metrics
    ADD CONSTRAINT coverage_metrics_scan_config_id_fkey FOREIGN KEY (scan_config_id) REFERENCES public.scan_configs(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.data_sources
    ADD CONSTRAINT data_sources_organization_id_fkey FOREIGN KEY (organization_id) REFERENCES public.organizations(id) ON DELETE RESTRICT;
-- tripl:statement
ALTER TABLE ONLY public.distribution_drifts
    ADD CONSTRAINT distribution_drifts_event_type_id_fkey FOREIGN KEY (event_type_id) REFERENCES public.event_types(id) ON DELETE SET NULL;
-- tripl:statement
ALTER TABLE ONLY public.distribution_drifts
    ADD CONSTRAINT distribution_drifts_scan_config_id_fkey FOREIGN KEY (scan_config_id) REFERENCES public.scan_configs(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.doc_files
    ADD CONSTRAINT doc_files_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.users(id) ON DELETE SET NULL;
-- tripl:statement
ALTER TABLE ONLY public.doc_files
    ADD CONSTRAINT doc_files_organization_id_fkey FOREIGN KEY (organization_id) REFERENCES public.organizations(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.doc_files
    ADD CONSTRAINT doc_files_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.doc_files
    ADD CONSTRAINT doc_files_updated_by_fkey FOREIGN KEY (updated_by) REFERENCES public.users(id) ON DELETE SET NULL;
-- tripl:statement
ALTER TABLE ONLY public.doc_folder_settings
    ADD CONSTRAINT doc_folder_settings_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.users(id) ON DELETE SET NULL;
-- tripl:statement
ALTER TABLE ONLY public.doc_folder_settings
    ADD CONSTRAINT doc_folder_settings_organization_id_fkey FOREIGN KEY (organization_id) REFERENCES public.organizations(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.doc_folder_settings
    ADD CONSTRAINT doc_folder_settings_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.doc_folder_settings
    ADD CONSTRAINT doc_folder_settings_updated_by_fkey FOREIGN KEY (updated_by) REFERENCES public.users(id) ON DELETE SET NULL;
-- tripl:statement
ALTER TABLE ONLY public.doc_folder_shares
    ADD CONSTRAINT doc_folder_shares_folder_id_fkey FOREIGN KEY (folder_id) REFERENCES public.doc_folder_settings(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.doc_folder_shares
    ADD CONSTRAINT doc_folder_shares_group_id_fkey FOREIGN KEY (group_id) REFERENCES public.organization_groups(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.doc_folder_shares
    ADD CONSTRAINT doc_folder_shares_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.doc_links
    ADD CONSTRAINT doc_links_doc_file_id_fkey FOREIGN KEY (doc_file_id) REFERENCES public.doc_files(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.doc_revisions
    ADD CONSTRAINT doc_revisions_author_id_fkey FOREIGN KEY (author_id) REFERENCES public.users(id) ON DELETE SET NULL;
-- tripl:statement
ALTER TABLE ONLY public.doc_revisions
    ADD CONSTRAINT doc_revisions_doc_file_id_fkey FOREIGN KEY (doc_file_id) REFERENCES public.doc_files(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.doc_shares
    ADD CONSTRAINT doc_shares_doc_file_id_fkey FOREIGN KEY (doc_file_id) REFERENCES public.doc_files(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.doc_shares
    ADD CONSTRAINT doc_shares_group_id_fkey FOREIGN KEY (group_id) REFERENCES public.organization_groups(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.doc_shares
    ADD CONSTRAINT doc_shares_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.duplicate_dismissals
    ADD CONSTRAINT duplicate_dismissals_dismissed_by_fkey FOREIGN KEY (dismissed_by) REFERENCES public.users(id) ON DELETE SET NULL;
-- tripl:statement
ALTER TABLE ONLY public.duplicate_dismissals
    ADD CONSTRAINT duplicate_dismissals_event_a_id_fkey FOREIGN KEY (event_a_id) REFERENCES public.events(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.duplicate_dismissals
    ADD CONSTRAINT duplicate_dismissals_event_b_id_fkey FOREIGN KEY (event_b_id) REFERENCES public.events(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.duplicate_dismissals
    ADD CONSTRAINT duplicate_dismissals_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.email_verification_tokens
    ADD CONSTRAINT email_verification_tokens_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.event_changes
    ADD CONSTRAINT event_changes_event_id_fkey FOREIGN KEY (event_id) REFERENCES public.events(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.event_changes
    ADD CONSTRAINT event_changes_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE SET NULL;
-- tripl:statement
ALTER TABLE ONLY public.event_field_values
    ADD CONSTRAINT event_field_values_event_id_fkey FOREIGN KEY (event_id) REFERENCES public.events(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.event_field_values
    ADD CONSTRAINT event_field_values_field_definition_id_fkey FOREIGN KEY (field_definition_id) REFERENCES public.field_definitions(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.event_meta_values
    ADD CONSTRAINT event_meta_values_event_id_fkey FOREIGN KEY (event_id) REFERENCES public.events(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.event_meta_values
    ADD CONSTRAINT event_meta_values_meta_field_definition_id_fkey FOREIGN KEY (meta_field_definition_id) REFERENCES public.meta_field_definitions(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.event_metric_breakdowns
    ADD CONSTRAINT event_metric_breakdowns_event_id_fkey FOREIGN KEY (event_id) REFERENCES public.events(id) ON DELETE SET NULL;
-- tripl:statement
ALTER TABLE ONLY public.event_metric_breakdowns
    ADD CONSTRAINT event_metric_breakdowns_event_type_id_fkey FOREIGN KEY (event_type_id) REFERENCES public.event_types(id) ON DELETE SET NULL;
-- tripl:statement
ALTER TABLE ONLY public.event_metric_breakdowns
    ADD CONSTRAINT event_metric_breakdowns_scan_config_id_fkey FOREIGN KEY (scan_config_id) REFERENCES public.scan_configs(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.event_metrics
    ADD CONSTRAINT event_metrics_event_id_fkey FOREIGN KEY (event_id) REFERENCES public.events(id) ON DELETE SET NULL;
-- tripl:statement
ALTER TABLE ONLY public.event_metrics
    ADD CONSTRAINT event_metrics_event_type_id_fkey FOREIGN KEY (event_type_id) REFERENCES public.event_types(id) ON DELETE SET NULL;
-- tripl:statement
ALTER TABLE ONLY public.event_metrics
    ADD CONSTRAINT event_metrics_scan_config_id_fkey FOREIGN KEY (scan_config_id) REFERENCES public.scan_configs(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.event_photo_comments
    ADD CONSTRAINT event_photo_comments_parent_id_fkey FOREIGN KEY (parent_id) REFERENCES public.event_photo_comments(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.event_photo_comments
    ADD CONSTRAINT event_photo_comments_photo_id_fkey FOREIGN KEY (photo_id) REFERENCES public.event_photos(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.event_photo_comments
    ADD CONSTRAINT event_photo_comments_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE SET NULL;
-- tripl:statement
ALTER TABLE ONLY public.event_photos
    ADD CONSTRAINT event_photos_event_id_fkey FOREIGN KEY (event_id) REFERENCES public.events(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.event_photos
    ADD CONSTRAINT event_photos_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.event_photos
    ADD CONSTRAINT event_photos_uploaded_by_user_id_fkey FOREIGN KEY (uploaded_by_user_id) REFERENCES public.users(id) ON DELETE SET NULL;
-- tripl:statement
ALTER TABLE ONLY public.event_tags
    ADD CONSTRAINT event_tags_event_id_fkey FOREIGN KEY (event_id) REFERENCES public.events(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.event_type_owners
    ADD CONSTRAINT event_type_owners_event_type_id_fkey FOREIGN KEY (event_type_id) REFERENCES public.event_types(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.event_type_owners
    ADD CONSTRAINT event_type_owners_granted_by_fkey FOREIGN KEY (granted_by) REFERENCES public.users(id) ON DELETE SET NULL;
-- tripl:statement
ALTER TABLE ONLY public.event_type_owners
    ADD CONSTRAINT event_type_owners_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.event_type_relations
    ADD CONSTRAINT event_type_relations_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.event_type_relations
    ADD CONSTRAINT event_type_relations_source_event_type_id_fkey FOREIGN KEY (source_event_type_id) REFERENCES public.event_types(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.event_type_relations
    ADD CONSTRAINT event_type_relations_source_field_id_fkey FOREIGN KEY (source_field_id) REFERENCES public.field_definitions(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.event_type_relations
    ADD CONSTRAINT event_type_relations_target_event_type_id_fkey FOREIGN KEY (target_event_type_id) REFERENCES public.event_types(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.event_type_relations
    ADD CONSTRAINT event_type_relations_target_field_id_fkey FOREIGN KEY (target_field_id) REFERENCES public.field_definitions(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.event_types
    ADD CONSTRAINT event_types_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.events
    ADD CONSTRAINT events_event_type_id_fkey FOREIGN KEY (event_type_id) REFERENCES public.event_types(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.events
    ADD CONSTRAINT events_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.fact_tables
    ADD CONSTRAINT fact_tables_data_source_id_fkey FOREIGN KEY (data_source_id) REFERENCES public.data_sources(id) ON DELETE SET NULL;
-- tripl:statement
ALTER TABLE ONLY public.fact_tables
    ADD CONSTRAINT fact_tables_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.field_definitions
    ADD CONSTRAINT field_definitions_event_type_id_fkey FOREIGN KEY (event_type_id) REFERENCES public.event_types(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.alert_rules
    ADD CONSTRAINT fk_alert_rules_scan_config_id FOREIGN KEY (scan_config_id) REFERENCES public.scan_configs(id) ON DELETE SET NULL;
-- tripl:statement
ALTER TABLE ONLY public.api_keys
    ADD CONSTRAINT fk_api_keys_created_with_sso_org_id FOREIGN KEY (created_with_sso_org_id) REFERENCES public.organizations(id) ON DELETE SET NULL;
-- tripl:statement
ALTER TABLE ONLY public.api_keys
    ADD CONSTRAINT fk_api_keys_project_id FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.api_keys
    ADD CONSTRAINT fk_api_keys_project_organization FOREIGN KEY (project_id, organization_id) REFERENCES public.projects(id, organization_id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.audit_log
    ADD CONSTRAINT fk_audit_log_branch FOREIGN KEY (branch_id) REFERENCES public.plan_branches(id) ON DELETE SET NULL;
-- tripl:statement
ALTER TABLE ONLY public.data_sources
    ADD CONSTRAINT fk_data_sources_project_id FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.data_sources
    ADD CONSTRAINT fk_data_sources_project_organization FOREIGN KEY (project_id, organization_id) REFERENCES public.projects(id, organization_id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.event_photo_comments
    ADD CONSTRAINT fk_event_photo_comment_event FOREIGN KEY (event_id) REFERENCES public.events(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.event_photo_comments
    ADD CONSTRAINT fk_event_photo_comments_resolved_by_users FOREIGN KEY (resolved_by) REFERENCES public.users(id) ON DELETE SET NULL;
-- tripl:statement
ALTER TABLE ONLY public.event_photos
    ADD CONSTRAINT fk_event_photos_storage_config_id_photo_storage_configs FOREIGN KEY (storage_config_id) REFERENCES public.photo_storage_configs(id) ON DELETE RESTRICT;
-- tripl:statement
ALTER TABLE ONLY public.event_photos
    ADD CONSTRAINT fk_event_photos_storage_org_id_organizations FOREIGN KEY (storage_org_id) REFERENCES public.organizations(id) ON DELETE SET NULL;
-- tripl:statement
ALTER TABLE ONLY public.event_type_relations
    ADD CONSTRAINT fk_event_type_relations_branch_id FOREIGN KEY (branch_id) REFERENCES public.plan_branches(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.event_types
    ADD CONSTRAINT fk_event_types_branch_id FOREIGN KEY (branch_id) REFERENCES public.plan_branches(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.events
    ADD CONSTRAINT fk_events_branch_id FOREIGN KEY (branch_id) REFERENCES public.plan_branches(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.events
    ADD CONSTRAINT fk_events_owner_id_users FOREIGN KEY (owner_id) REFERENCES public.users(id) ON DELETE SET NULL;
-- tripl:statement
ALTER TABLE ONLY public.events
    ADD CONSTRAINT fk_events_superseded_by_event_id_events FOREIGN KEY (superseded_by_event_id) REFERENCES public.events(id) ON DELETE SET NULL;
-- tripl:statement
ALTER TABLE ONLY public.meta_field_definitions
    ADD CONSTRAINT fk_meta_field_definitions_branch_id FOREIGN KEY (branch_id) REFERENCES public.plan_branches(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.metric_definitions
    ADD CONSTRAINT fk_metric_def_fact_table FOREIGN KEY (fact_table_id) REFERENCES public.fact_tables(id) ON DELETE SET NULL;
-- tripl:statement
ALTER TABLE ONLY public.plan_revisions
    ADD CONSTRAINT fk_plan_revisions_branch_id FOREIGN KEY (branch_id) REFERENCES public.plan_branches(id) ON DELETE SET NULL;
-- tripl:statement
ALTER TABLE ONLY public.projects
    ADD CONSTRAINT fk_projects_created_by_user_id FOREIGN KEY (created_by_user_id) REFERENCES public.users(id) ON DELETE SET NULL;
-- tripl:statement
ALTER TABLE ONLY public.schema_drifts
    ADD CONSTRAINT fk_schema_drifts_resolved_by FOREIGN KEY (resolved_by) REFERENCES public.users(id) ON DELETE SET NULL;
-- tripl:statement
ALTER TABLE ONLY public.user_sessions
    ADD CONSTRAINT fk_user_sessions_sso_organization_id FOREIGN KEY (sso_organization_id) REFERENCES public.organizations(id) ON DELETE SET NULL;
-- tripl:statement
ALTER TABLE ONLY public.variables
    ADD CONSTRAINT fk_variables_branch_id FOREIGN KEY (branch_id) REFERENCES public.plan_branches(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.implementation_tickets
    ADD CONSTRAINT implementation_tickets_branch_id_fkey FOREIGN KEY (branch_id) REFERENCES public.plan_branches(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.implementation_tickets
    ADD CONSTRAINT implementation_tickets_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.incident_summaries
    ADD CONSTRAINT incident_summaries_generated_by_fkey FOREIGN KEY (generated_by) REFERENCES public.users(id) ON DELETE SET NULL;
-- tripl:statement
ALTER TABLE ONLY public.incident_summaries
    ADD CONSTRAINT incident_summaries_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.invitations
    ADD CONSTRAINT invitations_invited_by_user_id_fkey FOREIGN KEY (invited_by_user_id) REFERENCES public.users(id) ON DELETE SET NULL;
-- tripl:statement
ALTER TABLE ONLY public.invitations
    ADD CONSTRAINT invitations_organization_id_fkey FOREIGN KEY (organization_id) REFERENCES public.organizations(id) ON DELETE RESTRICT;
-- tripl:statement
ALTER TABLE ONLY public.lifecycle_findings
    ADD CONSTRAINT lifecycle_findings_event_id_fkey FOREIGN KEY (event_id) REFERENCES public.events(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.lifecycle_findings
    ADD CONSTRAINT lifecycle_findings_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.lifecycle_findings
    ADD CONSTRAINT lifecycle_findings_related_event_id_fkey FOREIGN KEY (related_event_id) REFERENCES public.events(id) ON DELETE SET NULL;
-- tripl:statement
ALTER TABLE ONLY public.meta_field_definitions
    ADD CONSTRAINT meta_field_definitions_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.metric_anomalies
    ADD CONSTRAINT metric_anomalies_event_id_fkey FOREIGN KEY (event_id) REFERENCES public.events(id) ON DELETE SET NULL;
-- tripl:statement
ALTER TABLE ONLY public.metric_anomalies
    ADD CONSTRAINT metric_anomalies_event_type_id_fkey FOREIGN KEY (event_type_id) REFERENCES public.event_types(id) ON DELETE SET NULL;
-- tripl:statement
ALTER TABLE ONLY public.metric_anomalies
    ADD CONSTRAINT metric_anomalies_scan_config_id_fkey FOREIGN KEY (scan_config_id) REFERENCES public.scan_configs(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.metric_anomaly_attributions
    ADD CONSTRAINT metric_anomaly_attributions_anomaly_id_fkey FOREIGN KEY (anomaly_id) REFERENCES public.metric_anomalies(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.metric_baselines
    ADD CONSTRAINT metric_baselines_scan_config_id_fkey FOREIGN KEY (scan_config_id) REFERENCES public.scan_configs(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.metric_breakdown_anomalies
    ADD CONSTRAINT metric_breakdown_anomalies_event_id_fkey FOREIGN KEY (event_id) REFERENCES public.events(id) ON DELETE SET NULL;
-- tripl:statement
ALTER TABLE ONLY public.metric_breakdown_anomalies
    ADD CONSTRAINT metric_breakdown_anomalies_event_type_id_fkey FOREIGN KEY (event_type_id) REFERENCES public.event_types(id) ON DELETE SET NULL;
-- tripl:statement
ALTER TABLE ONLY public.metric_breakdown_anomalies
    ADD CONSTRAINT metric_breakdown_anomalies_scan_config_id_fkey FOREIGN KEY (scan_config_id) REFERENCES public.scan_configs(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.metric_definitions
    ADD CONSTRAINT metric_definitions_data_source_id_fkey FOREIGN KEY (data_source_id) REFERENCES public.data_sources(id) ON DELETE SET NULL;
-- tripl:statement
ALTER TABLE ONLY public.metric_definitions
    ADD CONSTRAINT metric_definitions_denominator_event_id_fkey FOREIGN KEY (denominator_event_id) REFERENCES public.events(id) ON DELETE SET NULL;
-- tripl:statement
ALTER TABLE ONLY public.metric_definitions
    ADD CONSTRAINT metric_definitions_denominator_event_type_id_fkey FOREIGN KEY (denominator_event_type_id) REFERENCES public.event_types(id) ON DELETE SET NULL;
-- tripl:statement
ALTER TABLE ONLY public.metric_definitions
    ADD CONSTRAINT metric_definitions_numerator_event_id_fkey FOREIGN KEY (numerator_event_id) REFERENCES public.events(id) ON DELETE SET NULL;
-- tripl:statement
ALTER TABLE ONLY public.metric_definitions
    ADD CONSTRAINT metric_definitions_numerator_event_type_id_fkey FOREIGN KEY (numerator_event_type_id) REFERENCES public.event_types(id) ON DELETE SET NULL;
-- tripl:statement
ALTER TABLE ONLY public.metric_definitions
    ADD CONSTRAINT metric_definitions_owner_id_fkey FOREIGN KEY (owner_id) REFERENCES public.users(id) ON DELETE SET NULL;
-- tripl:statement
ALTER TABLE ONLY public.metric_definitions
    ADD CONSTRAINT metric_definitions_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.metric_value_breakdowns
    ADD CONSTRAINT metric_value_breakdowns_metric_definition_id_fkey FOREIGN KEY (metric_definition_id) REFERENCES public.metric_definitions(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.metric_value_breakdowns
    ADD CONSTRAINT metric_value_breakdowns_scan_config_id_fkey FOREIGN KEY (scan_config_id) REFERENCES public.scan_configs(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.metric_values
    ADD CONSTRAINT metric_values_metric_definition_id_fkey FOREIGN KEY (metric_definition_id) REFERENCES public.metric_definitions(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.metric_values
    ADD CONSTRAINT metric_values_scan_config_id_fkey FOREIGN KEY (scan_config_id) REFERENCES public.scan_configs(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.notifications
    ADD CONSTRAINT notifications_actor_user_id_fkey FOREIGN KEY (actor_user_id) REFERENCES public.users(id) ON DELETE SET NULL;
-- tripl:statement
ALTER TABLE ONLY public.notifications
    ADD CONSTRAINT notifications_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.notifications
    ADD CONSTRAINT notifications_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.org_audit_webhooks
    ADD CONSTRAINT org_audit_webhooks_organization_id_fkey FOREIGN KEY (organization_id) REFERENCES public.organizations(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.org_scim_configs
    ADD CONSTRAINT org_scim_configs_admin_group_id_fkey FOREIGN KEY (admin_group_id) REFERENCES public.organization_groups(id) ON DELETE SET NULL;
-- tripl:statement
ALTER TABLE ONLY public.org_scim_configs
    ADD CONSTRAINT org_scim_configs_organization_id_fkey FOREIGN KEY (organization_id) REFERENCES public.organizations(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.org_scim_tokens
    ADD CONSTRAINT org_scim_tokens_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.users(id) ON DELETE SET NULL;
-- tripl:statement
ALTER TABLE ONLY public.org_scim_tokens
    ADD CONSTRAINT org_scim_tokens_organization_id_fkey FOREIGN KEY (organization_id) REFERENCES public.organizations(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.org_sso_configs
    ADD CONSTRAINT org_sso_configs_organization_id_fkey FOREIGN KEY (organization_id) REFERENCES public.organizations(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.org_sso_domains
    ADD CONSTRAINT org_sso_domains_organization_id_fkey FOREIGN KEY (organization_id) REFERENCES public.organizations(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.organization_group_members
    ADD CONSTRAINT organization_group_members_group_id_fkey FOREIGN KEY (group_id) REFERENCES public.organization_groups(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.organization_group_members
    ADD CONSTRAINT organization_group_members_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.organization_groups
    ADD CONSTRAINT organization_groups_organization_id_fkey FOREIGN KEY (organization_id) REFERENCES public.organizations(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.organization_members
    ADD CONSTRAINT organization_members_organization_id_fkey FOREIGN KEY (organization_id) REFERENCES public.organizations(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.organization_members
    ADD CONSTRAINT organization_members_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.password_reset_tokens
    ADD CONSTRAINT password_reset_tokens_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.photo_storage_configs
    ADD CONSTRAINT photo_storage_configs_organization_id_fkey FOREIGN KEY (organization_id) REFERENCES public.organizations(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.plan_branch_approvals
    ADD CONSTRAINT plan_branch_approvals_branch_id_fkey FOREIGN KEY (branch_id) REFERENCES public.plan_branches(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.plan_branch_approvals
    ADD CONSTRAINT plan_branch_approvals_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE SET NULL;
-- tripl:statement
ALTER TABLE ONLY public.plan_branch_comments
    ADD CONSTRAINT plan_branch_comments_branch_id_fkey FOREIGN KEY (branch_id) REFERENCES public.plan_branches(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.plan_branch_comments
    ADD CONSTRAINT plan_branch_comments_parent_id_fkey FOREIGN KEY (parent_id) REFERENCES public.plan_branch_comments(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.plan_branch_comments
    ADD CONSTRAINT plan_branch_comments_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE SET NULL;
-- tripl:statement
ALTER TABLE ONLY public.plan_branch_merge_resolutions
    ADD CONSTRAINT plan_branch_merge_resolutions_branch_id_fkey FOREIGN KEY (branch_id) REFERENCES public.plan_branches(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.plan_branch_merge_resolutions
    ADD CONSTRAINT plan_branch_merge_resolutions_resolved_by_fkey FOREIGN KEY (resolved_by) REFERENCES public.users(id) ON DELETE SET NULL;
-- tripl:statement
ALTER TABLE ONLY public.plan_branch_reviewers
    ADD CONSTRAINT plan_branch_reviewers_branch_id_fkey FOREIGN KEY (branch_id) REFERENCES public.plan_branches(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.plan_branch_reviewers
    ADD CONSTRAINT plan_branch_reviewers_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.plan_branches
    ADD CONSTRAINT plan_branches_base_revision_id_fkey FOREIGN KEY (base_revision_id) REFERENCES public.plan_revisions(id) ON DELETE SET NULL;
-- tripl:statement
ALTER TABLE ONLY public.plan_branches
    ADD CONSTRAINT plan_branches_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.users(id) ON DELETE SET NULL;
-- tripl:statement
ALTER TABLE ONLY public.plan_branches
    ADD CONSTRAINT plan_branches_merged_by_fkey FOREIGN KEY (merged_by) REFERENCES public.users(id) ON DELETE SET NULL;
-- tripl:statement
ALTER TABLE ONLY public.plan_branches
    ADD CONSTRAINT plan_branches_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.plan_revisions
    ADD CONSTRAINT plan_revisions_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.users(id) ON DELETE SET NULL;
-- tripl:statement
ALTER TABLE ONLY public.plan_revisions
    ADD CONSTRAINT plan_revisions_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.platform_step_ins
    ADD CONSTRAINT platform_step_ins_organization_id_fkey FOREIGN KEY (organization_id) REFERENCES public.organizations(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.platform_step_ins
    ADD CONSTRAINT platform_step_ins_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.project_anomaly_settings
    ADD CONSTRAINT project_anomaly_settings_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.project_branch_settings
    ADD CONSTRAINT project_branch_settings_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.project_health_snapshots
    ADD CONSTRAINT project_health_snapshots_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.project_members
    ADD CONSTRAINT project_members_added_by_user_id_fkey FOREIGN KEY (added_by_user_id) REFERENCES public.users(id) ON DELETE SET NULL;
-- tripl:statement
ALTER TABLE ONLY public.project_members
    ADD CONSTRAINT project_members_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.project_members
    ADD CONSTRAINT project_members_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.project_tracker_configs
    ADD CONSTRAINT project_tracker_configs_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.projects
    ADD CONSTRAINT projects_organization_id_fkey FOREIGN KEY (organization_id) REFERENCES public.organizations(id) ON DELETE RESTRICT;
-- tripl:statement
ALTER TABLE ONLY public.property_drifts
    ADD CONSTRAINT property_drifts_event_id_fkey FOREIGN KEY (event_id) REFERENCES public.events(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.property_drifts
    ADD CONSTRAINT property_drifts_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.property_drifts
    ADD CONSTRAINT property_drifts_resolved_by_fkey FOREIGN KEY (resolved_by) REFERENCES public.users(id) ON DELETE SET NULL;
-- tripl:statement
ALTER TABLE ONLY public.property_drifts
    ADD CONSTRAINT property_drifts_scan_config_id_fkey FOREIGN KEY (scan_config_id) REFERENCES public.scan_configs(id) ON DELETE SET NULL;
-- tripl:statement
ALTER TABLE ONLY public.property_drifts
    ADD CONSTRAINT property_drifts_variable_id_fkey FOREIGN KEY (variable_id) REFERENCES public.variables(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.release_comparabilities
    ADD CONSTRAINT release_comparabilities_scan_config_id_fkey FOREIGN KEY (scan_config_id) REFERENCES public.scan_configs(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.release_regressions
    ADD CONSTRAINT release_regressions_event_id_fkey FOREIGN KEY (event_id) REFERENCES public.events(id) ON DELETE SET NULL;
-- tripl:statement
ALTER TABLE ONLY public.release_regressions
    ADD CONSTRAINT release_regressions_event_type_id_fkey FOREIGN KEY (event_type_id) REFERENCES public.event_types(id) ON DELETE SET NULL;
-- tripl:statement
ALTER TABLE ONLY public.release_regressions
    ADD CONSTRAINT release_regressions_scan_config_id_fkey FOREIGN KEY (scan_config_id) REFERENCES public.scan_configs(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.saml_assertion_ids
    ADD CONSTRAINT saml_assertion_ids_organization_id_fkey FOREIGN KEY (organization_id) REFERENCES public.organizations(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.scan_configs
    ADD CONSTRAINT scan_configs_data_source_id_fkey FOREIGN KEY (data_source_id) REFERENCES public.data_sources(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.scan_configs
    ADD CONSTRAINT scan_configs_event_type_id_fkey FOREIGN KEY (event_type_id) REFERENCES public.event_types(id) ON DELETE SET NULL;
-- tripl:statement
ALTER TABLE ONLY public.scan_configs
    ADD CONSTRAINT scan_configs_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.scan_dry_run_jobs
    ADD CONSTRAINT scan_dry_run_jobs_data_source_id_fkey FOREIGN KEY (data_source_id) REFERENCES public.data_sources(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.scan_dry_run_jobs
    ADD CONSTRAINT scan_dry_run_jobs_event_type_id_fkey FOREIGN KEY (event_type_id) REFERENCES public.event_types(id) ON DELETE SET NULL;
-- tripl:statement
ALTER TABLE ONLY public.scan_dry_run_jobs
    ADD CONSTRAINT scan_dry_run_jobs_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.scan_dry_run_jobs
    ADD CONSTRAINT scan_dry_run_jobs_scan_config_id_fkey FOREIGN KEY (scan_config_id) REFERENCES public.scan_configs(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.scan_jobs
    ADD CONSTRAINT scan_jobs_scan_config_id_fkey FOREIGN KEY (scan_config_id) REFERENCES public.scan_configs(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.scan_preview_jobs
    ADD CONSTRAINT scan_preview_jobs_data_source_id_fkey FOREIGN KEY (data_source_id) REFERENCES public.data_sources(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.scan_preview_jobs
    ADD CONSTRAINT scan_preview_jobs_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.schema_drifts
    ADD CONSTRAINT schema_drifts_event_type_id_fkey FOREIGN KEY (event_type_id) REFERENCES public.event_types(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.schema_drifts
    ADD CONSTRAINT schema_drifts_scan_config_id_fkey FOREIGN KEY (scan_config_id) REFERENCES public.scan_configs(id) ON DELETE SET NULL;
-- tripl:statement
ALTER TABLE ONLY public.scim_group_links
    ADD CONSTRAINT scim_group_links_group_id_fkey FOREIGN KEY (group_id) REFERENCES public.organization_groups(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.scim_group_links
    ADD CONSTRAINT scim_group_links_organization_id_fkey FOREIGN KEY (organization_id) REFERENCES public.organizations(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.scim_user_links
    ADD CONSTRAINT scim_user_links_organization_id_fkey FOREIGN KEY (organization_id) REFERENCES public.organizations(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.scim_user_links
    ADD CONSTRAINT scim_user_links_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.search_documents
    ADD CONSTRAINT search_documents_branch_id_fkey FOREIGN KEY (branch_id) REFERENCES public.plan_branches(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.search_documents
    ADD CONSTRAINT search_documents_parent_event_id_fkey FOREIGN KEY (parent_event_id) REFERENCES public.events(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.search_documents
    ADD CONSTRAINT search_documents_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.shadow_event_candidates
    ADD CONSTRAINT shadow_event_candidates_accepted_event_id_fkey FOREIGN KEY (accepted_event_id) REFERENCES public.events(id) ON DELETE SET NULL;
-- tripl:statement
ALTER TABLE ONLY public.shadow_event_candidates
    ADD CONSTRAINT shadow_event_candidates_event_type_id_fkey FOREIGN KEY (event_type_id) REFERENCES public.event_types(id) ON DELETE SET NULL;
-- tripl:statement
ALTER TABLE ONLY public.shadow_event_candidates
    ADD CONSTRAINT shadow_event_candidates_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.shadow_event_candidates
    ADD CONSTRAINT shadow_event_candidates_resolved_by_fkey FOREIGN KEY (resolved_by) REFERENCES public.users(id) ON DELETE SET NULL;
-- tripl:statement
ALTER TABLE ONLY public.shadow_event_candidates
    ADD CONSTRAINT shadow_event_candidates_scan_config_id_fkey FOREIGN KEY (scan_config_id) REFERENCES public.scan_configs(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.signal_triage
    ADD CONSTRAINT signal_triage_annotation_id_fkey FOREIGN KEY (annotation_id) REFERENCES public.chart_annotations(id) ON DELETE SET NULL;
-- tripl:statement
ALTER TABLE ONLY public.signal_triage
    ADD CONSTRAINT signal_triage_created_by_user_id_fkey FOREIGN KEY (created_by_user_id) REFERENCES public.users(id) ON DELETE SET NULL;
-- tripl:statement
ALTER TABLE ONLY public.signal_triage
    ADD CONSTRAINT signal_triage_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.signal_triage
    ADD CONSTRAINT signal_triage_scan_config_id_fkey FOREIGN KEY (scan_config_id) REFERENCES public.scan_configs(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.sso_link_tickets
    ADD CONSTRAINT sso_link_tickets_organization_id_fkey FOREIGN KEY (organization_id) REFERENCES public.organizations(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.sso_link_tickets
    ADD CONSTRAINT sso_link_tickets_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.sso_login_states
    ADD CONSTRAINT sso_login_states_organization_id_fkey FOREIGN KEY (organization_id) REFERENCES public.organizations(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.sso_membership_blocks
    ADD CONSTRAINT sso_membership_blocks_organization_id_fkey FOREIGN KEY (organization_id) REFERENCES public.organizations(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.sso_membership_blocks
    ADD CONSTRAINT sso_membership_blocks_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.subscriptions
    ADD CONSTRAINT subscriptions_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.subscriptions
    ADD CONSTRAINT subscriptions_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.user_notification_prefs
    ADD CONSTRAINT user_notification_prefs_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.user_sessions
    ADD CONSTRAINT user_sessions_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.user_sso_identities
    ADD CONSTRAINT user_sso_identities_organization_id_fkey FOREIGN KEY (organization_id) REFERENCES public.organizations(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.user_sso_identities
    ADD CONSTRAINT user_sso_identities_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.variable_event_value_overrides
    ADD CONSTRAINT variable_event_value_overrides_branch_id_fkey FOREIGN KEY (branch_id) REFERENCES public.plan_branches(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.variable_event_value_overrides
    ADD CONSTRAINT variable_event_value_overrides_event_id_fkey FOREIGN KEY (event_id) REFERENCES public.events(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.variable_event_value_overrides
    ADD CONSTRAINT variable_event_value_overrides_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.variable_event_value_overrides
    ADD CONSTRAINT variable_event_value_overrides_variable_id_fkey FOREIGN KEY (variable_id) REFERENCES public.variables(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.variable_value_drifts
    ADD CONSTRAINT variable_value_drifts_event_id_fkey FOREIGN KEY (event_id) REFERENCES public.events(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.variable_value_drifts
    ADD CONSTRAINT variable_value_drifts_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.variable_value_drifts
    ADD CONSTRAINT variable_value_drifts_resolved_by_fkey FOREIGN KEY (resolved_by) REFERENCES public.users(id) ON DELETE SET NULL;
-- tripl:statement
ALTER TABLE ONLY public.variable_value_drifts
    ADD CONSTRAINT variable_value_drifts_scan_config_id_fkey FOREIGN KEY (scan_config_id) REFERENCES public.scan_configs(id) ON DELETE SET NULL;
-- tripl:statement
ALTER TABLE ONLY public.variable_value_drifts
    ADD CONSTRAINT variable_value_drifts_variable_id_fkey FOREIGN KEY (variable_id) REFERENCES public.variables(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.variable_values
    ADD CONSTRAINT variable_values_branch_id_fkey FOREIGN KEY (branch_id) REFERENCES public.plan_branches(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.variable_values
    ADD CONSTRAINT variable_values_event_id_fkey FOREIGN KEY (event_id) REFERENCES public.events(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.variable_values
    ADD CONSTRAINT variable_values_field_definition_id_fkey FOREIGN KEY (field_definition_id) REFERENCES public.field_definitions(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.variable_values
    ADD CONSTRAINT variable_values_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.variable_values
    ADD CONSTRAINT variable_values_variable_id_fkey FOREIGN KEY (variable_id) REFERENCES public.variables(id) ON DELETE CASCADE;
-- tripl:statement
ALTER TABLE ONLY public.variables
    ADD CONSTRAINT variables_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;
