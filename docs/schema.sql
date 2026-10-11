-- Market Data Intelligence & Analytics Pipeline
-- Schema DDL generated from Django migrations on 2026-10-10
-- Target: MySQL 8.0 / InnoDB

-- Regenerate with: .\scripts\migrate.ps1 -Sql

SET NAMES utf8mb4;
SET sql_mode = 'STRICT_TRANS_TABLES';

-- ============================================================
-- accounts
-- ============================================================
--
-- Create model User
--
CREATE TABLE `accounts_user` (`id` bigint AUTO_INCREMENT NOT NULL PRIMARY KEY, `password` varchar(128) NOT NULL, `last_login` datetime(6) NULL, `is_superuser` bool NOT NULL, `first_name` varchar(150) NOT NULL, `last_name` varchar(150) NOT NULL, `is_staff` bool NOT NULL, `is_active` bool NOT NULL, `date_joined` datetime(6) NOT NULL, `email` varchar(254) NOT NULL UNIQUE, `role` varchar(20) NOT NULL);
CREATE TABLE `accounts_user_groups` (`id` bigint AUTO_INCREMENT NOT NULL PRIMARY KEY, `user_id` bigint NOT NULL, `group_id` integer NOT NULL);
CREATE TABLE `accounts_user_user_permissions` (`id` bigint AUTO_INCREMENT NOT NULL PRIMARY KEY, `user_id` bigint NOT NULL, `permission_id` integer NOT NULL);
ALTER TABLE `accounts_user_groups` ADD CONSTRAINT `accounts_user_groups_user_id_group_id_59c0b32f_uniq` UNIQUE (`user_id`, `group_id`);
ALTER TABLE `accounts_user_groups` ADD CONSTRAINT `accounts_user_groups_user_id_52b62117_fk_accounts_user_id` FOREIGN KEY (`user_id`) REFERENCES `accounts_user` (`id`);
ALTER TABLE `accounts_user_groups` ADD CONSTRAINT `accounts_user_groups_group_id_bd11a704_fk_auth_group_id` FOREIGN KEY (`group_id`) REFERENCES `auth_group` (`id`);
ALTER TABLE `accounts_user_user_permissions` ADD CONSTRAINT `accounts_user_user_permi_user_id_permission_id_2ab516c2_uniq` UNIQUE (`user_id`, `permission_id`);
ALTER TABLE `accounts_user_user_permissions` ADD CONSTRAINT `accounts_user_user_p_user_id_e4f0a161_fk_accounts_` FOREIGN KEY (`user_id`) REFERENCES `accounts_user` (`id`);
ALTER TABLE `accounts_user_user_permissions` ADD CONSTRAINT `accounts_user_user_p_permission_id_113bb443_fk_auth_perm` FOREIGN KEY (`permission_id`) REFERENCES `auth_permission` (`id`);

-- ============================================================
-- instruments
-- ============================================================
--
-- Create model Exchange
--
CREATE TABLE `instruments_exchange` (`id` bigint AUTO_INCREMENT NOT NULL PRIMARY KEY, `code` varchar(16) NOT NULL UNIQUE, `name` varchar(128) NOT NULL, `timezone` varchar(64) NOT NULL);
--
-- Create model Instrument
--
CREATE TABLE `instruments_instrument` (`id` bigint AUTO_INCREMENT NOT NULL PRIMARY KEY, `symbol` varchar(32) NOT NULL UNIQUE, `name` varchar(255) NOT NULL, `asset_type` varchar(16) NOT NULL, `currency` varchar(8) NOT NULL, `is_active` bool NOT NULL, `created_at` datetime(6) NOT NULL, `updated_at` datetime(6) NOT NULL, `exchange_id` bigint NOT NULL);
ALTER TABLE `instruments_instrument` ADD CONSTRAINT `instruments_instrume_exchange_id_f25a9716_fk_instrumen` FOREIGN KEY (`exchange_id`) REFERENCES `instruments_exchange` (`id`);
CREATE INDEX `idx_instrument_asset_type` ON `instruments_instrument` (`asset_type`);

-- ============================================================
-- marketdata
-- ============================================================
--
-- Create model TradingCalendar
--
CREATE TABLE `marketdata_tradingcalendar` (`id` bigint AUTO_INCREMENT NOT NULL PRIMARY KEY, `date` date NOT NULL UNIQUE, `exchange_id` bigint NULL);
--
-- Create model PriceRecord
--
CREATE TABLE `marketdata_pricerecord` (`id` bigint AUTO_INCREMENT NOT NULL PRIMARY KEY, `trading_date` date NOT NULL, `open` numeric(18, 8) NOT NULL, `high` numeric(18, 8) NOT NULL, `low` numeric(18, 8) NOT NULL, `close` numeric(18, 8) NOT NULL, `adjusted_close` numeric(18, 8) NULL, `volume` bigint NOT NULL, `source` varchar(128) NOT NULL, `created_at` datetime(6) NOT NULL, `ingestion_job_id` char(32) NULL, `instrument_id` bigint NOT NULL, CONSTRAINT `uniq_price_instrument_trading_date` UNIQUE (`instrument_id`, `trading_date`), CONSTRAINT `chk_price_high_gte_low` CHECK (`high` >= (`low`)), CONSTRAINT `chk_price_volume_non_negative` CHECK (`volume` >= 0));
ALTER TABLE `marketdata_tradingcalendar` ADD CONSTRAINT `marketdata_tradingca_exchange_id_70802a01_fk_instrumen` FOREIGN KEY (`exchange_id`) REFERENCES `instruments_exchange` (`id`);
ALTER TABLE `marketdata_pricerecord` ADD CONSTRAINT `marketdata_pricereco_ingestion_job_id_c293824d_fk_ingestion` FOREIGN KEY (`ingestion_job_id`) REFERENCES `ingestion_ingestionjob` (`id`);
ALTER TABLE `marketdata_pricerecord` ADD CONSTRAINT `marketdata_pricereco_instrument_id_5a1e949e_fk_instrumen` FOREIGN KEY (`instrument_id`) REFERENCES `instruments_instrument` (`id`);
CREATE INDEX `idx_price_trading_date` ON `marketdata_pricerecord` (`trading_date`);

