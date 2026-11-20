<?php
require '/var/www/loris/vendor/autoload.php';

use LORIS\ContentProvider;

function ok(bool $condition, string $message): void
{
    if (!$condition) {
        fwrite(STDERR, "FAIL: $message\n");
        exit(1);
    }
    echo "ok: $message\n";
}

$base = getenv('LORIS_DATA_ROOT');
$root = getenv('LORIS_CONTENT_ROOT');
$viewDir = "$root/import-1/sub-01/eeg";
mkdir($viewDir, 0777, true);
putenv('LORIS_CONTENT_PROVIDER_URL=' . getenv('PROVIDER_URL'));

function contentView(string $site, string $filename, string $dataset = 'dataset'): string
{
    global $root, $viewDir;
    $target = "$root/$site/$dataset/sub-01/eeg/$filename";
    $view = "$viewDir/$site-$filename";
    symlink($target, $view);
    return $view;
}

$pending = contentView('pending', 'pending.vhdr');
$status = ContentProvider::check($pending);
ok(
    $status['state'] === 'pending' && $status['retryAfter'] === 60,
    'pending responses preserve the message and clamp Retry-After'
);
$dataset = 'study with ' . str_repeat('x', 100) . "\u{00e9}";
$longDatasetStatus = ContentProvider::check(
    contentView('pending', 'long-name.nii.gz', $dataset)
);
ok(
    $longDatasetStatus['state'] === 'pending',
    'provider content paths support spaces, UTF-8, and dataset names over 100 characters'
);
$delayed = contentView('ready-delayed', 'delayed.nii.gz');
$delayedStatus = ContentProvider::check($delayed);
ok(
    $delayedStatus['state'] === 'pending' && $delayedStatus['retryAfter'] === 1,
    'provider-ready content remains pending while the shared filesystem catches up'
);
mkdir("$root/ready-delayed/dataset/sub-01/eeg", 0777, true);
file_put_contents("$root/ready-delayed/dataset/sub-01/eeg/delayed.nii.gz", 'fixture');
ok(
    ContentProvider::check($delayed) === null,
    'provider-managed content is served after shared-filesystem propagation'
);
$response = ContentProvider::response($status);
ok(
    $response->getStatusCode() === 503
        && $response->getHeaderLine('Retry-After') === '60'
        && json_decode((string)$response->getBody(), true)['state'] === 'pending',
    'pending content is exposed as a 503 with Retry-After'
);

$unavailable = ContentProvider::check(contentView('unavailable', 'missing.nii.gz'));
ok(
    $unavailable['state'] === 'unavailable'
        && $unavailable['message'] === 'Fixture is unavailable.',
    'terminal provider failures are reported without retrying'
);

$malformed = ContentProvider::check(contentView('malformed', 'bad.nii.gz'));
ok(
    $malformed['state'] === 'unavailable'
        && $malformed['message'] === 'The content provider returned an invalid response.',
    'malformed provider responses fail closed'
);

$pendingOnce = contentView('pending-once', 'ready-after-pending.nii.gz');
$pendingStatus = ContentProvider::check($pendingOnce);
ok(
    $pendingStatus['state'] === 'pending',
    'the provider can report pending before content is ready: ' . json_encode($pendingStatus)
);
ok(ContentProvider::check($pendingOnce) === null, 'ready content is served after the provider prepares it');

$escape = "$viewDir/escape.vhdr";
symlink('/etc/passwd', $escape);
ok(ContentProvider::check($escape) === null, 'paths outside the configured content root are ignored');

$filename = 'sub-01_task-rest_eeg.vhdr';
$chunkView = contentView('chunks-once', $filename, $dataset);
$filePath = 'assembly_bids/import-1/sub-01/eeg/chunks-once-' . $filename;
$relativePath = 'sub-01/eeg/' . $filename;
$digest = hash('sha256', $relativePath);
$chunkPath = 'content-provider-chunks/chunks-once/' . $dataset . '/' . $digest . '.chunks';
ok(
    ContentProvider::chunks($base . '/', $filePath, $chunkPath, false)['state'] === 'pending',
    'missing EEG chunks are requested from the provider'
);
ok(
    ContentProvider::chunks($base . '/', $filePath, $chunkPath, false) === null
        && is_file($base . '/' . $chunkPath . '/index.json'),
    'ready EEG chunks are served from the shared data volume'
);
ok(
    ContentProvider::chunks($base . '/', $filePath, $chunkPath . 'x', false) === null,
    'chunk requests cannot substitute a path unrelated to the recording'
);
$delayedChunkView = contentView('chunks-ready-delayed', $filename);
$delayedChunkPath = 'content-provider-chunks/chunks-ready-delayed/dataset/'
    . $digest . '.chunks';
$delayedChunkStatus = ContentProvider::chunks(
    $base . '/',
    'assembly_bids/import-1/sub-01/eeg/chunks-ready-delayed-' . $filename,
    $delayedChunkPath,
    false
);
ok(
    $delayedChunkStatus['state'] === 'pending' && $delayedChunkStatus['retryAfter'] === 1,
    'provider-ready chunks remain pending until visible on shared storage'
);
mkdir($base . '/' . $delayedChunkPath, 0777, true);
file_put_contents($base . '/' . $delayedChunkPath . '/index.json', '{}');
ok(
    ContentProvider::chunks(
        $base . '/',
        'assembly_bids/import-1/sub-01/eeg/chunks-ready-delayed-' . $filename,
        $delayedChunkPath,
        false
    ) === null,
    'provider EEG chunks are served after shared-storage propagation'
);

$requests = array_map(
    static fn (string $line): array => json_decode($line, true),
    file(getenv('REQUEST_LOG'), FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES)
);
$chunkRequests = array_values(array_filter(
    $requests,
    static fn (array $request): bool => $request['endpoint'] === '/chunks'
));
ok(
    count($requests) === 11
        && count(array_filter($requests, static fn (array $request): bool =>
            isset($request['key']) || isset($request['site']) || isset($request['dataset'])
        )) === 0,
    'provider requests use only logical paths, never backend identifiers'
);
ok(
    in_array('pending/' . $dataset . '/sub-01/eeg/long-name.nii.gz',
        array_column($requests, 'path'), true),
    'provider requests preserve the original dataset path'
);
ok(
    $chunkRequests[1]['chunkPath'] === $chunkPath,
    'chunk requests include the deterministic shared-volume destination'
);

$timeout = ContentProvider::check(contentView('timeout', 'slow.nii.gz'));
ok(
    $timeout['state'] === 'unavailable'
        && $timeout['message'] === 'The content provider is unavailable.',
    'provider request timeouts have a terminal, bounded result'
);
