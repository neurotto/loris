<?php declare(strict_types=1);

require_once '/var/www/loris/vendor/autoload.php';

function requiredEnvironmentValue(string $name): string
{
    $value = getenv($name);
    if ($value === false || $value === '') {
        throw new RuntimeException("Required environment variable {$name} is missing.");
    }
    return $value;
}

function runInstallerStep(\LORIS\Installer\Installer $installer, string $name, array $values): void
{
    if ($installer->{$name}($values) === false) {
        throw new RuntimeException($installer->getLastError() ?: "LORIS installer step {$name} failed.");
    }
}

$projectConfig = '/var/www/loris/project/config.xml';
$dbHost = requiredEnvironmentValue('LORIS_DB_HOST');
$dbName = requiredEnvironmentValue('LORIS_DB_NAME');
if (!preg_match('/^[a-zA-Z][a-zA-Z0-9_]*$/', $dbName)) {
    throw new RuntimeException('LORIS_DB_NAME must be a SQL identifier.');
}
$rootPassword = requiredEnvironmentValue('MARIADB_ROOT_PASSWORD');
$publicUrl = parse_url(requiredEnvironmentValue('LORIS_PUBLIC_URL'));
if ($publicUrl === false || empty($publicUrl['host'])) {
    throw new RuntimeException('LORIS_PUBLIC_URL must be an absolute URL.');
}

$dsn = "mysql:host={$dbHost};charset=UTF8";
$rootConnection = new PDO($dsn, 'root', $rootPassword, [PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION]);
$lock = $rootConnection->prepare('SELECT GET_LOCK(:name, 60)');
$lock->execute(['name' => 'loris-bootstrap-' . substr(hash('sha256', $dbName), 0, 32)]);
if ((int)$lock->fetchColumn() !== 1) {
    throw new RuntimeException('Could not acquire the LORIS bootstrap lock.');
}
$tableCheck = $rootConnection->prepare(
    'SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = :database'
);
$tableCheck->execute(['database' => $dbName]);
$hasTables = (int)$tableCheck->fetchColumn() !== 0;

$_SERVER['HTTP_HOST'] = $publicUrl['host'];
$_SERVER['HTTPS'] = ($publicUrl['scheme'] ?? '') === 'https' ? 'on' : 'off';
$_SERVER['SERVER_PORT'] = $_SERVER['HTTPS'] === 'on' ? '443' : '80';
$_SERVER['REQUEST_URI'] = rtrim($publicUrl['path'] ?? '', '/') . '/installdb.php';

$factory = NDB_Factory::singleton();
$factory->setConfig(\LORIS\Installer\NDB_Config::singleton());
$installer = new \LORIS\Installer\Installer();
$values = [
    'dbhost' => $dbHost,
    'dbname' => $dbName,
    'dbadminuser' => 'root',
    'dbadminpassword' => $rootPassword,
    'lorismysqluser' => requiredEnvironmentValue('LORIS_DB_USER'),
    'lorismysqlpassword' => requiredEnvironmentValue('LORIS_DB_PASSWORD'),
    'frontenduser' => requiredEnvironmentValue('LORIS_ADMIN_USER'),
    'frontendpassword' => requiredEnvironmentValue('LORIS_FRONTEND_ADMIN_PASSWORD'),
];

if ($hasTables) {
    $markerCheck = $rootConnection->prepare(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = :database AND table_name = 'scilus_bootstrap'"
    );
    $markerCheck->execute(['database' => $dbName]);
    $hasMarker = (int)$markerCheck->fetchColumn() === 1;
    if ($hasMarker && (int)$rootConnection->query("SELECT Completed FROM `{$dbName}`.scilus_bootstrap")->fetchColumn() !== 1) {
        throw new RuntimeException('The LORIS database bootstrap is incomplete; refusing to serve partial data.');
    }
    if (!$hasMarker && !is_file($projectConfig)) {
        throw new RuntimeException('Existing database has no completed bootstrap marker or config; refusing to reinitialize data.');
    }
    if (!\LORIS\Installer\Database::canLogIn($dbHost, $dbName, $values['lorismysqluser'], $values['lorismysqlpassword'])) {
        throw new RuntimeException('The configured LORIS database account cannot connect.');
    }
    if (!is_file($projectConfig)) {
        runInstallerStep($installer, 'writeConfig', $values);
        chmod($projectConfig, 0600);
    }
    fwrite(STDOUT, "LORIS database is initialized; project configuration is available.\n");
    exit(0);
}

runInstallerStep($installer, 'sourceSchema', $values);
runInstallerStep($installer, 'updateBaseConfig', $values);
runInstallerStep($installer, 'createMySQLAccount', $values);
runInstallerStep($installer, 'resetFrontEndAdmin', $values);
$rootConnection->exec("CREATE TABLE `{$dbName}`.scilus_bootstrap (Completed BOOLEAN NOT NULL)");
$rootConnection->exec("INSERT INTO `{$dbName}`.scilus_bootstrap VALUES (TRUE)");
runInstallerStep($installer, 'writeConfig', $values);
chmod($projectConfig, 0600);

fwrite(STDOUT, "LORIS schema, database account, frontend admin, and config initialized.\n");