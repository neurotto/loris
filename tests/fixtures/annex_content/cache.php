<?php
// Fake re-move-cache: records requests, reports one key's site offline.
$body = json_decode(file_get_contents('php://input'), true);
$body['endpoint'] = parse_url($_SERVER['REQUEST_URI'], PHP_URL_PATH);
file_put_contents(getenv('REQUEST_LOG'), json_encode($body) . "\n", FILE_APPEND);
header('Content-Type: application/json');
echo json_encode(
    $body['key'] === 'SHA256E-s3--offline.nii.gz'
        ? ['state' => 'unavailable', 'message' => 'site S is offline', 'retryAfter' => 30]
        : ['state' => 'fetching', 'message' => 'Fetching', 'retryAfter' => 5]
);
