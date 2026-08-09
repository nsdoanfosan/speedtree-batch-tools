param(
    [Parameter(Mandatory = $true)][int]$OwnedProcessId,
    [Parameter(Mandatory = $true)][string]$ExecutablePath,
    [Parameter(Mandatory = $true)][string]$DocumentName,
    [Parameter(Mandatory = $true)][ValidateSet("save", "close")][string]$Operation,
    [Parameter(Mandatory = $true)][int]$OperationTimeoutSeconds
)

$ErrorActionPreference = "Stop"
$Contract = "speedtree_modeler_owned_semantic_uia_v1"
$Stopwatch = [System.Diagnostics.Stopwatch]::StartNew()
$PendingPhase = "bridge_start"
$PhaseStartedSeconds = 0.0

function Write-ProgressReceipt([string]$Phase) {
    $payload = [ordered]@{
        kind = "uia_bridge_progress"
        contract = $Contract
        owned_process_id = $OwnedProcessId
        document_accessible_name = $DocumentName
        operation = $Operation
        phase = $Phase
        elapsed_seconds = [Math]::Round($Stopwatch.Elapsed.TotalSeconds, 3)
        phase_elapsed_seconds = [Math]::Round(
            $Stopwatch.Elapsed.TotalSeconds - $PhaseStartedSeconds,
            3
        )
    } | ConvertTo-Json -Compress
    # Bypass the PowerShell success pipeline so progress cannot become part of
    # a function's return value while still remaining visible to the parent.
    [Console]::Out.WriteLine($payload)
    [Console]::Out.Flush()
}

function Set-Phase([string]$Phase) {
    $script:PendingPhase = $Phase
    $script:PhaseStartedSeconds = $Stopwatch.Elapsed.TotalSeconds
    Write-ProgressReceipt $Phase
}

function Throw-Reason([string]$Token, [string]$Message) {
    throw [System.InvalidOperationException]::new($Token + "|" + $Message)
}

function Find-ProcessElements(
    [System.Windows.Automation.AutomationElement]$SearchRoot,
    [bool]$UseRootProcessCondition
) {
    if ($UseRootProcessCondition) {
        $condition = New-Object System.Windows.Automation.PropertyCondition(
            [System.Windows.Automation.AutomationElement]::ProcessIdProperty,
            $OwnedProcessId
        )
        return $SearchRoot.FindAll(
            [System.Windows.Automation.TreeScope]::Descendants,
            $condition
        )
    }
    return $SearchRoot.FindAll(
        [System.Windows.Automation.TreeScope]::Descendants,
        [System.Windows.Automation.Condition]::TrueCondition
    )
}

function Wait-ExactElement(
    [System.Windows.Automation.AutomationElement]$SearchRoot,
    [bool]$UseRootProcessCondition,
    [string]$AccessibleName,
    [string[]]$AllowedControlTypes,
    [string]$MissingToken,
    [string]$AmbiguousToken
) {
    do {
        $matches = @(
            Find-ProcessElements $SearchRoot $UseRootProcessCondition |
                Where-Object {
                    $_.Current.ProcessId -eq $OwnedProcessId -and
                    $_.Current.Name -ceq $AccessibleName -and
                    $AllowedControlTypes -contains $_.Current.ControlType.ProgrammaticName -and
                    $_.Current.IsEnabled
                }
        )
        if ($matches.Count -gt 1) {
            Throw-Reason $AmbiguousToken "multiple exact accessible elements matched"
        }
        if ($matches.Count -eq 1) {
            return $matches[0]
        }
        if ($Stopwatch.Elapsed.TotalSeconds -ge $OperationTimeoutSeconds) {
            break
        }
        Start-Sleep -Milliseconds 100
    } while ($true)
    Throw-Reason $MissingToken "the exact accessible element did not appear within the operation budget"
}

function Get-TopLevelOwnedWindow(
    [System.Windows.Automation.AutomationElement]$Element
) {
    $walker = [System.Windows.Automation.TreeWalker]::RawViewWalker
    $current = $Element
    while ($true) {
        $parent = $walker.GetParent($current)
        if ($null -eq $parent -or
            $parent -eq [System.Windows.Automation.AutomationElement]::RootElement) {
            break
        }
        $current = $parent
    }
    if ($current.Current.ProcessId -ne $OwnedProcessId) {
        Throw-Reason "uia_document_pid_mismatch" "the document window is not owned by the exact PID"
    }
    return $current
}

function Invoke-ExactMenu(
    [System.Windows.Automation.AutomationElement]$SearchRoot,
    [string]$AccessibleName,
    [string]$MissingToken,
    [string]$ResolvePhase,
    [string]$InvokePhase
) {
    Set-Phase $ResolvePhase
    $parameters = @{
        SearchRoot = $SearchRoot
        UseRootProcessCondition = $false
        AccessibleName = $AccessibleName
        AllowedControlTypes = @("ControlType.MenuItem")
        MissingToken = $MissingToken
        AmbiguousToken = "uia_menu_ambiguous"
    }
    $element = Wait-ExactElement @parameters
    Set-Phase $InvokePhase
    $patternObject = $null
    if (-not $element.TryGetCurrentPattern(
        [System.Windows.Automation.InvokePattern]::Pattern,
        [ref]$patternObject
    )) {
        Throw-Reason "uia_invoke_pattern_missing" "the exact menu item has no InvokePattern"
    }
    ([System.Windows.Automation.InvokePattern]$patternObject).Invoke()
}

