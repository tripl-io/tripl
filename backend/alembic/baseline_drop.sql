DROP TABLE IF EXISTS public.variables CASCADE;
-- tripl:statement
DROP TABLE IF EXISTS public.variable_values CASCADE;
-- tripl:statement
DROP TABLE IF EXISTS public.variable_value_drifts CASCADE;
-- tripl:statement
DROP TABLE IF EXISTS public.variable_event_value_overrides CASCADE;
-- tripl:statement
DROP TABLE IF EXISTS public.users CASCADE;
-- tripl:statement
-- tripl:statement
DROP TABLE IF EXISTS public.user_sessions CASCADE;
-- tripl:statement
DROP TABLE IF EXISTS public.user_notification_prefs CASCADE;
-- tripl:statement
DROP TABLE IF EXISTS public.subscriptions CASCADE;
-- tripl:statement
-- tripl:statement
-- tripl:statement
-- tripl:statement
DROP TABLE IF EXISTS public.signal_triage CASCADE;
-- tripl:statement
DROP TABLE IF EXISTS public.shadow_event_candidates CASCADE;
-- tripl:statement
DROP TABLE IF EXISTS public.search_documents CASCADE;
-- tripl:statement
-- tripl:statement
-- tripl:statement
DROP TABLE IF EXISTS public.schema_drifts CASCADE;
-- tripl:statement
DROP TABLE IF EXISTS public.scan_preview_jobs CASCADE;
-- tripl:statement
DROP TABLE IF EXISTS public.scan_jobs CASCADE;
-- tripl:statement
DROP TABLE IF EXISTS public.scan_dry_run_jobs CASCADE;
-- tripl:statement
DROP TABLE IF EXISTS public.scan_configs CASCADE;
-- tripl:statement
-- tripl:statement
DROP TABLE IF EXISTS public.release_regressions CASCADE;
-- tripl:statement
DROP TABLE IF EXISTS public.release_comparabilities CASCADE;
-- tripl:statement
DROP TABLE IF EXISTS public.property_drifts CASCADE;
-- tripl:statement
DROP TABLE IF EXISTS public.projects CASCADE;
-- tripl:statement
DROP TABLE IF EXISTS public.project_tracker_configs CASCADE;
-- tripl:statement
DROP TABLE IF EXISTS public.project_members CASCADE;
-- tripl:statement
DROP TABLE IF EXISTS public.project_health_snapshots CASCADE;
-- tripl:statement
DROP TABLE IF EXISTS public.project_branch_settings CASCADE;
-- tripl:statement
DROP TABLE IF EXISTS public.project_anomaly_settings CASCADE;
-- tripl:statement
DROP TABLE IF EXISTS public.platform_step_ins CASCADE;
-- tripl:statement
DROP TABLE IF EXISTS public.plan_revisions CASCADE;
-- tripl:statement
DROP TABLE IF EXISTS public.plan_branches CASCADE;
-- tripl:statement
DROP TABLE IF EXISTS public.plan_branch_reviewers CASCADE;
-- tripl:statement
DROP TABLE IF EXISTS public.plan_branch_merge_resolutions CASCADE;
-- tripl:statement
DROP TABLE IF EXISTS public.plan_branch_comments CASCADE;
-- tripl:statement
DROP TABLE IF EXISTS public.plan_branch_approvals CASCADE;
-- tripl:statement
DROP TABLE IF EXISTS public.photo_storage_configs CASCADE;
-- tripl:statement
DROP TABLE IF EXISTS public.password_reset_tokens CASCADE;
-- tripl:statement
DROP TABLE IF EXISTS public.organizations CASCADE;
-- tripl:statement
DROP TABLE IF EXISTS public.organization_members CASCADE;
-- tripl:statement
DROP TABLE IF EXISTS public.organization_groups CASCADE;
-- tripl:statement
DROP TABLE IF EXISTS public.organization_group_members CASCADE;
-- tripl:statement
-- tripl:statement
-- tripl:statement
-- tripl:statement
-- tripl:statement
DROP TABLE IF EXISTS public.org_audit_webhooks CASCADE;
-- tripl:statement
DROP TABLE IF EXISTS public.notifications CASCADE;
-- tripl:statement
DROP TABLE IF EXISTS public.metric_values CASCADE;
-- tripl:statement
DROP TABLE IF EXISTS public.metric_value_breakdowns CASCADE;
-- tripl:statement
DROP TABLE IF EXISTS public.metric_definitions CASCADE;
-- tripl:statement
DROP TABLE IF EXISTS public.metric_breakdown_anomalies CASCADE;
-- tripl:statement
DROP TABLE IF EXISTS public.metric_baselines CASCADE;
-- tripl:statement
DROP TABLE IF EXISTS public.metric_anomaly_attributions CASCADE;
-- tripl:statement
DROP TABLE IF EXISTS public.metric_anomalies CASCADE;
-- tripl:statement
DROP TABLE IF EXISTS public.meta_field_definitions CASCADE;
-- tripl:statement
DROP TABLE IF EXISTS public.lifecycle_findings CASCADE;
-- tripl:statement
DROP TABLE IF EXISTS public.invitations CASCADE;
-- tripl:statement
DROP TABLE IF EXISTS public.incident_summaries CASCADE;
-- tripl:statement
DROP TABLE IF EXISTS public.implementation_tickets CASCADE;
-- tripl:statement
DROP TABLE IF EXISTS public.field_definitions CASCADE;
-- tripl:statement
DROP TABLE IF EXISTS public.fact_tables CASCADE;
-- tripl:statement
DROP TABLE IF EXISTS public.events CASCADE;
-- tripl:statement
DROP TABLE IF EXISTS public.event_types CASCADE;
-- tripl:statement
DROP TABLE IF EXISTS public.event_type_relations CASCADE;
-- tripl:statement
DROP TABLE IF EXISTS public.event_type_owners CASCADE;
-- tripl:statement
DROP TABLE IF EXISTS public.event_tags CASCADE;
-- tripl:statement
DROP TABLE IF EXISTS public.event_photos CASCADE;
-- tripl:statement
DROP TABLE IF EXISTS public.event_photo_comments CASCADE;
-- tripl:statement
DROP TABLE IF EXISTS public.event_metrics CASCADE;
-- tripl:statement
DROP TABLE IF EXISTS public.event_metric_breakdowns CASCADE;
-- tripl:statement
DROP TABLE IF EXISTS public.event_meta_values CASCADE;
-- tripl:statement
DROP TABLE IF EXISTS public.event_field_values CASCADE;
-- tripl:statement
DROP TABLE IF EXISTS public.event_changes CASCADE;
-- tripl:statement
DROP TABLE IF EXISTS public.email_verification_tokens CASCADE;
-- tripl:statement
DROP TABLE IF EXISTS public.duplicate_dismissals CASCADE;
-- tripl:statement
DROP TABLE IF EXISTS public.doc_shares CASCADE;
-- tripl:statement
DROP TABLE IF EXISTS public.doc_revisions CASCADE;
-- tripl:statement
DROP TABLE IF EXISTS public.doc_links CASCADE;
-- tripl:statement
DROP TABLE IF EXISTS public.doc_folder_shares CASCADE;
-- tripl:statement
DROP TABLE IF EXISTS public.doc_folder_settings CASCADE;
-- tripl:statement
DROP TABLE IF EXISTS public.doc_files CASCADE;
-- tripl:statement
DROP TABLE IF EXISTS public.distribution_drifts CASCADE;
-- tripl:statement
DROP TABLE IF EXISTS public.data_sources CASCADE;
-- tripl:statement
DROP TABLE IF EXISTS public.coverage_metrics CASCADE;
-- tripl:statement
DROP TABLE IF EXISTS public.chart_annotations CASCADE;
-- tripl:statement
DROP TABLE IF EXISTS public.audit_webhook_outbox CASCADE;
-- tripl:statement
DROP TABLE IF EXISTS public.audit_log CASCADE;
-- tripl:statement
DROP TABLE IF EXISTS public.app_settings CASCADE;
-- tripl:statement
DROP TABLE IF EXISTS public.api_keys CASCADE;
-- tripl:statement
DROP TABLE IF EXISTS public.anomaly_scope_overrides CASCADE;
-- tripl:statement
DROP TABLE IF EXISTS public.alert_rules CASCADE;
-- tripl:statement
DROP TABLE IF EXISTS public.alert_rule_states CASCADE;
-- tripl:statement
DROP TABLE IF EXISTS public.alert_rule_filters CASCADE;
-- tripl:statement
DROP TABLE IF EXISTS public.alert_pending_items CASCADE;
-- tripl:statement
DROP TABLE IF EXISTS public.alert_owner_notifications CASCADE;
-- tripl:statement
DROP TABLE IF EXISTS public.alert_destinations CASCADE;
-- tripl:statement
DROP TABLE IF EXISTS public.alert_delivery_items CASCADE;
-- tripl:statement
DROP TABLE IF EXISTS public.alert_deliveries CASCADE;
-- tripl:statement
DROP TABLE IF EXISTS public.alert_correlation_states CASCADE;
-- tripl:statement
DROP TEXT SEARCH CONFIGURATION IF EXISTS public.tripl_search_surface;
-- tripl:statement
DROP TEXT SEARCH CONFIGURATION IF EXISTS public.tripl_search;
-- tripl:statement
DROP TEXT SEARCH DICTIONARY IF EXISTS public.tripl_russian_stem;
-- tripl:statement
DROP TEXT SEARCH DICTIONARY IF EXISTS public.tripl_english_stem;
-- tripl:statement
DROP TYPE IF EXISTS public.variable_value_kind;
-- tripl:statement
DROP TYPE IF EXISTS public.variable_type;
-- tripl:statement
DROP TYPE IF EXISTS public.signal_triage_action;
-- tripl:statement
DROP TYPE IF EXISTS public.signal_expected_reason;
-- tripl:statement
DROP TYPE IF EXISTS public.shadow_event_status;
-- tripl:statement
DROP TYPE IF EXISTS public.sensitivity_level;
-- tripl:statement
DROP TYPE IF EXISTS public.schema_drift_type;
-- tripl:statement
DROP TYPE IF EXISTS public.schema_drift_status;
-- tripl:statement
DROP TYPE IF EXISTS public.scan_job_status;
-- tripl:statement
DROP TYPE IF EXISTS public.scan_interval;
-- tripl:statement
DROP TYPE IF EXISTS public.release_regression_kind;
-- tripl:statement
DROP TYPE IF EXISTS public.release_comparability_reason;
-- tripl:statement
DROP TYPE IF EXISTS public.project_member_role;
-- tripl:statement
DROP TYPE IF EXISTS public.project_generation_status;
-- tripl:statement
DROP TYPE IF EXISTS public.plan_revision_kind;
-- tripl:statement
DROP TYPE IF EXISTS public.plan_branch_status;
-- tripl:statement
DROP TYPE IF EXISTS public.plan_branch_kind;
-- tripl:statement
DROP TYPE IF EXISTS public.organization_status;
-- tripl:statement
DROP TYPE IF EXISTS public.organization_member_role;
-- tripl:statement
DROP TYPE IF EXISTS public.metric_status;
-- tripl:statement
DROP TYPE IF EXISTS public.metric_scope_type;
-- tripl:statement
DROP TYPE IF EXISTS public.metric_kind;
-- tripl:statement
DROP TYPE IF EXISTS public.metric_composition;
-- tripl:statement
DROP TYPE IF EXISTS public.metric_breakdown_anomaly_kind;
-- tripl:statement
DROP TYPE IF EXISTS public.metric_aggregation;
-- tripl:statement
DROP TYPE IF EXISTS public.meta_field_type;
-- tripl:statement
DROP TYPE IF EXISTS public.merge_resolution_choice;
-- tripl:statement
DROP TYPE IF EXISTS public.field_definition_type;
-- tripl:statement
DROP TYPE IF EXISTS public.event_status;
-- tripl:statement
DROP TYPE IF EXISTS public.event_photo_storage_backend;
-- tripl:statement
DROP TYPE IF EXISTS public.event_photo_kind;
-- tripl:statement
DROP TYPE IF EXISTS public.event_comment_status;
-- tripl:statement
DROP TYPE IF EXISTS public.distribution_drift_band;
-- tripl:statement
DROP TYPE IF EXISTS public.data_source_test_status;
-- tripl:statement
DROP TYPE IF EXISTS public.data_source_db_type;
-- tripl:statement
DROP TYPE IF EXISTS public.chart_annotation_source;
-- tripl:statement
DROP TYPE IF EXISTS public.chart_annotation_scope_type;
-- tripl:statement
DROP TYPE IF EXISTS public.api_key_scope;
-- tripl:statement
DROP TYPE IF EXISTS public.anomaly_direction;
-- tripl:statement
DROP TYPE IF EXISTS public.alert_rule_filter_operator;
-- tripl:statement
DROP TYPE IF EXISTS public.alert_rule_filter_field;
-- tripl:statement
DROP TYPE IF EXISTS public.alert_message_format;
-- tripl:statement
DROP TYPE IF EXISTS public.alert_inbox_status;
-- tripl:statement
DROP TYPE IF EXISTS public.alert_drift_type;
-- tripl:statement
DROP TYPE IF EXISTS public.alert_destination_type;
-- tripl:statement
DROP TYPE IF EXISTS public.alert_delivery_status;
