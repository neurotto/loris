<?php
require '/var/www/loris/vendor/autoload.php';

use LORIS\ReMove\AnnexContent;

function ok(bool $condition, string $message): void
{
    if (!$condition) {
        fwrite(STDERR, "FAIL: $message\n");
        exit(1);
    }
    echo "ok: $message\n";
}

function requests(): array
{
    global $before;
    $log = getenv('REQUEST_LOG');
    return array_slice(is_file($log) ? file($log) : [], $before);
}

$before = 0;
$before = count(requests());
$data = sys_get_temp_dir() . '/chunk-data/';
$root = "{$data}assembly_bids";
putenv("RE_MOVE_CLONE_ROOT=$root");
$key = 'SHA256E-s9--eeg.vhdr';
$objects = "$root/nandou/sigmo/.git/annex/objects/Ab/Cd/$key";
mkdir($objects, 0777, true);
file_put_contents("$objects/$key", 'header');
mkdir("$root/import-1/sub-01/eeg", 0777, true);
symlink("$objects/$key", "$root/import-1/sub-01/eeg/sub-01_task-rest_eeg.vhdr");
$file = 'assembly_bids/import-1/sub-01/eeg/sub-01_task-rest_eeg.vhdr';
$chunks = "re-move-chunks/nandou/sigmo/$key/sub-01_task-rest_eeg.chunks";

putenv('RE_MOVE_CACHE_URL');
ok(AnnexContent::chunks($data, $file, $chunks, true) === null, 'without RE_MOVE_CACHE_URL nothing changes');

putenv('RE_MOVE_CACHE_URL=http://127.0.0.1:1');
ok(
    AnnexContent::chunks($data, $file, $chunks, true)['message'] === 'The EEG cache service is unreachable.',
    'an unreachable cache reports unavailable'
);

putenv('RE_MOVE_CACHE_URL=' . getenv('CACHE_URL'));
ok(
    AnnexContent::chunks($data, $file, "re-move-chunks/other/sigmo/$key/x.chunks", true) === null,
    'a chunk path that does not match the recording is left alone'
);
ok(requests() === [], 'mismatches make no request');

$status = AnnexContent::chunks($data, $file, $chunks, false);
ok($status === ['state' => 'fetching', 'message' => 'Fetching', 'retryAfter' => 5], 'missing chunks are prepared');
$request = json_decode(requests()[0], true);
ok(
    $request === [
        'site' => 'nandou', 'dataset' => 'sigmo', 'key' => $key,
        'path' => 'sub-01/eeg/sub-01_task-rest_eeg.vhdr', 'endpoint' => '/chunks',
    ],
    'the request names the recording inside its dataset'
);

mkdir("$data$chunks", 0777, true);
file_put_contents("$data$chunks/index.json", '{}');
ok(AnnexContent::chunks($data, $file, $chunks, false) === null, 'present chunks are served');
ok(count(requests()) === 1, 'without touch present chunks make no request');
ok(AnnexContent::chunks($data, $file, $chunks, true) === null, 'present chunks are served when touched');
ok(count(requests()) === 2, 'touching present chunks records the access');
