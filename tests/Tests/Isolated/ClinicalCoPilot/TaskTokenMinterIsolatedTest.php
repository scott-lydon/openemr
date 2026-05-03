<?php

declare(strict_types=1);

/**
 * Isolated tests for the Clinical Co-Pilot HS256 task-token minter.
 *
 * These tests run without a database. They verify the wire format of
 * the JWT (header, payload, signature) and the input-validation
 * surface so any regression in `TaskTokenMinter::mint()` surfaces
 * before the token is ever sent to the Python sidecar.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Scott Lydon <relays.inanity.0n@icloud.com>
 * @copyright Copyright (c) 2026 Scott Lydon
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

namespace OpenEMR\Tests\Isolated\ClinicalCoPilot;

use InvalidArgumentException;
use OpenEMR\ClinicalCoPilot\TaskTokenConfigurationError;
use OpenEMR\ClinicalCoPilot\TaskTokenMinter;
use PHPUnit\Framework\Attributes\DataProvider;
use PHPUnit\Framework\TestCase;

final class TaskTokenMinterIsolatedTest extends TestCase
{
    private const SIGNING_KEY = 'this-is-a-32-byte-test-signing-key!!';

    public function testMintEmitsJsonArrayPurposeOfUseClaim(): void
    {
        $minter = new TaskTokenMinter(self::SIGNING_KEY);
        $token = $minter->mint(
            userId: 'dr.m@example.org',
            patientId: 'Patient/87413',
            purposesOfUse: ['diagnostic_cross_check', 'chart_error_scan', 'follow_up_question'],
        );

        $payload = self::decodePayload($token);

        $this->assertArrayHasKey('purpose_of_use', $payload);
        $this->assertSame(
            ['diagnostic_cross_check', 'chart_error_scan', 'follow_up_question'],
            $payload['purpose_of_use'],
            'purpose_of_use claim must be a JSON array preserving order'
        );
        $this->assertSame('Patient/87413', $payload['patient_id']);
        $this->assertSame('dr.m@example.org', $payload['sub']);
        $this->assertSame('openemr-launch', $payload['iss']);
    }

    public function testMintWithSinglePurposeStillEmitsArray(): void
    {
        $minter = new TaskTokenMinter(self::SIGNING_KEY);
        $token = $minter->mint(
            userId: 'u',
            patientId: 'Patient/1',
            purposesOfUse: ['diagnostic_cross_check'],
        );

        $payload = self::decodePayload($token);

        $this->assertSame(['diagnostic_cross_check'], $payload['purpose_of_use']);
    }

    public function testHs256SignatureMatchesHmacSha256(): void
    {
        $minter = new TaskTokenMinter(self::SIGNING_KEY);
        $token = $minter->mint(
            userId: 'u',
            patientId: 'Patient/1',
            purposesOfUse: ['diagnostic_cross_check'],
        );

        $parts = explode('.', $token);
        $this->assertCount(3, $parts, 'JWT must have exactly three dot-separated segments');

        [$headerB64, $payloadB64, $sigB64] = $parts;
        $expected = hash_hmac('sha256', $headerB64 . '.' . $payloadB64, self::SIGNING_KEY, binary: true);
        $expectedB64 = rtrim(strtr(base64_encode($expected), '+/', '-_'), '=');
        $this->assertSame($expectedB64, $sigB64, 'signature segment must equal HMAC-SHA256(header.payload, key)');
    }

    public function testConstructorRejectsPlaceholderSigningKey(): void
    {
        $this->expectException(TaskTokenConfigurationError::class);
        $this->expectExceptionMessageMatches('/empty or placeholder.*signing key/');
        new TaskTokenMinter('change-me-to-a-32-byte-hex-string');
    }

    public function testConstructorRejectsEmptySigningKey(): void
    {
        $this->expectException(TaskTokenConfigurationError::class);
        new TaskTokenMinter('');
    }

    public function testMintRejectsEmptyPurposeList(): void
    {
        $minter = new TaskTokenMinter(self::SIGNING_KEY);
        $this->expectException(InvalidArgumentException::class);
        $this->expectExceptionMessageMatches('/at least one purpose code/');
        $minter->mint(
            userId: 'u',
            patientId: 'Patient/1',
            purposesOfUse: [],
        );
    }

    public function testMintRejectsEmptyStringEntry(): void
    {
        $minter = new TaskTokenMinter(self::SIGNING_KEY);
        $this->expectException(InvalidArgumentException::class);
        $this->expectExceptionMessageMatches('/purposesOfUse\[0\] is empty/');
        $minter->mint(
            userId: 'u',
            patientId: 'Patient/1',
            purposesOfUse: [''],
        );
    }

    public function testMintRejectsNonStringEntry(): void
    {
        $minter = new TaskTokenMinter(self::SIGNING_KEY);
        $this->expectException(InvalidArgumentException::class);
        $this->expectExceptionMessageMatches('/purposesOfUse\[1\] must be a string/');
        $minter->mint(
            userId: 'u',
            patientId: 'Patient/1',
            // PHPStan rule: this is intentionally a bad input. The
            // mint() type signature accepts array<...> but the runtime
            // guard rejects non-string entries before signing.
            purposesOfUse: ['diagnostic_cross_check', 123], // @phpstan-ignore-line argument.type
        );
    }

    public function testMintRejectsBadPatientId(): void
    {
        $minter = new TaskTokenMinter(self::SIGNING_KEY);
        $this->expectException(InvalidArgumentException::class);
        $this->expectExceptionMessageMatches('/patientId must be of the form/');
        $minter->mint(
            userId: 'u',
            patientId: '87413',
            purposesOfUse: ['diagnostic_cross_check'],
        );
    }

    public function testMintRejectsZeroLifetime(): void
    {
        $minter = new TaskTokenMinter(self::SIGNING_KEY);
        $this->expectException(InvalidArgumentException::class);
        $this->expectExceptionMessageMatches('/lifetime must be in \(0, 3600\]/');
        $minter->mint(
            userId: 'u',
            patientId: 'Patient/1',
            purposesOfUse: ['diagnostic_cross_check'],
            lifetime: 0,
        );
    }

    public function testMintRejectsLifetimeAboveOneHour(): void
    {
        $minter = new TaskTokenMinter(self::SIGNING_KEY);
        $this->expectException(InvalidArgumentException::class);
        $minter->mint(
            userId: 'u',
            patientId: 'Patient/1',
            purposesOfUse: ['diagnostic_cross_check'],
            lifetime: 3601,
        );
    }

    /**
     * @return array<string, array{string}>
     *
     * @codeCoverageIgnore Data providers run before coverage instrumentation starts.
     */
    public static function knownPurposeProvider(): array
    {
        return [
            'diagnostic'   => ['diagnostic_cross_check'],
            'chart error'  => ['chart_error_scan'],
            'follow up'    => ['follow_up_question'],
        ];
    }

    #[DataProvider('knownPurposeProvider')]
    public function testEverySupportedPurposeRoundTripsThroughTheClaim(string $purpose): void
    {
        $minter = new TaskTokenMinter(self::SIGNING_KEY);
        $token = $minter->mint(
            userId: 'u',
            patientId: 'Patient/1',
            purposesOfUse: [$purpose],
        );
        $payload = self::decodePayload($token);
        $this->assertSame([$purpose], $payload['purpose_of_use']);
    }

    /**
     * @return array<string, mixed>
     */
    private static function decodePayload(string $token): array
    {
        $parts = explode('.', $token);
        if (count($parts) !== 3) {
            throw new \RuntimeException('expected three JWT segments, got ' . count($parts));
        }
        $payloadJson = self::b64UrlDecode($parts[1]);
        $payload = json_decode($payloadJson, associative: true);
        if (!is_array($payload)) {
            throw new \RuntimeException('JWT payload did not decode to an array: ' . $payloadJson);
        }
        return $payload;
    }

    private static function b64UrlDecode(string $segment): string
    {
        $padded = $segment . str_repeat('=', (4 - (strlen($segment) % 4)) % 4);
        $decoded = base64_decode(strtr($padded, '-_', '+/'), strict: true);
        if ($decoded === false) {
            throw new \RuntimeException('base64url decode failed for segment: ' . $segment);
        }
        return $decoded;
    }
}
