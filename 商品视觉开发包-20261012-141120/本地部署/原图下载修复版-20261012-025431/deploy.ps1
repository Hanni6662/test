param(
    [Parameter(Mandatory=$true)][ValidateSet('init','check','build','up','stop','health','backup','restore','export','import')][string]$Action,
    [Parameter(Mandatory=$true)][string]$Instance,
    [int]$Port = 18081,
    [string]$Bundle
)
$ErrorActionPreference = 'Stop'
$deploymentRoot = $PSScriptRoot
if ($Instance -notmatch '^localimg-[a-z0-9][a-z0-9-]{1,40}$') { throw '实例名须为 localimg- 加小写字母、数字或连字符，例如 localimg-demo01。' }
if ($Bundle -and $Bundle -notmatch '^[a-zA-Z0-9][a-zA-Z0-9_-]{1,80}$') { throw '备份名仅允许 ASCII 字母、数字、连字符和下划线。' }
$instanceEnv = Join-Path $deploymentRoot "instances/$Instance.env"
$images = Get-Content -LiteralPath (Join-Path $deploymentRoot 'release-images.json') -Raw | ConvertFrom-Json
function Run-Docker { & docker @args; if ($LASTEXITCODE -ne 0) { throw "Docker 命令失败 (exit=$LASTEXITCODE)，请查看上方诊断。" } }
function Compose {
    $composeArgs = @('compose','--env-file',$instanceEnv,'-p',$Instance,'-f',(Join-Path $deploymentRoot 'compose.yaml'))
    if (Test-Path -LiteralPath (Join-Path $deploymentRoot 'images/offline-mode')) { $composeArgs += @('-f',(Join-Path $deploymentRoot 'images/compose.offline.yaml')) }
    Run-Docker @composeArgs @args
}
function Tools { Compose --profile tools run --rm --no-deps -T tools @args }
function Fresh {
    $existingNetworks = @(& docker network ls --format '{{.Name}}')
    foreach ($suffix in @('private','ingress')) {
        if ($existingNetworks -contains "${Instance}_$suffix") { throw "目标实例已有网络 ${Instance}_$suffix；请使用全新实例，禁止复用已有资源。" }
    }
    $existingVolumes = @(& docker volume ls --format '{{.Name}}')
    foreach ($suffix in @('database_data','queue_data','private_data')) {
        if ($existingVolumes -contains "${Instance}_$suffix") { throw "目标实例已有数据卷 ${Instance}_$suffix；禁止初始化或恢复覆盖。请使用全新实例。" }
    }
    $existing = & docker ps -aq --filter "label=com.docker.compose.project=$Instance"
    if ($existing) { throw '目标实例已有容器；初始化/恢复需要全新实例。' }
}
if ($Action -eq 'init') {
    if (Test-Path -LiteralPath $instanceEnv) { throw '配置已存在，凭证保持不变。请使用 check/up 或新实例名。' }
    Fresh
    $networkIds = @(& docker network ls -q)
    $occupiedSubnets = (& docker network inspect @networkIds --format '{{range .IPAM.Config}}{{.Subnet}} {{end}}') -join ' '
    $pythonImage = $images.python
    if (Test-Path -LiteralPath (Join-Path $deploymentRoot 'images/offline-mode')) { $pythonImage = (Get-Content -LiteralPath (Join-Path $deploymentRoot 'images/python-image-id.txt') -Raw).Trim() }
    Run-Docker run --rm --network none --mount "type=bind,source=$deploymentRoot,target=/delivery" $pythonImage python /delivery/scripts/init_env.py $Instance $Port $occupiedSubnets
    exit
}
if ($Action -eq 'import') {
    $archivePath = Join-Path $deploymentRoot 'images/local-private-amd64.tar'
    $expectedHash = (Get-Content -LiteralPath (Join-Path $deploymentRoot 'images/archive-sha256.txt') -Raw).Trim()
    if ((Get-FileHash -Algorithm SHA256 -LiteralPath $archivePath).Hash.ToLowerInvariant() -ne $expectedHash) { throw '离线镜像归档 SHA-256 不匹配；拒绝导入。' }
    Run-Docker load -i (Join-Path $deploymentRoot 'images/local-private-amd64.tar')
    Set-Content -LiteralPath (Join-Path $deploymentRoot 'images/offline-mode') -Value 'Use immutable local image IDs; do not rely on RepoDigests surviving docker save/load.' -Encoding ascii
    exit
}
if (!(Test-Path -LiteralPath $instanceEnv)) { throw "缺少 instances/$Instance.env。首次部署先运行 init；旧数据迁移须先配套保管原配置。" }
$settings = @{}
Get-Content -LiteralPath $instanceEnv | ForEach-Object { if ($_ -match '^([A-Z_]+)=(.*)$') { $settings[$Matches[1]]=$Matches[2] } }
if ($settings.LOCAL_INSTANCE -ne $Instance) { throw '配置中的实例名与命令不一致。' }
foreach ($key in @('ADMIN_PASSWORD','MIGRATOR_PASSWORD','RUNTIME_PASSWORD','BROKER_PASSWORD','CALLBACK_SECRET','MAINTENANCE_SECRET')) {
    if ($settings[$key] -notmatch '^[a-f0-9]{64}$') { throw "缺少或无效配置 $key，须为独立 64 位十六进制随机凭证；不要复制旧业务环境凭证。" }
}
if (@($settings.ADMIN_PASSWORD,$settings.MIGRATOR_PASSWORD,$settings.RUNTIME_PASSWORD,$settings.BROKER_PASSWORD,$settings.CALLBACK_SECRET,$settings.MAINTENANCE_SECRET | Select-Object -Unique).Count -ne 6) { throw '六个凭证必须相互独立。' }
if ($settings.API_PORT -notmatch '^\d+$' -or [int]$settings.API_PORT -lt 1024 -or [int]$settings.API_PORT -gt 65535) { throw 'API_PORT 须为 1024..65535。' }
if ($settings.APP_IMAGE -ne $images.app) { throw 'APP_IMAGE 与本交付包的固定镜像不一致。' }
if ($settings.PRIVATE_SUBNET -notmatch '^\d{1,3}\.\d{1,3}\.\d{1,3}\.0/24$') { throw 'PRIVATE_SUBNET 须为独立 IPv4 /24 子网；避免现有 Docker 网络、LAN 和 VPN。' }
if ($settings.INGRESS_SUBNET -notmatch '^\d{1,3}\.\d{1,3}\.\d{1,3}\.0/24$' -or $settings.INGRESS_SUBNET -eq $settings.PRIVATE_SUBNET) { throw 'INGRESS_SUBNET 须为另一个独立 IPv4 /24 子网。' }
foreach ($key in $settings.Keys) { [Environment]::SetEnvironmentVariable($key, $settings[$key], 'Process') }
Compose config -q
switch ($Action) {
    check { Write-Host '配置合法；本地 mock 模式，无外部 Dify/供应商。' }
    build {
        $localImages = @(& docker image ls --format '{{.Repository}}:{{.Tag}}')
        if ($localImages -notcontains $images.app) { Compose build migrate }
        Run-Docker run --rm --network none --mount "type=bind,source=$deploymentRoot,target=/delivery,readonly" $images.app python /delivery/scripts/verify_image.py
    }
    up { Compose up -d --no-build --pull never --wait --wait-timeout 240 }
    stop { Compose stop --timeout 60 }
    health {
        Compose ps -a
        Compose exec -T api python -c "from urllib.request import urlopen; print(urlopen('http://127.0.0.1:8000/health/ready',timeout=3).read().decode())"
        Compose exec -T worker python -m app.workers.healthcheck
        Compose exec -T dispatcher python /opt/local/loop_health.py dispatcher
        Compose exec -T scheduler python /opt/local/loop_health.py scheduler
        Compose run --rm --no-deps -T migrate alembic current
    }
    backup {
        if (!$Bundle) { throw 'backup 需要 -Bundle 名称。' }
        if (Test-Path -LiteralPath (Join-Path $deploymentRoot "backups/$Bundle")) { throw '备份目录已存在；拒绝覆盖。' }
        Compose stop --timeout 60 api worker dispatcher scheduler
        Tools pack $Bundle
        Compose exec -T db sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" pg_dump -U image_backend_admin -d image_backend -Fc --no-owner --no-acl -f /tmp/local-private-backup.dump'
        $dbId = (& docker compose --env-file $instanceEnv -p $Instance -f (Join-Path $deploymentRoot 'compose.yaml') ps -q db).Trim()
        Run-Docker cp "${dbId}:/tmp/local-private-backup.dump" (Join-Path $deploymentRoot "backups/$Bundle/database.dump")
        Compose exec -T db rm /tmp/local-private-backup.dump
        Tools seal $Bundle
        Write-Host '数据库和图片配套备份已校验。业务进程保持停止；需要继续使用时运行 up。请安全保管备份与 instances 配置。'
    }
    restore {
        if (!$Bundle) { throw 'restore 需要 -Bundle 名称。' }
        Fresh
        Compose up -d --no-build --pull never --wait --wait-timeout 180 db rabbitmq
        Tools validate $Bundle
        Tools empty $Bundle
        Tools unpack $Bundle
        $dbId = (& docker compose --env-file $instanceEnv -p $Instance -f (Join-Path $deploymentRoot 'compose.yaml') ps -q db).Trim()
        Run-Docker cp (Join-Path $deploymentRoot "backups/$Bundle/database.dump") "${dbId}:/tmp/local-private-restore.dump"
        Compose exec -T db sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" pg_restore -U image_backend_admin -d image_backend --role=image_backend_migrator --no-owner --no-privileges --exit-on-error --single-transaction /tmp/local-private-restore.dump'
        Compose exec -T db rm /tmp/local-private-restore.dump
        Tools verify $Bundle
        Compose up -d --no-build --pull never --wait --wait-timeout 240
        Write-Host '恢复记录、关联文件及逐表摘要核对通过，服务已启动。'
    }
    export {
        $pythonImage = $images.python
        $postgresImage = $images.postgres
        $rabbitImage = $images.rabbitmq
        if (Test-Path -LiteralPath (Join-Path $deploymentRoot 'images/offline-mode')) {
            $imageLock = Get-Content -LiteralPath (Join-Path $deploymentRoot 'images/image-lock.json') -Raw | ConvertFrom-Json
            $pythonImage = $imageLock.python; $postgresImage = $imageLock.postgres; $rabbitImage = $imageLock.rabbitmq
        }
        Run-Docker save -o (Join-Path $deploymentRoot 'images/local-private-amd64.tar') $images.app $pythonImage $postgresImage $rabbitImage
        Run-Docker image inspect $images.app $pythonImage $postgresImage $rabbitImage --format '{{.Id}} {{json .RepoTags}}' | Set-Content -LiteralPath (Join-Path $deploymentRoot 'images/image-ids.txt') -Encoding utf8
        Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $deploymentRoot 'images/local-private-amd64.tar') | Select-Object Hash | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $deploymentRoot 'images/archive-sha256.json') -Encoding utf8
        $archiveHash = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $deploymentRoot 'images/local-private-amd64.tar')).Hash.ToLowerInvariant()
        Set-Content -LiteralPath (Join-Path $deploymentRoot 'images/archive-sha256.txt') -Value $archiveHash -Encoding ascii
        Run-Docker run --rm --network none --mount "type=bind,source=$deploymentRoot,target=/delivery" $pythonImage python /delivery/scripts/offline_manifest.py
    }
}