try {
    Set-Phase "loading_uia_assemblies"
    Add-Type -AssemblyName UIAutomationClient
    Add-Type -AssemblyName UIAutomationTypes

    Set-Phase "validating_owned_process"
    $process = Get-Process -Id $OwnedProcessId -ErrorAction Stop
    $expectedExecutable = [System.IO.Path]::GetFullPath($ExecutablePath)
    $actualExecutable = [System.IO.Path]::GetFullPath($process.MainModule.FileName)
    if (-not [string]::Equals(
        $expectedExecutable,
        $actualExecutable,
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        Throw-Reason "uia_owned_executable_mismatch" "the owned PID executable differs"
    }
    if ([System.IO.Path]::GetFileName($DocumentName) -cne $DocumentName -or
        [System.IO.Path]::GetExtension($DocumentName) -ine ".spm") {
        Throw-Reason "uia_document_identity_invalid" "the document name is not one exact SPM basename"
    }

    Set-Phase "resolving_document"
    $documentParameters = @{
        SearchRoot = [System.Windows.Automation.AutomationElement]::RootElement
        UseRootProcessCondition = $true
        AccessibleName = $DocumentName
        AllowedControlTypes = @(
            "ControlType.Document",
            "ControlType.TabItem",
            "ControlType.Window",
            "ControlType.Pane"
        )
        MissingToken = "uia_document_missing"
        AmbiguousToken = "uia_document_ambiguous"
    }
    $document = Wait-ExactElement @documentParameters
    if ($document.Current.ProcessId -ne $OwnedProcessId) {
        Throw-Reason "uia_document_pid_mismatch" "the document is not owned by the exact PID"
    }
    $ownedWindow = Get-TopLevelOwnedWindow $document

    Set-Phase "focusing_document"
    $document.SetFocus()

    Invoke-ExactMenu $ownedWindow "File" "uia_file_menu_missing" `
        "resolving_file_menu" "invoking_file_menu"
    $actionName = if ($Operation -eq "save") { "Save" } else { "Close" }
    Invoke-ExactMenu $ownedWindow $actionName ("uia_" + $Operation + "_menu_missing") `
        ("resolving_" + $Operation + "_menu") ("invoking_" + $Operation)

    if ($Operation -eq "close") {
        Set-Phase "verifying_exact_document_close"
        do {
            $remaining = @(
                Find-ProcessElements $ownedWindow $false |
                    Where-Object {
                        $_.Current.ProcessId -eq $OwnedProcessId -and
                        $_.Current.Name -ceq $DocumentName -and
                        @(
                            "ControlType.Document",
                            "ControlType.TabItem",
                            "ControlType.Window",
                            "ControlType.Pane"
                        ) -contains $_.Current.ControlType.ProgrammaticName
                    }
            )
            if ($remaining.Count -eq 0) { break }
            if ($remaining.Count -gt 1) {
                Throw-Reason "uia_document_ambiguous" "document identity became ambiguous during close"
            }
            if ($Stopwatch.Elapsed.TotalSeconds -ge $OperationTimeoutSeconds) {
                break
            }
            Start-Sleep -Milliseconds 100
        } while ($true)
        if ($remaining.Count -ne 0) {
            Throw-Reason "uia_exact_document_close_unverified" "the exact document remained visible"
        }
    }

    Set-Phase "complete"
    [ordered]@{
        ok = $true
        contract = $Contract
        owned_process_id = $OwnedProcessId
        document_accessible_name = $DocumentName
        operation = $Operation
        menu_path = @("File", $actionName)
        semantic_pattern = "InvokePattern"
        pending_phase = $PendingPhase
        elapsed_seconds = [Math]::Round($Stopwatch.Elapsed.TotalSeconds, 3)
        phase_elapsed_seconds = [Math]::Round(
            $Stopwatch.Elapsed.TotalSeconds - $PhaseStartedSeconds,
            3
        )
    } | ConvertTo-Json -Compress
    exit 0
}
catch {
    $rawMessage = [string]$_.Exception.Message
    $parts = $rawMessage.Split(@("|"), 2, [System.StringSplitOptions]::None)
    $reason = if ($parts.Count -eq 2) { $parts[0] } else { "uia_semantic_invoke_failed" }
    [ordered]@{
        ok = $false
        contract = $Contract
        reason_token = $reason
        owned_process_id = $OwnedProcessId
        document_accessible_name = $DocumentName
        operation = $Operation
        pending_phase = $PendingPhase
        elapsed_seconds = [Math]::Round($Stopwatch.Elapsed.TotalSeconds, 3)
        phase_elapsed_seconds = [Math]::Round(
            $Stopwatch.Elapsed.TotalSeconds - $PhaseStartedSeconds,
            3
        )
    } | ConvertTo-Json -Compress
    exit 1
}
