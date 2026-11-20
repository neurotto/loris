<?php
$request = json_decode(file_get_contents('php://input'), true);
$request['endpoint'] = parse_url($_SERVER['REQUEST_URI'], PHP_URL_PATH);

$parts = explode('/', $request['path'] ?? '');
$mode = $parts[0] ?? '';
$stateFile = sys_get_temp_dir() . '/content-provider-state/'
    . hash('sha256', $request['endpoint'] . ($request['path'] ?? ''));
$count = is_file($stateFile) ? (int)file_get_contents($stateFile) : 0;
$request['mode'] = $mode;
$request['count'] = $count;
file_put_contents(getenv('REQUEST_LOG'), json_encode($request) . "\n", FILE_APPEND);

if ($mode === 'timeout') {
    sleep(11);
}
if ($mode === 'pending' || ($mode === 'pending-once' && $count === 0)
    || ($mode === 'chunks-once' && $count === 0)
) {
    if (!is_dir(dirname($stateFile))) {
        mkdir(dirname($stateFile), 0777, true);
    }
    file_put_contents($stateFile, (string)($count + 1));
    http_response_code(202);
    header('Content-Type: application/json');
    echo json_encode([
        'state' => 'pending',
        'message' => 'Preparing fixture content.',
        'retryAfter' => 999,
    ]);
    return;
}
if ($mode === 'unavailable') {
    http_response_code(404);
    header('Content-Type: application/json');
    echo json_encode(['state' => 'unavailable', 'message' => 'Fixture is unavailable.']);
    return;
}
if ($mode === 'malformed') {
    http_response_code(200);
    header('Content-Type: application/json');
    echo '{"state":';
    return;
}
if ($mode === 'ready-delayed' || $mode === 'chunks-ready-delayed') {
    http_response_code(200);
    header('Content-Type: application/json');
    echo json_encode(['state' => 'ready']);
    return;
}

if ($request['endpoint'] === '/chunks') {
    $chunkPath = getenv('LORIS_DATA_ROOT') . '/' . ($request['chunkPath'] ?? '');
    mkdir($chunkPath, 0777, true);
    file_put_contents($chunkPath . '/index.json', '{}');
} else {
    $contentPath = getenv('LORIS_CONTENT_ROOT') . '/'
        . ($request['path'] ?? '');
    mkdir(dirname($contentPath), 0777, true);
    file_put_contents($contentPath, 'fixture');
}
http_response_code(200);
header('Content-Type: application/json');
echo json_encode(['state' => 'ready']);
