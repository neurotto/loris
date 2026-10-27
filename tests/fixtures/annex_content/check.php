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

$root = sys_get_temp_dir() . '/annex-root';
putenv("RE_MOVE_CLONE_ROOT=$root");
putenv('RE_MOVE_CACHE_URL');
$key = 'SHA256E-s3--abc.nii.gz';
$offline = 'SHA256E-s3--offline.nii.gz';
$objects = "$root/nandou/ds1/.git/annex/objects/Xy/Zw";
foreach ([$key, $offline] as $k) {
    mkdir("$objects/$k", 0777, true);
}
mkdir("$root/nandou/ds1/sub-1/anat", 0777, true);
mkdir("$root/view/sub-1", 0777, true);
symlink("../../.git/annex/objects/Xy/Zw/$key/$key", "$root/nandou/ds1/sub-1/anat/a.nii.gz");
symlink("../../.git/annex/objects/Xy/Zw/$offline/$offline", "$root/nandou/ds1/sub-1/anat/o.nii.gz");
symlink("$root/nandou/ds1/sub-1/anat/a.nii.gz", "$root/view/sub-1/a.nii.gz");
symlink('/etc/passwd', "$root/view/sub-1/escape");
symlink("$objects/$key/../../../../../../../../etc/passwd", "$root/view/sub-1/dots");

ok(
    AnnexContent::locate("$root/view/sub-1/a.nii.gz")
        === ['site' => 'nandou', 'dataset' => 'ds1', 'key' => $key],
    'locate follows the view and clone symlinks to the annex key'
);
ok(AnnexContent::locate("$root/view/sub-1/escape") === null, 'non-annex targets are ignored');
ok(AnnexContent::locate("$root/view/sub-1/dots") === null, 'dot-dot escapes are ignored');
ok(AnnexContent::check("$root/view/sub-1/a.nii.gz") === null, 'without RE_MOVE_CACHE_URL nothing changes');

putenv('RE_MOVE_CACHE_URL=http://127.0.0.1:1');
ok(
    AnnexContent::check("$root/view/sub-1/a.nii.gz")['state'] === 'unavailable',
    'an unreachable cache reports unavailable'
);

putenv('RE_MOVE_CACHE_URL=' . getenv('CACHE_URL'));
$status = AnnexContent::check("$root/view/sub-1/a.nii.gz");
ok($status === ['state' => 'fetching', 'message' => 'Fetching', 'retryAfter' => 5], 'missing content is fetched');
$response = AnnexContent::response($status);
ok(
    $response->getStatusCode() === 503 && $response->getHeaderLine('Retry-After') === '5',
    'the response is 503 with Retry-After'
);
ok(json_decode((string)$response->getBody(), true)['state'] === 'fetching', 'the body carries the state');
ok(
    AnnexContent::check("$root/nandou/ds1/sub-1/anat/o.nii.gz")['message'] === 'site S is offline',
    'the site status message is passed through'
);

file_put_contents("$objects/$key/$key", 'abc');
ok(AnnexContent::check("$root/view/sub-1/a.nii.gz") === null, 'present content is served');
$log = file(getenv('REQUEST_LOG'));
ok(
    count($log) === 3 && json_decode(end($log), true)['key'] === $key,
    'serving present content touches the cache'
);
