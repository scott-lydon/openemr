<?php

/**
 * Module-scoped settings repository for the Clinical Co-Pilot module.
 *
 * Replaces the previous hand-edited entries in library/globals.inc.php.
 * All settings persist in a module-private table
 * (module_oe_clinical_copilot_settings) and are exposed through a
 * typed accessor surface so callers never see raw string keys or null
 * sentinels.
 *
 * The table is created idempotently by the install listener so the
 * module can be installed and uninstalled without touching OpenEMR's
 * core schema.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 *
 * @author    Scott Lydon <relays.inanity.0n@icloud.com>
 * @copyright Copyright (c) 2026 Scott Lydon
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\ClinicalCoPilot;

use OpenEMR\Common\Crypto\CryptoGen;
use OpenEMR\Common\Logging\SystemLogger;

/**
 * Settings I/O for the Clinical Co-Pilot module.
 *
 * Keys are typed for safety. Encrypted columns (LLM API keys, future
 * Stripe webhook secrets) are wrapped in {@see CryptoGen} so an
 * attacker with read access to the DB cannot pivot into the LLM
 * provider.
 */
final class ModuleSettings
{
    public const TABLE = 'module_oe_clinical_copilot_settings';

    // Public keys: human-readable, machine-stable.
    public const KEY_SIDECAR_URL          = 'sidecar_url';
    public const KEY_JWT_SIGNING_KEY      = 'jwt_signing_key';
    public const KEY_LICENSE_KEY          = 'license_key';
    public const KEY_LLM_PROVIDER         = 'llm_provider'; // openai|anthropic|azure-openai|mock
    public const KEY_LLM_API_KEY          = 'llm_api_key'; // encrypted at rest
    public const KEY_PURPOSE_ALLOWLIST    = 'purpose_of_use_allowlist'; // CSV
    public const KEY_FF_DIAGNOSTIC        = 'ff_diagnostic_cross_check';
    public const KEY_FF_CHART_ERROR_SCAN  = 'ff_chart_error_scan';
    public const KEY_FF_FOLLOW_UP         = 'ff_follow_up_question';
    public const KEY_FF_DOCUMENT_INGEST   = 'ff_document_ingest';
    public const KEY_MODERN_DASHBOARD_URL = 'modern_dashboard_url';
    public const KEY_MODERN_DASHBOARD_DEFAULT = 'modern_dashboard_default_url';

    /**
     * Keys whose stored value is encrypted via CryptoGen. Anything that
     * could be exfiltrated for value (LLM keys, webhook secrets) lives
     * here; URLs and feature flags do not.
     */
    private const ENCRYPTED_KEYS = [
        self::KEY_LLM_API_KEY,
    ];

    private const DEFAULTS = [
        self::KEY_SIDECAR_URL          => '',
        self::KEY_JWT_SIGNING_KEY      => '',
        self::KEY_LICENSE_KEY          => '',
        self::KEY_LLM_PROVIDER         => 'mock',
        self::KEY_LLM_API_KEY          => '',
        self::KEY_PURPOSE_ALLOWLIST    => 'diagnostic_cross_check,chart_error_scan,follow_up_question,document_ingest',
        self::KEY_FF_DIAGNOSTIC        => '1',
        self::KEY_FF_CHART_ERROR_SCAN  => '1',
        self::KEY_FF_FOLLOW_UP         => '1',
        self::KEY_FF_DOCUMENT_INGEST   => '1',
        self::KEY_MODERN_DASHBOARD_URL => '',
        self::KEY_MODERN_DASHBOARD_DEFAULT => 'http://localhost:8400',
    ];

    private readonly CryptoGen $crypto;
    private readonly SystemLogger $logger;

    public function __construct(?CryptoGen $crypto = null, ?SystemLogger $logger = null)
    {
        $this->crypto = $crypto ?? new CryptoGen();
        $this->logger = $logger ?? new SystemLogger();
    }

