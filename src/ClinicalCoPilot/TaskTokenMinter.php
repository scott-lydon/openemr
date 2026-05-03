<?php

declare(strict_types=1);

/**
 * Mint HS256 task tokens for the Clinical Co-Pilot sidecar.
 *
 * Wire-compatible with ``clinical-copilot/sidecar/auth.py`` so a token
 * minted here verifies cleanly with ``verify_task_token`` on the Python
 * side. The signing key is shared via the ``clinical_copilot_jwt_signing_key``
 * global; the sidecar reads the same value from
 * ``COPILOT_BFF_JWT_SIGNING_KEY``.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Scott Lydon <relays.inanity.0n@icloud.com>
 * @copyright Copyright (c) 2026 Scott Lydon
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

namespace OpenEMR\ClinicalCoPilot;

/**
 * Configuration error: refusing to mint with the placeholder signing key.
 *
 * Distinct exception type so callers can return a precise admin-facing
 * "configure the global" message instead of a generic 500.
 */
final class TaskTokenConfigurationError extends \RuntimeException
{
}

/**
 * HS256 JWT minter wire-compatible with the sidecar's verify_task_token.
 *
 * Token shape (from sidecar/auth.py):
 *
 *     header  = {"alg":"HS256","typ":"JWT"}
 *     payload = {
 *         "iss": "openemr-launch",
 *         "sub": <user_id>,
 *         "patient_id": "Patient/<uuid>",
 *         "purpose_of_use": ["diagnostic_cross_check", "chart_error_scan",
 *                            "follow_up_question"],
 *         "scope": "<space-separated SMART scopes>",
 *         "iat": <unix>,
 *         "nbf": <unix>,
 *         "exp": <unix>,
 *         "jti": <random>,
 *     }
 *
 * The ``purpose_of_use`` claim is a JSON array of every purpose the
 * holder is authorised to invoke during the token's 5-minute lifetime.
 * The chat UI fans out one ``/chat`` call per purpose from a single
 * launch click; binding the token to one purpose would force the UI
 * to round-trip back to launch.php per purpose. The audit log still
 * records the per-call purpose (cfg.purpose), so authorisation breadth
 * and exercised purpose remain distinguishable downstream.
 */
final class TaskTokenMinter
{
    /**
     * The JSON_UNESCAPED_SLASHES flag matches Python's
     * ``json.dumps(..., separators=(",",":"))`` byte-for-byte; without
     * it, "Patient/<uuid>" would be encoded as "Patient\/<uuid>" and the
     * signature would still verify but wire-level diffing would fail.
     */
    private const JSON_FLAGS = JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE;

    public const DEFAULT_LIFETIME_SECONDS = 300; // five minutes per ARCHITECTURE.md §3.2

    public const DEFAULT_SCOPES = [
        'patient/Condition.r',
        'patient/MedicationRequest.r',
        'patient/AllergyIntolerance.r',
        'patient/Observation.r',
        'patient/Encounter.r',
        'patient/Procedure.r',
        'patient/DocumentReference.r',
    ];

    public function __construct(private readonly string $signingKey)
    {
        if ($signingKey === '' || $signingKey === 'change-me-to-a-32-byte-hex-string') {
            throw new TaskTokenConfigurationError(
                'Refusing to mint task token with empty or placeholder '
                . 'signing key. Set the "Clinical Co-Pilot JWT Signing Key" '
                . 'global to a 32-byte random secret (openssl rand -hex 32) '
                . 'and ensure the sidecar\'s COPILOT_BFF_JWT_SIGNING_KEY env '
                . 'var has the same value.'
            );
        }
    }

    /**
     * Mint a 5-minute task token.
     *
     * @param string        $userId          OpenEMR username (or any stable id).
     * @param string        $patientId       "Patient/<uuid>" — use a FHIR resource id.
     * @param list<string>  $purposesOfUse   Every purpose the holder may invoke during
     *                                       the token's lifetime, e.g.
     *                                       ["diagnostic_cross_check", "chart_error_scan"].
     *                                       Must contain at least one entry; each entry
     *                                       must be a non-empty string. The sidecar
     *                                       enforces membership (not equality) per
     *                                       /chat call, and the audit log records the
     *                                       per-call purpose separately.
     * @param list<string>  $scopes          SMART-on-FHIR scopes; defaults sane.
     * @param int           $lifetime        Lifetime in seconds; default 300.
     */
    public function mint(
        string $userId,
        string $patientId,
        array $purposesOfUse,
        array $scopes = self::DEFAULT_SCOPES,
        int $lifetime = self::DEFAULT_LIFETIME_SECONDS,
    ): string {
        if ($userId === '') {
            throw new \InvalidArgumentException('userId must be non-empty');
        }
        if ($patientId === '' || !str_starts_with($patientId, 'Patient/')) {
            throw new \InvalidArgumentException(
                'patientId must be of the form "Patient/<uuid>", got '
                . var_export($patientId, true)
            );
        }
        if ($purposesOfUse === []) {
            throw new \InvalidArgumentException(
                'purposesOfUse must contain at least one purpose code; '
                . 'a token with no authorised purposes would be unusable'
            );
        }
        // Re-pack as a numerically-indexed list so json_encode never
        // emits a JSON object for a sparse or string-keyed input array.
        // The Python verifier rejects non-list payloads.
        $purposesList = [];
        foreach ($purposesOfUse as $index => $purpose) {
            if (!is_string($purpose)) {
                throw new \InvalidArgumentException(sprintf(
                    'purposesOfUse[%s] must be a string, got %s: %s',
                    var_export($index, true),
                    get_debug_type($purpose),
                    var_export($purpose, true),
                ));
            }
            if ($purpose === '') {
                throw new \InvalidArgumentException(sprintf(
                    'purposesOfUse[%s] is empty; every entry must be a '
                    . 'non-empty purpose code',
                    var_export($index, true),
                ));
            }
            $purposesList[] = $purpose;
        }
        if ($lifetime <= 0 || $lifetime > 3600) {
            throw new \InvalidArgumentException(
                'lifetime must be in (0, 3600] seconds, got ' . $lifetime
            );
        }

        $now = time();
        $payload = [
            'iss' => 'openemr-launch',
            'sub' => $userId,
            'patient_id' => $patientId,
            'purpose_of_use' => $purposesList,
            'scope' => implode(' ', $scopes),
            'iat' => $now,
            'nbf' => $now,
            'exp' => $now + $lifetime,
            'jti' => bin2hex(random_bytes(8)),
        ];

        $headerJson = json_encode(['alg' => 'HS256', 'typ' => 'JWT'], self::JSON_FLAGS);
        $payloadJson = json_encode($payload, self::JSON_FLAGS);
        if ($headerJson === false || $payloadJson === false) {
            throw new \RuntimeException(
                'json_encode failed while building task token: '
                . json_last_error_msg()
            );
        }

        $headerB64 = self::b64UrlEncode($headerJson);
        $payloadB64 = self::b64UrlEncode($payloadJson);
        $signingInput = $headerB64 . '.' . $payloadB64;
        $sig = hash_hmac('sha256', $signingInput, $this->signingKey, binary: true);

        return $signingInput . '.' . self::b64UrlEncode($sig);
    }

    /**
     * RFC 7515 §2 base64url: standard base64 with +/= → -_ and no padding.
     */
    private static function b64UrlEncode(string $bytes): string
    {
        return rtrim(strtr(base64_encode($bytes), '+/', '-_'), '=');
    }
}
