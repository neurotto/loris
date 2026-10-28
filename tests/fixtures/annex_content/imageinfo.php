<?php
require '/var/www/loris/vendor/autoload.php';

class NDB_Page
{
    public $loris;
}

class NDB_Factory
{
    public static $instance;

    public static function singleton()
    {
        return self::$instance;
    }
}

require '/var/www/loris/modules/brainbrowser/php/imageinfo.class.inc';

function expect(bool $condition, string $message): void
{
    if (!$condition) {
        throw new RuntimeException($message);
    }
    echo "ok: $message\n";
}

$root = sys_get_temp_dir() . '/imageinfo-annex';
$key = 'SHA256E-s3--abc.nii.gz';
$object = "$root/nandou/ds1/.git/annex/objects/Xy/Zw/$key/$key";
mkdir(dirname($object), 0777, true);
mkdir("$root/view", 0777, true);
symlink($object, "$root/view/a.nii.gz");
putenv("RE_MOVE_CLONE_ROOT=$root");
putenv('RE_MOVE_CACHE_URL=' . getenv('CACHE_URL'));

$rows = [
    ['Source' => 'ImagingBrowser', 'CandID' => '700026', 'Visit_label' => '2',
     'File' => 'view/a.nii.gz', 'FileID' => 40],
];
$db = new class($rows) {
    public function __construct(private array $rows)
    {
    }

    public function pselect(string $query, array $params): Countable&IteratorAggregate
    {
        return new class($this->rows) implements Countable, IteratorAggregate {
            private NoRewindIterator $cursor;

            public function __construct(private array $rows)
            {
                $this->cursor = new NoRewindIterator(new ArrayIterator($rows));
            }

            public function count(): int
            {
                return count($this->rows);
            }

            public function getIterator(): Traversable
            {
                return $this->cursor;
            }
        };
    }
};
$loris = new class($db, $root) {
    public function __construct(private object $db, private string $root)
    {
    }

    public function getDatabaseConnection()
    {
        return $this->db;
    }

    public function config()
    {
        return $this;
    }

    public function getSetting(string $name)
    {
        return $this->root . '/';
    }

    public function settings()
    {
        return $this;
    }

    public function getBaseURL()
    {
        return '';
    }
};
NDB_Factory::$instance = $loris;
$endpoint = new LORIS\brainbrowser\ImageInfo();
$endpoint->loris = $loris;
$request = (new GuzzleHttp\Psr7\ServerRequest('GET', '/brainbrowser/imageinfo'))
    ->withQueryParams(['fileids' => '40']);

for ($attempt = 0; $attempt < 2; $attempt++) {
    $response = $endpoint->handle($request);
    expect($response->getStatusCode() === 503, 'cold metadata request waits for content');
    expect($response->getHeaderLine('Retry-After') === '5', 'metadata advertises retry delay');
    expect(
        json_decode((string)$response->getBody(), true)['state'] === 'fetching',
        'metadata exposes fetching state to the viewer retry loop'
    );
}

file_put_contents($object, 'abc');
clearstatcache();
$response = $endpoint->handle($request);
expect($response->getStatusCode() === 200, 'warm metadata request succeeds');
$body = json_decode((string)$response->getBody(), true);
expect(
    count($body) === 1
        && $body[0]['URL'] === '/api/v0.0.3/candidates/700026/2/images/a.nii.gz',
    'warm metadata retains the registered image URL'
);