    /**
     * Idempotent table create. Called by the install listener and by
     * any caller that lands here before installation completed (the
     * read path will see an empty result rather than a fatal "table
     * not found").
     */
    public function ensureSchema(): void
    {
        sqlStatementNoLog(
            'CREATE TABLE IF NOT EXISTS `' . self::TABLE . '` ('
            . '  `setting_key` VARCHAR(64) NOT NULL,'
            . '  `setting_value` LONGTEXT NOT NULL,'
            . '  `updated_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,'
            . '  PRIMARY KEY (`setting_key`)'
            . ') ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci'
        );
    }

    /**
     * Drop the settings table. Called by the uninstall listener. Kept
     * separate so the operator can also rerun the install listener
     * after a corrupt uninstall.
     */
    public function dropSchema(): void
    {
        sqlStatementNoLog('DROP TABLE IF EXISTS `' . self::TABLE . '`');
    }

    public function getString(string $key): string
    {
        $row = sqlQuery(
            'SELECT setting_value FROM `' . self::TABLE . '` WHERE setting_key = ?',
            [$key]
        );
        if (!is_array($row) || !array_key_exists('setting_value', $row)) {
            return self::DEFAULTS[$key] ?? '';
        }
        $value = (string) $row['setting_value'];
        if (in_array($key, self::ENCRYPTED_KEYS, true) && $value !== '') {
            $decrypted = $this->crypto->decryptStandard($value);
            if ($decrypted === false || $decrypted === '') {
                $this->logger->error(
                    'oe-module-clinical-copilot: failed to decrypt setting; '
                    . 'returning empty so the caller fails closed.',
                    ['key' => $key]
                );
                return '';
            }
            return $decrypted;
        }
        return $value;
    }

    public function getBool(string $key): bool
    {
        $raw = $this->getString($key);
        return in_array(strtolower($raw), ['1', 'true', 'yes', 'on'], true);
    }

    /**
     * @return list<string>
     */
    public function getList(string $key): array
    {
        $raw = $this->getString($key);
        if ($raw === '') {
            return [];
        }
        $parts = array_map('trim', explode(',', $raw));
        return array_values(array_filter($parts, static fn (string $p): bool => $p !== ''));
    }

    public function set(string $key, string $value): void
    {
        if (!array_key_exists($key, self::DEFAULTS)) {
            throw new \InvalidArgumentException(sprintf(
                'Unknown Clinical Co-Pilot setting key "%s". Add it to '
                . ModuleSettings::class . '::DEFAULTS before writing.',
                $key
            ));
        }
        $stored = $value;
        if (in_array($key, self::ENCRYPTED_KEYS, true) && $value !== '') {
            $encrypted = $this->crypto->encryptStandard($value);
            if ($encrypted === false) {
                throw new \RuntimeException(
                    'CryptoGen::encryptStandard() failed for setting "'
                    . $key . '"; refusing to write a plaintext fallback. '
                    . 'Verify that sites/<site>/documents/logs_and_misc/methods is '
                    . 'present (CryptoGen reads the keypair from there).'
                );
            }
            $stored = $encrypted;
        }
        sqlStatement(
            'REPLACE INTO `' . self::TABLE . '` (setting_key, setting_value) VALUES (?, ?)',
            [$key, $stored]
        );
    }

    /**
     * Generate a fresh 32-byte JWT signing key and persist it. Returns
     * the new key so the admin UI can display it once for the operator
     * to mirror into the sidecar's environment.
     */
    public function rotateJwtSigningKey(): string
    {
        $newKey = bin2hex(random_bytes(32));
        $this->set(self::KEY_JWT_SIGNING_KEY, $newKey);
        $this->logger->info(
            'oe-module-clinical-copilot: JWT signing key rotated. Sidecar '
            . 'COPILOT_BFF_JWT_SIGNING_KEY must be updated to match or all '
            . '/chat requests will return 401.'
        );
        return $newKey;
    }
}