-- ============================================================
-- ingestion
-- ============================================================
--
-- Create model IngestionJob
--
CREATE TABLE `ingestion_ingestionjob` (`id` char(32) NOT NULL PRIMARY KEY, `source_type` varchar(16) NOT NULL, `status` varchar(16) NOT NULL, `requested_at` datetime(6) NOT NULL, `started_at` datetime(6) NULL, `completed_at` datetime(6) NULL, `rows_received` integer UNSIGNED NOT NULL CHECK (`rows_received` >= 0), `rows_accepted` integer UNSIGNED NOT NULL CHECK (`rows_accepted` >= 0), `rows_rejected` integer UNSIGNED NOT NULL CHECK (`rows_rejected` >= 0), `error_summary` longtext NOT NULL, `created_by_id` bigint NOT NULL);
--
-- Create model IngestionFile
--
CREATE TABLE `ingestion_ingestionfile` (`id` char(32) NOT NULL PRIMARY KEY, `original_filename` varchar(255) NOT NULL, `storage_path` varchar(1024) NOT NULL, `checksum` varchar(64) NOT NULL, `content_type` varchar(128) NOT NULL, `job_id` char(32) NOT NULL, CONSTRAINT `uniq_file_job_checksum` UNIQUE (`job_id`, `checksum`));
--
-- Create model IngestionValidationError
--
CREATE TABLE `ingestion_ingestionvalidationerror` (`id` bigint AUTO_INCREMENT NOT NULL PRIMARY KEY, `row_number` integer UNSIGNED NOT NULL CHECK (`row_number` >= 0), `field_name` varchar(128) NOT NULL, `error_code` varchar(64) NOT NULL, `raw_row` json NULL, `job_id` char(32) NOT NULL);
ALTER TABLE `ingestion_ingestionjob` ADD CONSTRAINT `ingestion_ingestionj_created_by_id_fd110553_fk_accounts_` FOREIGN KEY (`created_by_id`) REFERENCES `accounts_user` (`id`);
ALTER TABLE `ingestion_ingestionfile` ADD CONSTRAINT `ingestion_ingestionf_job_id_e88de21c_fk_ingestion` FOREIGN KEY (`job_id`) REFERENCES `ingestion_ingestionjob` (`id`);
ALTER TABLE `ingestion_ingestionvalidationerror` ADD CONSTRAINT `ingestion_ingestionv_job_id_c8e15ced_fk_ingestion` FOREIGN KEY (`job_id`) REFERENCES `ingestion_ingestionjob` (`id`);
CREATE INDEX `idx_validation_error_code` ON `ingestion_ingestionvalidationerror` (`error_code`);

-- ============================================================
-- analytics
-- ============================================================
--
-- Create model AnalyticSnapshot
--
CREATE TABLE `analytics_analyticsnapshot` (`id` bigint AUTO_INCREMENT NOT NULL PRIMARY KEY, `as_of_date` date NOT NULL, `metric_type` varchar(32) NOT NULL, `lookback_days` integer UNSIGNED NOT NULL CHECK (`lookback_days` >= 0), `value` numeric(18, 8) NOT NULL, `metadata` json NULL, `calculated_at` datetime(6) NOT NULL, `instrument_id` bigint NOT NULL, CONSTRAINT `uniq_snapshot_grain` UNIQUE (`instrument_id`, `as_of_date`, `metric_type`, `lookback_days`));
ALTER TABLE `analytics_analyticsnapshot` ADD CONSTRAINT `analytics_analyticsn_instrument_id_19e2053c_fk_instrumen` FOREIGN KEY (`instrument_id`) REFERENCES `instruments_instrument` (`id`);
CREATE INDEX `idx_snapshot_instr_metric_date` ON `analytics_analyticsnapshot` (`instrument_id`, `metric_type`, `as_of_date` DESC);

-- ============================================================
-- audit
-- ============================================================
--
-- Create model AuditEvent
--
CREATE TABLE `audit_auditevent` (`id` bigint AUTO_INCREMENT NOT NULL PRIMARY KEY, `action` varchar(128) NOT NULL, `object_type` varchar(128) NOT NULL, `object_id` varchar(64) NOT NULL, `request_id` varchar(64) NOT NULL, `ip_address` char(39) NULL, `metadata` json NULL, `created_at` datetime(6) NOT NULL, `user_id` bigint NULL);
ALTER TABLE `audit_auditevent` ADD CONSTRAINT `audit_auditevent_user_id_871f21a9_fk_accounts_user_id` FOREIGN KEY (`user_id`) REFERENCES `accounts_user` (`id`);
CREATE INDEX `audit_auditevent_request_id_b6fdcfa6` ON `audit_auditevent` (`request_id`);
CREATE INDEX `idx_audit_user_created` ON `audit_auditevent` (`user_id`, `created_at` DESC);
CREATE INDEX `idx_audit_object` ON `audit_auditevent` (`object_type`, `object_id`);

