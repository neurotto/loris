<?php
class NDB_Page
{
}

class NDB_Factory
{
    public static object $instance;

    public static function singleton(): object
    {
        return self::$instance;
    }
}
class Utility
{
    public static function appendForwardSlash(string $path): string
    {
        return rtrim($path, '/') . '/';
    }
}

require '/var/www/loris/vendor/autoload.php';

$contentRoot = getenv('LORIS_CONTENT_ROOT');
$filePath = 'assembly_bids/import-reader/sub-01/eeg/reader.vhdr';
$source = "$contentRoot/pending/dataset/sub-01/eeg/reader.vhdr";
$view = "$contentRoot/import-reader/sub-01/eeg/reader.vhdr";
mkdir(dirname($view), 0777, true);
symlink($source, $view);
$relativePath = 'sub-01/eeg/reader.vhdr';
$digest = hash('sha256', $relativePath);
$chunkPath = 'content-provider-chunks/pending/dataset/' . $digest . '.chunks';

$database = new class($filePath) {
    public function __construct(private string $filePath)
    {
    }

    public function pselectOne(string $query, array $parameters): string
    {
        return $this->filePath;
    }
};
NDB_Factory::$instance = new class($database) {
    public function __construct(private object $database)
    {
    }

    public function database(): object
    {
        return $this->database;
    }

    public function config(): object
    {
        return new class {
            public function getSetting(string $name): string
            {
                return getenv('LORIS_DATA_ROOT');
            }
        };
    }
};
putenv('LORIS_CONTENT_PROVIDER_URL=' . getenv('PROVIDER_URL'));

require '/var/www/loris/modules/electrophysiology_browser/php/file_reader.class.inc';
$endpoint = new LORIS\electrophysiology_browser\File_Reader();
$request = (new GuzzleHttp\Psr7\ServerRequest('GET', '/file_reader'))
    ->withQueryParams(['file' => $chunkPath . '/index.json']);
$response = $endpoint->handle($request);
$body = json_decode((string)$response->getBody(), true);
if (
    $response->getStatusCode() !== 503
    || $response->getHeaderLine('Retry-After') !== '60'
    || ($body['state'] ?? null) !== 'pending'
) {
    fwrite(STDERR, 'FAIL: generic EEG chunk requests use the provider contract: '
        . $response->getStatusCode() . ' ' . (string)$response->getBody() . "\n");
    exit(1);
}
echo "ok: the EEG file reader waits for provider-managed chunks\n";
