<#
.SYNOPSIS
    win-search-aliases Windows installer.

.DESCRIPTION
    1. Finds Python 3.11+
    2. Creates a private virtual environment
    3. Installs / upgrades win-search-aliases into that environment
    4. Adds the environment Scripts directory to the user PATH

.EXAMPLE
    powershell.exe -ExecutionPolicy Bypass -File install.ps1
#>

$ErrorActionPreference = 'Stop'

$DefaultProjectSpec = 'https://github.com/mbv06/win-search-aliases/archive/refs/heads/main.zip'
$ProjectSpec = if ($env:WIN_SEARCH_ALIASES_PROJECT_SPEC) { $env:WIN_SEARCH_ALIASES_PROJECT_SPEC } else { $DefaultProjectSpec }
$AppName = 'win-search-aliases'
$MinMajor = 3
$MinMinor = 11

$AppHome = $null
$VenvDir = $null
$ScriptsDir = $null
$CommandExe = $null
$UiCommandExe = $null

function Log {
    param([string]$Message)
    Write-Host ''
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Die {
    param([string]$Message)
    Write-Host "Error: $Message" -ForegroundColor Red
    exit 1
}

function Test-EnvFlag {
    param([string]$Value)

    if (-not $Value) {
        return $false
    }

    $normalized = $Value.Trim()
    if (-not $normalized) {
        return $false
    }

    return ($normalized -notin @('0', 'false', 'no', 'off'))
}

function New-PythonCommand {
    param(
        [string]$FilePath,
        [string[]]$Arguments,
        [string]$Display
    )

    $item = New-Object PSObject
    $item | Add-Member -MemberType NoteProperty -Name FilePath -Value $FilePath
    $item | Add-Member -MemberType NoteProperty -Name Arguments -Value $Arguments
    $item | Add-Member -MemberType NoteProperty -Name Display -Value $Display
    return $item
}

function Format-Argument {
    param([string]$Value)

    if ($Value -notmatch '[\s"]') {
        return $Value
    }

    return '"' + ($Value -replace '"', '\"') + '"'
}

function Format-CommandForDisplay {
    param(
        [string]$FilePath,
        [string[]]$Arguments
    )

    $parts = @($FilePath) + @($Arguments)
    $quoted = @()
    foreach ($part in $parts) {
        $quoted += Format-Argument -Value $part
    }
    return ($quoted -join ' ')
}

function Invoke-NativeCommand {
    param(
        [string]$FilePath,
        [string[]]$Arguments,
        [string]$FailureMessage
    )

    Write-Host ("Running: {0}" -f (Format-CommandForDisplay -FilePath $FilePath -Arguments $Arguments)) -ForegroundColor DarkGray
    $stdoutPath = [System.IO.Path]::GetTempFileName()
    $stderrPath = [System.IO.Path]::GetTempFileName()
    $argumentLine = (@($Arguments) | ForEach-Object { Format-Argument -Value $_ }) -join ' '

    try {
        $process = Start-Process -FilePath $FilePath `
                                 -ArgumentList $argumentLine `
                                 -NoNewWindow `
                                 -Wait `
                                 -PassThru `
                                 -RedirectStandardOutput $stdoutPath `
                                 -RedirectStandardError $stderrPath

        if (Test-Path -LiteralPath $stdoutPath) {
            $stdout = @(Get-Content -LiteralPath $stdoutPath -ErrorAction SilentlyContinue)
        }
        else {
            $stdout = @()
        }

        if (Test-Path -LiteralPath $stderrPath) {
            $stderr = @(Get-Content -LiteralPath $stderrPath -ErrorAction SilentlyContinue)
        }
        else {
            $stderr = @()
        }

        $output = @($stdout + $stderr)
        $exitCode = $process.ExitCode
    }
    finally {
        Remove-Item -LiteralPath $stdoutPath, $stderrPath -Force -ErrorAction SilentlyContinue
    }

    if ($output) {
        foreach ($line in @($output)) {
            Write-Host $line
        }
    }

    if ($exitCode -ne 0) {
        Die $FailureMessage
    }
}

function Test-PythonCompatible {
    param(
        [string]$FilePath,
        [string[]]$Arguments
    )

    try {
        $checkArgs = @($Arguments) + @('-c', 'import sys; print(sys.version_info.major); print(sys.version_info.minor)')
        $version = & $FilePath @checkArgs 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $version) {
            return $false
        }

        $parts = @($version)
        if ($parts.Count -lt 2) {
            return $false
        }

        $major = [int]$parts[0]
        $minor = [int]$parts[1]
        return (($major -gt $MinMajor) -or (($major -eq $MinMajor) -and ($minor -ge $MinMinor)))
    }
    catch {
        return $false
    }
}

