<?php

declare(strict_types=1);

/**
 * ProvisionClinicalCoPilotApiClientCommand.php
 *
 * Idempotently provisions the OpenEMR API client used by the Clinical
 * Co-Pilot sidecar. Always leaves exactly one enabled `oauth_clients`
 * row whose name equals self::CLIENT_NAME, regardless of how many
 * existed before, and prints the credentials as a single JSON line on
 * stdout so a calling shell script can parse them deterministically.
 *
 * Design goals (the shell wrapper relies on every one of these):
 *
 *   - Idempotent. Re-running deletes any existing rows with our exact
 *     name, then inserts a fresh row. Never accumulates orphans across
 *     repeated runs.
 *   - Single source of truth for the registration shape. The list of
 *     SMART (Substitutable Medical Apps and Reusable Technology)
 *     system scopes, the redirect URI, the grant_types value, and the
 *     client name are all constants here. The shell wrapper does not
 *     pass them in, so they cannot drift.
 *   - Machine-readable output. Symfony's table renderer is awkward
 *     to parse from bash; a single JSON line is not.
 *   - Diagnosable failures. Every error path writes a precise message
 *     to STDERR and exits non-zero so the caller can surface the cause
 *     without re-running with --verbose.
 *
 * Wire-compatible with the sidecar's `OpenEMRTokenCache`:
 * `clinical-copilot/sidecar/openemr_oauth.py` uses JWT bearer assertion
 * on the OAuth2 `/token` endpoint with the JWKS registered here.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 *
 * @author    Scott Lydon <relays.inanity.0n@icloud.com>
 * @copyright Copyright (c) 2026 Scott Lydon
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

namespace OpenEMR\Modules\ClinicalCoPilot\Console;

use OpenEMR\Common\Auth\OpenIDConnect\Entities\ClientEntity;
use OpenEMR\Common\Auth\OpenIDConnect\Repositories\ClientRepository;
use Symfony\Component\Console\Command\Command;
use Symfony\Component\Console\Input\InputDefinition;
use Symfony\Component\Console\Input\InputInterface;
use Symfony\Component\Console\Input\InputOption;
use Symfony\Component\Console\Output\OutputInterface;

final class ProvisionClinicalCoPilotApiClientCommand extends Command
{
    /**
     * The exact `oauth_clients.client_name` value this command owns.
     * Re-running the command deletes every row matching this name
     * before inserting a fresh one, so collisions are never possible
     * but stale rows always get cleaned up.
     */
    private const CLIENT_NAME = 'Clinical Co-Pilot Sidecar';

    /**
     * The OAuth2 scopes the sidecar requests via client_credentials.
     * Trimmed to exactly what `clinical-copilot/sidecar/openemr_oauth.py`
     * SYSTEM_FHIR_SCOPES asks for, so the granted set never exceeds
     * what the sidecar can use (least-privilege).
     */
    private const SYSTEM_SCOPES = [
        'system/Patient.read',
        'system/Condition.read',
        'system/MedicationRequest.read',
        'system/AllergyIntolerance.read',
        'system/Observation.read',
        'system/Encounter.read',
        'system/Procedure.read',
        'system/DocumentReference.read',
    ];

    /**
     * The contact email written into oauth_clients.contacts. Not used
     * by the sidecar; only shown in OpenEMR's API Clients admin list.
     */
    private const CONTACT = 'sidecar@clinical-copilot.local';

    protected function configure(): void
    {
        $this
            ->setName('clinical-copilot:provision-api-client')
            ->setDescription(
                'Idempotently provision the Clinical Co-Pilot API client in '
                . 'oauth_clients (deletes any existing row with the same name '
                . 'first). Outputs one JSON line on stdout: '
                . '{"client_id":"...","client_secret":"...","rotated":bool,...}.'
            )
            ->setDefinition(new InputDefinition([
                new InputOption(
                    'site',
                    null,
                    InputOption::VALUE_REQUIRED,
                    'OpenEMR site id',
                    'default'
                ),
                new InputOption(
                    'redirect-uri',
                    null,
                    InputOption::VALUE_REQUIRED,
                    'OAuth2 redirect URI written into oauth_clients.redirect_uri. '
                    . 'client_credentials does not use it but the column is NOT NULL.',
                    'http://localhost:8801/oauth/callback'
                ),
                new InputOption(
                    'jwks-json',
                    null,
                    InputOption::VALUE_REQUIRED,
                    'JSON Web Key Set (JWK Set per RFC 7517) for the SMART '
                    . 'Backend Services jwt-bearer assertion verification. Either '
                    . 'a literal JSON string or @/path/to/file.json. Required: '
                    . 'OpenEMR\'s CustomClientCredentialsGrant only accepts '
                    . 'jwt-bearer (HTTP Basic is rejected with "assertion type '
                    . 'is not supported").',
                    null
                ),
            ]));
    }

    protected function execute(InputInterface $input, OutputInterface $output): int
    {
        try {
            $site = (string) $input->getOption('site');
            $redirectUri = (string) $input->getOption('redirect-uri');
            $jwksOpt = $input->getOption('jwks-json');
            if ($site === '') {
                $this->fail('--site must be a non-empty string');
                return Command::FAILURE;
            }
            if ($redirectUri === '') {
                $this->fail('--redirect-uri must be a non-empty string');
                return Command::FAILURE;
            }
            if (!is_string($jwksOpt) || $jwksOpt === '') {
                $this->fail(
                    '--jwks-json is required. Pass either an inline JSON string '
                    . '\'{"keys":[...]}\' or @/abs/path/to/jwks.json.'
                );
                return Command::FAILURE;
            }
            if (str_starts_with($jwksOpt, '@')) {
                $jwksPath = substr($jwksOpt, 1);
                if ($jwksPath === '' || !is_file($jwksPath) || !is_readable($jwksPath)) {
                    $this->fail("--jwks-json @{$jwksPath} not found or not readable inside the container");
                    return Command::FAILURE;
                }
                $jwksJson = file_get_contents($jwksPath);
                if ($jwksJson === false) {
                    $this->fail("--jwks-json @{$jwksPath} could not be read");
                    return Command::FAILURE;
                }
            } else {
                $jwksJson = $jwksOpt;
            }
            $jwksDecoded = json_decode($jwksJson, associative: true);
            if (
                !is_array($jwksDecoded)
                || !isset($jwksDecoded['keys'])
                || !is_array($jwksDecoded['keys'])
                || $jwksDecoded['keys'] === []
            ) {
                $this->fail(
                    '--jwks-json must decode to a JWK Set object with a non-empty '
                    . '"keys" array; got ' . substr($jwksJson, 0, 200)
                );
                return Command::FAILURE;
            }
            $jwksJsonNormalised = json_encode(
                $jwksDecoded,
                JSON_THROW_ON_ERROR | JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE
            );

            $repo = new ClientRepository();

            $existing = [];
            foreach ($repo->listClientEntities() as $client) {
                if ($client->getName() === self::CLIENT_NAME) {
                    $existing[] = $client;
                }
            }
            foreach ($existing as $client) {
                $repo->remove($client, noLog: true);
            }
            $rotated = $existing !== [];

            $clientId = $repo->generateClientId();
            $clientSecret = $repo->generateClientSecret();
            $info = [
                'client_role' => 'user',
                'client_name' => self::CLIENT_NAME,
                'client_secret' => $clientSecret,
                'registration_access_token' => $repo->generateRegistrationAccessToken(),
                'registration_client_uri_path' => $repo->generateRegistrationClientUriPath(),
                'contacts' => self::CONTACT,
                'redirect_uris' => [$redirectUri],
                'grant_types' => 'client_credentials',
                'scope' => implode(' ', self::SYSTEM_SCOPES),
                'dsi_type' => ClientEntity::DSI_TYPE_NONE,
                'jwks_uri' => null,
                'jwks' => $jwksJsonNormalised,
                'initiate_login_uri' => null,
            ];

            $saved = $repo->insertNewClient($clientId, $info, $site);
            if (!$saved) {
                $this->fail(sprintf(
                    'ClientRepository::insertNewClient() returned false for '
                    . 'client_id=%s site=%s. Check the OpenEMR error log.',
                    $clientId,
                    $site
                ));
                return Command::FAILURE;
            }

            $client = $repo->getClientEntity($clientId);
            if ($client === false) {
                $this->fail(sprintf(
                    'getClientEntity() returned false for client_id=%s '
                    . 'immediately after insertNewClient() succeeded. '
                    . 'oauth_clients.is_enabled left at the default; the '
                    . 'sidecar will get HTTP 401 on its first token call.',
                    $clientId
                ));
                return Command::FAILURE;
            }
            $repo->saveIsEnabled($client, true);

            $payload = [
                'client_id' => $clientId,
                'client_secret' => $clientSecret,
                'rotated' => $rotated,
                'previous_count' => count($existing),
                'site' => $site,
                'name' => self::CLIENT_NAME,
                'scope' => $info['scope'],
                'redirect_uri' => $redirectUri,
                'jwks_key_count' => count($jwksDecoded['keys']),
                'auth_method' => 'private_key_jwt',
            ];
            $json = json_encode($payload, JSON_THROW_ON_ERROR | JSON_UNESCAPED_SLASHES);
            $output->writeln($json);

            return Command::SUCCESS;
        } catch (\Throwable $e) {
            $this->fail(sprintf(
                '%s: %s',
                $e::class,
                $e->getMessage()
            ));
            fwrite(STDERR, $e->getTraceAsString() . PHP_EOL);
            return Command::FAILURE;
        }
    }

    /**
     * Write a precise error to STDERR. Stays off stdout so the JSON
     * channel remains parseable even when the command fails.
     */
    private function fail(string $message): void
    {
        fwrite(STDERR, '[clinical-copilot:provision-api-client] ERROR: ' . $message . PHP_EOL);
    }
}
