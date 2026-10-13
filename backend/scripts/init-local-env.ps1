param(
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$envPath = Join-Path $PSScriptRoot '..\.env'
$envPath = [System.IO.Path]::GetFullPath($envPath)

$existingCallbackSecret = $null
if (Test-Path -LiteralPath $envPath) {
    $existingContent = [System.IO.File]::ReadAllText($envPath)
    $callbackMatch = [regex]::Match(
        $existingContent,
        '(?m)^IMAGE_BACKEND_DIFY_CALLBACK_SECRET=([^\r\n]*)\r?$'
    )
    if ($callbackMatch.Success) {
        $existingCallbackSecret = $callbackMatch.Groups[1].Value
    }
}

function New-LocalPassword {
    $bytes = [byte[]]::new(48)
    $generator = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $generator.GetBytes($bytes)
    }
    finally {
        $generator.Dispose()
    }
    return [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
}

if ((Test-Path -LiteralPath $envPath) -and -not $Force) {
    if ($null -eq $existingCallbackSecret) {
        $callbackSecret = New-LocalPassword
        $separator = if ($existingContent.EndsWith("`n") -or $existingContent.EndsWith("`r")) {
            ''
        }
        else {
            "`r`n"
        }
        $appendContent = $separator + "IMAGE_BACKEND_DIFY_CALLBACK_SECRET=$callbackSecret`r`n"
        [System.IO.File]::AppendAllText($envPath, $appendContent, [System.Text.Encoding]::ASCII)
        Write-Output 'Added the independent callback secret to backend/.env. Values were not printed.'
    }
    else {
        Write-Output 'backend/.env already contains local credentials. Existing values were preserved.'
    }
    return
}

$admin = New-LocalPassword
$migrator = New-LocalPassword
$runtime = New-LocalPassword
$rabbit = New-LocalPassword
$testAdmin = New-LocalPassword
$testMigrator = New-LocalPassword
$testRuntime = New-LocalPassword
$testRabbit = New-LocalPassword
$callbackSecret = if ($null -ne $existingCallbackSecret) {
    $existingCallbackSecret
}
else {
    New-LocalPassword
}

$content = @"
IMAGE_BACKEND_DB_ADMIN_PASSWORD=$admin
IMAGE_BACKEND_MIGRATOR_PASSWORD=$migrator
IMAGE_BACKEND_RUNTIME_PASSWORD=$runtime
IMAGE_BACKEND_RABBITMQ_PASSWORD=$rabbit
IMAGE_BACKEND_TEST_DB_ADMIN_PASSWORD=$testAdmin
IMAGE_BACKEND_TEST_MIGRATOR_PASSWORD=$testMigrator
IMAGE_BACKEND_TEST_RUNTIME_PASSWORD=$testRuntime
IMAGE_BACKEND_TEST_RABBITMQ_PASSWORD=$testRabbit
IMAGE_BACKEND_DATABASE_URL=postgresql+psycopg://image_backend_runtime:$runtime@db:5432/image_backend
IMAGE_BACKEND_MIGRATION_DATABASE_URL=postgresql+psycopg://image_backend_migrator:$migrator@db:5432/image_backend
IMAGE_BACKEND_BROKER_URL=amqp://image_backend:$rabbit@rabbitmq:5672/image_backend
IMAGE_BACKEND_DIFY_CALLBACK_SECRET=$callbackSecret
IMAGE_BACKEND_GUEST_SESSION_TTL_DAYS=120
IMAGE_BACKEND_TEST_RUNTIME_DATABASE_URL=postgresql+psycopg://image_backend_runtime:$testRuntime@test-db:5432/image_backend_test
IMAGE_BACKEND_TEST_MIGRATION_DATABASE_URL=postgresql+psycopg://image_backend_migrator:$testMigrator@test-db:5432/image_backend_test
IMAGE_BACKEND_TEST_BROKER_URL=amqp://image_backend:$testRabbit@test-rabbitmq:5672/image_backend_test
BACKEND_API_PORT=8001
"@

Set-Content -LiteralPath $envPath -Value $content.Trim() -Encoding ascii
Write-Output 'Created backend/.env with independent random credentials. Values were not printed.'