function Find-Python {
    $candidates = @(
        'python3.14',
        'python3.13',
        'python3.12',
        'python3.11',
        'python3',
        'python'
    )

    foreach ($candidate in $candidates) {
        $command = Get-Command $candidate -ErrorAction SilentlyContinue
        if ($command -and (Test-PythonCompatible -FilePath $candidate -Arguments @())) {
            return New-PythonCommand -FilePath $candidate -Arguments @() -Display $candidate
        }
    }

    $py = Get-Command 'py' -ErrorAction SilentlyContinue
    if ($py) {
        foreach ($version in @('-3.14', '-3.13', '-3.12', '-3.11')) {
            if (Test-PythonCompatible -FilePath 'py' -Arguments @($version)) {
                return New-PythonCommand -FilePath 'py' -Arguments @($version) -Display ("py " + $version)
            }
        }

        if (Test-PythonCompatible -FilePath 'py' -Arguments @()) {
            return New-PythonCommand -FilePath 'py' -Arguments @() -Display 'py'
        }
    }

    return $null
}

function Add-ToUserPath {
    param([string]$Directory)

    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    if (-not $userPath) {
        $userPath = ''
    }

    $target = $Directory.TrimEnd('\')
    $entries = @()
    if ($userPath) {
        $entries = $userPath -split ';'
    }

    foreach ($entry in $entries) {
        if ($entry.TrimEnd('\') -ieq $target) {
            return $false
        }
    }

    if ($userPath) {
        $newPath = "$userPath;$Directory"
    }
    else {
        $newPath = $Directory
    }

    [Environment]::SetEnvironmentVariable('Path', $newPath, 'User')

    $sessionHasPath = $false
    foreach ($entry in ($env:Path -split ';')) {
        if ($entry.TrimEnd('\') -ieq $target) {
            $sessionHasPath = $true
        }
    }
    if (-not $sessionHasPath) {
        $env:Path = "$env:Path;$Directory"
    }

    return $true
}

function Write-SummaryLine {
    param(
        [string]$Label,
        [string]$Value
    )

    Write-Host ("  {0,-12}" -f $Label) -ForegroundColor Cyan -NoNewline
    Write-Host " $Value"
}

function Main {
    $appHomeOverride = $env:WIN_SEARCH_ALIASES_APP_HOME
    if (-not $appHomeOverride -and -not $env:LOCALAPPDATA) {
        Die 'LOCALAPPDATA is not set.'
    }
    $script:AppHome = if ($appHomeOverride) { $appHomeOverride } else { Join-Path $env:LOCALAPPDATA $AppName }
    $script:VenvDir = Join-Path $AppHome 'venv'
    $script:ScriptsDir = Join-Path $VenvDir 'Scripts'
    $script:CommandExe = Join-Path $ScriptsDir 'win-search-aliases.exe'
    $script:UiCommandExe = Join-Path $ScriptsDir 'win-search-aliases-ui.exe'

    Log 'Checking OS version'
    if ([Environment]::OSVersion.Version.Build -lt 22000) {
        Die 'This tool requires Windows 11. Your Windows version is not supported.'
    }

    Log 'Checking Python 3.11+'
    $python = Find-Python
    if (-not $python) {
        Write-Host ''
        Write-Host "Error: Python $MinMajor.$MinMinor+ was not found." -ForegroundColor Red
        Write-Host "Please install Python. You can use winget:" -ForegroundColor Yellow
        Write-Host "  winget install --id Python.Python.3.13" -ForegroundColor Cyan
        Write-Host ''
        Write-Host 'Enable "Add python.exe to PATH", then open a new terminal window and run this installer again.' -ForegroundColor DarkGray
        exit 1
    }
    Write-Host "Using Python: $($python.Display)"

    Log 'Checking built-in venv support'
    Invoke-NativeCommand -FilePath $python.FilePath -Arguments (@($python.Arguments) + @('-c', 'import venv')) -FailureMessage 'This Python does not support the built-in venv module. Install a full Python distribution and try again.'

    Log 'Creating private Python environment'
    if (-not (Test-Path $AppHome)) {
        New-Item -ItemType Directory -Path $AppHome -Force | Out-Null
    }
    Invoke-NativeCommand -FilePath $python.FilePath -Arguments (@($python.Arguments) + @('-m', 'venv', $VenvDir)) -FailureMessage 'Failed to create virtual environment.'

    $venvPython = Join-Path $ScriptsDir 'python.exe'

    Log 'Installing / upgrading win-search-aliases'
    Invoke-NativeCommand -FilePath $venvPython -Arguments @('-m', 'pip', 'install', '--upgrade', $ProjectSpec) -FailureMessage 'pip install failed.'

    if (-not (Test-Path $CommandExe)) {
        Die "Installed command was not found: $CommandExe"
    }
    if (-not (Test-Path $UiCommandExe)) {
        Die "Installed UI command was not found: $UiCommandExe"
    }

    if (Test-EnvFlag -Value $env:WIN_SEARCH_ALIASES_SKIP_PATH) {
        Log 'Skipping user PATH update'
        Write-Host "Scripts directory: $ScriptsDir"
    }
    else {
        Log 'Adding Scripts directory to user PATH'
        $added = Add-ToUserPath -Directory $ScriptsDir
        if ($added) {
            Write-Host "Added to PATH: $ScriptsDir"
        }
        else {
            Write-Host 'Already on PATH.'
        }
    }

    if (Test-EnvFlag -Value $env:WIN_SEARCH_ALIASES_SKIP_AUTO) {
        Log 'Skipping automatic alias generation'
    }
    else {
        Log 'Running automatic alias generation'
        Invoke-NativeCommand -FilePath $CommandExe -Arguments @('auto') -FailureMessage 'Automatic alias generation failed.'
    }

    Write-Host ''
    if (Test-EnvFlag -Value $env:WIN_SEARCH_ALIASES_SKIP_AUTO) {
        Write-Host 'Done! win-search-aliases is installed.' -ForegroundColor Green
    }
    else {
        Write-Host 'Done! win-search-aliases is installed and automatic aliases were applied.' -ForegroundColor Green
    }
    Write-Host ''
    Write-SummaryLine -Label 'Command:' -Value 'win-search-aliases'
    Write-SummaryLine -Label 'UI:' -Value 'win-search-aliases-ui'
    Write-SummaryLine -Label 'Environment:' -Value $VenvDir
    Write-SummaryLine -Label 'Next:' -Value 'win-search-aliases'
    Write-SummaryLine -Label 'Rollback:' -Value 'win-search-aliases remove-managed --kind auto'
    Write-Host ''
    Write-Host 'If "win-search-aliases" is not found, open a new terminal window.' -ForegroundColor DarkGray
    Write-Host 'To update later, run this installer again.' -ForegroundColor DarkGray
}

try {
    Main
}
catch {
    $message = $_.Exception.Message
    if (-not $message) {
        $message = ($_ | Out-String).Trim()
    }
    Die $message
}
